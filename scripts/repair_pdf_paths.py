"""Repoint pdf_local paths that no longer exist to where the file actually is.

2026-09-25 audit: ~21% of live refs' pdf_local paths pointed at nothing. The
files were there -- under the same name in the other PDF folder (the library
moved between "Mouseion PDFs" and the vault's Mouseion/PDFs), or stored as a
bare file name. Refs whose file exists in no known folder keep their Drive id
(streamable) and the dead path moves to extras["pdf_local_stale"], so it no
longer counts as a delivered PDF. Nothing is deleted: every changed row is
copied to pdf_path_bak_<date> first.

Usage: python scripts/repair_pdf_paths.py <dry|write>
"""
from __future__ import annotations

import json
import ntpath
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from mouseion.config import get_config  # noqa: E402

WRITE = len(sys.argv) > 1 and sys.argv[1] == "write"
cfg = get_config()
DB = Path(cfg.db_path).expanduser()

# every folder that holds library PDFs; the configured one first
FOLDERS = [Path(cfg.pdf_storage_path)] if cfg.pdf_storage_path else []
# Other places imports left PDFs (e.g. a Mendeley/Zotero/Paperpile folder on Drive):
# MOUSEION_PDF_DIRS, separated by os.pathsep. Kept out of the code (public repo).
FOLDERS += [Path(p) for p in os.environ.get("MOUSEION_PDF_DIRS", "").split(os.pathsep) if p.strip()]
FOLDERS = list(dict.fromkeys(FOLDERS))
MIN_FILES = 1000              # a folder listing smaller than this = Drive not mounted: abort


def index_folders() -> tuple[dict[str, str], dict[str, set]]:
    by_name: dict[str, str] = {}
    listing: dict[str, set] = {}
    for d in FOLDERS:
        names: set = set()
        try:
            for root, _dirs, files in os.walk(d):
                for f in files:
                    if not f.lower().endswith(".pdf"):
                        continue
                    names.add(os.path.join(root, f).lower())
                    by_name.setdefault(f.lower(), os.path.join(root, f))
        except OSError as e:
            print(f"cannot list {d}: {e}")
        listing[str(d).lower()] = names
        print(f"  {d}: {len(names):,} files")
    total = sum(len(v) for v in listing.values())
    if total < MIN_FILES:
        sys.exit(f"only {total} PDFs visible -- Google Drive looks unmounted; nothing changed")
    return by_name, listing


def main() -> None:
    print(f"[repair-pdf-paths] {'WRITE' if WRITE else 'DRY-RUN'} | folders:")
    by_name, listing = index_folders()
    every = set().union(*listing.values())
    conn = sqlite3.connect(str(DB), timeout=60, isolation_level=None)
    stamp = time.strftime("%Y%m%d")
    conn.execute(f"CREATE TABLE IF NOT EXISTS pdf_path_bak_{stamp} (ref_id TEXT PRIMARY KEY, pdf_local TEXT, extras TEXT)")
    rows = conn.execute("SELECT id, pdf_local, pdf_drive_id, extras FROM refs "
                        "WHERE COALESCE(status,'') != 'duplicate' AND COALESCE(pdf_local,'') != ''").fetchall()
    stats = Counter()
    samples: dict[str, list] = {}
    for rid, path, drive_id, extras in rows:
        low = path.lower()
        folder = ntpath.dirname(low)
        if low in every or (folder not in listing and folder and os.path.exists(path)):
            stats["ok"] += 1
            continue
        new = by_name.get(ntpath.basename(low))
        if new:
            kind = "repointed"
        else:
            kind = "stale_with_drive_id" if drive_id else "stale_no_copy"
        stats[kind] += 1
        samples.setdefault(kind, [])
        if len(samples[kind]) < 4:
            samples[kind].append([path[-70:], (new or "")[-70:]])
        if not WRITE:
            continue
        conn.execute(f"INSERT OR IGNORE INTO pdf_path_bak_{stamp} VALUES (?,?,?)", (rid, path, extras))
        if new:
            conn.execute("UPDATE refs SET pdf_local = ? WHERE id = ?", (new, rid))
        else:
            ex = json.loads(extras) if extras else {}
            ex["pdf_local_stale"] = path
            conn.execute("UPDATE refs SET pdf_local = '', extras = ? WHERE id = ?", (json.dumps(ex), rid))
    print(json.dumps({"checked": len(rows), **stats}, indent=1))
    for k, v in samples.items():
        print(k, v)


if __name__ == "__main__":
    main()
