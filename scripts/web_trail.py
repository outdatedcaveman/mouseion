"""Give every incomplete entry a web search and, where the web knows it, a paper trail.

See mouseion.web_trail. Entries without any identifier or URL go first (those with
authors, then the rest); each gets up to two searches. A result must BE the entry
(title words or series+volume, and a surname); identifiers harvested from the kept
results are resolved and must agree with the entry again before their record
fills empty fields; otherwise the best kept page becomes the entry's URL, with the
evidence (page title, snippet, query) in extras.web_trail. Backups in
web_trail_bak_<date>, resumable ledger web_trail_scan. A throttled search engine
pauses the run (10 min, then stops after 3 in a row) -- never worked around.

Usage: python scripts/web_trail.py <limit> <dry|write> [--show N] [--all]
  --all  also entries that already have a PDF (they lack an author, not a trail)
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
SHOW = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else (60 if not WRITE else 0)
ALL = "--all" in sys.argv
STAMP = date.today().strftime("%Y%m%d")
CFG = get_config()
MAILTO = CFG.crossref_email or CFG.openalex_email or ""


def title_agrees(stored: str, found: str) -> bool:
    ns, nf = W.norm(stored), W.norm(found)
    if not ns or not nf:
        return False
    if difflib.SequenceMatcher(None, ns, nf).ratio() >= 0.85:
        return True
    return len(ns.split()) >= 3 and len(ns) < len(nf) and ns in nf


def resolve_ids(ids: dict, entry: dict, trail: W.Trail, client: httpx.Client):
    """A record for the harvested identifiers that agrees with the entry, or None."""
    recs = []
    if ids.get("doi"):
        recs.append(("doi", PI._crossref_doi(client, ids["doi"], MAILTO)))
    if ids.get("jstor"):
        recs.append(("jstor", PI._crossref_doi(client, f"10.2307/{ids['jstor']}", MAILTO)))
    if ids.get("arxiv"):
        recs.append(("arxiv", PI._crossref_doi(client, f"10.48550/arxiv.{ids['arxiv']}", MAILTO)))
    if ids.get("isbn"):
        recs.append(("isbn", PI._isbn(client, ids["isbn"])))
    surn = {W.norm(s).split()[-1] for s in entry.get("surnames", []) if W.norm(s)}
    for how, rec in recs:
        if rec is None or not rec.title:
            continue
        rs = {W.norm(a.family).split()[-1] for a in rec.authors + list(getattr(rec, "editors", []) or [])
              if W.norm(a.family)}
        if surn and not (surn & rs):
            continue
        if entry.get("series"):
            # 'LNCS 3796': the record is the volume the kept page named
            ok = W.coverage(rec.title, trail.evidence.get("title", "") + " " + trail.evidence.get("snippet", "")) >= 0.7 \
                or (rec.volume and str(rec.volume) == entry["volume"])
        else:
            ok = title_agrees(entry.get("title", ""), rec.title)
        if ok:
            if how == "isbn" and not rec.isbn:
                rec.isbn = ids["isbn"]
            if how == "jstor" and not rec.doi:
                rec.doi = f"10.2307/{ids['jstor']}"
            if how == "arxiv":
                rec.arxiv_id = ids["arxiv"]
            return rec, how
    return None, ""


def main() -> None:
    conn = sqlite3.connect(str(Path(CFG.db_path).expanduser()), timeout=120, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS web_trail_scan (ref_id TEXT PRIMARY KEY, result TEXT, url TEXT, "
                 "query TEXT, scanned_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS web_trail_bak_{STAMP} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    no_web_id = ("COALESCE(url,'')='' AND COALESCE(oa_url,'')='' AND COALESCE(doi,'')='' AND COALESCE(isbn,'')='' "
                 "AND COALESCE(arxiv_id,'')='' AND COALESCE(pmid,'')=''")
    pdf_clause = "" if ALL else " AND COALESCE(pdf_local,'')='' AND COALESCE(pdf_drive_id,'')=''"
    ids = [r[0] for r in conn.execute(f"""SELECT id FROM refs WHERE NOT ({RefDatabase.COMPLETE_SQL}) AND {no_web_id}
        {pdf_clause} AND id NOT IN (SELECT ref_id FROM web_trail_scan)
        ORDER BY (authors IS NULL OR authors IN ('','[]')), RANDOM() LIMIT ?""", (LIMIT,))]
    print(f"[web-trail] {len(ids):,} entries | {'WRITE' if WRITE else 'DRY-RUN'}", flush=True)
    known = PI.NameVocab(conn).common            # ordinary words, for re-joining split ligatures
    db = RefDatabase()
    searcher = W.SearchPool.from_config(CFG, Path(CFG.db_path).expanduser().parent / "web_search_budget.json")
    print(f"  search allowances left: {searcher.status()}", flush=True)
    client = httpx.Client(timeout=30, headers={"User-Agent": "mouseion/0.3 (library repair)"}, follow_redirects=True)
    cols = [d[0] for d in conn.execute("SELECT * FROM refs LIMIT 0").description]
    stats: Counter = Counter()
    t0 = time.time()
    strikes = 0
    shown = 0
    for n, rid in enumerate(ids, 1):
        seed = db.get(rid)
        if seed is None:
            continue
        u = W.understand(seed.title or "", known)
        entry = {**u, "surnames": [a.family for a in seed.authors if a.family],
                 "year": seed.year, "journal": seed.journal or ""}
        try:
            trail = W.find_trail(entry, searcher, min_conf=0.7 if entry["surnames"] else 0.9)
            strikes = 0
        except W.Exhausted as e:
            print(f"  stopping: {e}; resumable, the next run continues here", flush=True)
            break
        except W.Throttled as e:
            strikes += 1
            print(f"  search engine throttled ({e}); pausing 10 min [{strikes}/3]", flush=True)
            if strikes >= 3:
                print("  stopping: resumable, the next run continues here", flush=True)
                break
            time.sleep(600)
            continue
        up, result = {}, trail.result
        if trail.result == "url":
            rec, how = resolve_ids(trail.ids, entry, trail, client) if trail.ids else (None, "")
            if rec is not None:
                if entry.get("series"):
                    seed.title = None          # 'LNCS 3796' gives way to the volume's real title
                up = PI.fill_plan(seed, rec)
                result = "record:" + how
            if not seed.url and not up.get("doi"):
                up["url"] = trail.url
        stats[result.split(":")[0]] += 1
        if WRITE:
            if up:
                row = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
                conn.execute(f"INSERT OR IGNORE INTO web_trail_bak_{STAMP} VALUES (?,?)",
                             (rid, json.dumps(dict(zip(cols, row)), default=str)))
                PI.apply_fill(db, rid, up)
            if trail.result == "url":
                ex = conn.execute("SELECT extras FROM refs WHERE id=?", (rid,)).fetchone()[0]
                try:
                    ex = json.loads(ex or "{}")
                except Exception:
                    ex = {}
                ex["web_trail"] = {"url": trail.url, **trail.evidence, "at": date.today().isoformat()}
                conn.execute("UPDATE refs SET extras=? WHERE id=?", (json.dumps(ex, ensure_ascii=False), rid))
            conn.execute("INSERT OR REPLACE INTO web_trail_scan (ref_id, result, url, query) VALUES (?,?,?,?)",
                         (rid, result, trail.url, trail.evidence.get("query", "")))
        if shown < SHOW:
            shown += 1
            ser = f"[{u.get('series','')} {u.get('volume','')}] " if u.get("series") else ""
            print(f"  {result:12s} {ser}{(seed.title or '')[:46]:46s} | {', '.join(entry['surnames'][:2])[:18]:18s} -> "
                  f"{(trail.evidence.get('title') or '')[:50]:50s} | {trail.url[:70]}"
                  + (f" | {up.get('title','')[:40]} {up.get('doi') or up.get('isbn') or ''}" if result.startswith("record") else ""),
                  flush=True)
        if n % 50 == 0:
            print(f"  ... {n:,}/{len(ids):,} | {dict(stats)} | {n / (time.time() - t0):.2f}/s", flush=True)
    print(f"done: {dict(stats)} in {time.time() - t0:.0f}s | allowances left: {searcher.status()}", flush=True)


if __name__ == "__main__":
    main()
