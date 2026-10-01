"""Build the junk-review page's data: candidates (scripts/junk_candidates.py output) in chunks of
100, each with its current web-search outcome (web_trail_scan + extras.web_trail), as JSON files
for the review artifact's `chunks` collection. Decisions come back in its `decisions` collection
and are applied by scripts/apply_junk_decisions.py.

Usage: python scripts/junk_review_chunks.py <candidates.json> <out_dir>
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from mouseion.config import get_config  # noqa: E402

CHUNK = 100


def main() -> None:
    cands = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{Path(get_config().db_path).expanduser()}?mode=ro", uri=True, timeout=60)
    scan = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT ref_id, result, url FROM web_trail_scan")}
    rows = []
    for c in cands:
        ex = conn.execute("SELECT extras FROM refs WHERE id=?", (c["id"],)).fetchone()
        if ex is None:
            continue                                  # gone since (archived or merged)
        try:
            wt = (json.loads(ex[0] or "{}") or {}).get("web_trail") or {}
        except Exception:
            wt = {}
        res, url = scan.get(c["id"], ("not searched yet", ""))
        rows.append({k: c.get(k, "") for k in ("id", "title", "authors", "year", "type", "reason", "pdf",
                                                "imported_from", "suggestion")}
                    | {"web": res, "web_url": url or wt.get("url", ""), "web_title": wt.get("title", "")})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    files = []
    for i in range(0, len(rows), CHUNK):
        name = f"c{i // CHUNK + 1:03d}"
        (out / f"{name}.json").write_text(json.dumps({"rows": rows[i:i + CHUNK], "updated": now},
                                                     ensure_ascii=False), encoding="utf-8")
        files.append(name)
    print(json.dumps({"rows": len(rows), "chunks": files}))


if __name__ == "__main__":
    main()
