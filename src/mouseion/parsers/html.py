"""
HTML / URL metadata extractor.

Scrapes a page for structured bibliographic metadata in the following
priority order (most → least reliable):

1. Highwire Press / Google Scholar meta tags
   (citation_title, citation_author, citation_doi, …)
2. Dublin Core meta tags  (DC.title, DC.creator, …)
3. Open Graph tags        (og:title, og:description, …)
4. Schema.org JSON-LD     (@type ScholarlyArticle / Article)
5. Heuristic extraction   (page <title>, first <h1>, DOI patterns in body)

The extractor also detects identifiers directly in the URL
(doi.org/…, arxiv.org/abs/…, pubmed.ncbi.nlm.nih.gov/…).

Usage
-----
    # From an already-fetched HTML string
    refs = parse_html_string(html, source_url="https://example.com/paper")

    # Fetch + parse in one call (async)
    refs = await parse_url("https://doi.org/10.1038/nature12373")
"""

from __future__ import annotations

import json
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import Author, RefType, Reference


# ---------------------------------------------------------------------------
# URL-level identifier detection
# ---------------------------------------------------------------------------

# doi.org/10.xxx/yyy  or  dx.doi.org/10.xxx/yyy
_DOI_URL_RE = re.compile(
    r"https?://(?:dx\.)?doi\.org/(10\.\d{4,}/\S+)", re.IGNORECASE
)
# Bare DOI pattern anywhere in a URL path
_DOI_PATH_RE = re.compile(r"\b(10\.\d{4,}/[^\s\"'<>]+)")

# arxiv.org/abs/NNNN.NNNNN  or  arxiv.org/pdf/…
_ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE
)

# pubmed.ncbi.nlm.nih.gov/NNNNNNN
_PUBMED_URL_RE = re.compile(
    r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.IGNORECASE
)


def _ids_from_url(url: str) -> dict:
    ids: dict = {}
    if not url:
        return ids
    m = _DOI_URL_RE.search(url)
    if m:
        ids["doi"] = m.group(1).rstrip(".")
    m = _ARXIV_URL_RE.search(url)
    if m:
        ids["arxiv_id"] = m.group(1).split("v")[0]  # strip version suffix
    m = _PUBMED_URL_RE.search(url)
    if m:
        ids["pmid"] = m.group(1)
    return ids


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _meta(soup: BeautifulSoup, *names: str) -> Optional[str]:
    """Return the first <meta> content matching any name/property."""
    for name in names:
        tag = soup.find("meta", attrs={"name": name}) or soup.find(
            "meta", attrs={"property": name}
        )
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _all_meta(soup: BeautifulSoup, name: str) -> List[str]:
    """Return contents of all <meta> tags with the given name."""
    tags = soup.find_all("meta", attrs={"name": name})
    tags += soup.find_all("meta", attrs={"property": name})
    return [t["content"].strip() for t in tags if t.get("content")]


# ---------------------------------------------------------------------------
# Highwire / Google Scholar meta tags
# ---------------------------------------------------------------------------

def _parse_highwire(soup: BeautifulSoup, ref: Reference) -> None:
    """Populate ref from citation_* meta tags (highest trust for papers)."""
    title = _meta(soup, "citation_title")
    if title:
        ref.title = title

    # Authors: multiple citation_author tags
    authors = _all_meta(soup, "citation_author")
    if authors:
        ref.authors = [Author.from_bibtex_str(a) for a in authors]

    year_raw = _meta(
        soup, "citation_publication_date", "citation_date", "citation_year"
    )
    if year_raw:
        m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", year_raw)
        if m:
            ref.year = int(m.group(1))

    doi = _meta(soup, "citation_doi")
    if doi:
        ref.doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi).strip()

    ref.journal = _meta(
        soup, "citation_journal_title", "citation_conference_title"
    ) or ref.journal
    ref.journal_abbrev = _meta(soup, "citation_journal_abbrev") or ref.journal_abbrev
    ref.volume = _meta(soup, "citation_volume") or ref.volume
    ref.issue  = _meta(soup, "citation_issue")  or ref.issue
    ref.pages  = _meta(soup, "citation_firstpage") or ref.pages
    last_page  = _meta(soup, "citation_lastpage")
    if ref.pages and last_page and "-" not in ref.pages:
        ref.pages = f"{ref.pages}-{last_page}"

    ref.issn = _meta(soup, "citation_issn") or ref.issn
    ref.isbn = _meta(soup, "citation_isbn") or ref.isbn
    ref.language = _meta(soup, "citation_language") or ref.language

    raw_arxiv = _meta(soup, "citation_arxiv_id")
    if raw_arxiv:
        ref.arxiv_id = raw_arxiv

    pdf_url = _meta(soup, "citation_pdf_url")
    if pdf_url:
        ref.oa_url = pdf_url

    # Abstract (not a standard Highwire tag but some publishers use it)
    ref.abstract = _meta(soup, "citation_abstract") or ref.abstract


# ---------------------------------------------------------------------------
# Dublin Core
# ---------------------------------------------------------------------------

def _parse_dublin_core(soup: BeautifulSoup, ref: Reference) -> None:
    ref.title    = ref.title    or _meta(soup, "DC.title", "dc.title", "DC.Title")
    ref.abstract = ref.abstract or _meta(soup, "DC.description", "dc.description")
    ref.language = ref.language or _meta(soup, "DC.language", "dc.language")
    ref.publisher = ref.publisher or _meta(soup, "DC.publisher", "dc.publisher")

    dc_creators = _all_meta(soup, "DC.creator") + _all_meta(soup, "dc.creator")
    if dc_creators and not ref.authors:
        ref.authors = [Author.from_bibtex_str(a) for a in dc_creators]

    date_raw = _meta(soup, "DC.date", "dc.date")
    if date_raw and not ref.year:
        m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", date_raw)
        if m:
            ref.year = int(m.group(1))

    doi_raw = _meta(soup, "DC.identifier", "dc.identifier")
    if doi_raw and not ref.doi:
        m = _DOI_PATH_RE.search(doi_raw)
        if m:
            ref.doi = m.group(1).rstrip(".")


# ---------------------------------------------------------------------------
# Open Graph
# ---------------------------------------------------------------------------

def _parse_opengraph(soup: BeautifulSoup, ref: Reference) -> None:
    ref.title    = ref.title    or _meta(soup, "og:title")
    ref.abstract = ref.abstract or _meta(soup, "og:description")
    if not ref.url:
        ref.url = _meta(soup, "og:url")


# ---------------------------------------------------------------------------
# Schema.org JSON-LD
# ---------------------------------------------------------------------------

_SCHOLARLY_TYPES = {
    "ScholarlyArticle", "Article", "NewsArticle",
    "MedicalScholarlyArticle", "TechArticle",
}


def _parse_jsonld(soup: BeautifulSoup, ref: Reference) -> None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        # Unwrap @graph arrays
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and "@graph" in data:
            items = data["@graph"]
        else:
            items = [data]

        for item in items:
            if not isinstance(item, dict):
                continue
            rtype = item.get("@type", "")
            if isinstance(rtype, list):
                rtype = rtype[0] if rtype else ""
            if rtype not in _SCHOLARLY_TYPES and rtype not in {"Book", "Thesis"}:
                continue

            ref.title = ref.title or item.get("name") or item.get("headline")
            ref.abstract = ref.abstract or item.get("description") or item.get("abstract")
            ref.url = ref.url or item.get("url")

            # Authors
            if not ref.authors:
                authors_raw = item.get("author", [])
                if isinstance(authors_raw, dict):
                    authors_raw = [authors_raw]
                for a in authors_raw:
                    if isinstance(a, dict):
                        name = a.get("name", "")
                        ref.authors.append(Author.from_bibtex_str(name))

            # Date
            if not ref.year:
                date_raw = item.get("datePublished") or item.get("dateCreated", "")
                if date_raw:
                    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(date_raw))
                    if m:
                        ref.year = int(m.group(1))

            # DOI
            if not ref.doi:
                ident = item.get("identifier", "")
                if isinstance(ident, dict):
                    ident = ident.get("value", "")
                if isinstance(ident, str):
                    m = _DOI_PATH_RE.search(ident)
                    if m:
                        ref.doi = m.group(1).rstrip(".")


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------

def _parse_heuristic(soup: BeautifulSoup, ref: Reference, source_url: str) -> None:
    """Last resort: page <title>, first <h1>, DOI in body text."""
    if not ref.title:
        h1 = soup.find("h1")
        if h1:
            ref.title = h1.get_text(" ", strip=True)
        elif soup.title:
            ref.title = soup.title.get_text(" ", strip=True)

    if not ref.doi:
        # Scan page text for bare DOI patterns
        text = soup.get_text(" ")
        m = _DOI_PATH_RE.search(text)
        if m:
            ref.doi = m.group(1).rstrip(".,;)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_html_string(html: str, source_url: str = "") -> Reference:
    """
    Extract bibliographic metadata from an HTML string.

    Returns a single (possibly very partial) Reference.  The caller is
    expected to pass this to the lookup engine for enrichment.
    """
    ref = Reference()
    ref.url = source_url or None

    # Seed from URL-level identifiers first
    for k, v in _ids_from_url(source_url).items():
        setattr(ref, k, v)

    soup = BeautifulSoup(html, "lxml")

    # Apply extractors in priority order (higher trust first)
    _parse_highwire(soup, ref)
    _parse_dublin_core(soup, ref)
    _parse_jsonld(soup, ref)
    _parse_opengraph(soup, ref)
    _parse_heuristic(soup, ref, source_url)

    ref.sources["html_input"] = 0.4
    ref.normalize()
    return ref


async def parse_url(url: str, client=None) -> Reference:
    """
    Fetch a URL and extract bibliographic metadata.

    `client` should be an httpx.AsyncClient (injected for testing / reuse).
    If None, a temporary client is created.
    """
    import httpx

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; mouseion/0.1; "
            "+https://github.com/outdatedcaveman/mouseion)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }

    async def _fetch(c: httpx.AsyncClient) -> str:
        resp = await c.get(url, headers=headers, follow_redirects=True, timeout=20)
        resp.raise_for_status()
        return resp.text

    if client is not None:
        html = await _fetch(client)
    else:
        async with httpx.AsyncClient(http2=True) as c:
            html = await _fetch(c)

    # Use the final (possibly redirected) URL as source_url
    return parse_html_string(html, source_url=url)
