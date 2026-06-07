"""
Google Books provider.

Uses the Google Books API for ISBN-based lookups.  Free, no API key needed.
Better coverage than OpenLibrary for many books, especially recent ones.

API docs: https://developers.google.com/books/docs/v1/using
Rate limit: generous for unauthenticated use (~1000 req/day by IP).
"""

from __future__ import annotations

import re
from typing import List, Optional

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_BASE = "https://www.googleapis.com/books/v1/volumes"


class GoogleBooksProvider(BaseProvider):
    name = "google_books"
    priority = 8          # Supplementary for book metadata
    _max_concurrent = 5
    _min_interval = 0.1

    # ------------------------------------------------------------------
    # Google Books JSON → Reference
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_volume(item: dict) -> Optional[Reference]:
        """Convert a Google Books volume item to a Reference."""
        info = item.get("volumeInfo", {})
        if not info:
            return None

        ref = Reference()
        ref.ref_type = RefType.BOOK

        # Title
        ref.title = info.get("title", "").strip() or None
        subtitle = info.get("subtitle", "").strip()
        if subtitle and ref.title and not ref.title.endswith(subtitle):
            ref.title = f"{ref.title}: {subtitle}"

        # Authors (Google Books gives flat name strings)
        for name in info.get("authors", []):
            if name:
                ref.authors.append(Author.from_bibtex_str(name))

        # Publisher
        ref.publisher = info.get("publisher") or None

        # Published date → year
        pub_date = info.get("publishedDate", "")
        if pub_date:
            m = re.search(r"(\d{4})", pub_date)
            if m:
                ref.year = int(m.group(1))
            m_month = re.search(r"\d{4}-(\d{2})", pub_date)
            if m_month:
                ref.month = int(m_month.group(1))

        # Abstract / description
        desc = info.get("description", "")
        if desc:
            ref.abstract = desc.strip()

        # ISBN identifiers
        for ident in info.get("industryIdentifiers", []):
            id_type = ident.get("type", "")
            id_val = ident.get("identifier", "")
            if id_type == "ISBN_13":
                ref.isbn = id_val
            elif id_type == "ISBN_10" and not ref.isbn:
                ref.isbn = id_val

        # Page count
        page_count = info.get("pageCount")
        if page_count:
            ref.num_pages = int(page_count)
            ref.pages = str(page_count)

        # Categories → keywords
        categories = info.get("categories", [])
        ref.keywords = categories[:10]

        # Language
        ref.language = info.get("language") or None

        # URL (canonical volume link)
        ref.url = info.get("canonicalVolumeLink") or info.get("infoLink") or None

        # Series info
        series_info = info.get("seriesInfo", {})
        if series_info:
            ref.series = series_info.get("shortSeriesBookTitle") or None

        if not ref.title:
            return None

        ref.sources["google_books"] = 0.85
        ref.normalize()
        return ref

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    async def lookup_by_isbn(
        self, isbn: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        params = {"q": f"isbn:{isbn}"}
        resp = await self._get(client, _BASE, params=params)
        if resp is None:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        items = data.get("items", [])
        if not items:
            return None
        return self._parse_volume(items[0])

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        # Google Books does not support DOI lookup
        return None

    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        # Build a book-oriented search query
        query = f'intitle:"{title}"'
        if authors:
            query += f'+inauthor:"{authors[0]}"'

        params = {"q": query, "maxResults": "5", "printType": "books"}

        async def _do(c: httpx.AsyncClient) -> List[Reference]:
            resp = await self._get(c, _BASE, params=params)
            if resp is None:
                return []
            try:
                data = resp.json()
            except Exception:
                return []
            items = data.get("items", [])
            results = []
            for item in items:
                ref = self._parse_volume(item)
                if ref:
                    results.append(ref)
            return results

        if client is not None:
            return await _do(client)
        async with self._make_client() as c:
            return await _do(c)
