"""
Disk-backed API response cache.

Avoids re-hitting providers for the same identifier, which is crucial
for bulk processing of hundreds of thousands of references.

Cache keys:  "{provider}:{lookup_type}:{value}"
Cache values: raw API response (bytes or dict) + timestamp

Default location: ~/.cache/mouseion/
Default TTL: 7 days (academic papers rarely change)
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional
import time

import diskcache


_DEFAULT_DIR = Path.home() / ".cache" / "mouseion"
_DEFAULT_TTL = 7 * 24 * 3600   # 7 days in seconds


class ReferenceCache:
    """
    Thin wrapper around diskcache.Cache that adds:
      - Namespaced keys
      - Optional TTL
      - Simple hit/miss statistics
    """

    def __init__(
        self,
        directory: Optional[Path | str] = None,
        ttl: int = _DEFAULT_TTL,
        size_limit: int = 2 * 1024 ** 3,   # 2 GB
    ) -> None:
        directory = Path(directory) if directory else _DEFAULT_DIR
        directory.mkdir(parents=True, exist_ok=True)
        self._cache = diskcache.Cache(str(directory), size_limit=size_limit)
        self._ttl = ttl
        self.hits = 0
        self.misses = 0

    def _make_key(self, provider: str, lookup_type: str, value: str) -> str:
        # Hash long values to keep keys short
        value_hash = hashlib.sha256(value.lower().encode()).hexdigest()[:16]
        return f"{provider}:{lookup_type}:{value_hash}"

    def _make_raw_key(self, provider: str, url: str, params: Optional[dict]) -> str:
        payload = json.dumps([url, params or {}], sort_keys=True, default=str)
        value_hash = hashlib.sha256(payload.encode()).hexdigest()
        return f"{provider}:http:{value_hash}"

    def get(self, provider: str, lookup_type: str, value: str) -> Optional[Any]:
        key = self._make_key(provider, lookup_type, value)
        result = self._cache.get(key, default=None)
        if result is None:
            self.misses += 1
        else:
            self.hits += 1
        return result

    def set(self, provider: str, lookup_type: str, value: str, data: Any) -> None:
        key = self._make_key(provider, lookup_type, value)
        self._cache.set(key, data, expire=self._ttl)

    def get_http(self, provider: str, url: str, params: Optional[dict]) -> Optional[dict]:
        """Return cached raw HTTP response metadata for a provider GET."""
        key = self._make_raw_key(provider, url, params)
        result = self._cache.get(key, default=None)
        if result is None:
            self.misses += 1
        else:
            self.hits += 1
        return result

    def set_http(
        self,
        provider: str,
        url: str,
        params: Optional[dict],
        status_code: int,
        headers: dict,
        content: bytes,
        ttl: Optional[int] = None,
    ) -> None:
        """Cache a raw HTTP response. Headers are stored without auth values."""
        key = self._make_raw_key(provider, url, params)
        safe_headers = {
            str(k): str(v)
            for k, v in (headers or {}).items()
            if str(k).lower() not in {"authorization", "x-api-key", "cookie", "set-cookie"}
        }
        self._cache.set(
            key,
            {
                "status_code": int(status_code),
                "headers": safe_headers,
                "content": bytes(content),
                "cached_at": time.time(),
            },
            expire=ttl if ttl is not None else self._ttl,
        )

    def get_cooldown(self, provider: str) -> float:
        """Return a persisted wall-clock cooldown-until timestamp, if any."""
        return float(self._cache.get(f"{provider}:cooldown_until", default=0.0) or 0.0)

    def set_cooldown(self, provider: str, until: float) -> None:
        ttl = max(1, int(until - time.time()))
        self._cache.set(f"{provider}:cooldown_until", float(until), expire=ttl)

    def clear(self) -> None:
        self._cache.clear()

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hits / total if total else 0.0,
            "size_bytes": self._cache.volume(),
        }

    def close(self) -> None:
        self._cache.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# Module-level default cache instance (lazily created)
_default_cache: Optional[ReferenceCache] = None


def get_default_cache() -> ReferenceCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = ReferenceCache()
    return _default_cache
