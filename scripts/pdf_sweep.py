"""Fetch PDFs for every ref that lacks one, from licensed and open-access sources.

Uses Mouseion's own fetcher (oa_url, arXiv, Unpaywall, Semantic Scholar, CORE,
publisher DOI -- which resolves to the full text when the institutional VPN is
up -- and web search), with the Sci-Hub / Anna's Archive strategies switched off
(MOUSEION_PDF_SHADOW=0; misses go to their own ledger key, so they never block
a later run of the app's full chain).

Order: refs with a known open-access link, then arXiv, then DOI. Resumable (the
attempt ledger skips what was already tried from the same network). Stops when
the system disk falls below the floor -- Drive for desktop stages uploads there.

Usage: python scripts/pdf_sweep.py [limit] [batch]
"""
from __future__ import annotations

import os

os.environ.setdefault("MOUSEION_PDF_SHADOW", "0")

import shutil  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import anyio  # noqa: E402

from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase  # noqa: E402
from mouseion.pdf_manager import download_pdfs_batch, get_pdf_dir  # noqa: E402

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 200000
BATCH = int(sys.argv[2]) if len(sys.argv) > 2 else 100
DISK_FLOOR_GB = float(os.environ.get("MOUSEION_SWEEP_DISK_FLOOR_GB", "12"))


def main() -> None:
    cfg = get_config()
    c = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=60)
    rows = c.execute(
        """SELECT id, lower(COALESCE(doi,'')) FROM refs
           WHERE COALESCE(pdf_local,'') = '' AND COALESCE(pdf_drive_id,'') = ''
             AND (COALESCE(oa_url,'') != '' OR COALESCE(arxiv_id,'') != '' OR COALESCE(doi,'') != '')
           ORDER BY (COALESCE(oa_url,'') != '') DESC, (COALESCE(arxiv_id,'') != '') DESC, year DESC""").fetchall()
    # Round-robin across publishers (DOI prefix): each publisher host is paced
    # (publisher_gate), so a batch of one publisher's refs would just queue.
    from collections import OrderedDict
    queues: "OrderedDict[str, list]" = OrderedDict()
    for rid, doi in rows:
        queues.setdefault(doi.split("/")[0] if doi else "-", []).append(rid)
    ids = []
    while queues and len(ids) < LIMIT:
        for k in list(queues):
            ids.append(queues[k].pop(0))
            if not queues[k]:
                del queues[k]
    ids = ids[:LIMIT]
    c.close()
    pdf_dir = get_pdf_dir()
    print(f"[pdf-sweep] {len(ids):,} refs | shadow sources {'ON' if os.environ['MOUSEION_PDF_SHADOW'] != '0' else 'off'} "
          f"| into {pdf_dir}", flush=True)
    db = RefDatabase()
    got = tried = 0
    t0 = time.time()
    for i in range(0, len(ids), BATCH):
        free = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").free / 1024 ** 3
        if free < DISK_FLOOR_GB:
            print(f"  stopping: {free:.1f} GB free on the system disk (< {DISK_FLOOR_GB} GB)", flush=True)
            break
        refs = []
        for rid in ids[i:i + BATCH]:
            ref = db.get(rid)
            if ref is not None:
                ref._db_id = rid
                refs.append(ref)
        results = anyio.run(download_pdfs_batch, refs)
        for ref, path in results:
            tried += 1
            if path:
                got += 1
                db.update_integration_ids(ref._db_id, pdf_local=str(pdf_dir / path), pdf_path=path)
        rate = tried / max(1.0, time.time() - t0)
        print(f"  ... {tried:,}/{len(ids):,} tried | {got:,} PDFs saved ({100 * got / max(1, tried):.1f}%) "
              f"| {rate:.2f}/s | disk {free:.0f} GB", flush=True)
    print(f"done: {got:,} PDFs from {tried:,} refs in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
