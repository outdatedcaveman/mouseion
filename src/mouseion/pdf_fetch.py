"""
PDF fetcher.

Tries multiple strategies to find and download the open-access PDF for a
reference, in order of reliability:

  1. oa_url already set on the Reference (from provider data)
  2. arXiv PDF (always free)
  3. Unpaywall API (best OA discovery tool, free, DOI-based)
  4. Semantic Scholar openAccessPdf (already checked during enrichment)

The downloaded PDF is saved to the configured pdf_storage_path and the
Reference is updated with pdf_local.

All functions are async.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

import httpx

from .models import Reference


_UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
_USER_AGENT = (
    "mouseion/0.1 (https://github.com/outdatedcaveman/mouseion; "
    "reference enrichment tool)"
)


async def fetch_pdf(
    ref: Reference,
    storage_dir: Optional[str | Path] = None,
    email: Optional[str] = None,
) -> Optional[Path]:
    """
    Try to find and download the PDF for `ref`.

    Returns the local Path on success, None otherwise.
    Saves the file to `storage_dir` (defaults to config value).
    """
    from .config import get_config
    cfg = get_config()
    storage = Path(storage_dir or cfg.pdf_storage_path).expanduser()
    storage.mkdir(parents=True, exist_ok=True)
    _email = email or cfg.openalex_email or cfg.crossref_email

    # Skip if the expected file already exists on disk — idempotent re-runs.
    expected_path = storage / (_make_filename(ref) + ".pdf")
    if expected_path.exists() and expected_path.stat().st_size >= 1024:
        return expected_path

    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
        timeout=60,
    ) as client:
        # --- Strategy 1: Use oa_url already set on reference ---
        if ref.oa_url:
            path = await _download(client, ref.oa_url, storage, ref)
            if path:
                return path

        # --- Strategy 2: arXiv ---
        if ref.arxiv_id:
            url = f"https://arxiv.org/pdf/{ref.arxiv_id}"
            path = await _download(client, url, storage, ref)
            if path:
                return path

        # --- Strategy 3: Unpaywall ---
        if ref.doi and _email:
            oa_url = await _unpaywall_lookup(client, ref.doi, _email)
            if oa_url:
                ref.oa_url = oa_url
                path = await _download(client, oa_url, storage, ref)
                if path:
                    return path

    return None


async def _unpaywall_lookup(
    client: httpx.AsyncClient, doi: str, email: str
) -> Optional[str]:
    """Query Unpaywall for the best OA PDF URL."""
    try:
        resp = await client.get(
            f"{_UNPAYWALL_BASE}/{doi}",
            params={"email": email},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("is_oa"):
            return None
        # Prefer PDF links, then landing pages
        best = data.get("best_oa_location") or {}
        return best.get("url_for_pdf") or best.get("url") or None
    except Exception:
        return None


async def _download(
    client: httpx.AsyncClient,
    url: str,
    storage: Path,
    ref: Reference,
) -> Optional[Path]:
    """
    Stream-download a PDF from ``url`` and save to ``storage``.

    Uses chunked streaming (64 KB chunks) so large PDFs are never fully
    buffered in memory.  Returns the local Path on success, None otherwise.
    Cleans up any partially-written file on failure.
    """
    path: Optional[Path] = None
    try:
        async with client.stream("GET", url, timeout=120) as resp:
            if resp.status_code != 200:
                return None
            content_type = resp.headers.get("content-type", "")
            if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
                return None

            filename = _make_filename(ref) + ".pdf"
            path = storage / filename
            bytes_written = 0
            with path.open("wb") as fh:
                async for chunk in resp.aiter_bytes(65536):  # 64 KB chunks
                    fh.write(chunk)
                    bytes_written += len(chunk)

        if bytes_written < 1024:  # suspiciously small — reject
            path.unlink(missing_ok=True)
            return None
        return path
    except Exception:
        if path is not None:
            path.unlink(missing_ok=True)
        return None


def _make_filename(ref: Reference) -> str:
    """Create a filesystem-safe filename for a reference's PDF."""
    parts = []
    if ref.authors:
        parts.append(_safe(ref.authors[0].family))
    if ref.year:
        parts.append(str(ref.year))
    if ref.title:
        # Take first 4 significant words
        words = [w for w in ref.title.split()[:6] if len(w) > 2]
        parts.append("_".join(_safe(w) for w in words[:4]))
    name = "_".join(p for p in parts if p) or "paper"
    # Use DOI hash as suffix to guarantee uniqueness
    if ref.doi:
        suffix = hashlib.md5(ref.doi.encode()).hexdigest()[:6]
        name = f"{name}_{suffix}"
    return name[:120]   # cap length


def _safe(s: str) -> str:
    import re
    return re.sub(r"[^\w\-]", "", s.replace(" ", "_"))[:30]


# ---------------------------------------------------------------------------
# Unpaywall as a standalone enrichment query
# ---------------------------------------------------------------------------

async def enrich_from_unpaywall(ref: Reference, email: str) -> None:
    """
    Query Unpaywall for `ref.doi` and update oa_url / open_access fields in-place.
    No-op if ref has no DOI or no email is configured.
    """
    if not ref.doi or not email:
        return
    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
        timeout=15,
    ) as client:
        oa_url = await _unpaywall_lookup(client, ref.doi, email)
        if oa_url:
            ref.open_access = True
            ref.oa_url = ref.oa_url or oa_url
