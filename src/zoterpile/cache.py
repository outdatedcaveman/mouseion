"""
Disk-backed API response cache.

Avoids re-hitting providers for the same identifier, which is crucial
for bulk processing of hundreds of thousands of references.

Cache keys:  "{provider}:{lookup_type}:{value}"
Cache values: raw API response (bytes or dict) + timestamp

Default location: ~/.cache/zoterpile/
Default TTL: 7 days (academic papers rarely change)
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

import diskcache


_DEFAULT_DIR = Path.home() / ".cache" / "zoterpile"
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
