"""
RIS format parser.

Uses the `rispy` library to do the heavy lifting, then maps the RIS tag
vocabulary to our Reference model.  Every ambiguity in the RIS spec is
handled conservatively (prefer data over loss).
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import IO, List, Optional, Union

import rispy

from ..models import Author, RefType, Reference


# ---------------------------------------------------------------------------
# RIS tag → Reference field mapping
# ---------------------------------------------------------------------------

# Tags that carry the title
_TITLE_TAGS = ("TI", "T1", "CT", "BT")

# Tags that carry author names (each occurrence is one author)
_AUTHOR_TAGS = ("AU", "A1", "A2", "A3", "A4")

# Tags that carry editor names
_EDITOR_TAGS = ("ED",)


def _ris_entry_to_reference(entry: dict) -> Reference:
    ref = Reference()

    # --- Type ---
    ty = entry.get("type_of_reference", entry.get("TY", ""))
    ref.ref_type = RefType.from_ris_type(ty) if ty else RefType.UNKNOWN

    # --- Title ---
    for tag in _TITLE_TAGS:
        val = entry.get(rispy.TAG_KEY_MAPPING.get(tag, tag), entry.get(tag))
        if val:
            ref.title = val.strip()
            break

    # --- Authors ---
    authors_raw: List[str] = []
    for tag in _AUTHOR_TAGS:
        mapped = rispy.TAG_KEY_MAPPING.get(tag, tag)
        raw = entry.get(mapped, entry.get(tag))
        if isinstance(raw, list):
            authors_raw.extend(raw)
        elif raw:
            authors_raw.append(raw)
    # Only use A1 if AU is empty (some exporters use A1 for primary authors)
    ref.authors = [Author.from_bibtex_str(a) for a in authors_raw if a]

    # --- Editors ---
    eds_raw = entry.get("editors", entry.get("ED", []))
    if isinstance(eds_raw, str):
        eds_raw = [eds_raw]
    ref.editors = [Author.from_bibtex_str(e) for e in eds_raw if e]

    # --- Year ---
    for year_tag in ("year", "PY", "Y1"):
        raw_year = entry.get(year_tag, "")
        if raw_year:
            m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(raw_year))
            if m:
                ref.year = int(m.group(1))
            break

    # --- Abstract ---
    ref.abstract = entry.get("abstract", entry.get("N2", "")) or None

    # --- Journal / container ---
    ref.journal = (
        entry.get("journal_name")
        or entry.get("secondary_title")   # T2
        or entry.get("JO")
        or entry.get("JF")
        or None
    )
    ref.journal_abbrev = entry.get("alternate_title1") or entry.get("J1") or None
    ref.container_title = ref.journal

    # --- Volume / issue / pages ---
    ref.volume = str(entry["volume"]) if entry.get("volume") else None
    ref.issue  = str(entry["number"]) if entry.get("number") else None
    start = str(entry["start_page"]) if entry.get("start_page") else ""
    end   = str(entry["end_page"])   if entry.get("end_page")   else ""
    if start and end:
        ref.pages = f"{start}-{end}"
    elif start:
        ref.pages = start

    # --- Identifiers ---
    raw_doi = entry.get("doi") or entry.get("DO") or ""
    if raw_doi:
        ref.doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw_doi).strip()

    ref.pmid = str(entry["accession_number"]) if entry.get("accession_number") else None
    ref.url  = entry.get("url") or entry.get("UR") or None

    isbn_raw = entry.get("isbn") or entry.get("SN") or ""
    if isbn_raw:
        # SN may hold either ISBN or ISSN depending on type
        if ref.ref_type in (RefType.BOOK, RefType.BOOK_CHAPTER):
            ref.isbn = isbn_raw
        else:
            ref.issn = isbn_raw

    # --- Publisher / place ---
    ref.publisher = entry.get("publisher") or entry.get("PB") or None
    ref.place     = entry.get("place_published") or entry.get("CY") or None

    # --- Keywords ---
    kw = entry.get("keywords", [])
    if isinstance(kw, str):
        kw = [k.strip() for k in re.split(r"[,;/]", kw)]
    ref.keywords = [k.strip() for k in kw if k.strip()]

    # --- Language ---
    ref.language = entry.get("language") or entry.get("LA") or None

    # --- Cite key ---
    ref.cite_key = entry.get("id") or None

    ref.sources["ris_input"] = 0.5   # input file confidence
    ref.normalize()
    return ref


def parse_ris_string(text: str) -> List[Reference]:
    """Parse a RIS-format string and return a list of Reference objects."""
    entries = rispy.loads(text)
    return [_ris_entry_to_reference(e) for e in entries]


def parse_ris_file(path: Union[str, Path, IO]) -> List[Reference]:
    """Parse a .ris file by path or file-like object."""
    if hasattr(path, "read"):
        text = path.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        return parse_ris_string(text)
    path = Path(path)
    return parse_ris_string(path.read_text(encoding="utf-8", errors="replace"))
