"""
Markdown exporter.

Produces human-readable Markdown summaries of Reference objects.
Includes completeness score and source provenance — useful for reviewing
enrichment results before importing into a reference manager.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Union

from ..models import Reference


_COMPLETENESS_BARS = {
    0.9: "█████████░",
    0.8: "████████░░",
    0.7: "███████░░░",
    0.6: "██████░░░░",
    0.5: "█████░░░░░",
    0.4: "████░░░░░░",
    0.3: "███░░░░░░░",
    0.2: "██░░░░░░░░",
    0.1: "█░░░░░░░░░",
    0.0: "░░░░░░░░░░",
}


def _completeness_bar(score: float) -> str:
    for threshold in sorted(_COMPLETENESS_BARS.keys(), reverse=True):
        if score >= threshold:
            return _COMPLETENESS_BARS[threshold]
    return _COMPLETENESS_BARS[0.0]


def _ref_to_markdown(ref: Reference, index: int | None = None) -> str:
    lines: List[str] = []

    # Header
    title = ref.title or "_[no title]_"
    if index is not None:
        lines.append(f"## {index}. {title}")
    else:
        lines.append(f"## {title}")
    lines.append("")

    # Completeness
    score = ref.completeness
    bar   = _completeness_bar(score)
    lines.append(f"**Completeness:** {bar} {score:.0%}")
    lines.append("")

    # Authors
    if ref.authors:
        author_str = "; ".join(
            f"{a.full_name}" + (f" ([ORCID](https://orcid.org/{a.orcid}))" if a.orcid else "")
            for a in ref.authors
        )
        lines.append(f"**Authors:** {author_str}")

    # Editors
    if ref.editors:
        editor_str = "; ".join(e.full_name for e in ref.editors)
        lines.append(f"**Editors:** {editor_str}")

    # Year
    if ref.year:
        lines.append(f"**Year:** {ref.year}")

    # Type
    lines.append(f"**Type:** {ref.ref_type.value}")

    # Conference / event
    if ref.event_name:
        lines.append(f"**Conference:** {ref.event_name}")

    # Journal / container
    journal = ref.journal or ref.container_title
    if journal:
        abbrev = f" ({ref.journal_abbrev})" if ref.journal_abbrev else ""
        lines.append(f"**Journal/Venue:** {journal}{abbrev}")

    # Volume / issue / pages
    if ref.volume or ref.issue or ref.pages:
        bib_parts = []
        if ref.volume:
            bib_parts.append(f"vol. {ref.volume}")
        if ref.issue:
            bib_parts.append(f"no. {ref.issue}")
        if ref.pages:
            bib_parts.append(f"pp. {ref.pages}")
        elif ref.article_number:
            bib_parts.append(f"art. {ref.article_number}")
        lines.append(f"**Bibliographic:** {', '.join(bib_parts)}")

    # Publisher
    if ref.publisher:
        pub_line = ref.publisher
        if ref.place:
            pub_line += f", {ref.place}"
        lines.append(f"**Publisher:** {pub_line}")

    lines.append("")

    # Identifiers
    ids: List[str] = []
    if ref.doi:
        ids.append(f"[DOI: {ref.doi}](https://doi.org/{ref.doi})")
    if ref.pmid:
        ids.append(f"[PMID: {ref.pmid}](https://pubmed.ncbi.nlm.nih.gov/{ref.pmid}/)")
    if ref.arxiv_id:
        ids.append(f"[arXiv: {ref.arxiv_id}](https://arxiv.org/abs/{ref.arxiv_id})")
    if ref.isbn:
        ids.append(f"ISBN: {ref.isbn}")
    if ids:
        lines.append("**Identifiers:** " + " | ".join(ids))

    # Open access
    if ref.open_access:
        oa_str = "Yes"
        if ref.oa_url:
            oa_str = f"[Yes (PDF)]({ref.oa_url})"
        lines.append(f"**Open Access:** {oa_str}")

    # Citation count
    if ref.citation_count is not None:
        lines.append(f"**Citations:** {ref.citation_count:,}")

    # Keywords
    if ref.keywords:
        lines.append(f"**Keywords:** {', '.join(ref.keywords)}")

    # Language
    if ref.language:
        lines.append(f"**Language:** {ref.language}")

    # License
    if ref.license:
        lines.append(f"**License:** {ref.license}")

    lines.append("")

    # Abstract
    if ref.abstract:
        lines.append("**Abstract:**")
        lines.append("")
        lines.append(f"> {ref.abstract}")
        lines.append("")

    # Sources
    if ref.sources:
        src_parts = [
            f"{src} ({conf:.0%})" for src, conf in sorted(
                ref.sources.items(), key=lambda x: -x[1]
            )
        ]
        lines.append(f"**Sources:** {', '.join(src_parts)}")

    # BibTeX cite key
    cite_key = ref.cite_key or ref.auto_cite_key()
    lines.append(f"**Cite key:** `{cite_key}`")

    lines.append("")
    lines.append("---")
    lines.append("")

    return "\n".join(lines)


def to_markdown_string(refs: Union[Reference, List[Reference]]) -> str:
    """Convert one or more References to Markdown."""
    if isinstance(refs, Reference):
        return _ref_to_markdown(refs)
    return "".join(_ref_to_markdown(r, i + 1) for i, r in enumerate(refs))


def export_markdown_file(
    refs: Union[Reference, List[Reference]],
    path: Union[str, Path],
) -> None:
    """Write References to a Markdown file."""
    content = to_markdown_string(refs)
    Path(path).write_text(content, encoding="utf-8")
