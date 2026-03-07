"""
arXiv provider.

Best for preprints.  Uses the arXiv Atom API.
Rate limit: ~3 req/s; we stay polite.

API docs: https://info.arxiv.org/help/api/index.html
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import List, Optional
from urllib.parse import quote

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_BASE = "https://export.arxiv.org/api"
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}


class ArXivProvider(BaseProvider):
    name = "arxiv"
    priority = 6
    _max_concurrent = 2
    _min_interval = 0.4   # 2.5 req/s max

    # -----------------------------------------------------------------------
    # XML → Reference
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_entry(entry: ET.Element) -> Reference:
        ref = Reference()
        ref.ref_type = RefType.PREPRINT

        # arXiv ID
        id_raw = _text(entry, "atom:id") or ""
        m = re.search(r"(\d{4}\.\d{4,5}(?:v\d+)?)", id_raw)
        if m:
            ref.arxiv_id = m.group(1).split("v")[0]  # strip version
        ref.url = id_raw or None

        # Title
        ref.title = (_text(entry, "atom:title") or "").replace("\n", " ").strip() or None

        # Abstract
        ref.abstract = (_text(entry, "atom:summary") or "").replace("\n", " ").strip() or None

        # Year / month from published
        published = _text(entry, "atom:published") or ""
        if published:
            m_year = re.search(r"(\d{4})-(\d{2})", published)
            if m_year:
                ref.year  = int(m_year.group(1))
                ref.month = int(m_year.group(2))

        # Authors
        for auth_el in entry.findall("atom:author", _NS):
            name = _text(auth_el, "atom:name") or ""
            if name:
                ref.authors.append(Author.from_bibtex_str(name))

        # DOI (arXiv sometimes has journal_ref)
        doi_el = entry.find("arxiv:doi", _NS)
        if doi_el is not None and doi_el.text:
            ref.doi = doi_el.text.strip()

        # Journal ref (if published)
        jref_el = entry.find("arxiv:journal_ref", _NS)
        if jref_el is not None and jref_el.text:
            ref.journal = jref_el.text.strip()
            ref.container_title = ref.journal
            # If we have a journal ref, it's been published, not just a preprint
            ref.ref_type = RefType.JOURNAL

        # Category → keywords
        for cat_el in entry.findall("atom:category", _NS):
            term = cat_el.get("term", "")
            if term:
                ref.keywords.append(term)
        ref.keywords = ref.keywords[:10]

        # Open access (arXiv is always OA)
        ref.open_access = True
        # PDF link
        for link_el in entry.findall("atom:link", _NS):
            if link_el.get("type") == "application/pdf":
                ref.oa_url = link_el.get("href")
                break

        ref.sources["arxiv"] = 1.0
        ref.normalize()
        return ref

    # -----------------------------------------------------------------------
    # Provider interface
    # -----------------------------------------------------------------------

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        # arXiv doesn't index by DOI; skip
        return None

    async def lookup_by_arxiv_id(
        self, arxiv_id: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        params = {
            "id_list": arxiv_id,
            "max_results": 1,
        }
        resp = await self._get(client, f"{_BASE}/query", params=params)
        if resp is None:
            return None
        return self._parse_atom(resp.text, single=True)

    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        query_parts = [f'ti:"{title}"']
        if authors:
            query_parts.append(f'au:{authors[0]}')
        params = {
            "search_query": " AND ".join(query_parts),
            "max_results": 5,
            "sortBy": "relevance",
        }

        async def _do(c):
            resp = await self._get(c, f"{_BASE}/query", params=params)
            if resp is None:
                return []
            return self._parse_atom(resp.text, single=False) or []

        if client is not None:
            return await _do(client)
        async with self._make_client() as c:
            return await _do(c)

    @staticmethod
    def _parse_atom(xml_text: str, single: bool = False):
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None if single else []

        entries = root.findall("atom:entry", _NS)
        if not entries:
            return None if single else []

        if single:
            return ArXivProvider._parse_entry(entries[0])
        return [ArXivProvider._parse_entry(e) for e in entries]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _text(el: ET.Element, tag: str) -> Optional[str]:
    child = el.find(tag, _NS)
    if child is not None and child.text:
        return child.text.strip()
    return None
