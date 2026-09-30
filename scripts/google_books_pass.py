"""Books and book chapters without ISBN or link, looked up in Google Books.

The Books API is free (no paid tier); with the owner's key it allows ~1,000 lookups a
day, kept at 900 here (usage in google_books_budget.json).

Books: intitle + inauthor search; a volume is accepted only when its title matches
(similarity >= 0.85, a series prefix or a cut-off stored title allowed), an author's
surname agrees, and the years (when both are known) are within 3 (reprints). Chapters:
the CONTAINING book (container_title) is looked up with the chapter's editors or
authors; its ISBN and link go on the chapter.
Fills only empty fields: ISBN, publisher, year, and the Google Books page as URL.
Backups in gbooks_bak_<date>; resumable ledger gbooks_scan.

Usage: python scripts/google_books_pass.py <limit> <dry|write> [--show N]
"""
from __future__ import annotations

import difflib
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import httpx  # noqa: E402

from mouseion import pdf_ingest as PI  # noqa: E402
from mouseion import web_trail as W  # noqa: E402
from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LIMIT = int(args[0]) if args else 50
WRITE = len(args) > 1 and args[1] == "write"
SHOW = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else (40 if not WRITE else 0)
STAMP = date.today().strftime("%Y%m%d")
CFG = get_config()
DAILY = 900
BUDGET = Path(CFG.db_path).expanduser().parent / "google_books_budget.json"   # own file: web_trail rewrites its own


def budget_used() -> int:
    try:
        return json.loads(BUDGET.read_text(encoding="utf-8")).get("google-books", {}).get(date.today().isoformat(), 0)
    except Exception:
        return 0


def budget_add(n: int) -> None:
    try:
        d = json.loads(BUDGET.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    day = date.today().isoformat()
    d.setdefault("google-books", {})
    d["google-books"] = {day: d["google-books"].get(day, 0) + n}      # only today is kept
    BUDGET.write_text(json.dumps(d, indent=1), encoding="utf-8")


def title_ok(stored: str, found: str, subtitle: str = "") -> float:
    s = W.norm(W.SERIES_VOL.sub("", stored or ""))
    best = 0.0
    for f in (found, f"{found} {subtitle}".strip()):
        nf = W.norm(f)
        if not s or not nf:
            continue
        sim = difflib.SequenceMatcher(None, s, nf).ratio()
        if len(s.split()) >= 3 and (nf.startswith(s) or s.startswith(nf) and len(nf.split()) >= 3):
            sim = max(sim, 0.9)
        best = max(best, sim)
    return best


class Quota(Exception):
    pass


def lookup(client: httpx.Client, title: str, names: list) -> list:
    q = f'intitle:"{title[:120]}"' + (f' inauthor:"{names[0]}"' if names else "")
    r = client.get("https://www.googleapis.com/books/v1/volumes",
                   params={"q": q, "maxResults": 5, "printType": "books", "key": CFG.google_books_api_key})
    budget_add(1)
    if r.status_code in (403, 429):
        raise Quota(f"HTTP {r.status_code}")
    r.raise_for_status()
    return r.json().get("items", [])


def best_volume(items: list, title: str, surnames: set, year):
    best = None
    for it in items:
        v = it.get("volumeInfo", {})
        sc = title_ok(title, v.get("title", ""), v.get("subtitle", ""))
        if sc < 0.85:
            continue
        va = {W.norm(a).split()[-1] for a in v.get("authors", []) if W.norm(a)}
        if surnames and not (surnames & va):
            continue
        vy = (v.get("publishedDate") or "")[:4]
        if year and vy.isdigit() and abs(int(vy) - int(year)) > 3:
            continue
        isbn = next((x["identifier"] for x in v.get("industryIdentifiers", []) if x.get("type") == "ISBN_13"), None) or \
            next((x["identifier"] for x in v.get("industryIdentifiers", []) if x.get("type") == "ISBN_10"), None)
        key = (sc, bool(isbn))
        if best is None or key > best[0]:
            best = (key, v, isbn)
    return best


def main() -> None:
    if not CFG.google_books_api_key:
        print("no Google Books key in Settings"); return
    conn = sqlite3.connect(str(Path(CFG.db_path).expanduser()), timeout=120, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS gbooks_scan (ref_id TEXT PRIMARY KEY, result TEXT, isbn TEXT, "
                 "scanned_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS gbooks_bak_{STAMP} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    ids = [r[0] for r in conn.execute(f"""SELECT id FROM refs WHERE NOT ({RefDatabase.COMPLETE_SQL})
        AND ref_type IN ('book','book-chapter') AND COALESCE(isbn,'')='' AND COALESCE(url,'')=''
        AND authors NOT IN ('','[]') AND id NOT IN (SELECT ref_id FROM gbooks_scan)
        ORDER BY RANDOM() LIMIT ?""", (LIMIT,))]
    print(f"[gbooks] {len(ids):,} entries | {'WRITE' if WRITE else 'DRY-RUN'} | used today {budget_used()}/{DAILY}",
          flush=True)
    db = RefDatabase()
    cl = httpx.Client(timeout=30)
    cols = [d[0] for d in conn.execute("SELECT * FROM refs LIMIT 0").description]
    stats: Counter = Counter()
    shown = 0
    for n, rid in enumerate(ids, 1):
        if budget_used() >= DAILY:
            print("  today's Google Books allowance used: stopping (resumable tomorrow)", flush=True)
            break
        seed = db.get(rid)
        if seed is None:
            continue
        chapter = str(seed.ref_type).endswith("CHAPTER") or "chapter" in str(seed.ref_type).lower()
        title = (seed.container_title or seed.journal) if chapter else seed.title
        people = (list(getattr(seed, "editors", []) or []) + list(seed.authors)) if chapter else list(seed.authors)
        names = [getattr(a, "family", "") or str(a) for a in people]
        names = [x for x in names if x]
        title = W.understand(title or "").get("title") or title or ""
        if len(W.norm(title).split()) < 2:
            stats["too_little"] += 1
            if WRITE:
                conn.execute("INSERT OR REPLACE INTO gbooks_scan (ref_id, result, isbn) VALUES (?,?,?)", (rid, "too_little", ""))
            continue
        try:
            items = lookup(cl, title, names)
            if not items and names:
                items = lookup(cl, title, [])
        except Quota as e:
            print(f"  Google Books refused ({e}): stopping", flush=True)
            break
        except Exception:
            stats["error"] += 1
            continue
        hit = best_volume(items, title, {W.norm(x).split()[-1] for x in names if W.norm(x)},
                          None if chapter else seed.year)
        up = {}
        if hit:
            _, v, isbn = hit
            if isbn:
                up["isbn"] = isbn
            if v.get("publisher") and not seed.publisher:
                up["publisher"] = v["publisher"]
            vy = (v.get("publishedDate") or "")[:4]
            if vy.isdigit() and not seed.year and not chapter:
                up["year"] = int(vy)
            link = v.get("canonicalVolumeLink") or v.get("infoLink")
            if link and not seed.url:
                up["url"] = link
        result = "matched" if up else ("no_match" if items else "no_result")
        stats[result] += 1
        if WRITE:
            if up:
                row = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
                conn.execute(f"INSERT OR IGNORE INTO gbooks_bak_{STAMP} VALUES (?,?)",
                             (rid, json.dumps(dict(zip(cols, row)), default=str)))
                PI.apply_fill(db, rid, up)
            conn.execute("INSERT OR REPLACE INTO gbooks_scan (ref_id, result, isbn) VALUES (?,?,?)",
                         (rid, result, up.get("isbn", "")))
        if up and shown < SHOW:
            shown += 1
            v = hit[1]
            print(f"  {'[ch] ' if chapter else ''}{title[:50]:50s} | {', '.join(names[:2])[:20]:20s} | {seed.year} -> "
                  f"{v.get('title','')[:45]:45s} | {', '.join(v.get('authors', [])[:2])[:25]:25s} | "
                  f"{(v.get('publishedDate') or '')[:4]} {up.get('isbn','')}", flush=True)
        if n % 100 == 0:
            print(f"  ... {n:,}/{len(ids):,} | {dict(stats)}", flush=True)
        time.sleep(0.3)
    print(f"done: {dict(stats)} | used today {budget_used()}/{DAILY}", flush=True)


if __name__ == "__main__":
    main()
