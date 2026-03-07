"""
OpenAlex provider.

OpenAlex is a fully open index of scholarly works (200M+ records).
No API key required; set OPENALEX_EMAIL for the polite pool (faster, higher limits).

API docs: https://docs.openalex.org/
"""

from __future__ import annotations

import os
import re
from typing import List, Optional
from urllib.parse import quote

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_BASE = "https://api.openalex.org"


class OpenAlexProvider(BaseProvider):
    name = "openalex"
    priority = 2
    _max_concurrent = 10
    _min_interval = 0.0

    def __init__(self, email: Optional[str] = None) -> None:
        super().__init__()
        self._email = email or os.environ.get("OPENALEX_EMAIL", "")

    def _mailto_params(self, extra: Optional[dict] = None) -> dict:
        params = dict(extra or {})
        if self._email:
            params["mailto"] = self._email
        return params

    # -----------------------------------------------------------------------
    # JSON → Reference
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_work(data: dict) -> Reference:
        ref = Reference()

        # --- Title ---
        ref.title = (data.get("title") or "").strip() or None

        # --- DOI ---
        doi_raw = data.get("doi") or ""
        if doi_raw:
            ref.doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_raw).strip()

        # --- IDs ---
        ids = data.get("ids") or {}
        ref.pmid    = str(ids["pmid"]).replace("https://pubmed.ncbi.nlm.nih.gov/", "").strip("/").strip() \
                        if ids.get("pmid") else None
        ref.pmcid   = str(ids.get("pmcid", "")).replace("https://www.ncbi.nlm.nih.gov/pmc/articles/", "").strip() or None
        arxiv_raw   = ids.get("openalex", "")  # not directly, check source

        # arXiv IDs sometimes in locations
        for loc in data.get("locations", []):
            src = loc.get("source") or {}
            if src.get("host_organization_lineage_names") and "arXiv" in str(src):
                # Try to extract from landing_page_url
                lp = loc.get("landing_page_url", "")
                m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", lp)
                if m:
                    ref.arxiv_id = m.group(1)

        # --- Year ---
        ref.year = data.get("publication_year") or None

        # --- Type ---
        type_map = {
            "journal-article":   RefType.JOURNAL,
            "book":              RefType.BOOK,
            "book-chapter":      RefType.BOOK_CHAPTER,
            "proceedings-article": RefType.CONFERENCE,
            "preprint":          RefType.PREPRINT,
            "dissertation":      RefType.THESIS,
            "dataset":           RefType.DATASET,
            "report":            RefType.REPORT,
            "reference-entry":   RefType.OTHER,
        }
        ref.ref_type = type_map.get(data.get("type", ""), RefType.UNKNOWN)

        # --- Authors ---
        for authorship in data.get("authorships", []):
            ref.authors.append(Author.from_openalex(authorship))

        # --- Abstract ---
        # OpenAlex stores abstract as inverted index; reconstruct it
        inv = data.get("abstract_inverted_index")
        if inv:
            ref.abstract = _reconstruct_abstract(inv)

        # --- Publication venue ---
        primary_loc = data.get("primary_location") or {}
        src = primary_loc.get("source") or {}
        ref.journal = src.get("display_name") or None
        if ref.journal:
            ref.container_title = ref.journal
        issns = src.get("issn") or []
        ref.issn  = src.get("issn_l") or (issns[0] if issns else None) or None
        ref.eissn = issns[1] if len(issns) > 1 else None

        # --- Bibliographic details ---
        bib = data.get("biblio") or {}
        ref.volume = bib.get("volume") or None
        ref.issue  = bib.get("issue") or None
        first = bib.get("first_page") or ""
        last  = bib.get("last_page") or ""
        if first and last:
            ref.pages = f"{first}-{last}"
        elif first:
            ref.pages = first

        # --- Publisher (from host_organization) ---
        ref.publisher = src.get("host_organization_name") or None

        # --- Open access ---
        oa = data.get("open_access") or {}
        ref.open_access = oa.get("is_oa") or False
        ref.oa_url = oa.get("oa_url") or None

        # --- Citation count ---
        ref.citation_count = data.get("cited_by_count") or None

        # --- Keywords (concepts + topics) ---
        kw_raw = (
            [c.get("display_name") for c in (data.get("topics") or [])[:5]]
            + [c.get("display_name") for c in (data.get("keywords") or [])[:5]]
        )
        ref.keywords = [k for k in kw_raw if k][:10]

        # --- Language ---
        ref.language = data.get("language") or None

        ref.sources["openalex"] = 1.0
        ref.normalize()
        return ref

    # -----------------------------------------------------------------------
    # Provider interface
    # -----------------------------------------------------------------------

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        # OpenAlex accepts DOI as "https://doi.org/{doi}"
        encoded = quote(f"https://doi.org/{doi}", safe="/:")
        url = f"{_BASE}/works/{encoded}"
        resp = await self._get(client, url, params=self._mailto_params())
        if resp is None:
            return None
        return self._parse_work(resp.json())

    async def lookup_by_pmid(
        self, pmid: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        url = f"{_BASE}/works"
        params = self._mailto_params({
            "filter": f"ids.pmid:{pmid}",
            "per-page": 1,
        })
        resp = await self._get(client, url, params=params)
        if resp is None:
            return None
        results = resp.json().get("results", [])
        return self._parse_work(results[0]) if results else None

    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        filters = []
        if year:
            filters.append(f"publication_year:{year}")
        params = self._mailto_params({
            "search": title,
            "per-page": 5,
        })
        if filters:
            params["filter"] = ",".join(filters)

        async def _do(c):
            resp = await self._get(c, f"{_BASE}/works", params=params)
            if resp is None:
                return []
            items = resp.json().get("results", [])
            return [self._parse_work(item) for item in items]

        if client is not None:
            return await _do(client)
        async with self._make_client() as c:
            return await _do(c)


# ---------------------------------------------------------------------------
# Helper: reconstruct abstract from OpenAlex inverted index
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inv: dict) -> str:
    """
    OpenAlex stores abstracts as {word: [position, ...], ...}.
    Reconstruct the original string.
    """
    pairs = []
    for word, positions in inv.items():
        for pos in positions:
            pairs.append((pos, word))
    pairs.sort(key=lambda x: x[0])
    return " ".join(word for _, word in pairs)
