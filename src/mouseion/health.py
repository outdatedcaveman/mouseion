"""Library health: invariants that failed SILENTLY before (2026-09-25 audit).

Each check measures an output, never a status flag:
  - search index 1:1 with refs (6,323 refs had become unsearchable),
  - no refs without an id,
  - the Drive backup is recent (it had failed hourly for five weeks),
  - pdf_local paths resolve (21% pointed at nothing),
  - case-variant DOI duplicates stay rare (4,383 groups had piled up),
  - no enrichment work queued for refs that no longer exist,
  - enrichment providers are answering (Google Books: 0 successes, ever),
  - the PDF finder actually finds PDFs (0 of 1,829 attempts in a week).

`mouseion health` prints the report; Egon's Connectors page shows the failures.
Read-only and cheap (~30 s on a 220k-ref library).
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def _check(name: str, ok: bool, value: Any, detail: str = "") -> Dict[str, Any]:
    return {"check": name, "ok": bool(ok), "value": value, "detail": detail}


def run(db_path: str | Path | None = None, sample: int = 400) -> Dict[str, Any]:
    from .config import get_config
    cfg = get_config()
    db_path = Path(db_path or cfg.db_path).expanduser()
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
    out: List[Dict[str, Any]] = []
    t0 = time.time()

    ids = {r[0] for r in c.execute("SELECT id FROM refs")}
    n = len(ids)
    fts = [r[0] for r in c.execute("SELECT ref_id FROM refs_fts")]
    fts_set = set(fts)
    unindexed = sum(1 for i in ids if i not in fts_set)
    stray = len(fts) - len(fts_set) + sum(1 for i in fts_set if i not in ids)
    out.append(_check("search_index", unindexed == 0 and stray == 0, {"unindexed": unindexed, "stray_rows": stray},
                      "every ref searchable exactly once"))
    null_ids = c.execute("SELECT COUNT(*) FROM refs WHERE id IS NULL OR id = ''").fetchone()[0]
    out.append(_check("ref_ids", null_ids == 0, null_ids, "refs without an id"))

    raw = c.execute("SELECT value FROM settings WHERE key = 'drive_last_backup_time'").fetchone()
    age_h = None
    if raw and raw[0]:
        try:
            last = datetime.fromisoformat(raw[0])
            last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
            age_h = round((datetime.now(timezone.utc) - last).total_seconds() / 3600, 1)
        except ValueError:
            pass
    out.append(_check("drive_backup", age_h is not None and age_h <= 48, age_h, "hours since the last Drive backup"))

    paths = [r[0] for r in c.execute("SELECT pdf_local FROM refs WHERE COALESCE(pdf_local,'') != ''")]
    picked = random.Random(int(time.time()) // 86400).sample(paths, min(sample, len(paths))) if paths else []
    ok_share = (sum(os.path.exists(p) for p in picked) / len(picked)) if picked else 1.0
    out.append(_check("pdf_paths", ok_share >= 0.97, round(ok_share, 3),
                      f"share of {len(picked)} sampled pdf_local paths that exist (scripts/repair_pdf_paths.py fixes)"))

    dup_doi = c.execute("SELECT COUNT(*) FROM (SELECT lower(doi) FROM refs WHERE COALESCE(doi,'') != '' "
                        "GROUP BY 1 HAVING COUNT(*) > 1)").fetchone()[0]
    out.append(_check("duplicate_dois", dup_doi <= 200, dup_doi, "case-variant DOI groups (mouseion dedup-all merges them)"))

    orphan_q = sum(1 for (i,) in c.execute("SELECT ref_id FROM enrich_queue") if i not in ids)
    out.append(_check("enrich_queue", orphan_q == 0, orphan_q, "queued work for refs that no longer exist"))

    router = db_path.parent / "api_router.db"
    if router.exists():
        r = sqlite3.connect(f"file:{router}?mode=ro", uri=True, timeout=30)
        dead = [a for (a, ok, fail, streak) in r.execute(
            "SELECT api, total_ok, total_fail, consec_errors FROM api_budget")
            if streak >= 20 or (ok == 0 and fail >= 20)]
        out.append(_check("providers", not dead, dead, "enrichment sources failing persistently"))
        r.close()
    # PDF finder: judged by files it actually saved, not by its ledger (which,
    # until 2026-09-25, recorded misses only -- "0 hits" proved nothing).
    week = time.time() - 7 * 86400
    new_files, tries = 0, 0
    try:
        with os.scandir(cfg.pdf_storage_path) as it:
            new_files = sum(1 for e in it if e.name.lower().endswith(".pdf") and e.stat().st_mtime >= week)
    except (OSError, TypeError):
        pass
    if router.exists():
        r = sqlite3.connect(f"file:{router}?mode=ro", uri=True, timeout=30)
        tries = r.execute("SELECT COUNT(*) FROM attempt_ledger WHERE api IN ('pdf', 'pdf_inst') AND ts >= ?",
                          (week,)).fetchone()[0]
        r.close()
    out.append(_check("pdf_finder", tries == 0 or new_files > 0, {"new_pdfs_7d": new_files, "attempts_7d": tries},
                      "PDFs saved to the storage folder in the last 7 days"))

    complete = c.execute("SELECT COUNT(*) FROM refs WHERE " + _complete_sql()).fetchone()[0]
    c.close()
    report = {"refs": n, "complete_pct": round(100 * complete / max(1, n), 2),
              "failed": [x["check"] for x in out if not x["ok"]], "checks": out,
              "seconds": round(time.time() - t0, 1), "at": datetime.now(timezone.utc).isoformat()}
    try:                                   # the last report, for Egon's Connectors page
        (db_path.parent / "health.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    except OSError:
        pass
    return report


def _complete_sql() -> str:
    from .db import RefDatabase
    return RefDatabase.COMPLETE_SQL


if __name__ == "__main__":
    print(json.dumps(run(), indent=1))
