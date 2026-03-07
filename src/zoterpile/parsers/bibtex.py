"""
BibTeX format parser.

Uses bibtexparser v1 (stable, well-known API) to parse .bib files, then
maps BibTeX fields to our Reference model.  Handles the messy real-world
quirks: LaTeX-escaped characters, multiple author formats, missing fields.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import IO, List, Union

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import (
    author,
    convert_to_unicode,
    homogenize_latex_encoding,
)

from ..models import Author, RefType, Reference


# ---------------------------------------------------------------------------
# Entry-type → RefType mapping
# ---------------------------------------------------------------------------

_ENTRY_TYPE_MAP = {
    "article":       RefType.JOURNAL,
    "book":          RefType.BOOK,
    "incollection":  RefType.BOOK_CHAPTER,
    "inbook":        RefType.BOOK_CHAPTER,
    "inproceedings": RefType.CONFERENCE,
    "conference":    RefType.CONFERENCE,
    "phdthesis":     RefType.THESIS,
    "mastersthesis": RefType.THESIS,
    "techreport":    RefType.REPORT,
    "unpublished":   RefType.PREPRINT,
    "misc":          RefType.OTHER,
    "online":        RefType.WEBSITE,
    "electronic":    RefType.WEBSITE,
}


def _clean(s: str) -> str:
    """Strip outer braces and whitespace from a BibTeX field value."""
    if not s:
        return s
    s = s.strip()
    # Strip outer {…} braces (bibtexparser sometimes leaves them)
    while s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    return s


def _bibtex_entry_to_reference(entry: dict) -> Reference:
    ref = Reference()

    # --- Type ---
    etype = entry.get("ENTRYTYPE", "misc").lower()
    ref.ref_type = _ENTRY_TYPE_MAP.get(etype, RefType.UNKNOWN)

    # --- Cite key ---
    ref.cite_key = entry.get("ID") or None

    # --- Title ---
    ref.title = _clean(entry.get("title", "")) or None

    # --- Authors ---
    # bibtexparser's author() customization pre-splits to a list;
    # without it, raw_authors is still a string. Handle both.
    raw_authors = entry.get("author", "")
    if raw_authors:
        if isinstance(raw_authors, list):
            parts = raw_authors
        else:
            parts = re.split(r"\s+and\s+", raw_authors, flags=re.IGNORECASE)
        ref.authors = [Author.from_bibtex_str(_clean(str(a))) for a in parts if str(a).strip()]

    # --- Editors ---
    raw_editors = entry.get("editor", "")
    if raw_editors:
        if isinstance(raw_editors, list):
            parts = raw_editors
        else:
            parts = re.split(r"\s+and\s+", raw_editors, flags=re.IGNORECASE)
        ref.editors = [Author.from_bibtex_str(_clean(str(e))) for e in parts if str(e).strip()]

    # --- Year ---
    raw_year = _clean(entry.get("year", ""))
    if raw_year:
        m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", raw_year)
        if m:
            ref.year = int(m.group(1))

    # --- Month ---
    raw_month = _clean(entry.get("month", "")).lower()
    month_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    if raw_month:
        ref.month = month_map.get(raw_month[:3]) or (int(raw_month) if raw_month.isdigit() else None)

    # --- Abstract ---
    ref.abstract = _clean(entry.get("abstract", "")) or None

    # --- Journal / container ---
    ref.journal = _clean(entry.get("journal", "") or entry.get("journaltitle", "")) or None
    ref.journal_abbrev = _clean(entry.get("shortjournal", "")) or None
    ref.container_title = (
        _clean(entry.get("booktitle", ""))
        or ref.journal
        or None
    )

    # --- Volume / issue / pages ---
    ref.volume = _clean(entry.get("volume", "")) or None
    ref.issue  = _clean(entry.get("number", "") or entry.get("issue", "")) or None
    ref.pages  = _clean(entry.get("pages", "")) or None

    # --- Identifiers ---
    raw_doi = _clean(entry.get("doi", ""))
    if raw_doi:
        ref.doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw_doi).strip()

    ref.url = _clean(entry.get("url", "") or entry.get("howpublished", "")) or None
    # Extract URL from howpublished if it contains \url{...}
    if ref.url:
        url_match = re.search(r"\\url\{([^}]+)\}", ref.url)
        if url_match:
            ref.url = url_match.group(1)

    # --- PMID / arXiv from note or eprint ---
    note  = _clean(entry.get("note", ""))
    arxiv = _clean(entry.get("eprint", ""))
    if arxiv:
        arxiv_archive = _clean(entry.get("archiveprefix", "")).lower()
        if arxiv_archive == "arxiv" or re.match(r"\d{4}\.\d{4,5}", arxiv):
            ref.arxiv_id = arxiv
    if note:
        pmid_m = re.search(r"PMID[:\s]+(\d+)", note, re.IGNORECASE)
        if pmid_m:
            ref.pmid = pmid_m.group(1)

    # --- ISBN / ISSN ---
    ref.isbn = _clean(entry.get("isbn", "")) or None
    ref.issn = _clean(entry.get("issn", "")) or None

    # --- Publisher / series / edition / place ---
    ref.publisher = _clean(entry.get("publisher", "")) or None
    ref.place     = (
        _clean(entry.get("address", "") or entry.get("location", "")) or None
    )
    ref.edition = _clean(entry.get("edition", "")) or None
    ref.series  = _clean(entry.get("series", "")) or None

    # --- Keywords ---
    kw_raw = _clean(entry.get("keywords", ""))
    if kw_raw:
        ref.keywords = [k.strip() for k in re.split(r"[,;/]", kw_raw) if k.strip()]

    # --- Language ---
    ref.language = _clean(entry.get("language", "")) or None

    ref.sources["bibtex_input"] = 0.5
    ref.normalize()
    return ref


def _make_parser() -> BibTexParser:
    parser = BibTexParser(common_strings=True)
    # Convert LaTeX escapes → unicode
    parser.customization = lambda r: author(convert_to_unicode(r))
    return parser


def parse_bibtex_string(text: str) -> List[Reference]:
    """Parse a BibTeX string and return a list of Reference objects."""
    db = bibtexparser.loads(text, parser=_make_parser())
    return [_bibtex_entry_to_reference(e) for e in db.entries]


def parse_bibtex_file(path: Union[str, Path, IO]) -> List[Reference]:
    """Parse a .bib file by path or file-like object."""
    if hasattr(path, "read"):
        text = path.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        return parse_bibtex_string(text)
    path = Path(path)
    return parse_bibtex_string(path.read_text(encoding="utf-8", errors="replace"))
