"""
OpenLibrary provider.

OpenLibrary (archive.org) provides free book metadata via ISBN.
No API key required.

API docs: https://openlibrary.org/dev/docs/api
"""

from __future__ import annotations

from typing import List, Optional

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_BASE = "https://openlibrary.org"


class OpenLibraryProvider(BaseProvider):
    name = "openlibrary"
    priority = 7        # After other providers; ISBN-only
    _max_concurrent = 5
    _min_interval = 0.2

    # Only fires on ISBN; skip title search (poor quality)

    async def _resolve_author(self, key: str, client: httpx.AsyncClient) -> Optional[Author]:
        """Resolve an OpenLibrary author key (e.g. '/authors/OL23919A') to an Author."""
        try:
            data = await self._get(f"{_BASE}{key}.json", client)
            if not data:
                return None
            name = data.get("name") or data.get("personal_name") or ""
            if not name:
                return None
            parts = name.rsplit(" ", 1)
            if len(parts) == 2:
                return Author(family=parts[1], given=parts[0])
            return Author(family=name)
        except Exception:
            return None

    async def lookup_by_isbn(
        self, isbn: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        url = f"{_BASE}/isbn/{isbn}.json"
        data = await self._get(url, client)
        if not data:
            return None
        return self._parse_book(data, isbn)

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        return None  # OpenLibrary doesn't support DOI lookup

    async def search(self, title: str, authors=None, client=None) -> List[Reference]:  # type: ignore[override]
        return []  # OpenLibrary search quality is too low; skip it

    @staticmethod
    def _parse_book(data: dict, isbn: str) -> Optional[Reference]:
        ref = Reference()
        ref.ref_type = RefType.BOOK
        ref.isbn = isbn

        ref.title = data.get("title", "")
        if data.get("subtitle"):
            ref.title += ": " + data["subtitle"]

        # Publish date / year
        date_str = data.get("publish_date", "")
        import re
        m = re.search(r"\d{4}", date_str)
        if m:
            ref.year = int(m.group(0))

        # Publisher
        publishers = data.get("publishers", [])
        if publishers:
            ref.journal = publishers[0] if isinstance(publishers[0], str) else ""

        # Authors resolved in lookup() after this static method returns

        # Number of pages
        pages = data.get("number_of_pages")
        if pages:
            ref.pages = str(pages)

        # OL work URL
        works = data.get("works", [])
        if works:
            work_key = works[0].get("key", "") if isinstance(works[0], dict) else ""
            if work_key:
                ref.url = f"https://openlibrary.org{work_key}"

        if not ref.title:
            return None

        return ref

    async def _lookup_with_authors(self, isbn: str, client: httpx.AsyncClient) -> Optional[Reference]:
        """Fetch book data and resolve up to 5 author names."""
        url  = f"{_BASE}/isbn/{isbn}.json"
        data = await self._get(url, client)
        if not data:
            return None
        result = self._parse_book(data, isbn)
        if not result:
            return None
        # Resolve author names concurrently (max 5)
        import asyncio as _asyncio
        author_entries = data.get("authors", [])[:5]
        author_keys = [
            a.get("key", "") for a in author_entries if isinstance(a, dict)
        ]
        if author_keys:
            resolved = await _asyncio.gather(
                *[self._resolve_author(k, client) for k in author_keys if k],
                return_exceptions=True,
            )
            result.authors = [a for a in resolved if isinstance(a, Author) and a]
        return result

    async def lookup(self, ref: Reference) -> List[Reference]:
        """Only fire for refs with ISBN."""
        if not ref.isbn:
            return []
        async with self._make_client() as client:
            result = await self._lookup_with_authors(ref.isbn, client)
        if result:
            result.sources[self.name] = 0.85
            return [result]
        return []
