"""Import the references of a library export that Mouseion does not have yet.

2026-09-25 audit: ~32% of the Paperpile export (about 4,700 of 14,995 refs)
was never in Mouseion -- not lost (older library copies never held them either),
just never imported. This adds exactly the absent ones:

  * parsed by Mouseion's own parsers (.ris / .bib / .json),
  * absent = no DOI match, no normalized-title match, and no FTS candidate
    whose title agrees >= 90 (token-set) -- live refs and the dedup archive,
  * saved without network enrichment (the enrichment/recovery pipelines
    complete them), tagged `import:<source>-<date>` so the batch is traceable
    and removable.

Usage: python scripts/import_missing.py <export file> <source-name> <dry|write>
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from rapidfuzz import fuzz  # noqa: E402

from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase  # noqa: E402
from mouseion.input import _parse_file  # noqa: E402

SRC, NAME = Path(sys.argv[1]), sys.argv[2]
WRITE = len(sys.argv) > 3 and sys.argv[3] == "write"


def norm(t) -> str:
    t = unicodedata.normalize("NFKD", re.sub(r"<[^>]+>", " ", str(t or "")))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", "".join(c for c in t if not unicodedata.combining(c)).lower()).split())


def main() -> None:
    refs = _parse_file(SRC)
    print(f"[import-missing] {SRC.name}: {len(refs):,} parsed | {'WRITE' if WRITE else 'DRY-RUN'}", flush=True)
    c = sqlite3.connect(f"file:{get_config().db_path}?mode=ro", uri=True, timeout=60)
    dois = {r[0] for r in c.execute("SELECT lower(doi) FROM refs WHERE COALESCE(doi,'') != ''")}
    titles = {norm(r[0]) for r in c.execute("SELECT title FROM refs WHERE COALESCE(title,'') != ''")}
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='refs_duplicates'").fetchone():
        dois |= {r[0] for r in c.execute("SELECT lower(doi) FROM refs_duplicates WHERE COALESCE(doi,'') != ''")}
        titles |= {norm(r[0]) for r in c.execute("SELECT title FROM refs_duplicates WHERE COALESCE(title,'') != ''")}

    def present(ref) -> str:
        if ref.doi and ref.doi.lower() in dois:
            return "doi"
        n = norm(ref.title)
        if not n:
            return "no_title"
        if n in titles:
            return "title"
        words = [w for w in n.split() if len(w) > 3][:6]
        if len(words) >= 2:
            try:
                rows = c.execute("SELECT r.title FROM refs_fts f JOIN refs r ON r.id = f.ref_id "
                                 "WHERE refs_fts MATCH ? LIMIT 25", (" ".join(f'"{w}"' for w in words),)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if any(fuzz.token_set_ratio(n, norm(t)) >= 90 for (t,) in rows):
                return "fuzzy_title"
        return ""

    stats: dict[str, int] = {}
    missing = []
    seen = set()
    for ref in refs:
        why = present(ref)
        key = (ref.doi or "").lower() or norm(ref.title)
        if not why and key in seen:
            why = "repeat_in_export"
        stats[why or "absent"] = stats.get(why or "absent", 0) + 1
        if not why:
            seen.add(key)
            missing.append(ref)
    c.close()
    print(json.dumps(stats), flush=True)
    for r in missing[:8]:
        print("  absent:", (r.title or "")[:70], r.year, (r.doi or "")[:30])
    if not WRITE or not missing:
        return
    tag = f"import:{NAME}-{time.strftime('%Y%m%d')}"
    db = RefDatabase()          # not opened: every upsert commits on its own (no long write lock)
    for i, ref in enumerate(missing, 1):
        ref.sources = {**(ref.sources or {}), f"{NAME}_export": 0.9}
        db.upsert(ref, tags=[tag])
        if i % 500 == 0:
            print(f"  ... {i:,}/{len(missing):,}", flush=True)
    print(f"imported {len(missing):,} refs tagged {tag}", flush=True)


if __name__ == "__main__":
    main()
