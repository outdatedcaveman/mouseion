"""Bring every relevant PDF under a folder into the library (mouseion.pdf_ingest).

Resumable: each file's outcome is recorded in the pdf_ingest_log table (path,
size, mtime, action, ref_id, detail); a file is looked at again only if it
changed. Skipped files keep their reason there for review -- nothing is moved
or deleted, and linked PDFs stay where they are.

Usage: python scripts/ingest_folder.py <folder> <tag> [limit] [dry|write] [workers]
  e.g. python scripts/ingest_folder.py "G:\\My Drive\\Archives" archives 200000 write 4
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from mouseion import pdf_ingest as PI  # noqa: E402
from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase  # noqa: E402

ROOT, TAG = sys.argv[1], sys.argv[2]
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 10 ** 9
WRITE = len(sys.argv) > 4 and sys.argv[4] == "write"
WORKERS = int(sys.argv[5]) if len(sys.argv) > 5 else 4
cfg = get_config()


def main() -> None:
    conn = sqlite3.connect(str(Path(cfg.db_path).expanduser()), timeout=60, isolation_level=None)
    conn.execute("""CREATE TABLE IF NOT EXISTS pdf_ingest_log (path TEXT PRIMARY KEY, size INTEGER, mtime REAL,
                    action TEXT, ref_id TEXT, via TEXT, detail TEXT, tag TEXT, at TEXT DEFAULT (datetime('now')))""")
    seen = {p: (s, m) for p, s, m in conn.execute("SELECT path, size, mtime FROM pdf_ingest_log")}
    todo = []
    for dp, _dn, fn in os.walk(ROOT):
        for f in fn:
            if not f.lower().endswith((".pdf", ".djvu")):
                continue
            p = os.path.join(dp, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if seen.get(p) == (st.st_size, st.st_mtime):
                continue
            todo.append((p, st.st_size, st.st_mtime))
            if len(todo) >= LIMIT:
                break
        if len(todo) >= LIMIT:
            break
    print(f"[ingest] {ROOT} | {len(todo):,} new/changed PDFs | {'WRITE' if WRITE else 'DRY-RUN'} | "
          f"workers={WORKERS}", flush=True)
    index = PI.LibraryIndex(conn)
    db = RefDatabase()
    stats: Counter = Counter()
    t0 = time.time()

    import shutil
    floor = float(os.environ.get("MOUSEION_INGEST_DISK_FLOOR_GB", "12"))

    def guarded():
        # Drive for desktop stages each file it streams on the system disk: stop early, resume later
        for i, item in enumerate(todo):
            if i % 200 == 0 and shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").free / 1024 ** 3 < floor:
                print(f"  stopping: system disk below {floor} GB free (resumes on the next run)", flush=True)
                return
            yield item

    def results_stream(pool):
        # chunks of 200 so the disk guard is re-checked as Drive stages files
        gen = guarded()
        while True:
            chunk = [it for _, it in zip(range(200), gen)]
            if not chunk:
                return
            yield from zip(chunk, pool.map(PI.process_file, [it[0] for it in chunk], chunksize=4))

    with ProcessPoolExecutor(max_workers=WORKERS) as pool:   # PDF parsing holds the GIL: processes, not threads
        for n, (item, (f, keep, why, rec, via, err)) in enumerate(results_stream(pool), 1):
            p, size, mtime = item
            if err:
                why = "error: " + err
            if f is None or not keep:
                action = "error" if why.startswith("error") else "skipped"
                res = PI.IngestResult(action, detail=why)
            else:
                # matching and writing stay on this thread (one writer, one index)
                ref = rec or PI.from_pdf_only(f)
                hit = index.find(ref) or (index.find(PI.from_pdf_only(f)) if rec else None)
                if hit and hit[1]:
                    res = PI.IngestResult("exists", hit[0], "library already has a PDF", via)
                elif hit:
                    if WRITE:
                        db.update_integration_ids(hit[0], pdf_local=p, pdf_path=Path(p).name)
                        index.add(hit[0], ref, True)
                    res = PI.IngestResult("attached", hit[0], "", via)
                else:
                    rid = ""
                    if WRITE:
                        ref.sources = {**(ref.sources or {}), "archive_pdf": 0.9 if rec else 0.4}
                        rid = db.upsert(ref, tags=[f"archive:{TAG}"] + ([] if rec else ["pdf:unresolved"]))
                        db.update_integration_ids(rid, pdf_local=p, pdf_path=Path(p).name)
                        index.add(rid, ref, True)
                    res = PI.IngestResult("created" if rec else "created_unresolved", rid, "", via or "pdf-only")
            stats[res.action] += 1
            if WRITE:
                conn.execute("INSERT OR REPLACE INTO pdf_ingest_log (path,size,mtime,action,ref_id,via,detail,tag) "
                             "VALUES (?,?,?,?,?,?,?,?)", (p, size, mtime, res.action, res.ref_id, res.via,
                                                          res.detail[:200], TAG))
            elif n <= 60:
                print(f"  {res.action:18s} {res.via:6s} {res.detail[:40]:40s} {os.path.relpath(p, ROOT)[:70]}",
                      flush=True)
            if n % 100 == 0:
                print(f"  ... {n:,}/{len(todo):,} | {dict(stats)} | {n / (time.time() - t0):.2f}/s", flush=True)
    print(f"done: {dict(stats)} in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
