"""
RIS exporter.

Produces RIS format output (.ris) from Reference objects.
RIS is the most universally supported format for reference managers
(Zotero, Mendeley, EndNote, Papers, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Union

from ..models import Reference


def _ref_to_ris(ref: Reference) -> str:
    lines: List[str] = []

    def tag(t: str, value):
        if value is None:
            return
        s = str(value).strip()
        if s:
            lines.append(f"{t}  - {s}")

    # Record type (must be first)
    tag("TY", ref.ref_type.to_ris_type())

    # Identifiers
    tag("DO", ref.doi)
    if ref.pmid:
        tag("AN", ref.pmid)
    if ref.arxiv_id:
        tag("UR", f"https://arxiv.org/abs/{ref.arxiv_id}")
    tag("SN", ref.issn or ref.eissn or ref.isbn)
    if ref.eissn and ref.issn:
        tag("SN", ref.eissn)
    tag("UR", ref.url or ref.oa_url)

    # Core
    tag("TI", ref.title)

    # Authors (one per line)
    for author in ref.authors:
        tag("AU", author.to_bibtex_str())

    # Editors
    for editor in ref.editors:
        tag("ED", editor.to_bibtex_str())

    # Dates
    if ref.year:
        year_str = str(ref.year)
        month_str = f"{ref.month:02d}" if ref.month else "00"
        tag("PY", f"{year_str}/{month_str}//")

    tag("AB", ref.abstract)

    # Publication details
    tag("JO", ref.journal or ref.container_title)
    tag("JA", ref.journal_abbrev)
    tag("VL", ref.volume)
    tag("IS", ref.issue)
    tag("SP", _start_page(ref.pages))
    tag("EP", _end_page(ref.pages))
    if not ref.pages and ref.article_number:
        tag("M1", ref.article_number)

    # Secondary title: conference name or book title for chapters
    if ref.event_name:
        tag("T2", ref.event_name)
    elif ref.ref_type.value in ("book-chapter",):
        tag("T2", ref.container_title)

    # Book / publisher
    tag("PB", ref.publisher)
    tag("CY", ref.place)

    # Keywords
    for kw in ref.keywords:
        tag("KW", kw)

    tag("LA", ref.language)
    tag("M3", ref.license)

    # End record (required)
    lines.append("ER  - ")
    lines.append("")    # blank line between records

    return "\n".join(lines)


def _start_page(pages: str | None) -> str | None:
    if not pages:
        return None
    return pages.split("-")[0].strip()


def _end_page(pages: str | None) -> str | None:
    if not pages or "-" not in pages:
        return None
    return pages.split("-", 1)[1].strip()


def to_ris_string(refs: Union[Reference, List[Reference]]) -> str:
    """Convert one or more References to a RIS string."""
    if isinstance(refs, Reference):
        refs = [refs]
    return "\n".join(_ref_to_ris(r) for r in refs)


def export_ris_file(
    refs: Union[Reference, List[Reference]],
    path: Union[str, Path],
) -> None:
    """Write References to a .ris file."""
    content = to_ris_string(refs)
    Path(path).write_text(content, encoding="utf-8")
