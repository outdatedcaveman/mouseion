"""
DBLP provider.

DBLP indexes computer science literature (conferences, journals, books).
No authentication required.  Open JSON API.

API docs: https://dblp.org/faq/How+to+use+the+dblp+search+API.html
"""

from __future__ import annotations

import re
from typing import List, Optional

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_SEARCH_URL = "https://dblp.org/search/publ/api"


class DBLPProvider(BaseProvider):
    name = "dblp"
    priority = 5
    _max_concurrent = 3
    _min_interval = 0.2   # be polite

    # -----------------------------------------------------------------------
    # JSON → Reference
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_hit(hit: dict) -> Optional[Reference]:
        info = hit.get("info", {})
        if not info:
            return None

        ref = Reference()

        ref.title = (info.get("title") or "").rstrip(".").strip() or None

        # Authors
        authors_raw = info.get("authors", {}).get("author", [])
        if isinstance(authors_raw, dict):
            authors_raw = [authors_raw]
        for a in authors_raw:
            name = a if isinstance(a, str) else (a.get("text") or "")
            if name:
                ref.authors.append(Author.from_bibtex_str(name))

        # Year
        year_raw = info.get("year", "")
        if year_raw and str(year_raw).isdigit():
            ref.year = int(year_raw)

        # Type
        venue_type = info.get("type", "").lower()
        if "journal" in venue_type:
            ref.ref_type = RefType.JOURNAL
        elif "conference" in venue_type or "proceedings" in venue_type:
            ref.ref_type = RefType.CONFERENCE
        elif "book" in venue_type:
            ref.ref_type = RefType.BOOK
        else:
            ref.ref_type = RefType.UNKNOWN

        # Venue (journal / conference)
        ref.journal = info.get("venue") or None
        if ref.journal:
            ref.container_title = ref.journal

        # Volume / pages
        ref.volume = str(info["volume"]) if info.get("volume") else None
        ref.pages  = info.get("pages") or None

        # DOI
        doi_raw = info.get("doi", "")
        if doi_raw:
            ref.doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_raw).strip()

        # URL (DBLP page)
        ref.url = info.get("url") or None

        # Access
        access = info.get("access", "")
        if access == "open":
            ref.open_access = True

        ref.sources["dblp"] = 1.0
        ref.normalize()
        return ref

    # -----------------------------------------------------------------------
    # Provider interface
    # -----------------------------------------------------------------------

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        # DBLP doesn't have a direct DOI endpoint — search for it
        results = await self.search(doi, client=client)
        for r in results:
            if r.doi and r.doi.lower() == doi.lower():
                return r
        return None

    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        query = title
        if authors:
            query += " " + " ".join(authors[:1])

        params = {
            "q": query,
            "format": "json",
            "h": 5,
            "f": 0,
        }

        async def _do(c):
            resp = await self._get(c, _SEARCH_URL, params=params)
            if resp is None:
                return []
            hits = (
                resp.json()
                .get("result", {})
                .get("hits", {})
                .get("hit", [])
            )
            refs = []
            for hit in hits:
                r = self._parse_hit(hit)
                if r:
                    refs.append(r)
            return refs

        if client is not None:
            return await _do(client)
        async with self._make_client() as c:
            return await _do(c)
