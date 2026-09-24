"""Bounded OA-only PDF fetch batch for Mouseion.

This intentionally avoids the web worker's VPN and gray-source fallbacks. It
only tries:
  1. existing oa_url
  2. arXiv PDF URL
  3. Unpaywall best OA PDF for DOI rows

It uses api_router for shared budgets and the per-entry PDF attempt ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import httpx

from mouseion.api_router import get_router
from mouseion.config import get_config
from mouseion.db import RefDatabase, _row_to_ref
from mouseion.pdf_manager import get_pdf_dir, sanitize_filename


USER_AGENT = "mouseion/0.1 (https://github.com/outdatedcaveman/mouseion; oa-pdf-batch)"


def _metrics(db_path: str) -> dict:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN pdf_local IS NOT NULL AND pdf_local != '' THEN 1 ELSE 0 END) AS pdfs,
              SUM(CASE WHEN title IS NOT NULL AND title != ''
                        AND authors IS NOT NULL AND authors != '[]'
                        AND publisher IS NOT NULL AND publisher != ''
                        AND year IS NOT NULL AND year != 0
                        AND ((url IS NOT NULL AND url != '') OR (doi IS NOT NULL AND doi != ''))
                       THEN 1 ELSE 0 END) AS complete,
              AVG(completeness) AS avg_completeness
            FROM refs
            """
        ).fetchone()
        return dict(row)


def _candidate_rows(
    db_path: str,
    limit: int,
    offset: int = 0,
    scan_multiplier: int = 10,
) -> list[sqlite3.Row]:
    router = get_router()
    rows: list[sqlite3.Row] = []
    # Overscan because many rows will have a current pdf miss in the router ledger.
    query_limit = max(limit * scan_multiplier, limit + 200)
    with sqlite3.connect(db_path, timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT *
            FROM refs
            WHERE (pdf_local IS NULL OR pdf_local = '')
              AND (pdf_drive_id IS NULL OR pdf_drive_id = '')
              AND (
                (oa_url IS NOT NULL AND oa_url != '')
                OR (arxiv_id IS NOT NULL AND arxiv_id != '')
                OR (doi IS NOT NULL AND doi != '')
              )
            ORDER BY
              CASE
                WHEN (arxiv_id IS NOT NULL AND arxiv_id != '') THEN 1
                WHEN (oa_url IS NOT NULL AND oa_url != '') THEN 2
                WHEN (doi IS NOT NULL AND doi != '') THEN 3
                ELSE 4
              END,
              year DESC
            LIMIT ? OFFSET ?
            """,
            (query_limit, offset),
        )
        for row in cur.fetchall():
            ref = _row_to_ref(row)
            ref_id = row["id"]
            entry_hash = router.entry_hash(ref)
            if router.was_tried(ref_id, "pdf", entry_hash) or router.was_tried(ref_id, "pdf_oa", entry_hash):
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


def _missing_pdf_rows(db_path: str, limit: int, offset: int = 0) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path, timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT *
            FROM refs
            WHERE (pdf_local IS NULL OR pdf_local = '')
              AND (pdf_drive_id IS NULL OR pdf_drive_id = '')
            ORDER BY year DESC, title ASC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return cur.fetchall()


def reconcile_existing(limit: int, offset: int = 0) -> dict:
    cfg = get_config(reload=True)
    before = _metrics(cfg.db_path)
    rows = _missing_pdf_rows(cfg.db_path, limit, offset)
    pdf_dir = get_pdf_dir()
    checked = 0
    linked = 0
    started = time.time()
    for row in rows:
        checked += 1
        ref = _row_to_ref(row)
        ref_id = row["id"]
        filename = sanitize_filename(ref)
        dest = pdf_dir / filename
        if dest.exists() and dest.stat().st_size >= 1024:
            with RefDatabase() as db:
                db.update_integration_ids(ref_id, pdf_local=str(dest), pdf_path=filename)
            linked += 1
    after = _metrics(cfg.db_path)
    return {
        "mode": "existing_only",
        "selected": len(rows),
        "checked": checked,
        "linked": linked,
        "limit": limit,
        "offset": offset,
        "before": before,
        "after": after,
        "pdf_delta": int(after["pdfs"] or 0) - int(before["pdfs"] or 0),
        "elapsed_s": round(time.time() - started, 2),
    }


async def _unpaywall_pdf(client: httpx.AsyncClient, doi: str, email: str) -> Optional[str]:
    router = get_router()
    if not await router.acquire("unpaywall", max_wait=5.0):
        return None
    status_code = None
    ok = False
    try:
        resp = await client.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": email},
            timeout=20.0,
        )
        status_code = resp.status_code
        ok = resp.status_code == 200
        if not ok:
            return None
        data = resp.json()
        if not data.get("is_oa"):
            return None
        best = data.get("best_oa_location") or {}
        return best.get("url_for_pdf") or best.get("url")
    except Exception:
        return None
    finally:
        router.report("unpaywall", status_code, ok)


async def _download_pdf(client: httpx.AsyncClient, url: str, dest: Path) -> bool:
    router = get_router()
    if not await router.acquire("pdf_host", max_wait=5.0):
        return False
    status_code = None
    ok = False
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with client.stream("GET", url, timeout=90.0) as resp:
            status_code = resp.status_code
            if resp.status_code != 200:
                return False
            content_type = (resp.headers.get("content-type") or "").lower()
            saw_bytes = False
            first = b""
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    if not chunk:
                        continue
                    if not saw_bytes:
                        first = chunk[:16]
                        saw_bytes = True
                    fh.write(chunk)
        if not tmp.exists() or tmp.stat().st_size < 1024:
            return False
        looks_pdf = first.startswith(b"%PDF") or "pdf" in content_type or url.lower().split("?", 1)[0].endswith(".pdf")
        if not looks_pdf:
            return False
        os.replace(tmp, dest)
        ok = True
        return True
    except Exception:
        return False
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        router.report("pdf_host", status_code, ok)


async def _process_one(row: sqlite3.Row, client: httpx.AsyncClient, sem: asyncio.Semaphore, email: str) -> dict:
    ref = _row_to_ref(row)
    ref_id = row["id"]
    router = get_router()
    entry_hash = router.entry_hash(ref)
    pdf_dir = get_pdf_dir()
    filename = sanitize_filename(ref)
    dest = pdf_dir / filename

    async with sem:
        if dest.exists() and dest.stat().st_size >= 1024:
            with RefDatabase() as db:
                db.update_integration_ids(ref_id, pdf_local=str(dest), pdf_path=filename)
            router.record_attempt(ref_id, "pdf", entry_hash, "hit")
            return {"ref_id": ref_id, "result": "existing"}

        urls: list[str] = []
        if ref.arxiv_id:
            urls.append(f"https://arxiv.org/pdf/{ref.arxiv_id}")
        if ref.oa_url:
            urls.append(ref.oa_url)
        if ref.doi and email:
            upw = await _unpaywall_pdf(client, ref.doi, email)
            if upw:
                urls.append(upw)

        tried = 0
        for url in dict.fromkeys(u for u in urls if u):
            tried += 1
            if await _download_pdf(client, url, dest):
                with RefDatabase() as db:
                    db.update_integration_ids(ref_id, pdf_local=str(dest), pdf_path=filename)
                router.record_attempt(ref_id, "pdf", entry_hash, "hit")
                return {"ref_id": ref_id, "result": "downloaded", "url_count": tried}

        # This script is intentionally OA-only; do not record a miss under the
        # app's broad "pdf" key, because that would suppress later VPN/proxy or
        # broader source attempts for the same entry.
        router.record_attempt(ref_id, "pdf_oa", entry_hash, "miss")
        return {"ref_id": ref_id, "result": "miss", "url_count": tried}


async def run(limit: int, concurrency: int, offset: int = 0) -> dict:
    cfg = get_config(reload=True)
    db_path = cfg.db_path
    before = _metrics(db_path)
    rows = _candidate_rows(db_path, limit, offset=offset)
    email = cfg.openalex_email or cfg.crossref_email
    started = time.time()
    sem = asyncio.Semaphore(concurrency)
    counts = {"downloaded": 0, "existing": 0, "miss": 0, "error": 0}

    timeout = httpx.Timeout(30.0, connect=8.0)
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=timeout,
    ) as client:
        tasks = [_process_one(row, client, sem, email) for row in rows]
        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
                counts[result["result"]] = counts.get(result["result"], 0) + 1
            except Exception:
                counts["error"] += 1

    get_router().flush()
    after = _metrics(db_path)
    return {
        "selected": len(rows),
        "limit": limit,
        "offset": offset,
        "concurrency": concurrency,
        "counts": counts,
        "before": before,
        "after": after,
        "pdf_delta": int(after["pdfs"] or 0) - int(before["pdfs"] or 0),
        "elapsed_s": round(time.time() - started, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--existing-only", action="store_true")
    args = parser.parse_args()
    if args.existing_only:
        result = reconcile_existing(max(1, args.limit), max(0, args.offset))
    else:
        result = asyncio.run(
            run(
                max(1, args.limit),
                max(1, args.concurrency),
                max(0, args.offset),
            )
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
