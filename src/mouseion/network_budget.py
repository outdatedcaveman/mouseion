"""Shared network budget for enrichment and PDF fetching.

Mouseion has several independent engines that can all make HTTP requests at
once. A global budget keeps them fast together by preventing PDF fallback
traffic from starving metadata providers, and vice versa.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
from urllib.parse import urlparse


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except Exception:
        return default


# Global HTTP concurrency ceiling across all roles. Raised 12->22 so the
# metadata budget below can actually run at full width (title-search enrichment
# is the throughput-critical path). Still bounded so PDF streaming etc. coexist.
_GLOBAL_LIMIT = _int_env("MOUSEION_NET_GLOBAL", 60)
_ROLE_LIMITS = {
    # metadata 18->32: ref-concurrency 18 x ~2-3 providers wants ~40-50 calls;
    # the providers self-cap (crossref 10, openalex 8, s2 2), so the real
    # binding limit is here. 32 lets the widened providers actually run in
    # parallel instead of queueing on the budget. Safe now that slots can't leak.
    "metadata": _int_env("MOUSEION_NET_METADATA", 48),
    "pdf_lookup": _int_env("MOUSEION_NET_PDF_LOOKUP", 16),
    "pdf_stream": _int_env("MOUSEION_NET_PDF_STREAM", 20),
    "gray_source": _int_env("MOUSEION_NET_GRAY_SOURCE", 2),
}

_global_sem = threading.BoundedSemaphore(_GLOBAL_LIMIT)
_role_sems = {
    role: threading.BoundedSemaphore(limit)
    for role, limit in _ROLE_LIMITS.items()
}
_rate_lock = threading.Lock()
_last_request = defaultdict(float)


def bucket_from_url(url: str, fallback: str = "unknown") -> str:
    try:
        host = (urlparse(url).netloc or fallback).lower()
        if host.startswith("www."):
            host = host[4:]
        return host or fallback
    except Exception:
        return fallback


async def _acquire_thread_sem(sem: threading.BoundedSemaphore) -> None:
    """Cancellation-safe semaphore acquire.

    CRITICAL: do NOT use `asyncio.to_thread(sem.acquire)` — a blocking acquire in
    a worker thread cannot be cancelled, so when the awaiting task is cancelled
    (e.g. a provider lookup hits its timeout via asyncio.wait_for), the thread
    still acquires the slot but the release never runs → the slot leaks. Enough
    leaks and every slot is gone → the whole daemon deadlocks at 0% CPU.

    Polling with a non-blocking acquire is cancellation-safe: the slot is only
    ever held once acquire(blocking=False) returns True, and there is no await
    between that and the caller marking it acquired, so a CancelledError can
    never strand a held slot.
    """
    while not sem.acquire(blocking=False):
        await asyncio.sleep(0.05)


@asynccontextmanager
async def network_slot(
    role: str,
    bucket: Optional[str] = None,
    min_interval: float = 0.0,
) -> AsyncGenerator[None, None]:
    """Acquire a cross-thread/cross-event-loop HTTP budget slot.

    Both acquisitions live INSIDE the try/finally and are tracked, so a
    cancellation at any point releases exactly what was actually acquired —
    no leaks, ever.
    """
    role_sem = _role_sems.get(role)
    got_global = False
    got_role = False
    try:
        if role_sem:
            await _acquire_thread_sem(role_sem)
            got_role = True
        await _acquire_thread_sem(_global_sem)
        got_global = True
        if bucket and min_interval > 0:
            with _rate_lock:
                now = time.monotonic()
                next_at = _last_request[bucket] + min_interval
                sleep_for = max(0.0, next_at - now)
                _last_request[bucket] = now + sleep_for
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
        yield
    finally:
        if got_role and role_sem:
            try:
                role_sem.release()
            except ValueError:
                pass
        if got_global:
            try:
                _global_sem.release()
            except ValueError:
                pass


def status() -> dict:
    return {
        "global_limit": _GLOBAL_LIMIT,
        "role_limits": dict(_ROLE_LIMITS),
    }
