"""
PDF management for Mouseion.

Handles PDF storage directory configuration, filename sanitization,
and async downloading of open-access PDFs.

PDFs are stored in a local folder that defaults to:
  - ~/Google Drive/Mouseion PDFs/  (Windows, if Google Drive folder exists)
  - ~/Mouseion PDFs/               (fallback)

The folder can be overridden via config (pdf_storage_path) or the
/api/settings/pdf-dir endpoint.

Downloaded files are named consistently:
  {first_author}_{year}_{short_title}.pdf
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from .semaphore import SafeSemaphore
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import httpx

from .models import Reference
from .network_budget import bucket_from_url, network_slot

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "mouseion/0.1 (https://github.com/outdatedcaveman/mouseion; "
    "reference enrichment tool)"
)

# Concurrency limit for batch downloads
_MAX_CONCURRENT = 12



# ---------------------------------------------------------------------------
# PDF directory helpers
# ---------------------------------------------------------------------------

def get_pdf_dir() -> Path:
    """Return the configured PDF storage directory, creating it if needed.

    Resolution order:
      1. ``pdf_storage_path`` from config / settings (if non-default and non-empty)
      2. ``~/Google Drive/Mouseion PDFs/`` if the Google Drive desktop folder exists
      3. ``~/Mouseion PDFs/``
    """
    from .config import get_config

    cfg = get_config()
    configured = cfg.pdf_storage_path

    # Check if it's the built-in default (which we want to override with
    # our smarter logic) vs. a user-customised path
    default_pdfs = str(Path.home() / ".local" / "share" / "mouseion" / "pdfs")
    if configured and configured != default_pdfs:
        pdf_dir = Path(configured).expanduser()
    else:
        # Try Google Drive desktop folder (common Windows path)
        gdrive = Path.home() / "Google Drive"
        if not gdrive.exists():
            # Also check the newer "My Drive" path
            gdrive = Path.home() / "Google Drive" / "My Drive"
        if gdrive.exists():
            pdf_dir = gdrive / "Mouseion PDFs"
        else:
            pdf_dir = Path.home() / "Mouseion PDFs"

    pdf_dir.mkdir(parents=True, exist_ok=True)
    return pdf_dir


def set_pdf_dir(path: str) -> Path:
    """Persist a custom PDF directory in config and return the resolved path."""
    from .config import get_config, save_config

    cfg = get_config()
    resolved = Path(path).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    cfg.pdf_storage_path = str(resolved)
    save_config(cfg)
    return resolved


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------

def sanitize_filename(ref: Reference) -> str:
    """Generate a clean, consistent PDF filename from reference metadata.

    Format: ``{first_author}_{year}_{short_title}.pdf``

    The filename is filesystem-safe (no special chars) and capped to a
    reasonable length.
    """
    parts: list[str] = []

    # First author family name
    if ref.authors:
        family = _safe(ref.authors[0].family)
        if family:
            parts.append(family)

    # Year
    if ref.year:
        parts.append(str(ref.year))

    # Short title: first 4 meaningful words
    if ref.title:
        stop_words = {"a", "an", "the", "of", "in", "on", "for", "and", "with", "to", "is", "by"}
        words = [
            _safe(w) for w in ref.title.split()
            if len(w) > 2 and w.lower() not in stop_words
        ]
        short_title = "_".join(words[:4])
        if short_title:
            parts.append(short_title)

    name = "_".join(p for p in parts if p) or "paper"

    # Cap length (leave room for .pdf extension)
    name = name[:120]

    return name + ".pdf"


def _safe(s: str) -> str:
    """Make a string filesystem-safe."""
    return re.sub(r"[^\w\-]", "", s.replace(" ", "_"))[:30]


# ---------------------------------------------------------------------------
# Sci-Hub / Anna's Archive mirror rotation
# ---------------------------------------------------------------------------

# Sci-Hub mirrors — rotated on failure.  These change frequently;
# the code falls through on 4xx/5xx so stale mirrors are harmless.
_SCIHUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.ren",
]

# Anna's Archive mirrors
_ANNAS_MIRRORS = [
    "https://annas-archive.org",
    "https://annas-archive.se",
    "https://annas-archive.li",
]

_active_scihub_mirror = None
_active_annas_mirror = None

# CORE API base
_CORE_BASE = "https://api.core.ac.uk/v3"

# Conservative rate-limiting delays (seconds) for legally gray sources
_SCIHUB_DELAY = 5.0    # be very gentle
_ANNAS_DELAY = 3.0
_CORE_DELAY = 0.5

# Global locks and last request times to rate limit PDF searches
_unpaywall_lock = asyncio.Lock()
_unpaywall_last_time = 0.0

_s2_lock = asyncio.Lock()
_s2_last_time = 0.0

_core_lock = asyncio.Lock()
_core_last_time = 0.0
# When CORE.ac.uk starts returning 429 (its free tier is easily exhausted), we
# park it for a while so the daemon stops flooding it with doomed requests —
# this was the single biggest source of wasted PDF-fetch time.
_core_cooldown_until = 0.0


# ---------------------------------------------------------------------------
# Single PDF download
# ---------------------------------------------------------------------------

async def download_pdf(
    ref: Reference,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Download the PDF for a single reference.

    Tries multiple strategies in order of reliability and legality:
      1. ``ref.oa_url`` (already known OA link from enrichment)
      2. arXiv PDF (always free)
      3. Unpaywall lookup (DOI-based OA discovery)
      4. Semantic Scholar openAccessPdf link
      5. CORE.ac.uk full-text search
      6. DOI via institutional proxy / EZproxy
      7. Sci-Hub (rate-limited, rotating mirrors)
      8. Anna's Archive (rate-limited, rotating mirrors)

    Returns the relative path (from the PDF dir) on success, or None.
    Stores the absolute file on disk and sets ``ref.pdf_path``.
    """
    pdf_dir = get_pdf_dir()
    filename = sanitize_filename(ref)
    dest = pdf_dir / filename

    # Skip if already downloaded
    if dest.exists() and dest.stat().st_size >= 1024:
        rel = filename
        ref.pdf_path = rel
        return rel

    from .config import get_config
    cfg = get_config()
    proxy_url = cfg.institutional_proxy_url.strip() if cfg.institutional_proxy_url else ""

    # Check if proxy_url is a network proxy (starts with http/https/socks and doesn't contain a query param redirect)
    use_network_proxy = False
    if proxy_url and any(proxy_url.startswith(p) for p in ("http://", "https://", "socks5://")) and not ("=" in proxy_url or "login" in proxy_url):
        use_network_proxy = True

    own_client = client is None
    if own_client:
        client_kwargs = {
            "headers": {"User-Agent": _USER_AGENT},
            "follow_redirects": True,
            "timeout": 20.0,
        }
        if use_network_proxy:
            client_kwargs["proxy"] = proxy_url
        client = httpx.AsyncClient(**client_kwargs)

    try:
        # Strategy 1: Use oa_url already on the reference
        if ref.oa_url:
            result = await _stream_download(client, ref.oa_url, dest)
            if result:
                ref.pdf_path = filename
                return filename

        # Strategy 2: arXiv PDF
        if ref.arxiv_id:
            url = f"https://arxiv.org/pdf/{ref.arxiv_id}"
            result = await _stream_download(client, url, dest)
            if result:
                ref.pdf_path = filename
                return filename

        # Strategy 3: Unpaywall
        if ref.doi:
            email = cfg.openalex_email or cfg.crossref_email
            if email:
                oa_url = await _unpaywall_lookup(client, ref.doi, email)
                if oa_url:
                    ref.oa_url = oa_url
                    result = await _stream_download(client, oa_url, dest)
                    if result:
                        ref.pdf_path = filename
                        return filename

        # Strategy 4: Semantic Scholar openAccessPdf
        s2_url = await _s2_oa_lookup(client, ref, cfg)
        if s2_url:
            result = await _stream_download(client, s2_url, dest)
            if result:
                ref.pdf_path = filename
                ref.oa_url = ref.oa_url or s2_url
                return filename

        # Strategy 5: CORE.ac.uk
        core_url = await _core_lookup(client, ref)
        if core_url:
            result = await _stream_download(client, core_url, dest)
            if result:
                ref.pdf_path = filename
                ref.oa_url = ref.oa_url or core_url
                return filename

        # Strategy 6: DOI Proxy Download (EZproxy/Institutional Proxy prepending)
        if ref.doi and proxy_url and not use_network_proxy:
            target_url = f"https://doi.org/{ref.doi}"
            url = f"{proxy_url}{target_url}"
            result = await _stream_download(client, url, dest)
            if result:
                ref.pdf_path = filename
                return filename

        # Strategy 7: Sci-Hub (with rate limiting to avoid bans)
        if ref.doi:
            scihub_result = await _scihub_lookup(client, ref.doi, dest)
            if scihub_result:
                ref.pdf_path = filename
                return filename

        # Strategy 8: Anna's Archive (last resort, rate-limited)
        annas_result = await _annas_archive_lookup(client, ref, dest)
        if annas_result:
            ref.pdf_path = filename
            return filename

        return None
    except Exception as exc:
        logger.warning("PDF download failed for %s: %s", ref.doi or ref.title, exc)
        return None
    finally:
        if own_client:
            await client.aclose()



async def _stream_download(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
) -> bool:
    """Stream-download a URL to *dest*.  Returns True on success.

    Uses chunked streaming (64 KB) so large PDFs are never fully buffered.
    Cleans up partial files on failure.
    """
    try:
        async with network_slot("pdf_stream", bucket=bucket_from_url(url), min_interval=0.05):
            async with client.stream("GET", url, timeout=25.0) as resp:
                if resp.status_code != 200:
                    return False
                content_type = resp.headers.get("content-type", "")
                if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
                    return False

                bytes_written = 0
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(65536):
                        fh.write(chunk)
                        bytes_written += len(chunk)

        if bytes_written < 1024:
            dest.unlink(missing_ok=True)
            return False
        return True
    except Exception:
        dest.unlink(missing_ok=True)
        return False


async def _unpaywall_lookup(
    client: httpx.AsyncClient, doi: str, email: str
) -> Optional[str]:
    """Query Unpaywall for the best OA PDF URL."""
    global _unpaywall_last_time
    async with _unpaywall_lock:
        elapsed = time.monotonic() - _unpaywall_last_time
        sleep_for = max(0.0, 0.25 - elapsed)
        _unpaywall_last_time = time.monotonic() + sleep_for
    if sleep_for > 0:
        await asyncio.sleep(sleep_for)
    try:
        async with network_slot("pdf_lookup", bucket="unpaywall", min_interval=0.25):
            resp = await client.get(
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": email},
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("is_oa"):
            return None
        best = data.get("best_oa_location") or {}
        return best.get("url_for_pdf") or best.get("url") or None
    except Exception:
        return None


async def _s2_oa_lookup(
    client: httpx.AsyncClient, ref: Reference, cfg
) -> Optional[str]:
    """Query Semantic Scholar for the openAccessPdf link."""
    global _s2_last_time
    async with _s2_lock:
        elapsed = time.monotonic() - _s2_last_time
        sleep_for = max(0.0, 1.05 - elapsed)
        _s2_last_time = time.monotonic() + sleep_for
    if sleep_for > 0:
        await asyncio.sleep(sleep_for)
    try:
        s2_id = None
        if ref.doi:
            s2_id = f"DOI:{ref.doi}"
        elif ref.arxiv_id:
            s2_id = f"ARXIV:{ref.arxiv_id}"
        elif ref.pmid:
            s2_id = f"PMID:{ref.pmid}"
        if not s2_id:
            return None

        headers = {}
        if cfg.semantic_scholar_api_key:
            headers["x-api-key"] = cfg.semantic_scholar_api_key

        from urllib.parse import quote
        async with network_slot("pdf_lookup", bucket="semantic_scholar", min_interval=1.0):
            resp = await client.get(
                f"https://api.semanticscholar.org/graph/v1/paper/{quote(s2_id, safe=':')}",
                params={"fields": "openAccessPdf"},
                headers=headers,
                timeout=8.0,
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        oa_pdf = data.get("openAccessPdf") or {}
        url = oa_pdf.get("url")
        if url and url.startswith("http"):
            return url
        return None
    except Exception:
        return None


async def _core_lookup(
    client: httpx.AsyncClient, ref: Reference
) -> Optional[str]:
    """Query CORE.ac.uk for a free full-text PDF link.

    CORE provides free API access without a key for moderate usage.
    """
    global _core_last_time, _core_cooldown_until
    # Skip entirely while CORE is in a 429-cooldown — don't waste a request slot.
    if time.monotonic() < _core_cooldown_until:
        return None
    async with _core_lock:
        elapsed = time.monotonic() - _core_last_time
        sleep_for = max(0.0, 1.5 - elapsed)
        _core_last_time = time.monotonic() + sleep_for
    if sleep_for > 0:
        await asyncio.sleep(sleep_for)
    try:
        query = None
        if ref.doi:
            query = f'doi:"{ref.doi}"'
        elif ref.title and len(ref.title) > 15:
            # Use title search — quote for exact match
            query = f'title:"{ref.title}"'
        if not query:
            return None

        async with network_slot("pdf_lookup", bucket="core", min_interval=2.0):
            resp = await client.get(
                f"{_CORE_BASE}/search/works",
                params={"q": query, "limit": 3},
                headers={"Accept": "application/json"},
                timeout=8.0,
            )
        if resp.status_code == 429:
            # CORE quota hit — park it for 15 min so the pool stops flooding it.
            _core_cooldown_until = time.monotonic() + 900.0
            logger.info("CORE.ac.uk 429 — cooling down 15 min")
            return None
        if resp.status_code != 200:
            return None

        results = resp.json().get("results", [])
        for result in results:
            download_url = result.get("downloadUrl")
            if download_url and download_url.startswith("http"):
                await asyncio.sleep(_CORE_DELAY)
                return download_url
            # Try links array
            for link in result.get("links", []):
                if link.get("type") == "download":
                    await asyncio.sleep(_CORE_DELAY)
                    return link.get("url")
        return None
    except Exception:
        return None


async def _scihub_lookup(
    client: httpx.AsyncClient, doi: str, dest: Path
) -> bool:
    """Try Sci-Hub mirrors to download a PDF by DOI.

    Uses a 5-second delay between attempts to be polite.
    Rotates through mirrors, returning True on first success.
    """
    global _active_scihub_mirror
    import asyncio
    import re

    mirrors = _SCIHUB_MIRRORS
    if _active_scihub_mirror:
        mirrors = [_active_scihub_mirror] + [m for m in _SCIHUB_MIRRORS if m != _active_scihub_mirror]

    for mirror in mirrors:
        request_succeeded = False
        try:
            # Sci-Hub serves the PDF directly at /{doi}
            async with network_slot("gray_source", bucket=bucket_from_url(mirror), min_interval=_SCIHUB_DELAY):
                resp = await client.get(
                    f"{mirror}/{doi}",
                    timeout=10.0,
                    follow_redirects=True,
                )
            request_succeeded = True
            _active_scihub_mirror = mirror

            if resp.status_code != 200:
                continue

            html = resp.text
            # Sci-Hub embeds the PDF in an iframe or a direct link
            # Look for the PDF URL in the response (including relative and absolute)
            pdf_urls = []
            for match in re.finditer(r'(?:src|href|location\.href)\s*=\s*["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I):
                pdf_urls.append(match.group(1))

            if not pdf_urls:
                # Fallback to loose search without quotes
                for match in re.finditer(r'(?:src|href)\s*=\s*([^\s>"\']+\.pdf[^\s>"\']*)', html, re.I):
                    pdf_urls.append(match.group(1))

            if not pdf_urls:
                # Maybe the response IS the PDF (check content-type)
                ct = resp.headers.get("content-type", "")
                if "pdf" in ct.lower():
                    if len(resp.content) >= 1024:
                        dest.write_bytes(resp.content)
                        return True
                continue

            # Download the first PDF URL found
            pdf_url = pdf_urls[0]
            if pdf_url.startswith("//"):
                pdf_url = "https:" + pdf_url
            elif pdf_url.startswith("/"):
                pdf_url = mirror.rstrip("/") + pdf_url
            elif not pdf_url.startswith("http"):
                pdf_url = mirror.rstrip("/") + "/" + pdf_url

            result = await _stream_download(client, pdf_url, dest)
            if result:
                return True

        except Exception as e:
            logger.debug("Sci-Hub mirror %s failed: %s", mirror, e)

        # Polite delay between mirror attempts (only if request actually went through)
        if request_succeeded:
            await asyncio.sleep(_SCIHUB_DELAY)

    return False


async def _annas_archive_lookup(
    client: httpx.AsyncClient, ref: Reference, dest: Path
) -> bool:
    """Try Anna's Archive to find a PDF download link.

    Searches by DOI or ISBN, then follows the download chain.
    Rate-limited with 3s delays between attempts.
    """
    import asyncio
    import re

    search_query = None
    if ref.doi:
        search_query = ref.doi
    elif ref.isbn:
        search_query = ref.isbn
    else:
        # Title searches on Anna's Archive are too slow (noisy HTTP requests + 3s delay)
        # for mass processing of Tier 4/5. Skip unless we have a strong identifier.
        return False

    global _active_annas_mirror
    mirrors = _ANNAS_MIRRORS
    if _active_annas_mirror:
        mirrors = [_active_annas_mirror] + [m for m in _ANNAS_MIRRORS if m != _active_annas_mirror]

    for mirror in mirrors:
        request_succeeded = False
        try:
            # Search for the paper (omit content=book_any to support articles/journals)
            async with network_slot("gray_source", bucket=bucket_from_url(mirror), min_interval=_ANNAS_DELAY):
                resp = await client.get(
                    f"{mirror}/search",
                    params={"q": search_query, "ext": "pdf"},
                    timeout=10.0,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                )
            request_succeeded = True
            _active_annas_mirror = mirror

            if resp.status_code != 200:
                continue

            html = resp.text

            # Parse result links — Anna's Archive uses /md5/ paths
            md5_links = re.findall(
                r'href="(/md5/[a-fA-F0-9]{32})"',
                html
            )

            if not md5_links:
                continue

            # Follow the first result to get the download page
            detail_url = f"{mirror}{md5_links[0]}"
            await asyncio.sleep(_ANNAS_DELAY)

            async with network_slot("gray_source", bucket=bucket_from_url(detail_url), min_interval=_ANNAS_DELAY):
                resp2 = await client.get(
                    detail_url,
                    timeout=10.0,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                )

            if resp2.status_code != 200:
                continue

            # Look for direct download links (various mirrors)
            download_links = re.findall(
                r'href="(https?://[^"]+)"[^>]*>\s*(?:.*?(?:download|Libgen|fast|slow|partner|IPFS|gateway|option|sci-hub|z-library))',
                resp2.text, re.I
            )
            
            if not download_links:
                # Fallback to parsing all external download subdomain links
                download_links = re.findall(
                    r'href="(https?://[^"]+(?:ipfs|libgen|scihub|sci-hub|annas|pinata|cloudflare|gateway|download|get\.php)[^"]*)"',
                    resp2.text, re.I
                )

            for dl_link in download_links[:3]:  # try first 3 download options
                result = await _stream_download(client, dl_link, dest)
                if result:
                    return True
                await asyncio.sleep(1.0)

        except Exception as e:
            logger.debug("Anna's Archive mirror %s failed: %s", mirror, e)

        if request_succeeded:
            await asyncio.sleep(_ANNAS_DELAY)

    return False


# ---------------------------------------------------------------------------
# Batch PDF download
# ---------------------------------------------------------------------------

async def download_pdfs_batch(
    refs: List[Reference],
    progress_cb: Optional[Callable[[int, int, Optional[str]], None]] = None,
) -> List[Tuple[Reference, Optional[str]]]:
    """Download PDFs for multiple references concurrently.

    Uses a semaphore to limit concurrency to ``_MAX_CONCURRENT`` (5).

    Parameters
    ----------
    refs : list[Reference]
        References to attempt downloads for.  Only refs with
        ``oa_url``, ``arxiv_id``, or ``doi`` are attempted.
    progress_cb : callable, optional
        Called as ``progress_cb(done, total, last_title)`` after each
        ref is processed (whether successful or not).

    Returns
    -------
    list of (Reference, path_or_none) tuples in the same order as *refs*.
    """
    sem = SafeSemaphore(_MAX_CONCURRENT)
    total = len(refs)
    done_count = 0
    results: list[Optional[str]] = [None] * total

    async def _download_one(idx: int, ref: Reference, client: httpx.AsyncClient):
        nonlocal done_count
        async with sem:
            if ref.oa_url or ref.arxiv_id or ref.doi:
                try:
                    path = await download_pdf(ref, client=client)
                    results[idx] = path
                except Exception:
                    pass
            done_count += 1
            if progress_cb:
                try:
                    progress_cb(done_count, total, ref.title)
                except Exception:
                    pass

    from .config import get_config
    cfg = get_config()
    proxy_url = cfg.institutional_proxy_url.strip() if cfg.institutional_proxy_url else ""
    use_network_proxy = False
    if proxy_url and any(proxy_url.startswith(p) for p in ("http://", "https://", "socks5://")) and not ("=" in proxy_url or "login" in proxy_url):
        use_network_proxy = True

    client_kwargs = {
        "headers": {"User-Agent": _USER_AGENT},
        "follow_redirects": True,
        "timeout": 20.0,
    }
    if use_network_proxy:
        client_kwargs["proxy"] = proxy_url

    async with httpx.AsyncClient(**client_kwargs) as client:
        tasks = [
            asyncio.create_task(_download_one(i, ref, client))
            for i, ref in enumerate(refs)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    return list(zip(refs, results))


# ---------------------------------------------------------------------------
# Google Drive streaming & LRU cache
# ---------------------------------------------------------------------------

def get_pdf_bytes(ref: Reference, config=None) -> Tuple[Optional[bytes], str]:
    """Get PDF bytes for a reference from the best available source.

    Returns (bytes, source) where source is one of:
        "local"   — read from local pdf_path
        "cache"   — read from local LRU cache (originally from Drive)
        "drive"   — streamed fresh from Google Drive
        "none"    — no PDF available

    If bytes is None, source will be "none".
    """
    if config is None:
        from .config import get_config
        config = get_config()

    pdf_path = getattr(ref, "pdf_path", None)
    drive_id = getattr(ref, "pdf_drive_id", None)

    # 1. Try local file first (always fastest)
    if pdf_path:
        local = Path(pdf_path)
        if not local.is_absolute():
            local = Path(config.pdf_storage_path) / pdf_path
        if local.exists():
            try:
                return local.read_bytes(), "local"
            except Exception as e:
                logger.warning("Failed to read local PDF %s: %s", local, e)

    # 2. Try LRU cache (for streaming mode)
    if drive_id:
        cached = _drive_cache_path(drive_id, config)
        if cached.exists():
            try:
                cached.touch()  # update mtime for LRU
            except Exception:
                pass
            try:
                return cached.read_bytes(), "cache"
            except Exception as e:
                logger.warning("Failed to read cached PDF %s: %s", cached, e)

    # 3. Stream from Google Drive
    if drive_id:
        try:
            from .integrations.google_drive import stream_pdf
            data = stream_pdf(drive_id)
            _drive_cache_write(drive_id, data, config)
            return data, "drive"
        except Exception as e:
            logger.warning("Failed to stream PDF from Drive %s: %s", drive_id, e)

    return None, "none"


def evict_drive_cache(config=None, target_mb: Optional[int] = None) -> int:
    """Remove oldest cached Drive PDFs until cache is under the size limit.

    Returns the number of files evicted.
    """
    if config is None:
        from .config import get_config
        config = get_config()

    cache_dir = _drive_cache_dir(config)
    if not cache_dir.exists():
        return 0

    limit_bytes = (target_mb or config.google_drive_local_cache_mb) * 1024 * 1024

    entries = []
    total_size = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.suffix == ".pdf":
            stat = f.stat()
            entries.append((f, stat.st_mtime, stat.st_size))
            total_size += stat.st_size

    if total_size <= limit_bytes:
        return 0

    entries.sort(key=lambda e: e[1])  # oldest first

    evicted = 0
    for path, _, size in entries:
        if total_size <= limit_bytes:
            break
        try:
            path.unlink()
            total_size -= size
            evicted += 1
        except Exception:
            pass

    if evicted:
        logger.info("Evicted %d cached PDFs (now %.0f MB)", evicted, total_size / 1024 / 1024)
    return evicted


def drive_cache_stats(config=None) -> dict:
    """Return Drive cache statistics."""
    if config is None:
        from .config import get_config
        config = get_config()

    cache_dir = _drive_cache_dir(config)
    if not cache_dir.exists():
        return {"files": 0, "size_mb": 0, "limit_mb": config.google_drive_local_cache_mb}

    total_size = 0
    count = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.suffix == ".pdf":
            total_size += f.stat().st_size
            count += 1

    return {
        "files": count,
        "size_mb": round(total_size / 1024 / 1024, 1),
        "limit_mb": config.google_drive_local_cache_mb,
    }


def _drive_cache_dir(config) -> Path:
    return Path(config.pdf_storage_path) / ".drive_cache"


def _drive_cache_path(drive_id: str, config) -> Path:
    return _drive_cache_dir(config) / f"{drive_id}.pdf"


def _drive_cache_write(drive_id: str, data: bytes, config) -> Path:
    cache_dir = _drive_cache_dir(config)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{drive_id}.pdf"
    path.write_bytes(data)
    try:
        evict_drive_cache(config)
    except Exception:
        pass
    return path
