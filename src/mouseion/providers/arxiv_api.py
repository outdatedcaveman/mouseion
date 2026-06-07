"""
arXiv API provider (authoritative).

Uses the arXiv API to enrich arXiv references with full metadata:
title, authors, abstract, categories, published date, and PDF URL.

This is the authoritative source for arXiv papers.  It complements the
existing ``arxiv.py`` provider but is registered separately so it can be
selected independently in the lookup engine (e.g., for arXiv-ID refs).

API docs: https://info.arxiv.org/help/api/index.html
Rate limit: ~3 req/s; we stay polite with _min_interval.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import List, Optional

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_BASE = "https://export.arxiv.org/api"
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _text(el: ET.Element, tag: str) -> Optional[str]:
    child = el.find(tag, _NS)
    if child is not None and child.text:
        return child.text.strip()
    return None


class ArXivAPIProvider(BaseProvider):
    name = "arxiv_api"
    priority = 5          # Authoritative for arXiv content
    _max_concurrent = 2
    _min_interval = 0.4   # ~2.5 req/s max

    # ------------------------------------------------------------------
    # XML → Reference
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_entry(entry: ET.Element) -> Reference:
        """Convert an Atom entry to a Reference."""
        ref = Reference()
        ref.ref_type = RefType.PREPRINT

        # arXiv ID
        id_raw = _text(entry, "atom:id") or ""
        m = re.search(r"(\d{4}\.\d{4,5}(?:v\d+)?)", id_raw)
        if m:
            ref.arxiv_id = m.group(1).split("v")[0]
        ref.url = id_raw or None

        # Title
        title = _text(entry, "atom:title") or ""
        ref.title = " ".join(title.split()).strip() or None

        # Abstract
        abstract = _text(entry, "atom:summary") or ""
        ref.abstract = " ".join(abstract.split()).strip() or None

        # Published date
        published = _text(entry, "atom:published") or ""
        if published:
            m_date = re.search(r"(\d{4})-(\d{2})", published)
            if m_date:
                ref.year = int(m_date.group(1))
                ref.month = int(m_date.group(2))

        # Authors
        for auth_el in entry.findall("atom:author", _NS):
            name = _text(auth_el, "atom:name") or ""
            if name:
                ref.authors.append(Author.from_bibtex_str(name))

        # DOI (if the paper was published in a journal)
        doi_el = entry.find("arxiv:doi", _NS)
        if doi_el is not None and doi_el.text:
            ref.doi = doi_el.text.strip()

        # Journal reference
        jref_el = entry.find("arxiv:journal_ref", _NS)
        if jref_el is not None and jref_el.text:
            ref.journal = jref_el.text.strip()
            ref.container_title = ref.journal
            ref.ref_type = RefType.JOURNAL

        # Primary category
        primary_cat = entry.find("arxiv:primary_category", _NS)
        if primary_cat is not None:
            term = primary_cat.get("term", "")
            if term:
                ref.keywords.append(term)

        # All categories
        for cat_el in entry.findall("atom:category", _NS):
            term = cat_el.get("term", "")
            if term and term not in ref.keywords:
                ref.keywords.append(term)
        ref.keywords = ref.keywords[:15]

        # Open access (arXiv is always OA)
        ref.open_access = True

        # PDF link
        for link_el in entry.findall("atom:link", _NS):
            if link_el.get("type") == "application/pdf":
                ref.oa_url = link_el.get("href")
                break
        # Fallback: construct PDF URL from arXiv ID
        if not ref.oa_url and ref.arxiv_id:
            ref.oa_url = f"https://arxiv.org/pdf/{ref.arxiv_id}"

        # Comment (often contains page count, conference info)
        comment_el = entry.find("arxiv:comment", _NS)
        if comment_el is not None and comment_el.text:
            comment = comment_el.text.strip()
            # Try to extract page count
            m_pages = re.search(r"(\d+)\s+pages", comment)
            if m_pages:
                ref.num_pages = int(m_pages.group(1))

        ref.sources["arxiv_api"] = 1.0
        ref.normalize()
        return ref

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        # arXiv API does not index by DOI
        return None

    async def lookup_by_arxiv_id(
        self, arxiv_id: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        params = {"id_list": arxiv_id, "max_results": "1"}
        resp = await self._get(client, f"{_BASE}/query", params=params)
        if resp is None:
            return None
        return self._parse_feed(resp.text, single=True)

    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        query_parts = [f'ti:"{title}"']
        if authors:
            query_parts.append(f"au:{authors[0]}")
        params = {
            "search_query": " AND ".join(query_parts),
            "max_results": "5",
            "sortBy": "relevance",
        }

        async def _do(c: httpx.AsyncClient) -> List[Reference]:
            resp = await self._get(c, f"{_BASE}/query", params=params)
            if resp is None:
                return []
            results = self._parse_feed(resp.text, single=False)
            return results if results else []

        if client is not None:
            return await _do(client)
        async with self._make_client() as c:
            return await _do(c)

    @classmethod
    def _parse_feed(cls, xml_text: str, single: bool = False):
        """Parse an Atom feed and return Reference(s)."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None if single else []

        entries = root.findall("atom:entry", _NS)
        if not entries:
            return None if single else []

        # arXiv returns a stub entry with no title when an ID is not found
        if single:
            entry = entries[0]
            title = _text(entry, "atom:title") or ""
            if not title or title.lower().startswith("error"):
                return None
            return cls._parse_entry(entry)

        results = []
        for e in entries:
            title = _text(e, "atom:title") or ""
            if title and not title.lower().startswith("error"):
                results.append(cls._parse_entry(e))
        return results
