"""
Semantic Scholar provider.

Very strong coverage for CS/AI/ML papers; good citation graph data.
Free API (no key needed for basic use); rate-limited to 1 req/s without a key.
Set SEMANTIC_SCHOLAR_API_KEY in the environment for higher limits (100 req/s).

API docs: https://api.semanticscholar.org/api-docs/
"""

from __future__ import annotations

import os
import re
from typing import List, Optional
from urllib.parse import quote

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_BASE = "https://api.semanticscholar.org/graph/v1"

# Fields to request on every call (maximise data, respect API constraints)
_PAPER_FIELDS = (
    "paperId,externalIds,title,abstract,tldr,authors,year,venue,journal,"
    "publicationVenue,publicationDate,publicationTypes,openAccessPdf,"
    "citationCount,fieldsOfStudy,s2FieldsOfStudy"
)


class SemanticScholarProvider(BaseProvider):
    name = "semantic_scholar"
    priority = 3
    # Without an API key S2 allows ~1 req/s. Serialise with max_concurrent=1
    # so the semaphore itself gates requests — avoids lock contention and
    # prevents burst on startup while still being polite to the free tier.
    # With an API key up to 100 req/s is allowed; relax limits accordingly.
    _max_concurrent = 1

    def __init__(self, api_key: Optional[str] = None) -> None:
        if not api_key:
            try:
                from ..config import get_config
                cfg = get_config()
                api_key = api_key or cfg.semantic_scholar_api_key
            except Exception:
                pass
        self._api_key = api_key or os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
        # Free tier is 1 req/s. Standard API keys get 10 req/s. Premium keys get 100 req/s.
        if self._api_key:
            is_premium = os.environ.get("MOUSEION_S2_PREMIUM", "").lower() in ("true", "1", "yes")
            if is_premium:
                self._max_concurrent = 20
                self._min_interval = 0.02
            else:
                self._max_concurrent = 2
                self._min_interval = 1.05
        else:
            self._max_concurrent = 1
            self._min_interval = 1.05  # ~1 req/s
        super().__init__()

    def _make_client(self, **kwargs) -> httpx.AsyncClient:
        headers: dict = {}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return super()._make_client(headers=headers, **kwargs)

    # -----------------------------------------------------------------------
    # JSON → Reference
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_paper(data: dict) -> Reference:
        ref = Reference()

        # --- External IDs ---
        ext = data.get("externalIds") or {}
        doi_raw = ext.get("DOI") or ""
        if doi_raw:
            ref.doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_raw).strip()
        ref.arxiv_id = ext.get("ArXiv") or None
        ref.pmid     = str(ext.get("PubMed")) if ext.get("PubMed") else None
        ref.pmcid    = str(ext.get("PubMedCentral")) if ext.get("PubMedCentral") else None

        # --- Core ---
        ref.title    = (data.get("title") or "").strip() or None
        ref.abstract = (data.get("abstract") or "").strip() or None
        # Use TLDR as abstract fallback when full abstract is missing
        if not ref.abstract:
            tldr = data.get("tldr") or {}
            tldr_text = (tldr.get("text") or "" if isinstance(tldr, dict) else str(tldr or "")).strip()
            if tldr_text:
                ref.abstract = f"[TLDR] {tldr_text}"

        # --- Year / date ---
        ref.year = data.get("year") or None
        date_raw = data.get("publicationDate") or ""
        if date_raw and not ref.year:
            m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", date_raw)
            if m:
                ref.year = int(m.group(1))

        # --- Authors (S2 uses full name only, no family/given split) ---
        for a in (data.get("authors") or []):
            name = (a.get("name") or "").strip()
            if name:
                ref.authors.append(Author.from_bibtex_str(name))

        # --- Publication venue / journal ---
        venue = data.get("venue") or ""
        journal = (data.get("journal") or {})
        pub_venue = data.get("publicationVenue") or {}

        ref.journal = (
            pub_venue.get("name")
            or journal.get("name")
            or venue
            or None
        )
        alt_names = pub_venue.get("alternate_names")
        ref.journal_abbrev = (alt_names[0] if isinstance(alt_names, list) and alt_names else None) or None
        if ref.journal:
            ref.container_title = ref.journal

        # Volume / pages from journal sub-object
        ref.volume = journal.get("volume") or None
        ref.pages  = journal.get("pages") or None

        # --- Type ---
        pub_types = data.get("publicationTypes") or []
        type_map = {
            "JournalArticle":  RefType.JOURNAL,
            "Conference":      RefType.CONFERENCE,
            "Book":            RefType.BOOK,
            "BookSection":     RefType.BOOK_CHAPTER,
            "Preprint":        RefType.PREPRINT,
            "Thesis":          RefType.THESIS,
            "Review":          RefType.JOURNAL,
        }
        for pt in pub_types:
            if pt in type_map:
                ref.ref_type = type_map[pt]
                break
        else:
            ref.ref_type = RefType.UNKNOWN

        # --- Open access ---
        oa_pdf = data.get("openAccessPdf") or {}
        if oa_pdf.get("url"):
            ref.open_access = True
            ref.oa_url = oa_pdf["url"]

        # --- Citation count ---
        ref.citation_count = data.get("citationCount") or None

        # --- Keywords (from fields of study) ---
        fos = [
            f.get("category") or f.get("field")
            for f in (data.get("s2FieldsOfStudy") or data.get("fieldsOfStudy") or [])
            if f
        ]
        ref.keywords = [f for f in fos if f][:10]

        ref.sources["semantic_scholar"] = 1.0
        ref.normalize()
        return ref

    # -----------------------------------------------------------------------
    # Provider interface
    # -----------------------------------------------------------------------

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        url = f"{_BASE}/paper/DOI:{quote(doi, safe='/')}"
        resp = await self._get(client, url, params={"fields": _PAPER_FIELDS})
        if resp is None:
            return None
        return self._parse_paper(resp.json())

    async def lookup_by_arxiv_id(
        self, arxiv_id: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        url = f"{_BASE}/paper/ARXIV:{quote(arxiv_id, safe='/')}"
        resp = await self._get(client, url, params={"fields": _PAPER_FIELDS})
        if resp is None:
            return None
        return self._parse_paper(resp.json())

    async def lookup_by_pmid(
        self, pmid: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        url = f"{_BASE}/paper/PMID:{pmid}"
        resp = await self._get(client, url, params={"fields": _PAPER_FIELDS})
        if resp is None:
            return None
        return self._parse_paper(resp.json())

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
            "query": query,
            "fields": _PAPER_FIELDS,
            "limit": 5,
        }

        async def _do(c):
            resp = await self._get(
                c, f"{_BASE}/paper/search", params=params
            )
            if resp is None:
                return []
            items = resp.json().get("data", [])
            return [self._parse_paper(item) for item in items]

        if client is not None:
            return await _do(client)
        async with self._make_client() as c:
            return await _do(c)
