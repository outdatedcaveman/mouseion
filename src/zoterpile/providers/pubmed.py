"""
PubMed / NCBI E-utilities provider.

Best source for biomedical and life-science literature.
NCBI rate limits: 3 req/s without API key, 10 req/s with one.
Set NCBI_API_KEY in environment for higher throughput.

E-utilities docs: https://www.ncbi.nlm.nih.gov/books/NBK25500/
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import List, Optional
from urllib.parse import quote

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_BASE_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_BASE_FETCH  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_BASE_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


class PubMedProvider(BaseProvider):
    name = "pubmed"
    priority = 4
    _max_concurrent = 3

    def __init__(self, api_key: Optional[str] = None) -> None:
        super().__init__()
        self._api_key = api_key or os.environ.get("NCBI_API_KEY", "")
        self._min_interval = 0.1 if self._api_key else 0.34  # 10/s vs 3/s

    def _base_params(self, extra: Optional[dict] = None) -> dict:
        params = dict(extra or {})
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    # -----------------------------------------------------------------------
    # XML → Reference
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_xml(xml_text: str) -> Optional[Reference]:
        """Parse PubMed XML (PubmedArticle format) into a Reference."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None

        article = root.find(".//PubmedArticle")
        if article is None:
            return None

        ref = Reference()
        ref.ref_type = RefType.JOURNAL  # PubMed is almost always journal articles

        # PMID
        pmid_el = article.find(".//PMID")
        if pmid_el is not None:
            ref.pmid = pmid_el.text

        # PMC ID
        pmc_el = article.find(".//ArticleId[@IdType='pmc']")
        if pmc_el is not None:
            ref.pmcid = pmc_el.text

        # DOI
        doi_el = article.find(".//ArticleId[@IdType='doi']")
        if doi_el is not None and doi_el.text:
            ref.doi = doi_el.text.strip()

        # Title
        title_el = article.find(".//ArticleTitle")
        if title_el is not None:
            ref.title = "".join(title_el.itertext()).strip() or None

        # Abstract
        abstract_parts = article.findall(".//AbstractText")
        if abstract_parts:
            parts = []
            for ap in abstract_parts:
                label = ap.get("Label", "")
                text  = "".join(ap.itertext()).strip()
                if label:
                    parts.append(f"{label}: {text}")
                else:
                    parts.append(text)
            ref.abstract = " ".join(parts) or None

        # Authors
        for auth_el in article.findall(".//Author"):
            last  = _text(auth_el, "LastName")
            fore  = _text(auth_el, "ForeName") or _text(auth_el, "Initials")
            orcid_el = auth_el.find("Identifier[@Source='ORCID']")
            orcid = orcid_el.text.replace("https://orcid.org/", "").strip() \
                    if orcid_el is not None and orcid_el.text else None
            if last:
                ref.authors.append(Author(family=last, given=fore or "", orcid=orcid))

        # Year / month
        pub_date = article.find(".//PubDate")
        if pub_date is not None:
            year_text = _text(pub_date, "Year")
            if year_text and year_text.isdigit():
                ref.year = int(year_text)
            month_text = (_text(pub_date, "Month") or "").lower()
            month_map = {
                "jan": 1, "feb": 2, "mar": 3, "apr": 4,
                "may": 5, "jun": 6, "jul": 7, "aug": 8,
                "sep": 9, "oct": 10, "nov": 11, "dec": 12,
            }
            ref.month = month_map.get(month_text[:3]) or None

        # Journal
        journal_el = article.find(".//Journal")
        if journal_el is not None:
            ref.journal = _text(journal_el, "Title")
            ref.journal_abbrev = _text(journal_el, "ISOAbbreviation")
            ref.container_title = ref.journal
            issue_el = journal_el.find("JournalIssue")
            if issue_el is not None:
                ref.volume = _text(issue_el, "Volume")
                ref.issue  = _text(issue_el, "Issue")

        # Pagination
        pagination = article.find(".//Pagination/MedlinePgn")
        if pagination is not None and pagination.text:
            ref.pages = pagination.text.strip()

        # ISSN (print → issn, electronic → eissn)
        print_el     = article.find(".//ISSN[@IssnType='Print']")
        electronic_el = article.find(".//ISSN[@IssnType='Electronic']")
        if print_el is not None and print_el.text:
            ref.issn = print_el.text.strip()
        if electronic_el is not None and electronic_el.text:
            ref.eissn = electronic_el.text.strip()
        # Fallback: any ISSN goes to issn
        if not ref.issn and not ref.eissn:
            fallback = article.find(".//ISSN")
            if fallback is not None and fallback.text:
                ref.issn = fallback.text.strip()

        # Keywords
        kws = [_text(kw, ".") or kw.text or "" for kw in article.findall(".//Keyword")]
        ref.keywords = [k.strip() for k in kws if k.strip()][:10]

        # Language
        lang_el = article.find(".//Language")
        if lang_el is not None:
            ref.language = lang_el.text

        ref.sources["pubmed"] = 1.0
        ref.normalize()
        return ref

    # -----------------------------------------------------------------------
    # Provider interface
    # -----------------------------------------------------------------------

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        # Use esearch to resolve DOI → PMID, then efetch
        params = self._base_params({
            "db": "pubmed",
            "term": f"{doi}[DOI]",
            "retmode": "json",
            "retmax": 1,
        })
        resp = await self._get(client, _BASE_SEARCH, params=params)
        if resp is None:
            return None
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return None
        return await self.lookup_by_pmid(ids[0], client)

    async def lookup_by_pmid(
        self, pmid: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        params = self._base_params({
            "db": "pubmed",
            "id": pmid,
            "rettype": "xml",
            "retmode": "xml",
        })
        resp = await self._get(client, _BASE_FETCH, params=params)
        if resp is None:
            return None
        return self._parse_xml(resp.text)

    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        query_parts = [f'"{title}"[Title]']
        if authors:
            query_parts.append(f'{authors[0]}[Author]')
        if year:
            query_parts.append(f"{year}[PDAT]")
        query = " AND ".join(query_parts)

        async def _do(c):
            params = self._base_params({
                "db": "pubmed",
                "term": query,
                "retmode": "json",
                "retmax": 5,
            })
            resp = await self._get(c, _BASE_SEARCH, params=params)
            if resp is None:
                return []
            ids = resp.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                return []
            # Fetch all in one call
            fetch_params = self._base_params({
                "db": "pubmed",
                "id": ",".join(ids),
                "rettype": "xml",
                "retmode": "xml",
            })
            fetch_resp = await self._get(c, _BASE_FETCH, params=fetch_params)
            if fetch_resp is None:
                return []
            # Parse multi-article XML
            refs = []
            try:
                root = ET.fromstring(fetch_resp.text)
                for article_el in root.findall("PubmedArticle"):
                    # Wrap single article in a root element and parse
                    wrapped = f"<PubmedArticleSet>{ET.tostring(article_el, encoding='unicode')}</PubmedArticleSet>"
                    r = self._parse_xml(wrapped)
                    if r:
                        refs.append(r)
            except ET.ParseError:
                pass
            return refs

        if client is not None:
            return await _do(client)
        async with self._make_client() as c:
            return await _do(c)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _text(el: ET.Element, tag: str) -> Optional[str]:
    """Find a child element and return its text, or None."""
    child = el.find(tag) if tag != "." else el
    if child is not None and child.text:
        return child.text.strip()
    return None
