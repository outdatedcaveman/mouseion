"""
Obsidian integration.

Writes one Markdown file per reference into an Obsidian vault directory.
Uses YAML frontmatter for metadata and Wikilinks for cross-referencing.

No API required — just filesystem access to the vault.

File naming (configurable):
  - {cite_key}.md  (default)
  - {author} ({year}) {title_short}.md

Setup:
    [obsidian]
    vault_path        = "/path/to/MyVault"
    notes_folder      = "References"
    filename_template = "{cite_key}"
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from ..models import Reference
from .base import BaseIntegration


def _safe_filename(s: str) -> str:
    """Make a string safe for use as a filename (Obsidian-compatible)."""
    s = re.sub(r'[\\/:*?"<>|#^[\]]', "", s)
    return s.strip(".").strip()[:100] or "untitled"


def _make_filename(ref: Reference, template: str) -> str:
    cite_key = ref.cite_key or ref.auto_cite_key()
    author   = ref.authors[0].family if ref.authors else "Unknown"
    year     = str(ref.year) if ref.year else "n.d."
    title_short = " ".join((ref.title or "").split()[:6])

    name = template.format(
        cite_key=cite_key,
        author=author,
        year=year,
        title=title_short,
    )
    return _safe_filename(name) + ".md"


def _yaml_str(value: str) -> str:
    """Quote a YAML string value if needed."""
    if not value:
        return '""'
    if any(c in value for c in ':#{}[]|>&!\'"%@`'):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _ref_to_markdown(ref: Reference) -> str:
    """Render a Reference as Obsidian-flavoured Markdown with YAML frontmatter."""
    lines: List[str] = ["---"]

    # --- YAML frontmatter ---
    cite_key = ref.cite_key or ref.auto_cite_key()
    lines.append(f"cite_key: {_yaml_str(cite_key)}")
    lines.append(f"title: {_yaml_str(ref.title or '')}")

    if ref.authors:
        lines.append("authors:")
        for a in ref.authors:
            name = a.to_bibtex_str()
            lines.append(f"  - {_yaml_str(name)}")

    if ref.year:
        lines.append(f"year: {ref.year}")

    lines.append(f"type: {ref.ref_type.value}")

    if ref.doi:
        lines.append(f"doi: {_yaml_str(ref.doi)}")

    if ref.journal or ref.container_title:
        lines.append(f"journal: {_yaml_str(ref.journal or ref.container_title or '')}")

    if ref.volume:
        lines.append(f"volume: {_yaml_str(ref.volume)}")
    if ref.issue:
        lines.append(f"issue: {_yaml_str(ref.issue)}")
    if ref.pages:
        lines.append(f"pages: {_yaml_str(ref.pages)}")

    if ref.publisher:
        lines.append(f"publisher: {_yaml_str(ref.publisher)}")

    if ref.arxiv_id:
        lines.append(f"arxiv_id: {_yaml_str(ref.arxiv_id)}")
    if ref.pmid:
        lines.append(f"pmid: {_yaml_str(ref.pmid)}")

    if ref.keywords:
        lines.append("tags:")
        for kw in ref.keywords[:20]:
            safe_kw = re.sub(r"\s+", "-", kw.lower().strip())
            safe_kw = re.sub(r"[^\w\-]", "", safe_kw)
            lines.append(f"  - {safe_kw}")

    if ref.open_access is not None:
        lines.append(f"open_access: {str(ref.open_access).lower()}")
    if ref.citation_count is not None:
        lines.append(f"citation_count: {ref.citation_count}")
    if ref.language:
        lines.append(f"language: {_yaml_str(ref.language)}")
    if ref.url or ref.oa_url:
        lines.append(f"url: {_yaml_str(ref.url or ref.oa_url or '')}")
    if ref.oa_url:
        lines.append(f"pdf_url: {_yaml_str(ref.oa_url)}")

    lines.append(f"completeness: {ref.completeness:.2f}")
    lines.append("---")
    lines.append("")

    # --- Note body ---
    title_display = ref.title or "(untitled)"
    lines.append(f"# {title_display}")
    lines.append("")

    # Authors as wikilinks
    if ref.authors:
        author_links = " · ".join(
            f"[[{a.full_name}]]" for a in ref.authors
        )
        lines.append(f"**Authors:** {author_links}")

    if ref.year:
        lines.append(f"**Year:** {ref.year}")

    if ref.doi:
        lines.append(f"**DOI:** [10.{ref.doi.split('10.', 1)[-1]}](https://doi.org/{ref.doi})")

    if ref.journal or ref.container_title:
        lines.append(f"**Published in:** *{ref.journal or ref.container_title}*")

    bib_parts = []
    if ref.volume: bib_parts.append(f"Vol. {ref.volume}")
    if ref.issue:  bib_parts.append(f"No. {ref.issue}")
    if ref.pages:  bib_parts.append(f"pp. {ref.pages}")
    if bib_parts:
        lines.append(f"**Bibliographic:** {', '.join(bib_parts)}")

    if ref.open_access:
        oa_line = "**Open Access:** ✓"
        if ref.oa_url:
            oa_line += f" [PDF]({ref.oa_url})"
        lines.append(oa_line)

    if ref.citation_count is not None:
        lines.append(f"**Citations:** {ref.citation_count:,}")

    lines.append("")

    if ref.abstract:
        lines.append("## Abstract")
        lines.append("")
        lines.append(ref.abstract)
        lines.append("")

    if ref.keywords:
        lines.append("## Keywords")
        lines.append("")
        kw_tags = " ".join(f"#{re.sub(r'[^\\w]', '-', kw.lower())}" for kw in ref.keywords)
        lines.append(kw_tags)
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("<!-- Add your notes here -->")
    lines.append("")

    return "\n".join(lines)


class ObsidianIntegration(BaseIntegration):
    """Write References as Markdown notes to an Obsidian vault."""

    def __init__(
        self,
        vault_path: Optional[str] = None,
        notes_folder: Optional[str] = None,
        filename_template: Optional[str] = None,
    ) -> None:
        from ..config import get_config
        cfg = get_config()
        self._vault_path       = Path(vault_path or cfg.obsidian_vault_path or ".").expanduser()
        self._notes_folder     = notes_folder    or cfg.obsidian_notes_folder or "References"
        self._filename_template = filename_template or cfg.obsidian_filename_template or "{cite_key}"

    @property
    def _notes_dir(self) -> Path:
        d = self._vault_path / self._notes_folder
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def is_configured(self) -> bool:
        return self._vault_path.exists()

    async def push(self, refs: List[Reference]) -> List[str]:
        """
        Write one .md file per reference.
        Returns list of file paths (as strings).
        """
        if not await self.is_configured():
            raise RuntimeError(
                f"Obsidian vault not found at {self._vault_path}. "
                "Set obsidian_vault_path in config."
            )
        paths: List[str] = []
        for ref in refs:
            filename = _make_filename(ref, self._filename_template)
            dest     = self._notes_dir / filename
            content  = _ref_to_markdown(ref)
            dest.write_text(content, encoding="utf-8")
            paths.append(str(dest))
        return paths
