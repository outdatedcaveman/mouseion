"""Complete incomplete entries from their OWN PDFs, with today's ingest pipeline.

Entries created by older imports (Mendeley merge, Paperpile, early PDF scans) often
carry a garbled title and no author although the PDF right next to them says who
wrote it and often what it is: a JSTOR number in the file name, an AMS article id or
DOI on page 1, an arXiv stamp. The Archives ingest found such files "already in the
library" and moved on without improving the old entry; this pass closes that gap.

Per entry (incomplete by RefDatabase.COMPLETE_SQL, local PDF present):
  1. mouseion.pdf_ingest.extract + resolve -- the record must match the PDF's own
     page (resolve checks the record's title words against the page);
     empty fields are filled from it; the title is replaced only when the record
     was verified on the page and the stored title differs (it came from a file
     name or a garbled first line).
  2. no record: the authors printed next to the title on page 1
     (pdf_ingest.authors_from_page), else a reference-manager file name
     ("Gao_2022_...") -- names that are read, never guessed.
Only empty fields are written. Each changed row is backed up first
(own_pdf_bak_<date>); every entry is logged in own_pdf_scan (resumable).

Usage: python scripts/complete_from_pdfs.py <limit> <dry|write> [workers] [--show N]
"""
from __future__ import annotations

import difflib
import re
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
from mouseion import pdf_ingest as PI  # noqa: E402
from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase, _authors_json  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LIMIT = int(args[0]) if args else 100
WRITE = len(args) > 1 and args[1] == "write"
WORKERS = int(args[2]) if len(args) > 2 else 4
SHOW = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else (40 if not WRITE else 0)
STAMP = date.today().strftime("%Y%m%d")
# --retry-nothing: look again at files the first pass found nothing in (file-name parser, ISBN, JSTOR, 2026-10-01)
RETRY = "--retry-nothing" in sys.argv
# page 1 of a long document is a cover, a series page or a whole issue's first article:
# its "author line" is an editor or someone else (hand check 2026-09-30)
LONG_PAGES = 60
VOCAB = None


# a stored/printed "title" that is really a series page, a journal header or a library stamp
HEADERISH = re.compile(r"(lecture notes in|edited by|editorial ?board|series editors?|^international series|"
                       r"volume \d+, number|vol\. ?\d+, no\.|^journal of [\w ]+,? vol|the library|carnegie institute|"
                       r"^mathematics of computation|^proceedings of the|^transactions of the|^[A-Z ]+ volume \d+)", re.I)


def _book_or_title_record(f, title: str, authors) -> object:
    """A record for a title the file name gave: Crossref/OpenAlex title search, books for long files;
    it must share a surname with the file name's authors."""
    if len(PI._norm(title).split()) < 3:
        return None
    alt = PI.PdfFacts(path=f.path, text=f.text, meta_title=title, year_hint=f.year_hint, pages=f.pages)
    import httpx
    from mouseion.config import get_config
    cfg = get_config()
    mailto = cfg.crossref_email or cfg.openalex_email or ""
    want = {PI._norm(a.family).split()[-1] for a in authors if PI._norm(a.family)}
    try:
        with httpx.Client(timeout=30, headers=PI._UA, follow_redirects=True) as client:
            rec = PI._title_search(client, alt, mailto, getattr(cfg, "openalex_api_key", "")) or \
                (PI._book_search(client, alt, mailto) if f.pages >= 80 else None)
    except Exception:
        rec = None
    if rec is None or not rec.title:
        return None
    got = {PI._norm(a.family).split()[-1] for a in rec.authors if PI._norm(a.family)}
    return rec if (not want or want & got) else None


def work(path: str):
    """Worker process: what the PDF and its FILE NAME say, resolved.
    -> (rec, via, authors, authors_from, year, pages, error, fn_title)."""
    try:
        f = PI.extract(path)
        if f.error and not f.text:
            return None, "", [], "", None, 0, f.error, ""
        stem = Path(path).stem
        rec, via = PI.resolve(f)
        fn = PI.filename_meta(stem)
        fn_title = fn.get("title", "") if isinstance(fn.get("title"), str) else ""
        # ISBN in the file name ('019920554X.Oxford.University.Press...')
        if rec is None and fn.get("isbn"):
            import httpx
            with httpx.Client(timeout=30, headers=PI._UA, follow_redirects=True) as client:
                r2 = PI._isbn(client, fn["isbn"])
            if r2 is not None and r2.title:
                r2.isbn = r2.isbn or fn["isbn"]
                rec, via = r2, "filename-isbn"
        # JSTOR number as file name, confirmed by the journal named on the page (scans lack the title)
        if rec is None and re.fullmatch(r"\d{6,9}", stem):
            import httpx
            from mouseion.config import get_config
            cfg = get_config()
            with httpx.Client(timeout=30, headers=PI._UA, follow_redirects=True) as client:
                r2 = PI._crossref_doi(client, f"10.2307/{stem}", cfg.crossref_email or "")
            page = PI._norm((f.text or "")[:4000])
            if r2 is not None and r2.title and (
                    (r2.journal and all(w in page.split() for w in PI._norm(r2.journal).split() if len(w) > 3)) or
                    PI._title_ok(f, r2)):
                r2.doi = r2.doi or f"10.2307/{stem}"
                rec, via = r2, "jstor-filename+journal-on-page"
        src = ""
        # OCR text garbles names: author lines only from a real text layer
        page_authors = PI.authors_from_page(f) if f.pages < LONG_PAGES and not f.ocr else []
        if page_authors:
            src = "page"
        if not page_authors and fn.get("authors"):
            fa = fn["authors"]
            # the file name's people must be on the page -- or, for scans/books, at least look like names
            if PI.names_on_page(fa, f.text) or f.ocr or f.pages >= LONG_PAGES:
                page_authors, src = fa, "filename-meta"
        if not page_authors:
            fa = PI.filename_author(stem)
            if fa:
                page_authors, src = [fa], "filename"
        if not page_authors and re.fullmatch(r"[A-Z][a-z'\-]{3,30}", stem) and \
                re.search(rf"\b{re.escape(stem)}\b", (f.text or "")[:2500]):
            page_authors, src = [PI.Author(family=stem)], "filename-surname"      # 'Crosilla.pdf'
        if rec is None and fn_title and page_authors:
            r2 = _book_or_title_record(f, fn_title, page_authors)
            if r2 is not None:
                rec, via = r2, "filename-title-search"
        if rec is None:
            # the indexes know the title; the page (or file name) prints an author of the candidate
            titles = [x for x in (f.title, fn_title, (f.meta_title or "")) if x and not HEADERISH.search(x)]
            r2, v2 = PI.search_verified_by_page(f, titles)
            if r2 is not None:
                rec, via = r2, v2
        # a year read off the page text can be anything: only the folder's / file name's year
        year = f.year_hint or (fn.get("year") if isinstance(fn.get("year"), int) else None)
        return rec, via, page_authors, src, year, f.pages, "", fn_title
    except Exception as e:
        return None, "", [], "", None, 0, f"{type(e).__name__}: {str(e)[:80]}", ""


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, PI._norm(a), PI._norm(b)).ratio()


def plan(seed, rec, via, page_authors, src, year, pages, fn_title=""):
    """The field updates for one entry (only empty fields; title per the rules above)."""
    up = {}
    how = ""
    if rec is not None:
        if HEADERISH.search(seed.title or "") or J_reason(seed.title):
            seed.title = None                      # a series page / header is not the title: the record's is
        up = PI.fill_plan(seed, rec, pages)
        how = "record:" + via if up else ""
        if up:
            return up, how
    if page_authors and not seed.authors and VOCAB.ok(page_authors):
        how = src + "-authors"
        up["authors_json"] = _authors_json(page_authors)
        if year and not seed.year:
            up["year"] = year
    # the file name's title replaces a stored title that is a header, a stamp or junk
    st = PI._norm(seed.title or "")
    good_fn = fn_title and len(fn_title.split()) >= 3 and not J_reason(fn_title) and not HEADERISH.search(fn_title)         and not (st and st.startswith(PI._norm(fn_title)) and len(st) > len(PI._norm(fn_title)))   # not a cut-off of it
    if good_fn and (HEADERISH.search(seed.title or "") or J_reason(seed.title)):
        up["title"] = fn_title
        how = (how + "+" if how else "") + "filename-title"
    return up, how


def J_reason(t) -> bool:
    try:
        import junk_candidates as J
        return bool(J.reason(t or "", set(), __import__("collections").Counter()))
    except Exception:
        return False


def main() -> None:
    cfg = get_config()
    dbp = str(Path(cfg.db_path).expanduser())
    conn = sqlite3.connect(dbp, timeout=120, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS own_pdf_scan (ref_id TEXT PRIMARY KEY, result TEXT, via TEXT, "
                 "scanned_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS own_pdf_bak_{STAMP} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    rows = conn.execute(f"""SELECT id, pdf_local FROM refs WHERE NOT ({RefDatabase.COMPLETE_SQL})
        AND COALESCE(pdf_local,'') != '' AND id NOT IN (SELECT ref_id FROM own_pdf_scan
            WHERE NOT (? AND result = 'nothing'))
        ORDER BY (authors IS NULL OR authors IN ('','[]')) DESC, RANDOM()""", (RETRY,)).fetchall()
    todo = [(rid, p) for rid, p in rows if p.lower().endswith((".pdf", ".djvu")) and os.path.exists(p)][:LIMIT]
    print(f"[own-pdf] {len(rows):,} incomplete with a PDF path | {len(todo):,} this run | "
          f"{'WRITE' if WRITE else 'DRY-RUN'} | workers={WORKERS}", flush=True)
    global VOCAB
    VOCAB = PI.NameVocab(conn)
    db = RefDatabase()
    stats: Counter = Counter()
    t0 = time.time()
    cols = [d[0] for d in conn.execute("SELECT * FROM refs LIMIT 0").description]
    shown = 0
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for n, ((rid, path), (rec, via, page_authors, src, year, pages, err, fn_title)) in enumerate(
                zip(todo, pool.map(work, [p for _, p in todo], chunksize=2)), 1):
            seed = db.get(rid)
            if seed is None:
                continue
            up, how = plan(seed, rec, via, page_authors, src, year, pages, fn_title) if not err else ({}, "")
            result = ("error" if err else (how.split(":")[0] if up else ("no_gain" if how else "nothing")))
            stats[result] += 1
            if up and WRITE:
                row = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
                conn.execute(f"INSERT OR IGNORE INTO own_pdf_bak_{STAMP} VALUES (?,?)",
                             (rid, json.dumps(dict(zip(cols, row)), default=str)))
                PI.apply_fill(db, rid, up)
                after = db.get(rid)
                stats["now_complete"] += bool(after is not None and after.is_complete)
            if WRITE:
                conn.execute("INSERT OR REPLACE INTO own_pdf_scan (ref_id, result, via) VALUES (?,?,?)",
                             (rid, result, how))
            if up and shown < SHOW:
                shown += 1
                auth = json.loads(up.get("authors_json", "[]"))
                print(f"  {how:18s} {(seed.title or '')[:48]:48s} -> "
                      f"{(up.get('title') or '')[:48]:48s} | {', '.join(a.get('family','') for a in auth[:3])[:30]:30s}"
                      f" | {up.get('doi') or up.get('arxiv_id') or ''} {up.get('year') or ''}", flush=True)
            if n % 100 == 0:
                print(f"  ... {n:,}/{len(todo):,} | {dict(stats)} | {n / (time.time() - t0):.2f}/s", flush=True)
    print(f"done: {dict(stats)} in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
