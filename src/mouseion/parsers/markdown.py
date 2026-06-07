"""
Markdown parser for reference imports.

Parses Markdown files that contain references in various formats:
  - YAML front-matter blocks (common in Obsidian, Jekyll, academic note tools)
  - Inline DOIs, arXiv IDs, PMIDs, URLs
  - Structured Markdown lists with key: value pairs
  - Reference lists with formatted citations

Example supported formats:

    ---
    title: "My Paper Title"
    doi: 10.1234/example
    authors: ["Smith, John", "Doe, Jane"]
    year: 2023
    ---

    - **Smith et al. (2023)** — "Some title" — 10.1234/foo
    - DOI: 10.5678/bar
    - https://arxiv.org/abs/2301.12345

Also handles YAML-only files (multiple documents separated by ---).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Union

from ..models import Author, Reference


def _parse_yaml_block(text: str) -> dict:
    """Parse a YAML-like front-matter block into a dict (no PyYAML dependency)."""
    result = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^(\w[\w_-]*)\s*:\s*(.+)$', line)
        if m:
            key = m.group(1).strip()
            val = m.group(2).strip()
            # Handle quoted strings
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            # Handle arrays: ["a", "b"] or [a, b]
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1]
                items = []
                for item in re.split(r',\s*', inner):
                    item = item.strip().strip('"').strip("'")
                    if item:
                        items.append(item)
                val = items
            result[key] = val
    return result


def _yaml_to_ref(d: dict) -> Optional[Reference]:
    """Convert a YAML dict to a Reference."""
    if not d:
        return None
    # We reuse the JSON parser's logic since the dict structure is the same
    from .json_parser import _parse_one
    return _parse_one(d)


def _extract_frontmatter_refs(text: str) -> List[Reference]:
    """Extract references from YAML front-matter blocks (--- delimited)."""
    refs = []
    # Split on --- boundaries
    blocks = re.split(r'^---\s*$', text, flags=re.MULTILINE)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        d = _parse_yaml_block(block)
        if d and ("title" in d or "doi" in d or "url" in d or "arxiv_id" in d):
            ref = _yaml_to_ref(d)
            if ref:
                refs.append(ref)
    return refs


def _extract_inline_ids(text: str) -> List[Reference]:
    """Extract DOIs, arXiv IDs, PMIDs, and URLs from Markdown body text."""
    from ..input import parse_input
    # Collect all identifiers from lines that aren't YAML
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("---"):
            continue
        # Strip Markdown formatting
        line = re.sub(r'\*\*|__|\*|_|`|#{1,6}\s*', '', line)
        # Extract DOIs, URLs, etc. from the line
        if re.search(r'10\.\d{4,}/', line) or \
           re.search(r'arxiv\.org|arXiv:', line, re.I) or \
           re.search(r'PMID:\s*\d+', line, re.I) or \
           re.search(r'https?://\S+', line):
            lines.append(line)
    if not lines:
        return []
    return parse_input("\n".join(lines))


def parse_markdown_string(text: str) -> List[Reference]:
    """
    Parse a Markdown string and extract references.

    Tries YAML front-matter first, then inline identifiers.
    """
    refs = []

    # 1. YAML front-matter blocks
    if "---" in text:
        refs.extend(_extract_frontmatter_refs(text))

    # 2. Inline DOIs/URLs/IDs (only add if not already found via YAML)
    existing_ids = set()
    for r in refs:
        if r.doi:
            existing_ids.add(r.doi)
        if r.url:
            existing_ids.add(r.url)
    inline = _extract_inline_ids(text)
    for r in inline:
        if r.doi and r.doi in existing_ids:
            continue
        if r.url and r.url in existing_ids:
            continue
        refs.append(r)

    return refs


def parse_markdown_file(path: Union[str, Path]) -> List[Reference]:
    """Parse a Markdown file."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_markdown_string(text)
