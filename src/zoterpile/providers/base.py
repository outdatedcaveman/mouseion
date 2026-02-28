"""
BaseProvider — abstract base class for all academic database providers.

Responsibilities
----------------
* Define the interface every provider must implement
* Manage per-provider rate limiting (asyncio.Semaphore + min interval)
* Provide shared HTTP client factory with sensible defaults
* Provide a unified `lookup()` entry point that dispatches to the correct
  method based on what identifiers the reference has
* Cache raw API responses via the cache layer
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import List, Optional

import httpx

from ..models import Reference


# Shared user-agent string used by all providers
_USER_AGENT = (
    "zoterpile/0.1 (https://github.com/outdatedcaveman/zoterpile; "
    "reference enrichment tool)"
)


class BaseProvider(ABC):
    """
    Abstract base for a single bibliographic data source.

    Subclasses MUST implement:
        name            — short identifier, e.g. "crossref"
        priority        — int, lower = higher priority in merge
        lookup_by_doi() — or return None if unsupported
        search()        — title-based search fallback

    Subclasses MAY implement:
        lookup_by_pmid()
        lookup_by_arxiv_id()
        lookup_by_isbn()
    """

    # --- Subclass-defined constants ---
    name: str = "base"
    priority: int = 99          # lower is better; set in each subclass

    # Rate limiting: max concurrent requests to this provider
    _max_concurrent: int = 5
    # Minimum seconds between requests (0 = no enforced delay)
    _min_interval: float = 0.0

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._last_request_time: float = 0.0

    # -----------------------------------------------------------------------
    # HTTP client factory
    # -----------------------------------------------------------------------

    def _make_client(self, **kwargs) -> httpx.AsyncClient:
        headers = {"User-Agent": _USER_AGENT, **kwargs.pop("headers", {})}
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            http2=True,
            **kwargs,
        )

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> Optional[httpx.Response]:
        """
        Rate-limited GET with concurrency cap.
        Returns None on 404 / 410.  Raises on other errors.
        """
        async with self._semaphore:
            # Enforce minimum interval between requests
            if self._min_interval > 0:
                elapsed = time.monotonic() - self._last_request_time
                if elapsed < self._min_interval:
                    await asyncio.sleep(self._min_interval - elapsed)
            try:
                resp = await client.get(url, params=params, headers=headers or {})
                self._last_request_time = time.monotonic()
                if resp.status_code in (404, 410):
                    return None
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError:
                return None
            except httpx.RequestError:
                return None

    # -----------------------------------------------------------------------
    # Abstract interface
    # -----------------------------------------------------------------------

    @abstractmethod
    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        """Look up by DOI.  Return None if not found."""

    @abstractmethod
    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        """
        Title-based search.  Return up to ~5 candidate References,
        ordered by relevance.  Return [] if nothing found.
        """

    # Optional methods — default to None / []
    async def lookup_by_pmid(
        self, pmid: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        return None

    async def lookup_by_arxiv_id(
        self, arxiv_id: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        return None

    async def lookup_by_isbn(
        self, isbn: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        return None

    # -----------------------------------------------------------------------
    # Unified dispatch
    # -----------------------------------------------------------------------

    async def lookup(self, ref: Reference) -> List[Reference]:
        """
        Try all available identifier-based lookups for `ref`, then fall back
        to title search.  Returns a list of candidate References.

        This is the main entry point called by the lookup orchestrator.
        """
        results: List[Reference] = []

        async with self._make_client() as client:
            # 1. DOI lookup (highest fidelity)
            if ref.doi:
                r = await self.lookup_by_doi(ref.doi, client)
                if r:
                    r.sources[self.name] = 1.0
                    results.append(r)
                    return results   # DOI hit is definitive — no need to search

            # 2. PMID
            if ref.pmid and not results:
                r = await self.lookup_by_pmid(ref.pmid, client)
                if r:
                    r.sources[self.name] = 0.95
                    results.append(r)
                    return results

            # 3. arXiv ID
            if ref.arxiv_id and not results:
                r = await self.lookup_by_arxiv_id(ref.arxiv_id, client)
                if r:
                    r.sources[self.name] = 0.90
                    results.append(r)
                    return results

            # 4. ISBN
            if ref.isbn and not results:
                r = await self.lookup_by_isbn(ref.isbn, client)
                if r:
                    r.sources[self.name] = 0.90
                    results.append(r)
                    return results

            # 5. Title search fallback
            if ref.title and not results:
                author_names = [a.family for a in ref.authors if a.family]
                candidates = await self.search(
                    ref.title,
                    authors=author_names or None,
                    year=ref.year,
                    client=client,
                )
                for c in candidates:
                    c.sources[self.name] = 0.70
                results.extend(candidates)

        return results

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} priority={self.priority}>"
