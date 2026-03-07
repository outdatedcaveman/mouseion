"""
PDF metadata extractor.

Extracts bibliographic metadata from PDF files using pypdf.
Scans both the document metadata (XMP/Info dict) and the first page
text to find DOIs, titles, and author patterns.

pypdf is a soft dependency — if not installed, returns a minimal Reference
with just the file URL.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..models import Author, Reference


_DOI_RE = re.compile(r"\b(10\.\d{4,}/[^\s\"'<>\]\)]+)", re.IGNORECASE)
_ARXIV_RE = re.compile(r"arXiv:(\d{4}\.\d{4,5})", re.IGNORECASE)


def parse_pdf_file(path: str | Path) -> Reference:
    """
    Extract bibliographic metadata from a PDF file.

    Returns a (typically partial) Reference intended for enrichment.
    """
    path = Path(path)
    ref = Reference()
    ref.url = path.as_uri()

    try:
        from pypdf import PdfReader
    except ImportError:
        # pypdf not installed — return minimal reference with path as URL
        ref.title = path.stem.replace("_", " ").replace("-", " ")
        ref.sources["pdf_input"] = 0.1
        return ref

    try:
        reader = PdfReader(str(path), strict=False)
    except Exception:
        ref.title = path.stem
        ref.sources["pdf_input"] = 0.1
        return ref

    # --- Document Info dict ---
    meta = reader.metadata or {}

    title = _clean_meta(meta.get("/Title", "") or meta.get("Title", ""))
    if title and len(title) > 3:
        ref.title = title

    author_raw = _clean_meta(meta.get("/Author", "") or meta.get("Author", ""))
    if author_raw:
        # Try splitting on common separators
        sep = re.split(r";\s*|,\s*and\s*|\s+and\s+", author_raw)
        ref.authors = [Author.from_bibtex_str(a) for a in sep if a.strip()]

    subject = _clean_meta(meta.get("/Subject", "") or "")
    if subject:
        ref.keywords = [k.strip() for k in re.split(r"[,;]", subject) if k.strip()]

    # Year from CreationDate (/CreationDate format: D:YYYYMMDDHHmmSS)
    creation = _clean_meta(meta.get("/CreationDate", "") or "")
    if creation:
        m = re.search(r"(\d{4})", creation)
        if m:
            yr = int(m.group(1))
            if 1950 <= yr <= 2030:
                ref.year = yr

    # --- Scan first 2 pages for identifiers ---
    first_pages_text = ""
    for page in reader.pages[:2]:
        try:
            first_pages_text += (page.extract_text() or "")
            if len(first_pages_text) > 5000:
                break
        except Exception:
            continue

    if first_pages_text:
        # DOI
        m = _DOI_RE.search(first_pages_text)
        if m:
            ref.doi = m.group(1).rstrip(".,;)")

        # arXiv
        m = _ARXIV_RE.search(first_pages_text)
        if m:
            ref.arxiv_id = m.group(1)

        # If no title from metadata, try first non-trivial line of text
        if not ref.title:
            lines = [l.strip() for l in first_pages_text.splitlines() if l.strip()]
            for line in lines[:10]:
                if len(line) > 10 and not line.startswith("http"):
                    ref.title = line[:200]
                    break

    ref.sources["pdf_input"] = 0.3
    ref.normalize()
    return ref


def _clean_meta(s: str) -> str:
    if not s:
        return ""
    # Remove BOM and control characters
    s = s.encode("utf-8", errors="ignore").decode("utf-8")
    return s.strip()
