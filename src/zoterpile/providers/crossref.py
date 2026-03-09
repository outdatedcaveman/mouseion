"""
CrossRef provider.

CrossRef is the DOI registration agency for scholarly literature.
It is the gold standard for DOI-based lookups and has broad coverage
for journal articles, conference papers, books, and book chapters.

API docs: https://api.crossref.org/swagger-ui/index.html
Rate limits: ~50 req/s with polite pool (mailto in User-Agent)
"""

from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import quote

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_BASE = "https://api.crossref.org"


class CrossRefProvider(BaseProvider):
    name = "crossref"
    priority = 1        # Highest priority
    _max_concurrent = 10
    _min_interval = 0.0  # Polite pool is generous

    # CrossRef works best when you identify yourself (polite pool)
    # Set via CROSSREF_EMAIL env var; gracefully degraded if absent
    def __init__(self, email: Optional[str] = None) -> None:
        super().__init__()
        import os
        self._email = email or os.environ.get("CROSSREF_EMAIL", "")

    def _make_client(self, **kwargs) -> httpx.AsyncClient:
        ua = (
            "zoterpile/0.1 (https://github.com/outdatedcaveman/zoterpile"
            + (f"; mailto:{self._email}" if self._email else "")
            + ")"
        )
        return super()._make_client(headers={"User-Agent": ua}, **kwargs)

    # -----------------------------------------------------------------------
    # CrossRef JSON → Reference
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_work(data: dict) -> Reference:
        """Convert a CrossRef 'work' object to a Reference."""
        ref = Reference()

        # Type
        ref.ref_type = RefType.from_crossref(data.get("type", ""))

        # DOI
        doi_raw = data.get("DOI", "")
        ref.doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_raw).strip() or None

        # Title (CrossRef returns a list)
        titles = data.get("title", [])
        ref.title = titles[0].strip() if titles else None

        # Subtitle
        subtitles = data.get("subtitle", [])
        if subtitles and ref.title:
            sub = subtitles[0].strip()
            if sub and not ref.title.endswith(sub):
                ref.title = f"{ref.title}: {sub}"

        # Authors
        ref.authors = [
            Author.from_crossref(a)
            for a in data.get("author", [])
        ]

        # Editors
        ref.editors = [
            Author.from_crossref(e)
            for e in data.get("editor", [])
        ]

        # Year — CrossRef uses 'published' > 'published-print' > 'published-online'
        for date_key in ("published", "published-print", "published-online", "issued"):
            dp = data.get(date_key, {}).get("date-parts", [[]])
            if dp and dp[0]:
                parts = dp[0]
                if parts[0]:
                    ref.year = int(parts[0])
                    if len(parts) > 1 and parts[1]:
                        ref.month = int(parts[1])
                    break

        # Abstract
        abstract_raw = data.get("abstract", "")
        if abstract_raw:
            # Strip JATS XML tags that CrossRef sometimes returns
            ref.abstract = re.sub(r"<[^>]+>", "", abstract_raw).strip() or None

        # Container (journal / book title)
        containers = data.get("container-title", [])
        if containers:
            ref.journal = containers[0].strip()
            ref.container_title = ref.journal
        short_containers = data.get("short-container-title", [])
        if short_containers:
            ref.journal_abbrev = short_containers[0].strip()

        # Event (conference name)
        event = data.get("event", {})
        if isinstance(event, dict) and event.get("name"):
            ref.event_name = event["name"]

        # Volume / issue / pages
        ref.volume = data.get("volume") or None
        ref.issue  = data.get("issue")  or None
        ref.pages  = data.get("page")   or None
        ref.article_number = data.get("article-number") or None

        # Publisher
        ref.publisher = data.get("publisher") or None

        # ISSN (prefer electronic)
        issns = data.get("ISSN", [])
        issn_types = data.get("issn-type", [])
        eissn = next(
            (t["value"] for t in issn_types if t.get("type") == "electronic"), None
        )
        pissn = next(
            (t["value"] for t in issn_types if t.get("type") == "print"), None
        )
        ref.issn  = pissn  or (issns[0] if issns else None)
        ref.eissn = eissn  or (issns[1] if len(issns) > 1 else None)

        # ISBN
        isbns = data.get("ISBN", [])
        ref.isbn = isbns[0] if isbns else None

        # URL
        ref.url = data.get("URL") or None

        # License
        licenses = data.get("license", [])
        if licenses:
            ref.license = licenses[0].get("URL") or None

        # Open access (CrossRef doesn't directly flag OA, but Unpaywall does;
        # we infer from license URLs for now)
        if ref.license and any(
            oa in ref.license for oa in ("creativecommons", "open-access")
        ):
            ref.open_access = True

        # Citation count
        ref.citation_count = data.get("is-referenced-by-count") or None

        # Subject / keywords (CrossRef uses 'subject')
        ref.keywords = data.get("subject", [])[:10]  # cap at 10

        # Language
        ref.language = data.get("language") or None

        # Book-specific: series
        series_list = data.get("container-title", [])
        if ref.ref_type == RefType.BOOK_CHAPTER and len(series_list) > 1:
            ref.series = series_list[-1]

        ref.sources["crossref"] = 1.0
        ref.normalize()
        return ref

    # -----------------------------------------------------------------------
    # Provider interface
    # -----------------------------------------------------------------------

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        url = f"{_BASE}/works/{quote(doi, safe='/')}"
        resp = await self._get(client, url)
        if resp is None:
            return None
        data = resp.json().get("message", {})
        if not data:
            return None
        return self._parse_work(data)

    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        params: dict = {
            "query.bibliographic": title,
            "rows": 5,
            "select": (
                "DOI,type,title,subtitle,author,editor,published,published-print,"
                "published-online,issued,abstract,container-title,"
                "short-container-title,volume,issue,page,article-number,"
                "publisher,ISSN,issn-type,ISBN,URL,license,subject,language,"
                "is-referenced-by-count,event"
            ),
        }
        if authors:
            params["query.author"] = " ".join(authors[:2])
        if year:
            params["filter"] = f"from-pub-date:{year-1},until-pub-date:{year+1}"

        async def _do(c):
            resp = await self._get(c, f"{_BASE}/works", params=params)
            if resp is None:
                return []
            items = resp.json().get("message", {}).get("items", [])
            return [self._parse_work(item) for item in items]

        if client is not None:
            return await _do(client)
        async with self._make_client() as c:
            return await _do(c)
