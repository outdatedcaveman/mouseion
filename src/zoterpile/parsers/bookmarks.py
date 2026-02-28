"""
Chrome / Firefox / Safari Netscape-format HTML bookmark parser.

When you export bookmarks from Chrome (Bookmarks Manager → Export),
you get a "Netscape Bookmark File" — an HTML file with <DT><A> links.
Each link may be a paper, a news article, or anything.

Strategy
--------
1. Parse all <A> tags from the file.
2. For each URL, try to extract embedded identifiers (DOI, arXiv, PMID).
3. If the URL looks academic, create a seed Reference with URL + title.
4. Non-academic URLs are still included as WEBSITE references so the
   user can optionally enrich them later.

The parser also handles folder structure (bookmark folders → tags).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from bs4 import BeautifulSoup

from ..models import RefType, Reference
from ..input import _DOI_BARE_RE, _ARXIV_URL_RE, _PMID_URL_RE, _ACADEMIC_URL_PATTERNS


_NETSCAPE_MARKER = re.compile(r"NETSCAPE-Bookmark-file", re.IGNORECASE)


def _is_bookmarks_file(html: str) -> bool:
    return bool(_NETSCAPE_MARKER.search(html[:500]))


def _folder_path(tag) -> List[str]:
    """Walk up the DOM tree to collect parent <H3> folder names as tag breadcrumbs."""
    folders: List[str] = []
    node = tag.parent
    while node and node.name:
        if node.name == "dl":
            prev = node.find_previous_sibling("h3")
            if prev:
                folders.append(prev.get_text(strip=True))
        node = node.parent
    folders.reverse()
    return folders


def _url_to_seed(url: str, title: str, folders: List[str]) -> Reference:
    ref = Reference()
    ref.url = url
    ref.title = title.strip() or None
    ref.ref_type = RefType.WEBSITE

    # Extract identifiers from URL
    m = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", url, re.I)
    if m:
        ref.doi = m.group(1).rstrip(".,;)")
        ref.ref_type = RefType.UNKNOWN   # will be resolved by enrichment

    m = _ARXIV_URL_RE.search(url)
    if m:
        ref.arxiv_id = m.group(1).split("v")[0]
        ref.ref_type = RefType.PREPRINT

    m = _PMID_URL_RE.search(url)
    if m:
        ref.pmid = m.group(1)
        ref.ref_type = RefType.JOURNAL

    # Also check for DOI in the URL path itself (some publisher links embed it)
    if not ref.doi:
        m = _DOI_BARE_RE.search(url)
        if m:
            candidate = m.group(1).rstrip(".,;)")
            # Basic sanity: should contain a slash
            if "/" in candidate:
                ref.doi = candidate

    # Convert folder names to tags (stored in keywords for now,
    # the CLI/db layer will turn them into proper tags)
    ref.keywords = [f for f in folders if f]

    ref.sources["bookmark_import"] = 0.3
    return ref


def parse_bookmarks_string(html: str) -> List[Reference]:
    """
    Parse a Netscape bookmark HTML string.
    Raises ValueError if the string doesn't look like a bookmarks file.
    """
    if not _is_bookmarks_file(html):
        raise ValueError("Not a Netscape bookmark file")

    soup = BeautifulSoup(html, "lxml")
    refs: List[Reference] = []
    seen_urls: set = set()

    for a in soup.find_all("a", href=True):
        url = a["href"].strip()
        if not url or url.startswith("javascript:"):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = a.get_text(strip=True)
        folders = _folder_path(a)
        ref = _url_to_seed(url, title, folders)
        refs.append(ref)

    return refs


def parse_bookmarks_file(path: str | Path) -> List[Reference]:
    """Parse a Netscape bookmark HTML file."""
    path = Path(path)
    html = path.read_text(encoding="utf-8", errors="replace")
    return parse_bookmarks_string(html)
