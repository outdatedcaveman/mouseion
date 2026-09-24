"""Run a bounded Mouseion enrichment-queue batch.

This is a foreground, fixed-size wrapper around mouseion.enrich_daemon's
existing tier handlers. It avoids starting the background daemon when an
orchestrator wave needs attributable counts.
"""

from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import time
from typing import Any

from mouseion.config import get_config
from mouseion.db import RefDatabase
from mouseion import enrich_daemon as ed


STRICT_COMPLETE_SQL = """
title IS NOT NULL AND title != ''
AND authors IS NOT NULL AND authors != '[]' AND authors != ''
AND publisher IS NOT NULL AND publisher != ''
AND year IS NOT NULL AND year != 0
AND ((url IS NOT NULL AND url != '') OR (doi IS NOT NULL AND doi != ''))
"""


def _metrics(db_path: str) -> dict[str, Any]:
    with sqlite3.connect(db_path, timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN pdf_local IS NOT NULL AND pdf_local != '' THEN 1 ELSE 0 END) AS pdfs,
              SUM(CASE WHEN {STRICT_COMPLETE_SQL} THEN 1 ELSE 0 END) AS strict_complete,
              SUM(CASE WHEN completeness >= 0.8 THEN 1 ELSE 0 END) AS completeness_ge_08,
              AVG(completeness) AS avg_completeness
            FROM refs
            """
        ).fetchone()
        queue = conn.execute(
            "SELECT status, COUNT(*) AS count FROM enrich_queue GROUP BY status ORDER BY status"
        ).fetchall()
    out = dict(row)
    out["queue"] = {str(r["status"]): int(r["count"]) for r in queue}
    return out


def _active_ids(db: RefDatabase, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ids = [r["ref_id"] for r in rows]
    with db._db() as conn:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE enrich_queue SET status = 'active', last_attempt = datetime('now') "
            f"WHERE ref_id IN ({placeholders})",
            ids,
        )
    if db._conn:
        db._conn.commit()


def _dequeue(db: RefDatabase, tier_where: str, limit: int) -> list[dict[str, Any]]:
    """Dequeue up to limit rows for one tier using the daemon's tier query."""
    with db._db() as conn:
        rows = conn.execute(
            f"""
            SELECT eq.ref_id, eq.priority, eq.difficulty, eq.strategy_level,
                   eq.attempts, eq.last_error, r.doi, r.pmid, r.arxiv_id, r.isbn,
                   r.url, r.title, r.completeness, r.year
            FROM enrich_queue eq
            JOIN refs r ON r.id = eq.ref_id
            WHERE eq.status = 'pending'
              AND (eq.last_attempt IS NULL OR eq.last_attempt < datetime('now', '-90 seconds'))
              AND ({tier_where})
            ORDER BY
              CASE WHEN r.completeness >= 0.6 AND r.completeness < 0.8 THEN 1 ELSE 2 END ASC,
              eq.priority DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = [dict(r) for r in rows]
    _active_ids(db, out)
    return out


def _mark_no_match(db: RefDatabase, ref_id: str, ref, row: dict[str, Any]) -> None:
    if (
        ed._is_junk_title(getattr(ref, "title", None), ref, strict=True)
        and not (ref.doi or ref.url or ref.arxiv_id or ref.pmid)
    ):
        with db._db() as conn:
            conn.execute(
                "UPDATE enrich_queue SET status='done', attempts = attempts + 1, "
                "last_error='parked: unrecoverable junk/filename title' WHERE ref_id = ?",
                (ref_id,),
            )
        return
    db.complete_enrich(ref_id, new_completeness=ref.completeness or 0.0, error="no match")


def run(limit: int, min_tier: int, max_tier: int, dry_run: bool = False) -> dict[str, Any]:
    cfg = get_config(reload=True)
    before = _metrics(cfg.db_path)
    started = time.time()
    counts: dict[str, Any] = {
        "selected": 0,
        "result_rows": 0,
        "saved_results": 0,
        "unresolved": 0,
        "requeued_on_error": 0,
        "by_tier": {},
    }

    db = RefDatabase()
    try:
        try:
            counts["stale_active_reset"] = db.reset_stale_active()
        except Exception:
            counts["stale_active_reset"] = 0

        for tier_num, (tier_where, handler, tier_batch_size) in enumerate(ed.TIERS, 1):
            if tier_num < min_tier or tier_num > max_tier:
                continue
            while counts["selected"] < limit:
                take = min(int(tier_batch_size), int(limit - counts["selected"]))
                if take <= 0:
                    break
                rows = _dequeue(db, tier_where, take)
                if not rows:
                    break
                tier_counts = counts["by_tier"].setdefault(
                    str(tier_num),
                    {"selected": 0, "result_rows": 0, "saved_results": 0, "unresolved": 0},
                )
                counts["selected"] += len(rows)
                tier_counts["selected"] += len(rows)
                refs_by_id = {}
                rows_by_id = {}
                for row in rows:
                    ref = db.get(row["ref_id"])
                    if ref:
                        refs_by_id[row["ref_id"]] = ref
                        rows_by_id[row["ref_id"]] = row

                if dry_run:
                    ed._requeue_batch(db, rows)
                    continue

                try:
                    original_by_id = {
                        ref_id: copy.deepcopy(ref)
                        for ref_id, ref in refs_by_id.items()
                    }
                    result = handler(list(refs_by_id.values()), list(rows_by_id.values()))
                    if not isinstance(result, dict):
                        result = {}
                    counts["result_rows"] += len(result)
                    tier_counts["result_rows"] += len(result)
                    with RefDatabase() as batch_db:
                        for ref_id, enriched in result.items():
                            row = rows_by_id.get(ref_id)
                            original = original_by_id.get(ref_id) or refs_by_id.get(ref_id)
                            if not (row and original and enriched):
                                continue
                            ed._save_result(batch_db, original, enriched, row)
                            counts["saved_results"] += 1
                            tier_counts["saved_results"] += 1
                        for ref_id, ref in refs_by_id.items():
                            if ref_id in result:
                                continue
                            _mark_no_match(batch_db, ref_id, ref, rows_by_id[ref_id])
                            counts["unresolved"] += 1
                            tier_counts["unresolved"] += 1
                except Exception:
                    ed._requeue_batch(db, rows)
                    counts["requeued_on_error"] += len(rows)
                    break
    finally:
        db.close()

    after = _metrics(cfg.db_path)
    return {
        "mode": "enrich_queue_batch",
        "limit": limit,
        "min_tier": min_tier,
        "max_tier": max_tier,
        "dry_run": dry_run,
        "counts": counts,
        "before": before,
        "after": after,
        "pdf_delta": int(after["pdfs"] or 0) - int(before["pdfs"] or 0),
        "strict_complete_delta": int(after["strict_complete"] or 0) - int(before["strict_complete"] or 0),
        "completeness_ge_08_delta": int(after["completeness_ge_08"] or 0)
        - int(before["completeness_ge_08"] or 0),
        "elapsed_s": round(time.time() - started, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument("--min-tier", type=int, default=1)
    parser.add_argument("--max-tier", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run(
        max(1, args.limit),
        max(1, min(5, args.min_tier)),
        max(1, min(5, args.max_tier)),
        args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
