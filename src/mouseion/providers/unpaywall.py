"""
Unpaywall provider.

Uses the Unpaywall API to find open access PDF URLs for references that
have a DOI.  Free, no API key needed — just requires an email address.

This fills the critical gap of finding OA URLs and PDF links.

API docs: https://unpaywall.org/products/api
Rate limit: 100,000 req/day; keep requests polite.
"""

from __future__ import annotations

import os
from typing import List, Optional

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_BASE = "https://api.unpaywall.org/v2"


class UnpaywallProvider(BaseProvider):
    name = "unpaywall"
    priority = 8          # Supplementary — mainly adds OA URLs
    _max_concurrent = 5
    _min_interval = 0.1

    def __init__(self, email: Optional[str] = None) -> None:
        super().__init__()
        self._email = email or os.environ.get(
            "UNPAYWALL_EMAIL",
            "mouseion-user@example.com",
        )

    # ------------------------------------------------------------------
    # JSON → Reference
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(data: dict) -> Reference:
        """Convert an Unpaywall response to a Reference."""
        ref = Reference()

        # DOI
        ref.doi = data.get("doi") or None

        # Title
        ref.title = data.get("title") or None

        # Year
        year = data.get("year")
        if year:
            try:
                ref.year = int(year)
            except (ValueError, TypeError):
                pass

        # Authors (Unpaywall provides z_authors list)
        for a in data.get("z_authors", []) or []:
            family = a.get("family", "")
            given = a.get("given", "")
            if family or given:
                ref.authors.append(Author(family=family, given=given))

        # Journal
        ref.journal = data.get("journal_name") or None
        ref.container_title = ref.journal

        # Publisher
        ref.publisher = data.get("publisher") or None

        # ISSN
        issn_l = data.get("journal_issn_l")
        if issn_l:
            ref.issn = issn_l
        issns = data.get("journal_issns", "")
        if issns and not ref.issn:
            ref.issn = issns.split(",")[0].strip()

        # Type
        genre = data.get("genre", "")
        genre_map = {
            "journal-article": RefType.JOURNAL,
            "book-chapter": RefType.BOOK_CHAPTER,
            "proceedings-article": RefType.CONFERENCE,
            "book": RefType.BOOK,
            "dataset": RefType.DATASET,
            "posted-content": RefType.PREPRINT,
        }
        ref.ref_type = genre_map.get(genre, RefType.UNKNOWN)

        # Open access info — the main value of this provider
        ref.open_access = data.get("is_oa", False)

        # Best OA location
        best_oa = data.get("best_oa_location") or {}
        if best_oa:
            ref.oa_url = (
                best_oa.get("url_for_pdf")
                or best_oa.get("url_for_landing_page")
                or best_oa.get("url")
            )
            ref.license = best_oa.get("license") or None

        # Landing page URL
        if not ref.url:
            ref.url = data.get("doi_url") or None

        ref.sources["unpaywall"] = 0.85
        ref.normalize()
        return ref

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        url = f"{_BASE}/{doi}"
        params = {"email": self._email}
        resp = await self._get(client, url, params=params)
        if resp is None:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("doi"):
            return None
        return self._parse_response(data)

    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        # Unpaywall only supports DOI-based lookups
        return []
