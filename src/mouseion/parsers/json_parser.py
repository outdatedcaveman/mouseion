"""
JSON format parser for reference imports.

Accepts JSON arrays of reference objects, or single reference objects.
Handles multiple JSON schemas:
  - Native Mouseion format (field names match our model)
  - CSL-JSON (Citation Style Language JSON, used by Zotero, Mendeley, etc.)
  - Generic key-value dicts (best-effort mapping)

Unknown fields are preserved in the ``extras`` dict so no data is lost.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, List, Optional, Union

from ..models import Author, RefType, Reference


# ── Known field names (our model) ──────────────────────────────────────────

_KNOWN_FIELDS = {
    "doi", "pmid", "pmcid", "arxiv_id", "isbn", "issn", "eissn", "url",
    "oa_url", "title", "authors", "editors", "year", "month", "abstract",
    "ref_type", "journal", "journal_abbrev", "container_title", "volume",
    "issue", "pages", "article_number", "event_name", "publisher", "place",
    "edition", "series", "num_pages", "keywords", "language", "open_access",
    "license", "citation_count", "pdf_path", "cite_key", "sources", "extras",
}


# ── CSL-JSON type mapping ──────────────────────────────────────────────────

_CSL_TYPE_MAP = {
    "article-journal": RefType.JOURNAL,
    "article":         RefType.JOURNAL,
    "book":            RefType.BOOK,
    "chapter":         RefType.BOOK_CHAPTER,
    "paper-conference": RefType.CONFERENCE,
    "thesis":          RefType.THESIS,
    "dataset":         RefType.DATASET,
    "report":          RefType.REPORT,
    "webpage":         RefType.WEBSITE,
    "manuscript":      RefType.PREPRINT,
}


def _parse_author(raw) -> Optional[Author]:
    """Parse an author from various formats."""
    if isinstance(raw, str):
        return Author.from_bibtex_str(raw)
    if isinstance(raw, dict):
        # CSL-JSON: {"family": "Smith", "given": "John"}
        # or our native: same
        family = raw.get("family", raw.get("lastName", raw.get("last", "")))
        given = raw.get("given", raw.get("firstName", raw.get("first", "")))
        # CSL also uses "literal" for institutional authors
        if not family and not given:
            literal = raw.get("literal", raw.get("name", ""))
            if literal:
                return Author.from_bibtex_str(literal)
            return None
        return Author(
            family=family or "",
            given=given or "",
            orcid=raw.get("orcid") or raw.get("ORCID") or None,
            affiliation=raw.get("affiliation") or None,
        )
    return None


def _csl_date_to_year(date_obj) -> Optional[int]:
    """Extract year from CSL date-parts: {"date-parts": [[2020, 1, 15]]}."""
    if isinstance(date_obj, dict):
        parts = date_obj.get("date-parts", [[]])
        if parts and parts[0] and len(parts[0]) >= 1:
            try:
                return int(parts[0][0])
            except (ValueError, TypeError):
                pass
        # Also try "raw" or "literal" date strings
        raw = date_obj.get("raw", date_obj.get("literal", ""))
        if raw:
            import re
            m = re.search(r"\b(19|20)\d{2}\b", str(raw))
            if m:
                return int(m.group())
    if isinstance(date_obj, (int, float)):
        return int(date_obj)
    return None


def _parse_one(d: dict) -> Reference:
    """Parse a single JSON object into a Reference, preserving unknown fields."""
    # Detect if this is CSL-JSON (has "type" field with CSL values)
    is_csl = "type" in d and d.get("type") in _CSL_TYPE_MAP

    # ── Authors ──
    authors_raw = d.get("authors", d.get("author", []))
    if isinstance(authors_raw, str):
        authors_raw = [authors_raw]
    authors = [a for a in (_parse_author(a) for a in authors_raw) if a]

    editors_raw = d.get("editors", d.get("editor", []))
    if isinstance(editors_raw, str):
        editors_raw = [editors_raw]
    editors = [e for e in (_parse_author(e) for e in editors_raw) if e]

    # ── Year ──
    year = d.get("year")
    if year is None and "issued" in d:
        year = _csl_date_to_year(d["issued"])
    if year is not None:
        try:
            year = int(year)
        except (ValueError, TypeError):
            year = None

    # ── Ref type ──
    rt_raw = d.get("ref_type", d.get("type", ""))
    if rt_raw in _CSL_TYPE_MAP:
        ref_type = _CSL_TYPE_MAP[rt_raw]
    else:
        try:
            ref_type = RefType(rt_raw) if rt_raw else RefType.UNKNOWN
        except ValueError:
            ref_type = RefType.UNKNOWN

    # ── Keywords ──
    kw = d.get("keywords", d.get("keyword", []))
    if isinstance(kw, str):
        kw = [k.strip() for k in kw.replace(";", ",").split(",") if k.strip()]

    # ── CSL field mapping ──
    journal = d.get("journal", "")
    if not journal and is_csl:
        journal = d.get("container-title", d.get("container_title", ""))
    journal = journal or d.get("container_title", "")

    # ── Collect unknown fields into extras ──
    _mapped_keys = {
        "authors", "author", "editors", "editor", "year", "issued",
        "ref_type", "type", "keywords", "keyword", "journal",
        "container-title", "container_title", "doi", "DOI", "pmid", "PMID",
        "pmcid", "arxiv_id", "isbn", "ISBN", "issn", "ISSN", "eissn",
        "url", "URL", "oa_url", "title", "abstract", "journal_abbrev",
        "volume", "issue", "pages", "page", "article_number", "event_name",
        "publisher", "place", "publisher-place", "edition", "series",
        "num_pages", "number-of-pages", "language", "open_access",
        "license", "citation_count", "pdf_path", "cite_key", "id",
        "sources", "extras", "month",
    }
    extras = d.get("extras", {})
    if not isinstance(extras, dict):
        extras = {}
    for k, v in d.items():
        if k not in _mapped_keys and v is not None and v != "":
            extras[k] = v

    ref = Reference(
        doi=d.get("doi", d.get("DOI")),
        pmid=d.get("pmid", d.get("PMID")),
        pmcid=d.get("pmcid"),
        arxiv_id=d.get("arxiv_id"),
        isbn=d.get("isbn", d.get("ISBN")),
        issn=d.get("issn", d.get("ISSN")),
        eissn=d.get("eissn"),
        url=d.get("url", d.get("URL")),
        oa_url=d.get("oa_url"),
        title=d.get("title"),
        authors=authors,
        editors=editors,
        year=year,
        month=d.get("month"),
        abstract=d.get("abstract"),
        ref_type=ref_type,
        journal=journal,
        journal_abbrev=d.get("journal_abbrev"),
        container_title=d.get("container_title", d.get("container-title")),
        volume=d.get("volume"),
        issue=d.get("issue"),
        pages=d.get("pages", d.get("page")),
        article_number=d.get("article_number"),
        event_name=d.get("event_name", d.get("event-name")),
        publisher=d.get("publisher"),
        place=d.get("place", d.get("publisher-place")),
        edition=d.get("edition"),
        series=d.get("series"),
        num_pages=d.get("num_pages", d.get("number-of-pages")),
        keywords=kw,
        language=d.get("language"),
        open_access=d.get("open_access"),
        license=d.get("license"),
        citation_count=d.get("citation_count"),
        pdf_path=d.get("pdf_path"),
        cite_key=d.get("cite_key", d.get("id")),
        sources=d.get("sources", {}),
        extras=extras,
    )
    return ref


def parse_json_string(text: str) -> List[Reference]:
    """Parse a JSON string containing one or more references."""
    data = json.loads(text)
    if isinstance(data, dict):
        # Could be a single ref or a wrapper {"items": [...]}
        items = data.get("items", data.get("references", data.get("refs")))
        if isinstance(items, list):
            data = items
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    return [_parse_one(d) for d in data if isinstance(d, dict)]


def parse_json_file(path: Union[str, Path]) -> List[Reference]:
    """Parse a JSON file."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_json_string(text)
