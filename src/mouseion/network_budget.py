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
_GLOBAL_LIMIT = _int_env("MOUSEION_NET_GLOBAL", 22)
_ROLE_LIMITS = {
    # metadata 8->18: with S2 cooled, each ref hits crossref+openalex (~2 calls),
    # each provider self-caps at _max_concurrent=8, so ~16 useful concurrent
    # metadata calls. 18 gives headroom without starving the other roles.
    "metadata": _int_env("MOUSEION_NET_METADATA", 18),
    "pdf_lookup": _int_env("MOUSEION_NET_PDF_LOOKUP", 4),
    "pdf_stream": _int_env("MOUSEION_NET_PDF_STREAM", 3),
    "gray_source": _int_env("MOUSEION_NET_GRAY_SOURCE", 1),
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
    await asyncio.to_thread(sem.acquire)


@asynccontextmanager
async def network_slot(
    role: str,
    bucket: Optional[str] = None,
    min_interval: float = 0.0,
) -> AsyncGenerator[None, None]:
    """Acquire a cross-thread/cross-event-loop HTTP budget slot."""
    role_sem = _role_sems.get(role)
    await _acquire_thread_sem(_global_sem)
    if role_sem:
        await _acquire_thread_sem(role_sem)
    try:
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
        if role_sem:
            try:
                role_sem.release()
            except ValueError:
                pass
        try:
            _global_sem.release()
        except ValueError:
            pass


def status() -> dict:
    return {
        "global_limit": _GLOBAL_LIMIT,
        "role_limits": dict(_ROLE_LIMITS),
    }
