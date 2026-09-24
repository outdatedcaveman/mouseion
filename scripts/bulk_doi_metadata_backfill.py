"""Bounded DOI metadata backfill for Mouseion.

Targets high-yield rows that already have a DOI but are still missing strict
completion fields, especially publisher. Uses existing Mouseion batch provider
code so network throttling/cooldowns remain shared with the app.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from typing import Any

from mouseion.batch_lookup import batch_crossref, batch_openalex
from mouseion.config import get_config
from mouseion.db import _authors_json, _row_to_ref


STRICT_COMPLETE_SQL = """
title IS NOT NULL AND title != ''
AND authors IS NOT NULL AND authors != '[]' AND authors != ''
AND publisher IS NOT NULL AND publisher != ''
AND year IS NOT NULL AND year != 0
AND ((url IS NOT NULL AND url != '') OR (doi IS NOT NULL AND doi != ''))
"""

# Mirrors scripts/recompute_completeness.py / Reference.completeness.
_RED = "(CASE WHEN ref_type IN ('book','book-chapter','preprint')"
COMPLETENESS_FORMULA = f"""ROUND(MIN(1.0,
   (CASE WHEN title IS NOT NULL AND title!='' THEN 0.22 ELSE 0 END)
 + (CASE WHEN authors IS NOT NULL AND authors!='[]' AND authors!='' THEN 0.15 ELSE 0 END)
 + (CASE WHEN year IS NOT NULL AND year!=0 THEN 0.10 ELSE 0 END)
 + (CASE WHEN (doi IS NOT NULL AND doi!='') OR (arxiv_id IS NOT NULL AND arxiv_id!='')
            OR (pmid IS NOT NULL AND pmid!='') OR (isbn IS NOT NULL AND isbn!='') THEN 0.15 ELSE 0 END)
 + (CASE WHEN (journal IS NOT NULL AND journal!='') OR (container_title IS NOT NULL AND container_title!='')
            OR (publisher IS NOT NULL AND publisher!='') THEN 0.08 ELSE 0 END)
 + 0.12
 + (CASE WHEN volume IS NOT NULL AND volume!='' THEN {_RED} THEN 0.03 ELSE 0.05 END) ELSE 0 END)
 + (CASE WHEN issue IS NOT NULL AND issue!='' THEN {_RED} THEN 0.02 ELSE 0.04 END) ELSE 0 END)
 + (CASE WHEN (pages IS NOT NULL AND pages!='') OR (article_number IS NOT NULL AND article_number!='')
        THEN {_RED} THEN 0.04 ELSE 0.05 END) ELSE 0 END)
 + (CASE WHEN citation_count IS NOT NULL AND citation_count>0 THEN 0.04 ELSE 0 END)
), 4)"""


def _metrics(conn: sqlite3.Connection) -> dict[str, Any]:
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
    return dict(row)


def _candidate_rows(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT *
        FROM refs
        WHERE doi IS NOT NULL AND doi != ''
          AND (
            NOT ({STRICT_COMPLETE_SQL})
            OR oa_url IS NULL OR oa_url = ''
          )
        ORDER BY
          CASE
            WHEN title IS NOT NULL AND title != ''
             AND authors IS NOT NULL AND authors != '[]' AND authors != ''
             AND year IS NOT NULL AND year != 0
             AND (publisher IS NULL OR publisher = '') THEN 1
            WHEN NOT ({STRICT_COMPLETE_SQL}) THEN 2
            WHEN (oa_url IS NULL OR oa_url = '') THEN 3
            ELSE 4
          END,
          completeness DESC,
          year DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _sources_json(existing: str | None, provider: str) -> str:
    try:
        sources = json.loads(existing or "{}")
        if not isinstance(sources, dict):
            sources = {}
    except json.JSONDecodeError:
        sources = {}
    sources[provider] = max(float(sources.get(provider, 0) or 0), 1.0)
    return json.dumps(sources, sort_keys=True)


def _best_url(enriched) -> str | None:
    return enriched.url or enriched.oa_url or (
        f"https://doi.org/{enriched.doi}" if enriched.doi else None
    )


def _apply_result(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    enriched,
    provider: str,
) -> tuple[bool, list[str]]:
    fields: dict[str, Any] = {}
    changed: list[str] = []

    def fill(col: str, value: Any) -> None:
        if value is None or value == "" or value == []:
            return
        current = row[col]
        if current is None or current == "" or current == "[]" or current == 0:
            fields[col] = value
            changed.append(col)

    fill("title", enriched.title)
    if enriched.authors:
        fill("authors", _authors_json(enriched.authors))
    fill("year", enriched.year)
    fill("publisher", enriched.publisher)
    fill("url", _best_url(enriched))
    fill("journal", enriched.journal)
    fill("container_title", enriched.container_title)
    fill("issn", enriched.issn)
    fill("eissn", enriched.eissn)
    fill("pmid", enriched.pmid)
    fill("pmcid", enriched.pmcid)
    fill("arxiv_id", enriched.arxiv_id)
    fill("abstract", enriched.abstract)
    fill("license", enriched.license)
    fill("oa_url", enriched.oa_url)
    if enriched.open_access is not None:
        fill("open_access", int(bool(enriched.open_access)))
    fill("citation_count", enriched.citation_count)
    if not fields:
        return False, []

    fields["sources"] = _sources_json(row["sources"], provider)
    fields["updated_at"] = int(time.time())
    sets = ", ".join(f"{col} = :{col}" for col in fields)
    params = dict(fields)
    params["id"] = row["id"]
    conn.execute(f"UPDATE refs SET {sets} WHERE id = :id", params)
    conn.execute(
        f"UPDATE refs SET completeness = {COMPLETENESS_FORMULA} WHERE id = ?",
        (row["id"],),
    )
    return True, changed


async def _resolve(rows: list[sqlite3.Row], crossref_fallback: bool) -> dict[str, tuple[Any, str]]:
    refs = []
    for row in rows:
        ref = _row_to_ref(row)
        ref._batch_id = row["id"]
        refs.append(ref)

    cfg = get_config(reload=True)
    resolved: dict[str, tuple[Any, str]] = {}
    oa = await batch_openalex(
        refs,
        email=cfg.openalex_email,
        api_key=cfg.openalex_api_key,
    )
    for ref_id, candidates in oa.items():
        if candidates:
            candidates.sort(key=lambda item: item[1], reverse=True)
            resolved[ref_id] = (candidates[0][0], "openalex")

    if crossref_fallback:
        unresolved = [ref for ref in refs if ref._batch_id not in resolved]
        cr = await batch_crossref(unresolved, email=cfg.crossref_email or cfg.openalex_email)
        for ref_id, candidates in cr.items():
            if candidates:
                candidates.sort(key=lambda item: item[1], reverse=True)
                resolved[ref_id] = (candidates[0][0], "crossref")
    return resolved


def run(limit: int, crossref_fallback: bool, dry_run: bool) -> dict[str, Any]:
    cfg = get_config(reload=True)
    started = time.time()
    with sqlite3.connect(cfg.db_path, timeout=120) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 120000")
        before = _metrics(conn)
        rows = _candidate_rows(conn, limit)
        resolved = asyncio.run(_resolve(rows, crossref_fallback))

        counts = {
            "selected": len(rows),
            "resolved": len(resolved),
            "updated": 0,
            "provider_openalex": 0,
            "provider_crossref": 0,
        }
        changed_fields: dict[str, int] = {}

        if dry_run:
            after = before
        else:
            for row in rows:
                result = resolved.get(row["id"])
                if not result:
                    continue
                enriched, provider = result
                ok, changed = _apply_result(conn, row, enriched, provider)
                if not ok:
                    continue
                counts["updated"] += 1
                counts[f"provider_{provider}"] += 1
                for field in changed:
                    changed_fields[field] = changed_fields.get(field, 0) + 1
            conn.commit()
            after = _metrics(conn)

    return {
        "mode": "doi_metadata_backfill",
        "limit": limit,
        "crossref_fallback": crossref_fallback,
        "dry_run": dry_run,
        "counts": counts,
        "changed_fields": dict(sorted(changed_fields.items())),
        "before": before,
        "after": after,
        "strict_complete_delta": int(after["strict_complete"] or 0) - int(before["strict_complete"] or 0),
        "completeness_ge_08_delta": int(after["completeness_ge_08"] or 0) - int(before["completeness_ge_08"] or 0),
        "elapsed_s": round(time.time() - started, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--crossref-fallback", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run(max(1, args.limit), args.crossref_fallback, args.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
