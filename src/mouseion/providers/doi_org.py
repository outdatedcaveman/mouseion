"""
DOI.org content negotiation provider.

Uses the DOI resolver's content negotiation API to get structured citation
metadata in Citeproc JSON format.  This is the most authoritative source for
DOI-based references because it returns exactly what the publisher registered.

No API key required.

API docs: https://citation.crosscite.org/docs.html
"""

from __future__ import annotations

import re
from typing import List, Optional

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider


_BASE = "https://doi.org"


class DOIOrgProvider(BaseProvider):
    name = "doi_org"
    priority = 2          # Very authoritative for DOI lookups
    _max_concurrent = 5
    _min_interval = 0.1

    # ------------------------------------------------------------------
    # Citeproc JSON → Reference
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_citeproc(data: dict) -> Reference:
        """Convert a Citeproc JSON object to a Reference."""
        ref = Reference()

        # Type mapping from Citeproc types
        ctype = data.get("type", "")
        type_map = {
            "article-journal": RefType.JOURNAL,
            "book": RefType.BOOK,
            "chapter": RefType.BOOK_CHAPTER,
            "paper-conference": RefType.CONFERENCE,
            "thesis": RefType.THESIS,
            "dataset": RefType.DATASET,
            "report": RefType.REPORT,
            "webpage": RefType.WEBSITE,
            "article": RefType.JOURNAL,
            "manuscript": RefType.PREPRINT,
        }
        ref.ref_type = type_map.get(ctype, RefType.UNKNOWN)

        # DOI
        doi_raw = data.get("DOI", "")
        ref.doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_raw).strip() or None

        # Title
        ref.title = data.get("title", "").strip() or None

        # Authors
        for a in data.get("author", []):
            family = a.get("family", "")
            given = a.get("given", "")
            orcid_raw = a.get("ORCID", "") or ""
            orcid = re.sub(r"^https?://orcid\.org/", "", orcid_raw) or None
            if family or given:
                ref.authors.append(Author(family=family, given=given, orcid=orcid))

        # Editors
        for e in data.get("editor", []):
            family = e.get("family", "")
            given = e.get("given", "")
            if family or given:
                ref.editors.append(Author(family=family, given=given))

        # Year / month from issued date-parts
        for date_key in ("issued", "published-print", "published-online", "created"):
            dp = data.get(date_key, {}).get("date-parts", [[]])
            if dp and dp[0]:
                parts = dp[0]
                if parts and parts[0]:
                    ref.year = int(parts[0])
                    if len(parts) > 1 and parts[1]:
                        ref.month = int(parts[1])
                    break

        # Abstract
        abstract = data.get("abstract", "")
        if abstract:
            ref.abstract = re.sub(r"<[^>]+>", "", abstract).strip() or None

        # Container / journal
        container = data.get("container-title", "")
        if container:
            ref.journal = container
            ref.container_title = container

        # Short container
        short = data.get("container-title-short", "") or data.get("journalAbbreviation", "")
        if short:
            ref.journal_abbrev = short

        # Volume / issue / pages
        ref.volume = data.get("volume") or None
        ref.issue = data.get("issue") or None
        ref.pages = data.get("page") or None
        ref.article_number = data.get("article-number") or None

        # Publisher
        ref.publisher = data.get("publisher") or None
        ref.place = data.get("publisher-place") or None

        # ISBN / ISSN
        ref.isbn = data.get("ISBN") or None
        issn = data.get("ISSN")
        if isinstance(issn, list) and issn:
            ref.issn = issn[0]
        elif isinstance(issn, str):
            ref.issn = issn

        # URL
        ref.url = data.get("URL") or None

        # Language
        ref.language = data.get("language") or None

        # Subject / keywords
        subjects = data.get("subject", [])
        if isinstance(subjects, list):
            ref.keywords = subjects[:10]

        ref.sources["doi_org"] = 1.0
        ref.normalize()
        return ref

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        url = f"{_BASE}/{doi}"
        resp = await self._get(
            client, url,
            headers={"Accept": "application/vnd.citeprocjson"},
        )
        if resp is None:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("DOI"):
            return None
        return self._parse_citeproc(data)

    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        # DOI.org does not support title search
        return []
