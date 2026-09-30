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
from mouseion import pdf_ingest as PI  # noqa: E402
from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase, _authors_json  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LIMIT = int(args[0]) if args else 100
WRITE = len(args) > 1 and args[1] == "write"
WORKERS = int(args[2]) if len(args) > 2 else 4
SHOW = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else (40 if not WRITE else 0)
STAMP = date.today().strftime("%Y%m%d")
# page 1 of a long document is a cover, a series page or a whole issue's first article:
# its "author line" is an editor or someone else (hand check 2026-09-30)
LONG_PAGES = 60
VOCAB = None


def work(path: str):
    """Worker process: what the PDF says, resolved.
    -> (rec, via, authors, authors_from, year, pages, error)."""
    try:
        f = PI.extract(path)
        if f.error and not f.text:
            return None, "", [], "", None, 0, f.error
        rec, via = PI.resolve(f)
        src = ""
        # OCR text garbles names: author lines only from a real text layer
        page_authors = PI.authors_from_page(f) if f.pages < LONG_PAGES and not f.ocr else []
        if page_authors:
            src = "page"
        else:
            fa = PI.filename_author(Path(path).stem)
            page_authors, src = ([fa], "filename") if fa else ([], "")
        # a year read off the page text can be anything: only the folder's year
        return rec, via, page_authors, src, f.year_hint, f.pages, ""
    except Exception as e:
        return None, "", [], "", None, 0, f"{type(e).__name__}: {str(e)[:80]}"


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, PI._norm(a), PI._norm(b)).ratio()


def plan(seed, rec, via, page_authors, src, year, pages):
    """The field updates for one entry (only empty fields; title per the rules above)."""
    up = {}
    how = ""
    if rec is not None:
        up = PI.fill_plan(seed, rec, pages)
        how = "record:" + via if up else ""
        if up:
            return up, how
    if page_authors and not seed.authors and VOCAB.ok(page_authors):
        how = src + "-authors"
        up["authors_json"] = _authors_json(page_authors)
        if year and not seed.year:
            up["year"] = year
    return up, how


def main() -> None:
    cfg = get_config()
    dbp = str(Path(cfg.db_path).expanduser())
    conn = sqlite3.connect(dbp, timeout=120, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS own_pdf_scan (ref_id TEXT PRIMARY KEY, result TEXT, via TEXT, "
                 "scanned_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS own_pdf_bak_{STAMP} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    rows = conn.execute(f"""SELECT id, pdf_local FROM refs WHERE NOT ({RefDatabase.COMPLETE_SQL})
        AND COALESCE(pdf_local,'') != '' AND id NOT IN (SELECT ref_id FROM own_pdf_scan)
        ORDER BY (authors IS NULL OR authors IN ('','[]')) DESC, RANDOM()""").fetchall()
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
        for n, ((rid, path), (rec, via, page_authors, src, year, pages, err)) in enumerate(
                zip(todo, pool.map(work, [p for _, p in todo], chunksize=2)), 1):
            seed = db.get(rid)
            if seed is None:
                continue
            up, how = plan(seed, rec, via, page_authors, src, year, pages) if not err else ({}, "")
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
