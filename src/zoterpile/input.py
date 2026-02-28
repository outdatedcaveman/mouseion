"""
Universal input parser.

Accepts *any* kind of input — a DOI, a URL, an arXiv ID, a PMID, an ISBN,
a plain title, a formatted citation string, a comma/newline/semicolon-
separated list of any of the above, a Chrome bookmarks HTML export, a
BibTeX string, a RIS string, or a file path to a .bib / .ris / .html / .pdf.

Entry points
-----------
    parse_input(text_or_path)  → List[Reference]
    detect_item_type(s)        → str  (for debugging / UI feedback)

The returned References are *seed* objects — typically partial — intended
to be passed to the enrichment engine (lookup.enrich_one / enrich_batch).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from .models import Author, Reference


# ---------------------------------------------------------------------------
# Compiled identifier patterns
# ---------------------------------------------------------------------------

# DOI: bare (10.xxx/yyy) or prefixed with doi:, https://doi.org/, etc.
# Deliberately liberal — the enrichment step validates.
_DOI_RE = re.compile(
    r"(?:(?:https?://(?:dx\.)?doi\.org/)|(?:doi:\s*)|(?:DOI:\s*))?"
    r"(10\.\d{4,}[/\\][^\s\"'<>|,;]+)",
    re.IGNORECASE,
)
_DOI_BARE_RE = re.compile(r"\b(10\.\d{4,}/[^\s\"'<>|,;]+)")

# arXiv  — either full URL or bare ID (NNNN.NNNNN or old-style cs/NNNNNN)
_ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf|html)/([a-z\-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
_ARXIV_ID_RE = re.compile(
    r"(?:^|\s|arXiv:?)([a-zA-Z\-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?(?:\s|$)",
    re.IGNORECASE,
)

# PubMed
_PMID_URL_RE  = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{6,9})")
_PMID_RE      = re.compile(r"(?:PMID:?\s*|pmid:?\s*)(\d{6,9})", re.IGNORECASE)
_PMCID_RE     = re.compile(r"\bPMC(\d+)\b", re.IGNORECASE)

# ISBN (ISBN-13 or ISBN-10, with or without hyphens/spaces)
_ISBN_RE = re.compile(
    r"(?:ISBN:?\s*)?(97[89][\d\-\s]{10,17}\d|(?<!\d)\d{9}[\dXx](?!\d))",
    re.IGNORECASE,
)

# Any http/https URL
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

# Known publisher/platform patterns — used to determine if a URL is academic
_ACADEMIC_URL_PATTERNS = re.compile(
    r"(?:doi\.org|arxiv\.org|pubmed|ncbi\.nlm\.nih|semanticscholar|"
    r"nature\.com|science\.org|springer|elsevier|wiley|oxford|cambridge|"
    r"jstor|plos|biorxiv|medrxiv|ssrn|acm\.org|ieee\.org|"
    r"researchgate|academia\.edu|scholar\.google|unpaywall|"
    r"tandfonline|sagepub|cell\.com|aps\.org|physicstoday)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Type detection
# ---------------------------------------------------------------------------

InputType = str   # one of the constants below

DOI       = "doi"
ARXIV     = "arxiv"
PMID      = "pmid"
PMCID     = "pmcid"
ISBN      = "isbn"
URL       = "url"
TITLE     = "title"
FILE_BIB  = "file:bib"
FILE_RIS  = "file:ris"
FILE_HTML = "file:html"
FILE_PDF  = "file:pdf"
UNKNOWN   = "unknown"


def detect_item_type(s: str) -> Tuple[InputType, str]:
    """
    Identify what kind of input `s` is.

    Returns (type, normalised_value).
    e.g. ("doi", "10.1038/nature12373")
         ("arxiv", "1706.03762")
         ("url", "https://example.com/paper")
         ("title", "Attention Is All You Need")
    """
    s = s.strip()

    # File path
    p = Path(s)
    if p.exists() and p.is_file():
        suf = p.suffix.lower()
        if suf in (".bib", ".bibtex"):
            return FILE_BIB,  s
        if suf == ".ris":
            return FILE_RIS,  s
        if suf in (".html", ".htm"):
            return FILE_HTML, s
        if suf == ".pdf":
            return FILE_PDF,  s

    # DOI URL first (before generic URL check)
    m = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", s, re.I)
    if m:
        return DOI, _clean_doi(m.group(1))

    # Bare or prefixed DOI
    m = re.search(r"(?:^doi:\s*|^DOI:\s*)(10\.[^\s\"'<>]+)", s, re.I)
    if m:
        return DOI, _clean_doi(m.group(1))
    m = re.match(r"^(10\.\d{4,}/[^\s\"'<>|,;]+)$", s)
    if m:
        return DOI, _clean_doi(m.group(1))

    # arXiv URL
    m = _ARXIV_URL_RE.search(s)
    if m:
        return ARXIV, m.group(1).split("v")[0]

    # arXiv bare ID (strict: must be whole value after stripping prefix)
    m = re.match(r"^(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?$", s, re.I)
    if m:
        return ARXIV, m.group(1)

    # PubMed URL
    m = _PMID_URL_RE.search(s)
    if m:
        return PMID, m.group(1)

    # PMID with prefix
    m = re.match(r"^(?:PMID:?\s*)(\d{6,9})$", s, re.I)
    if m:
        return PMID, m.group(1)

    # PMC ID
    m = re.match(r"^PMC(\d+)$", s, re.I)
    if m:
        return PMCID, m.group(1)

    # Any URL
    if re.match(r"https?://", s, re.I):
        return URL, s

    # ISBN
    m = re.match(r"^(?:ISBN:?\s*)?(97[89][\d\-]{9,16}\d|\d{9}[\dXx])$", s, re.I)
    if m:
        return ISBN, re.sub(r"[-\s]", "", m.group(1))

    # Default: treat as title/free-text search
    return TITLE, s


def _clean_doi(doi: str) -> str:
    return doi.rstrip(".,;)\"'").strip()


# ---------------------------------------------------------------------------
# Single-item → Reference
# ---------------------------------------------------------------------------

def _item_to_seed(itype: InputType, value: str) -> Reference:
    ref = Reference()
    if itype == DOI:
        ref.doi = value
    elif itype == ARXIV:
        ref.arxiv_id = value
    elif itype == PMID:
        ref.pmid = value
    elif itype == PMCID:
        ref.pmcid = value
    elif itype == ISBN:
        ref.isbn = value
    elif itype == URL:
        ref.url = value
        # Seed identifiers from URL if possible
        m = _ARXIV_URL_RE.search(value)
        if m:
            ref.arxiv_id = m.group(1).split("v")[0]
        m = _PMID_URL_RE.search(value)
        if m:
            ref.pmid = m.group(1)
        m = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", value, re.I)
        if m:
            ref.doi = _clean_doi(m.group(1))
    elif itype == TITLE:
        ref.title = value.strip()
    return ref


# ---------------------------------------------------------------------------
# Multi-item splitting
# ---------------------------------------------------------------------------

# Characters that definitely separate items (not found inside titles/DOIs)
_HARD_SPLIT_RE = re.compile(r"\n{2,}|\r\n\r\n")  # blank lines
_SOFT_SPLIT_RE = re.compile(r"\n|;")              # single newlines, semicolons

# A "chunk" is considered a composite if it contains multiple identifiable items
# separated by commas.  We split on commas only if every resulting part
# looks like a non-title identifier.
def _looks_like_identifier(s: str) -> bool:
    s = s.strip()
    t, _ = detect_item_type(s)
    return t not in (TITLE, UNKNOWN)


def _split_composite(chunk: str) -> List[str]:
    """
    If `chunk` is a comma-separated list of identifiers, return the parts.
    Otherwise return [chunk] unchanged.
    """
    parts = [p.strip() for p in chunk.split(",")]
    # Only split if there are multiple parts AND all look like identifiers
    if len(parts) >= 2 and all(_looks_like_identifier(p) for p in parts if p):
        return [p for p in parts if p]
    return [chunk]


def _split_input(text: str) -> List[str]:
    """Split a multi-item input string into individual items."""
    # Phase 1: hard split on blank lines
    chunks = _HARD_SPLIT_RE.split(text.strip())

    items: List[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        # Phase 2: soft split each chunk on newlines and semicolons
        sub_chunks = [c.strip() for c in _SOFT_SPLIT_RE.split(chunk) if c.strip()]
        for sc in sub_chunks:
            # Phase 3: comma-split if all parts are identifiers
            items.extend(_split_composite(sc))

    return [i for i in items if i]


# ---------------------------------------------------------------------------
# File-type dispatchers
# ---------------------------------------------------------------------------

def _parse_file(path: Path) -> List[Reference]:
    suf = path.suffix.lower()
    if suf in (".bib", ".bibtex"):
        from .parsers.bibtex import parse_bibtex_file
        return parse_bibtex_file(path)
    if suf == ".ris":
        from .parsers.ris import parse_ris_file
        return parse_ris_file(path)
    if suf in (".html", ".htm"):
        # Try Chrome bookmarks first, fall back to academic HTML
        from .parsers.bookmarks import parse_bookmarks_file
        from .parsers.html import parse_html_string
        try:
            return parse_bookmarks_file(path)
        except ValueError:
            html = path.read_text(encoding="utf-8", errors="replace")
            return [parse_html_string(html, source_url=path.as_uri())]
    if suf == ".pdf":
        from .parsers.pdf import parse_pdf_file
        return [parse_pdf_file(path)]
    # Unknown file extension — try to read as text
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return parse_input(text)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_input(text_or_path: str) -> List[Reference]:
    """
    Parse any input and return seed Reference objects.

    Accepts:
    - File paths (.bib, .ris, .html, .pdf)
    - DOIs, arXiv IDs, PMIDs, URLs, ISBNs (bare or prefixed)
    - Plain title strings
    - Comma/newline/semicolon-separated lists of any of the above
    - Chrome Netscape bookmark HTML files
    - BibTeX or RIS content as a string
    """
    s = text_or_path.strip()
    if not s:
        return []

    # 1. Is it a file path?
    p = Path(s)
    if p.exists() and p.is_file():
        return _parse_file(p)

    # 2. Does it look like BibTeX content?
    if re.search(r"@\w+\s*\{", s):
        try:
            from .parsers.bibtex import parse_bibtex_string
            refs = parse_bibtex_string(s)
            if refs:
                return refs
        except Exception:
            pass

    # 3. Does it look like RIS content?
    if re.match(r"TY\s+-\s+\w+", s, re.MULTILINE):
        try:
            from .parsers.ris import parse_ris_string
            refs = parse_ris_string(s)
            if refs:
                return refs
        except Exception:
            pass

    # 4. Does it look like a Chrome bookmark HTML file?
    if "NETSCAPE-Bookmark-file" in s or ("<DL" in s and "<DT><A HREF=" in s):
        try:
            from .parsers.bookmarks import parse_bookmarks_string
            refs = parse_bookmarks_string(s)
            if refs:
                return refs
        except Exception:
            pass

    # 5. Split into individual items and detect each one's type
    items = _split_input(s)
    refs: List[Reference] = []
    for item in items:
        itype, value = detect_item_type(item)

        if itype in (FILE_BIB, FILE_RIS, FILE_HTML, FILE_PDF):
            refs.extend(_parse_file(Path(value)))
        else:
            refs.append(_item_to_seed(itype, value))

    return refs
