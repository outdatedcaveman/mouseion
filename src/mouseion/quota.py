"""
Provider quota manager — sliding-window rate limiting for bulk operations.

Tracks per-provider request counts over configurable windows (per minute,
per hour, per day) and enforces back-pressure before external API limits are
hit.  Works on top of the per-request retry/backoff in BaseProvider._get()
and operates at a longer time scale (budget management vs. individual errors).

The quota state is persisted to ``~/.cache/mouseion/quota.json`` so limits
are respected across multiple process invocations within the same day.

Usage
-----
    from mouseion.quota import get_default_quota_manager

    qm = get_default_quota_manager()
    async with qm.acquire("crossref"):
        response = await http_client.get(url)

Integration in providers
------------------------
BaseProvider._get() calls ``qm.acquire(self.name)`` before every outgoing
request, so quota enforcement is automatic for all providers.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional


_DEFAULT_QUOTA_FILE = Path.home() / ".cache" / "mouseion" / "quota.json"


@dataclass
class ProviderLimits:
    """Rate limits for a single provider.  0 means unlimited for that window."""
    requests_per_minute: int = 0
    requests_per_hour: int = 0
    requests_per_day: int = 0
    min_interval_seconds: float = 0.0   # minimum gap between consecutive requests


# Conservative defaults aligned with each provider's public documentation.
# A provider with an API key can override these via QuotaManager.update_limits().
DEFAULT_PROVIDER_LIMITS: Dict[str, ProviderLimits] = {
    # Default anonymous limits for providers. If credentials/emails are supplied,
    # providers will dynamically update these via get_default_quota_manager().update_limits().
    "crossref":         ProviderLimits(requests_per_minute=600,  requests_per_hour=20_000,  requests_per_day=40_000,  min_interval_seconds=0.0),
    "openalex":         ProviderLimits(requests_per_minute=480,  requests_per_hour=15_000,  requests_per_day=50_000,  min_interval_seconds=0.0),
    "semantic_scholar": ProviderLimits(requests_per_minute=20,   requests_per_hour=1_000,   requests_per_day=10_000,  min_interval_seconds=0.0),
    "pubmed":           ProviderLimits(requests_per_minute=180,  requests_per_hour=5_000,   requests_per_day=10_000),
    "dblp":             ProviderLimits(requests_per_minute=120,  requests_per_hour=2_000,   requests_per_day=10_000),
    "arxiv":            ProviderLimits(requests_per_minute=20,   requests_per_hour=1_000,   requests_per_day=3_000,   min_interval_seconds=0.0),
}


class QuotaExceeded(Exception):
    """Raised when a provider's daily quota has been exhausted."""
    def __init__(self, provider: str, window: str, limit: int, retry_after: float = 0.0) -> None:
        self.provider = provider
        self.window   = window
        self.limit    = limit
        # Seconds until the window frees up enough to allow another request.
        self.retry_after = retry_after
        super().__init__(f"{provider}: {limit} req/{window} quota exhausted")


class _ProviderState:
    """Sliding-window request counters + per-provider asyncio lock."""

    def __init__(self, limits: ProviderLimits) -> None:
        self.limits = limits
        self._lock  = asyncio.Lock()
        # Each deque stores wall-clock timestamps (time.time()) of recent requests.
        self._minute: deque = deque()
        self._hour:   deque = deque()
        self._day:    deque = deque()
        self._last_request: float = 0.0

    def _prune(self, now: float) -> None:
        while self._minute and self._minute[0] < now - 60:
            self._minute.popleft()
        while self._hour and self._hour[0] < now - 3600:
            self._hour.popleft()
        while self._day and self._day[0] < now - 86400:
            self._day.popleft()

    def wait_seconds(self, now: float, provider: str = "") -> float:
        """Return how many seconds to sleep before the next request is allowed."""
        self._prune(now)
        L = self.limits
        waits: List[float] = []

        if L.requests_per_minute and len(self._minute) >= L.requests_per_minute:
            waits.append(self._minute[0] + 60 - now)
        if L.requests_per_hour and len(self._hour) >= L.requests_per_hour:
            waits.append(self._hour[0] + 3600 - now)
        if L.requests_per_day and len(self._day) >= L.requests_per_day:
            # Daily quota exhausted — this is the QuotaExceeded signal
            remaining = self._day[0] + 86400 - now
            if remaining > 600:   # more than 10 min: raise rather than sleep
                raise QuotaExceeded(provider, "day", L.requests_per_day, retry_after=remaining)
            waits.append(remaining)
        if L.min_interval_seconds:
            gap = self._last_request + L.min_interval_seconds - now
            if gap > 0:
                waits.append(gap)

        return max(waits, default=0.0)

    def record(self, now: float) -> None:
        self._minute.append(now)
        self._hour.append(now)
        self._day.append(now)
        self._last_request = now

    def current_counts(self, now: float) -> Dict[str, int]:
        self._prune(now)
        return {
            "last_minute": len(self._minute),
            "last_hour":   len(self._hour),
            "last_day":    len(self._day),
        }

    def load_timestamps(self, timestamps: List[float]) -> None:
        """Restore persisted timestamps (called on startup)."""
        now = time.time()
        for t in timestamps:
            if t > now - 86400:
                self._day.append(t)
            if t > now - 3600:
                self._hour.append(t)
            if t > now - 60:
                self._minute.append(t)
        if self._day:
            self._last_request = max(self._day)


class QuotaManager:
    """
    Async-safe quota manager for all providers.

    Wraps every outgoing HTTP request with a sliding-window budget check.
    Sleeps automatically when approaching limits; raises QuotaExceeded only
    when the daily budget is fully exhausted and the reset is far away.

    Usage
    -----
        qm = QuotaManager()
        async with qm.acquire("crossref"):
            await client.get(url)
    """

    def __init__(
        self,
        limits: Optional[Dict[str, ProviderLimits]] = None,
        state_file: Optional[Path] = None,
    ) -> None:
        self._limits = limits or dict(DEFAULT_PROVIDER_LIMITS)
        self._state_file = state_file or _DEFAULT_QUOTA_FILE
        self._states: Dict[str, _ProviderState] = {}
        self._load_state()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(self, provider: str) -> AsyncGenerator[None, None]:
        """
        Async context manager: block until the provider's quota permits a
        request, record the request, then yield.

        Wraps every HTTP request in BaseProvider._get().
        """
        state = self._get_state(provider)
        async with state._lock:
            while True:
                now = time.time()
                try:
                    wait = state.wait_seconds(now, provider=provider)
                except QuotaExceeded as exc:
                    # Daily budget is spent. Set a provider cooldown ONCE so every
                    # other caller (BaseProvider._get, batch_lookup) short-circuits
                    # on the cooldown check *before* reaching acquire() — instead of
                    # each of thousands of refs re-raising this and spamming the log.
                    # Capped at 6h so we re-probe periodically as the window slides.
                    try:
                        from .cache import get_default_cache
                        cooldown_for = min(max(exc.retry_after, 300.0), 6 * 3600.0)
                        get_default_cache().set_cooldown(provider, time.time() + cooldown_for)
                    except Exception:
                        pass
                    raise
                if wait <= 0:
                    break
                # Sleep in small chunks so we stay responsive to cancellation.
                await asyncio.sleep(min(wait, 2.0))
            state.record(time.time())
        yield

    def get_status(self) -> Dict[str, Any]:
        """Return current usage counts for all active providers."""
        now = time.time()
        return {
            provider: {
                **state.current_counts(now),
                "limits": {
                    "per_minute": state.limits.requests_per_minute,
                    "per_hour":   state.limits.requests_per_hour,
                    "per_day":    state.limits.requests_per_day,
                },
            }
            for provider, state in self._states.items()
        }

    def update_limits(self, provider: str, limits: ProviderLimits) -> None:
        """
        Override limits for a provider — call this after obtaining an API key
        that unlocks higher rate limits.
        """
        self._limits[provider] = limits
        if provider in self._states:
            self._states[provider].limits = limits

    def save_state(self) -> None:
        """Persist the last 24 h of request timestamps to disk."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            cutoff = now - 86400
            data = {
                provider: [t for t in state._day if t > cutoff]
                for provider, state in self._states.items()
                if state._day
            }
            self._state_file.write_text(json.dumps(data))
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _get_state(self, provider: str) -> _ProviderState:
        if provider not in self._states:
            self._states[provider] = _ProviderState(
                self._limits.get(provider, ProviderLimits())
            )
        return self._states[provider]

    def _load_state(self) -> None:
        """Restore persisted request timestamps so limits survive restarts."""
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                for provider, timestamps in data.items():
                    if timestamps:
                        self._get_state(provider).load_timestamps(timestamps)
        except Exception:
            pass   # corrupt or missing file — start fresh


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_quota_manager: Optional[QuotaManager] = None


def get_default_quota_manager() -> QuotaManager:
    global _default_quota_manager
    if _default_quota_manager is None:
        _default_quota_manager = QuotaManager()
    return _default_quota_manager
