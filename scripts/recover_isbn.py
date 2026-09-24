"""ISBN recovery for books (and chapters of single-author books) via OpenLibrary.

Why (2026-09-24): ~20k books/chapters sit below 0.8 completeness with no
identifier; an ISBN is worth +0.15 and usually brings publisher and year.
OpenLibrary had never answered a single Mouseion request. Probe: 4/30 books
got an ISBN -- but one was WRONG ("Individuals and Non", a truncated title,
matched "Prosecutions by private individuals and non-police"). So:

  * AUTHOR AGREEMENT IS MANDATORY: the reference's surname (family name, or
    the longest given-name token when the family field is only an initial --
    a swapped record) must appear in the work's author list. References
    without authors are not attempted. A title-only query is tried when the
    author-filtered one finds nothing; the author check is still ours.
  * short generic titles ("Machine Translation") and fragments of a longer
    title must also agree on the year (+-2): a prolific author reuses words.
  * title: >= 85% of the reference's title words in the work's title (main
    title before ':' compared too), or near-identical (ratio >= 0.9);
  * year: the reference may not predate the work's first publication - 1;
  * chapters only when their author wrote the whole book (monographs); an
    edited volume's editors are not the chapter's author, so it can't verify;
  * the ISBN comes from the edition OpenLibrary matched (13-digit preferred),
    with that edition's publisher/date; Mouseion's merge applies it
    net-positively (never clobbers).
Reversible (isbn_bak_<date>), resumable (isbn_scan), polite (~1.5 req/s,
contact e-mail from config).

Usage:
  python scripts/recover_isbn.py <limit> <dry|write> [--show N]
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import httpx  # noqa: E402

from mouseion.db import RefDatabase  # noqa: E402
from mouseion.merge import merge  # noqa: E402
from mouseion.models import Author, Reference  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LIMIT = int(args[0]) if args else 100
WRITE = len(args) > 1 and args[1] == "write"
SHOW = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 0
DB = RefDatabase()
STATS = {"checked": 0, "no_authors": 0, "no_result": 0, "rejected": 0, "matched": 0, "updated": 0, "err": 0}
SAMPLES: list = []
_STOP = {"the", "a", "an", "of", "in", "on", "and", "or", "to", "for", "with", "by", "from", "as", "at", "de", "la",
         "le", "el", "der", "die", "das", "und", "do", "da", "dos", "em", "e", "o", "os", "um", "uma", "del", "les",
         "des", "du", "y", "en", "et", "il", "lo", "los", "las"}


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _words(s: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", _fold(s)).split() if w not in _STOP]


def _title_ok(seed: str, cand: str) -> bool:
    sw = [w for w in _words(seed) if len(w) > 2]
    if len(sw) < 2:
        return False                      # too little to verify ("Black Holes" alone is not enough)
    cset = set(_words(cand))
    if sum(w in cset for w in sw) / len(sw) >= 0.85:
        return True
    a, b = " ".join(_words(seed)), " ".join(_words(cand))
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.9:
        return True
    main = " ".join(_words(cand.split(":")[0]))
    return bool(main) and difflib.SequenceMatcher(None, a, main).ratio() >= 0.9


# corporate "authors" ("International Conference on Automated Deduction") are not
# people: their words matched CADE-14 (1997) for a CADE-13 (1996) reference
_CORPORATE = {"international", "conference", "society", "university", "association", "institute", "committee",
              "workshop", "symposium", "congress", "council", "national", "department", "centre", "center",
              "foundation", "academy", "organization", "organisation", "group", "press", "editors", "unknown",
              "anonymous", "various", "staff", "team", "proceedings", "meeting", "annual", "school", "college"}


def _surname(ref) -> str | None:
    s = _surname_raw(ref)
    return None if (not s or s in _CORPORATE) else s


def _surname_raw(ref) -> str | None:
    """The longest name token of the first author: the surname even when the
    record stores given/family swapped ({"family": "M.", "given": "Bianchi"})."""
    for a in (ref.authors or []):
        fam, giv = ([getattr(a, "family", "") or "", getattr(a, "given", "") or ""] if not isinstance(a, dict)
                    else [a.get("family") or "", a.get("given") or ""])
        fam_toks = [t for t in re.sub(r"[^a-z]", " ", _fold(fam)).split() if len(t) >= 3]
        if fam_toks:            # the family name, unless it is just an initial (a swapped record)
            return max(fam_toks, key=len)
        giv_toks = [t for t in re.sub(r"[^a-z]", " ", _fold(giv)).split() if len(t) >= 3]
        if giv_toks:
            return max(giv_toks, key=len)
    return None


def _email() -> str:
    try:
        from mouseion.config import get_config
        c = get_config()
        return getattr(c, "crossref_email", "") or getattr(c, "openalex_email", "") or ""
    except Exception:
        return ""


EMAIL = _email()
UA = {"User-Agent": f"mouseion/0.2 ({'mailto:' + EMAIL if EMAIL else 'library enrichment'})"}
FIELDS = ("key,title,author_name,first_publish_year,editions,editions.key,editions.isbn,editions.publisher,"
          "editions.publish_date,editions.title")


def _search(client, title: str, surname: str) -> list[dict]:
    for attempt in range(4):
        try:
            r = client.get("https://openlibrary.org/search.json",
                           params={"title": title[:200], **({"author": surname} if surname else {}), "limit": 8, "fields": FIELDS})
            if r.status_code == 200:
                return r.json().get("docs", [])
            if r.status_code in (429, 503):
                time.sleep(5 * (attempt + 1))
                continue
            return []
        except Exception:
            time.sleep(2 * (attempt + 1))
    return []


def _pick(seed, docs: list[dict], surname: str):
    for d in docs:
        if not _title_ok(seed.title or "", d.get("title", "")):
            continue
        names = {t for n in d.get("author_name") or [] for t in re.sub(r"[^a-z]", " ", _fold(n)).split()}
        if surname not in names:
            continue
        fpy = d.get("first_publish_year")
        if seed.year and fpy and int(seed.year) < int(fpy) - 1:
            continue
        eds = ((d.get("editions") or {}).get("docs") or [])
        ed = eds[0] if eds else {}
        isbns = ed.get("isbn") or []
        isbn = next((i for i in isbns if len(i) == 13), None) or next((i for i in isbns if len(i) == 10), None)
        if not isbn:
            continue
        year = None
        m = re.search(r"(1[5-9]\d\d|20\d\d)", " ".join(ed.get("publish_date") or []))
        if m:
            year = int(m.group(1))
        # a short generic title ("Machine Translation") or a fragment of a longer
        # one proves little even with the author (prolific authors reuse words):
        # then the year must agree too (seen: Hutchins 2006 -> a 2000 book)
        sw = [w for w in _words(seed.title or "") if len(w) > 2]
        cw = [w for w in _words(d.get("title", "")) if len(w) > 2]
        if len(sw) <= 2 or len(sw) < len(cw) / 2:
            years = {y for y in (year, fpy) if y}
            if not seed.year or not years or min(abs(int(seed.year) - int(y)) for y in years) > 2:
                continue
        return d, ed, isbn, year
    return None


def main():
    stamp = time.strftime("%Y%m%d")
    conn = sqlite3.connect(str(DB._path), timeout=60,
                           isolation_level=None)   # autocommit: never hold the write lock across network calls
    conn.execute("CREATE TABLE IF NOT EXISTS isbn_scan (ref_id TEXT PRIMARY KEY, result TEXT, isbn TEXT, "
                 "scanned_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS isbn_bak_{stamp} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    conn.commit()
    ids = [r[0] for r in conn.execute(
        """SELECT id FROM refs WHERE ref_type IN ('book', 'book-chapter') AND COALESCE(status,'') != 'duplicate'
             AND COALESCE(isbn,'') = '' AND COALESCE(doi,'') = '' AND LENGTH(title) >= 8
             AND id NOT IN (SELECT ref_id FROM isbn_scan)
           ORDER BY (COALESCE(completeness, 0) >= 0.8), RANDOM() LIMIT ?""", (LIMIT,))]
    print(f"[isbn] {len(ids):,} books/chapters | {'WRITE' if WRITE else 'DRY-RUN'}", flush=True)
    t0 = time.time()
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as client:
        for n, rid in enumerate(ids, 1):
            result = "err"
            try:
                seed = DB.get(rid)
                surname = _surname(seed) if seed else None
                STATS["checked"] += 1
                if not surname:
                    STATS["no_authors"] += 1
                    result = "no_authors"
                else:
                    docs = _search(client, seed.title, surname)
                    time.sleep(0.65)                           # ~1.5 req/s
                    if not docs or not _pick(seed, docs, surname):
                        # OpenLibrary's author filter is all-or-nothing on spelling; the author
                        # check is ours (_pick), so a title-only query widens recall safely
                        docs = _search(client, seed.title, "") or docs
                        time.sleep(0.65)
                    hit = _pick(seed, docs, surname) if docs else None
                    if not docs:
                        STATS["no_result"] += 1
                        result = "no_result"
                    elif not hit:
                        STATS["rejected"] += 1
                        result = "rejected"
                    else:
                        d, ed, isbn, year = hit
                        if seed.ref_type == "book-chapter":
                            cand = Reference(isbn=isbn, publisher=(ed.get("publisher") or [None])[0],
                                             year=seed.year or year)
                        else:
                            cand = Reference(title=d.get("title"), isbn=isbn, year=year or d.get("first_publish_year"),
                                             publisher=(ed.get("publisher") or [None])[0], ref_type="book",
                                             authors=[Author(family=n.split()[-1], given=" ".join(n.split()[:-1]))
                                                      for n in (d.get("author_name") or [])[:6]])
                        cand.sources = {"openlibrary": 0.9}
                        merged = merge(seed, [(cand, 0.9)])
                        if not merged.isbn:
                            merged.isbn = isbn
                        STATS["matched"] += 1
                        result = f"matched:{isbn}"
                        before = seed.completeness or 0.0
                        if len(SAMPLES) < SHOW:
                            SAMPLES.append([seed.title[:55], seed.year, surname, d.get("title", "")[:55],
                                            (d.get("author_name") or [""])[0][:25], year, isbn,
                                            round(before, 2), round(merged.completeness, 2)])
                        if merged.completeness > before + 0.005 or (merged.isbn and not seed.isbn):
                            STATS["updated"] += 1
                            STATS["comp_gain"] = STATS.get("comp_gain", 0.0) + merged.completeness - before
                            if WRITE:
                                row = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
                                cols = [c[0] for c in conn.execute("SELECT * FROM refs LIMIT 0").description]
                                conn.execute(f"INSERT OR IGNORE INTO isbn_bak_{stamp} VALUES (?,?)",
                                             (rid, json.dumps(dict(zip(cols, row)), default=str)))
                                conn.commit()
                                DB.replace_ref(rid, merged)
            except Exception as e:
                STATS["err"] += 1
                result = f"err:{type(e).__name__}"
            if WRITE:
                conn.execute("INSERT OR REPLACE INTO isbn_scan (ref_id, result, isbn) VALUES (?,?,?)",
                             (rid, result.split(":")[0], result.split(":")[1] if result.startswith("matched") else None))
                if n % 50 == 0:
                    conn.commit()
            if n % 200 == 0:
                print(f"  ... {n:,}/{len(ids):,} | matched {STATS['matched']:,} | updated {STATS['updated']:,} "
                      f"| {n / (time.time() - t0):.2f}/s", flush=True)
    if WRITE:
        conn.commit()
    for s in SAMPLES:
        print("MATCH", json.dumps(s, ensure_ascii=False))
    STATS["seconds"] = int(time.time() - t0)
    print(json.dumps(STATS), flush=True)


if __name__ == "__main__":
    main()
