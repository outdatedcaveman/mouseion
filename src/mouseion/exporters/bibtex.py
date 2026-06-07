"""
BibTeX exporter.

Produces well-formatted .bib output from Reference objects.
Handles LaTeX special characters, multi-author formatting, and
all standard BibTeX entry types.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Union

from ..models import Reference


# Characters that need escaping in BibTeX field values
_LATEX_ESCAPES = {
    "&":  r"\&",
    "%":  r"\%",
    "$":  r"\$",
    "#":  r"\#",
    "_":  r"\_",
    "^":  r"\^{}",
    "~":  r"\~{}",
    # Don't escape { } because we use them for protection
}

# Fields that should be protected with double braces {{ }} to preserve case
# (BibTeX otherwise lower-cases all title words)
_TITLE_FIELDS = {"title", "booktitle", "journal", "series"}


def _escape(value: str, protect_case: bool = False) -> str:
    """Escape special LaTeX characters and optionally protect title case."""
    # Replace common Unicode that BibTeX chokes on
    value = value.replace("\u2013", "--").replace("\u2014", "---")
    value = value.replace("\u2018", "`").replace("\u2019", "'")
    value = value.replace("\u201c", "``").replace("\u201d", "''")
    value = value.replace("\u00a0", "~")    # non-breaking space
    value = value.replace("\u00d7", r"$\times$")

    for char, replacement in _LATEX_ESCAPES.items():
        value = value.replace(char, replacement)

    if protect_case:
        # Wrap in extra braces to prevent BibTeX from changing case
        return f"{{{value}}}"
    return value


def _format_authors(ref: Reference) -> str:
    if not ref.authors:
        return ""
    return " and ".join(a.to_bibtex_str() for a in ref.authors)


def _format_editors(ref: Reference) -> str:
    if not ref.editors:
        return ""
    return " and ".join(e.to_bibtex_str() for e in ref.editors)


def _ref_to_bibtex_entry(ref: Reference) -> str:
    """Convert a single Reference to a BibTeX entry string."""
    entry_type = ref.ref_type.to_bibtex_type()
    cite_key   = ref.cite_key or ref.auto_cite_key()

    fields: List[tuple] = []

    def add(name: str, value, protect: bool = False):
        if value is None:
            return
        s = str(value).strip()
        if not s:
            return
        escaped = _escape(s, protect_case=(protect or name in _TITLE_FIELDS))
        if protect and name not in _TITLE_FIELDS:
            # Don't double-wrap
            escaped = _escape(s, protect_case=False)
            fields.append((name, f"{{{escaped}}}"))
        else:
            fields.append((name, f"{{{escaped}}}"))

    # Core
    add("title",   ref.title,   protect=True)
    add("author",  _format_authors(ref))
    add("editor",  _format_editors(ref))
    add("year",    ref.year)
    add("month",   ref.month)

    # Journal/conference
    add("journal",   ref.journal    or ref.container_title)
    add("booktitle", ref.event_name or (ref.container_title if entry_type in ("incollection", "inproceedings") else None))
    add("volume",  ref.volume)
    add("number",  ref.issue)
    add("pages",   ref.pages or ref.article_number)

    # Book
    add("publisher", ref.publisher)
    add("address",   ref.place)
    add("edition",   ref.edition)
    add("series",    ref.series, protect=True)
    add("isbn",      ref.isbn)
    add("issn",      ref.issn)

    # Identifiers
    add("doi",    ref.doi)
    add("url",    ref.url or ref.oa_url)
    if ref.eissn:
        add("eissn", ref.eissn)
    if ref.arxiv_id:
        add("eprint",       ref.arxiv_id)
        add("archiveprefix", "arXiv")
        add("primaryclass",  ref.keywords[0] if ref.keywords else None)

    # PMID / PMCID in note field (standard practice)
    note_parts = []
    if ref.pmid:
        note_parts.append(f"PMID: {ref.pmid}")
    if ref.pmcid:
        note_parts.append(f"PMCID: {ref.pmcid}")
    if note_parts:
        add("note", ". ".join(note_parts))

    # Article number (for journals that use it instead of pages)
    if ref.article_number and not ref.pages:
        add("eid", ref.article_number)

    # Abstract
    add("abstract",  ref.abstract)

    # Keywords
    if ref.keywords:
        add("keywords", ", ".join(ref.keywords))

    # Language
    add("language", ref.language)

    # License
    add("license", ref.license)

    # Format the entry
    lines = [f"@{entry_type}{{{cite_key},"]
    max_name_len = max((len(name) for name, _ in fields), default=0)
    for name, value in fields:
        padding = " " * (max_name_len - len(name))
        lines.append(f"  {name}{padding} = {value},")

    # Remove trailing comma from last field
    if len(lines) > 1:
        lines[-1] = lines[-1].rstrip(",")

    lines.append("}")
    return "\n".join(lines)


def to_bibtex_string(refs: Union[Reference, List[Reference]]) -> str:
    """Convert one or more References to a BibTeX string."""
    if isinstance(refs, Reference):
        refs = [refs]
    return "\n\n".join(_ref_to_bibtex_entry(r) for r in refs)


def export_bibtex_file(
    refs: Union[Reference, List[Reference]],
    path: Union[str, Path],
) -> None:
    """Write References to a .bib file."""
    content = to_bibtex_string(refs)
    Path(path).write_text(content + "\n", encoding="utf-8")
