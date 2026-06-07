"""
PDF metadata extractor.

Extracts bibliographic metadata from PDF files.
Scans the document metadata (XMP/Info dict) and the first pages
to find DOIs, titles, author patterns, and other identifiers.

Supports pymupdf (PyMuPDF) as the primary backend, with pypdf as fallback.
If neither is installed, returns a minimal Reference with just the filename.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from ..models import Author, Reference


_DOI_RE = re.compile(r"\b(10\.\d{4,}/[^\s\"'<>\]\)]+)", re.IGNORECASE)
_ARXIV_RE = re.compile(r"arXiv:(\d{4}\.\d{4,5})", re.IGNORECASE)
_PMID_RE = re.compile(r"PMID:\s*(\d+)", re.IGNORECASE)
_ISBN_RE = re.compile(r"ISBN[:\- ]*(\d[\d\- ]{8,}[\dXx])")


def parse_pdf_file(path: str | Path) -> Reference:
    """
    Extract bibliographic metadata from a PDF file.

    Returns a (typically partial) Reference intended for enrichment.
    """
    path = Path(path)
    ref = Reference()
    ref.url = path.as_uri()

    # Try pymupdf first (best quality), then pypdf, then filename-only
    text = ""
    meta = {}

    try:
        text, meta = _extract_pymupdf(path)
    except ImportError:
        try:
            text, meta = _extract_pypdf(path)
        except ImportError:
            ref.title = _title_from_filename(path)
            ref.sources["pdf_input"] = 0.1
            return ref
    except Exception:
        try:
            text, meta = _extract_pypdf(path)
        except Exception:
            ref.title = _title_from_filename(path)
            ref.sources["pdf_input"] = 0.1
            return ref

    # --- Apply metadata ---
    title = meta.get("title", "")
    if title and len(title) > 3 and not title.lower().startswith("untitled"):
        ref.title = title

    author_raw = meta.get("author", "")
    if author_raw:
        sep = re.split(r";\s*|,\s*and\s*|\s+and\s+", author_raw)
        ref.authors = [Author.from_bibtex_str(a) for a in sep if a.strip()]

    keywords = meta.get("keywords", "")
    if keywords:
        ref.keywords = [k.strip() for k in re.split(r"[,;]", keywords) if k.strip()]

    year = meta.get("year")
    if year:
        ref.year = year

    # --- Scan text for identifiers ---
    if text:
        m = _DOI_RE.search(text)
        if m:
            ref.doi = m.group(1).rstrip(".,;)")

        m = _ARXIV_RE.search(text)
        if m:
            ref.arxiv_id = m.group(1)

        m = _PMID_RE.search(text)
        if m:
            ref.pmid = m.group(1)

        m = _ISBN_RE.search(text)
        if m:
            ref.isbn = re.sub(r"[\s-]", "", m.group(1))

        # If no title from metadata, try first substantial line of text
        if not ref.title:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for line in lines[:10]:
                if len(line) > 10 and not line.startswith("http"):
                    ref.title = line[:200]
                    break

    if not ref.title:
        ref.title = _title_from_filename(path)

    ref.sources["pdf_input"] = 0.3
    ref.normalize()
    return ref


def parse_pdf_bytes(data: bytes, filename: str = "upload.pdf") -> Reference:
    """Extract metadata from PDF bytes (for file uploads)."""
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        ref = parse_pdf_file(tmp)
        # Replace file:// URI with the original filename
        ref.url = ""
        ref.pdf_path = filename
        return ref
    finally:
        os.unlink(tmp)


def _extract_pymupdf(path: Path) -> tuple:
    """Extract text + metadata using pymupdf (PyMuPDF)."""
    import pymupdf
    doc = pymupdf.open(str(path))
    meta = doc.metadata or {}

    title = _clean(meta.get("title", ""))
    author = _clean(meta.get("author", ""))
    keywords = _clean(meta.get("keywords", "") or meta.get("subject", ""))

    year = None
    for date_key in ("creationDate", "modDate"):
        d = meta.get(date_key, "")
        if d:
            m = re.search(r"(\d{4})", d)
            if m:
                yr = int(m.group(1))
                if 1950 <= yr <= 2030:
                    year = yr
                    break

    # Extract text from first 3 pages
    text_parts = []
    for i in range(min(3, len(doc))):
        try:
            text_parts.append(doc[i].get_text())
        except Exception:
            continue
        if sum(len(t) for t in text_parts) > 8000:
            break
    doc.close()

    return "\n".join(text_parts), {
        "title": title, "author": author,
        "keywords": keywords, "year": year,
    }


def _extract_pypdf(path: Path) -> tuple:
    """Extract text + metadata using pypdf (fallback)."""
    from pypdf import PdfReader
    reader = PdfReader(str(path), strict=False)
    meta = reader.metadata or {}

    title = _clean(meta.get("/Title", "") or meta.get("Title", ""))
    author = _clean(meta.get("/Author", "") or meta.get("Author", ""))
    keywords = _clean(meta.get("/Subject", "") or "")

    year = None
    creation = _clean(meta.get("/CreationDate", "") or "")
    if creation:
        m = re.search(r"(\d{4})", creation)
        if m:
            yr = int(m.group(1))
            if 1950 <= yr <= 2030:
                year = yr

    text_parts = []
    for page in reader.pages[:3]:
        try:
            text_parts.append(page.extract_text() or "")
        except Exception:
            continue
        if sum(len(t) for t in text_parts) > 8000:
            break

    return "\n".join(text_parts), {
        "title": title, "author": author,
        "keywords": keywords, "year": year,
    }


def _title_from_filename(path: Path) -> str:
    """Derive a rough title from the filename."""
    return path.stem.replace("_", " ").replace("-", " ").strip()


def _clean(s: str) -> str:
    if not s:
        return ""
    s = s.encode("utf-8", errors="ignore").decode("utf-8")
    return s.strip()
