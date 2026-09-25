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
_MAX_CONCURRENT = int(os.environ.get("MOUSEION_PDF_CONCURRENCY", "20"))


def _shadow_sources_on() -> bool:
    """Sci-Hub / Anna's Archive strategies: on unless MOUSEION_PDF_SHADOW=0
    (runs that must use licensed and open-access sources only)."""
    return os.environ.get("MOUSEION_PDF_SHADOW", "1") != "0"


_LEDGER_KEY_CACHE: list = [0.0, "pdf"]


def _pdf_ledger_key() -> str:
    """The attempt ledger remembers misses per NETWORK: a miss from the open
    internet must not block a retry through the institution's tunnel, where
    subscription PDFs resolve (2026-09-25: every ref had been tried without
    USP access, so connecting the VPN would have changed nothing)."""
    import time as _t
    if _t.time() - _LEDGER_KEY_CACHE[0] > 60:
        key = "pdf"
        try:
            from .config import get_config
            from .vpn_manager import vpn_adapter_up
            cfg = get_config()
            if cfg.vpn_enabled and cfg.vpn_gateway and vpn_adapter_up():
                key = "pdf_inst"
        except Exception:
            pass
        _LEDGER_KEY_CACHE[:] = [_t.time(), key]
    # a run without the shadow sources must not mark refs as tried for runs with them
    return _LEDGER_KEY_CACHE[1] + ("" if _shadow_sources_on() else "_open")


class TemporaryDownloadError(Exception):
    """Exception raised when a PDF download strategy fails due to transient reasons (rate limits, timeouts, etc.)."""
    pass


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
_SCIHUB_DELAY = 3.0    # reduced but still gentle
_ANNAS_DELAY = 2.0
_CORE_DELAY = 0.3

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
    """Download the PDF for a single reference; a newly fetched file is recorded
    as a `hit` in the attempt ledger (only misses were, so "0 hits" in the
    ledger could not tell a working finder from a dead one)."""
    dest_existed = (get_pdf_dir() / sanitize_filename(ref)).exists()
    result = await _download_pdf_impl(ref, client)
    if result and not dest_existed:
        try:
            from .api_router import get_router
            router = get_router()
            rid = getattr(ref, "_db_id", None) or getattr(ref, "_batch_id", None) or (ref.doi or ref.arxiv_id or ref.title or "")
            if rid:
                router.record_attempt(rid, _pdf_ledger_key(), router.entry_hash(ref), "hit")
        except Exception:
            pass
    return result


async def _download_pdf_impl(
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

    # Attempt ledger: if this exact entry already failed a PDF fetch, don't try
    # again until the entry changes (e.g. enrichment adds an oa_url/doi → hash
    # changes → eligible again). Stops the "thousands found, counter never moves"
    # re-churn that wasted PDF-source quota every run.
    from .api_router import get_router
    _router = get_router()
    _rid = getattr(ref, "_db_id", None) or getattr(ref, "_batch_id", None) or (ref.doi or ref.arxiv_id or ref.title or "")
    _eh = _router.entry_hash(ref)
    _ledger = _pdf_ledger_key()
    if _rid and _router.was_tried(_rid, _ledger, _eh):
        return None

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

    temporary_failure = False

    try:
        # Strategy 1: Use oa_url already on the reference
        if ref.oa_url:
            try:
                result = await _stream_download(client, ref.oa_url, dest)
                if result:
                    ref.pdf_path = filename
                    return filename
            except TemporaryDownloadError as e:
                logger.info("Temporary failure using ref.oa_url: %s", e)
                temporary_failure = True

        # Strategy 2: arXiv PDF
        if ref.arxiv_id:
            try:
                url = f"https://arxiv.org/pdf/{ref.arxiv_id}"
                result = await _stream_download(client, url, dest)
                if result:
                    ref.pdf_path = filename
                    return filename
            except TemporaryDownloadError as e:
                logger.info("Temporary failure using arXiv: %s", e)
                temporary_failure = True

        # Strategy 3: Unpaywall
        if ref.doi:
            email = cfg.openalex_email or cfg.crossref_email
            if email:
                try:
                    oa_url = await _unpaywall_lookup(client, ref.doi, email)
                    if oa_url:
                        ref.oa_url = oa_url
                        result = await _stream_download(client, oa_url, dest)
                        if result:
                            ref.pdf_path = filename
                            return filename
                except TemporaryDownloadError as e:
                    logger.info("Temporary failure using Unpaywall: %s", e)
                    temporary_failure = True

        # Strategy 4: Semantic Scholar openAccessPdf
        try:
            s2_url = await _s2_oa_lookup(client, ref, cfg)
            if s2_url:
                result = await _stream_download(client, s2_url, dest)
                if result:
                    ref.pdf_path = filename
                    ref.oa_url = ref.oa_url or s2_url
                    return filename
        except TemporaryDownloadError as e:
            logger.info("Temporary failure using Semantic Scholar: %s", e)
            temporary_failure = True

        # Strategy 5: CORE.ac.uk
        try:
            core_url = await _core_lookup(client, ref)
            if core_url:
                result = await _stream_download(client, core_url, dest)
                if result:
                    ref.pdf_path = filename
                    ref.oa_url = ref.oa_url or core_url
                    return filename
        except TemporaryDownloadError as e:
            logger.info("Temporary failure using CORE: %s", e)
            temporary_failure = True

        # Strategy 5.5: Direct DOI/Publisher URL download (takes advantage of VPN direct access)
        if ref.doi:
            try:
                doi_url = f"https://doi.org/{ref.doi}"
                result = await _stream_download(client, doi_url, dest)
                if result:
                    ref.pdf_path = filename
                    return filename
            except TemporaryDownloadError as e:
                logger.info("Temporary failure using Direct DOI: %s", e)
                temporary_failure = True

        if ref.url and not ref.doi:
            try:
                result = await _stream_download(client, ref.url, dest)
                if result:
                    ref.pdf_path = filename
                    return filename
            except TemporaryDownloadError as e:
                logger.info("Temporary failure using Direct URL: %s", e)
                temporary_failure = True

        # Strategy 6: DOI Proxy Download (EZproxy/Institutional Proxy prepending)
        if ref.doi and proxy_url and not use_network_proxy:
            try:
                target_url = f"https://doi.org/{ref.doi}"
                url = f"{proxy_url}{target_url}"
                result = await _stream_download(client, url, dest)
                if result:
                    ref.pdf_path = filename
                    return filename
            except TemporaryDownloadError as e:
                logger.info("Temporary failure using DOI Proxy: %s", e)
                temporary_failure = True

        # Strategy 7: Sci-Hub (with rate limiting to avoid bans)
        if ref.doi and _shadow_sources_on():
            try:
                scihub_result = await _scihub_lookup(client, ref.doi, dest)
                if scihub_result:
                    ref.pdf_path = filename
                    return filename
            except TemporaryDownloadError as e:
                logger.info("Temporary failure using Sci-Hub: %s", e)
                temporary_failure = True

        # Strategy 8: Anna's Archive (last resort, rate-limited)
        if _shadow_sources_on():
            try:
                annas_result = await _annas_archive_lookup(client, ref, dest)
                if annas_result:
                    ref.pdf_path = filename
                    return filename
            except TemporaryDownloadError as e:
                logger.info("Temporary failure using Anna's Archive: %s", e)
                temporary_failure = True

        # Strategy 9: Web Search Fallback (DuckDuckGo Lite for direct PDF links)
        try:
            ddg_result = await _ddg_pdf_search(client, ref, dest)
            if ddg_result:
                ref.pdf_path = filename
                return filename
        except TemporaryDownloadError as e:
            logger.info("Temporary failure using DuckDuckGo: %s", e)
            temporary_failure = True

        # Record miss even when temporary failures happened — the entry_hash
        # changes if enrichment adds an oa_url/doi, making the ref eligible
        # again.  Without this, refs where gray sources (Sci-Hub/CORE/Anna's)
        # are all dead get re-attempted every sweep forever.
        if _rid:
            _router.record_attempt(_rid, _ledger, _eh, "miss")
        if temporary_failure:
            logger.info("All strategies exhausted (some had temporary failures) for ref: %s", _rid)
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
    follow_html_links: bool = True,
) -> bool:
    """Stream-download a URL to *dest*.  Returns True on success.

    Uses chunked streaming (64 KB) so large PDFs are never fully buffered.
    Cleans up partial files on failure.
    
    If follow_html_links is True and the URL returns HTML, attempts to parse
    and download candidate PDF links from the page.
    """
    html_content = None
    try:
        async with network_slot("pdf_stream", bucket=bucket_from_url(url), min_interval=0.05):
            async with client.stream("GET", url, timeout=25.0) as resp:
                if resp.status_code in (429, 503, 504):
                    raise TemporaryDownloadError(f"HTTP {resp.status_code} rate limit/server error during stream download")
                if resp.status_code != 200:
                    return False
                content_type = resp.headers.get("content-type", "")
                
                # Check if it's HTML
                if "html" in content_type.lower() or "text/xml" in content_type.lower():
                    if not follow_html_links:
                        return False
                    body_bytes = await resp.aread()
                    html_content = body_bytes.decode("utf-8", errors="ignore")
                else:
                    is_pdf = "pdf" in content_type.lower() or url.lower().endswith(".pdf")
                    bytes_written = 0
                    is_first_chunk = True
                    with dest.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(65536):
                            if is_first_chunk:
                                is_first_chunk = False
                                # Validate PDF magic bytes if not explicitly labeled as PDF
                                if not is_pdf:
                                    if chunk.startswith(b"%PDF"):
                                        is_pdf = True
                                    else:
                                        break
                            fh.write(chunk)
                            bytes_written += len(chunk)

                    if not is_pdf or bytes_written < 1024:
                        dest.unlink(missing_ok=True)
                        return False
                    return True
    except TemporaryDownloadError:
        dest.unlink(missing_ok=True)
        raise
    except httpx.RequestError as exc:
        dest.unlink(missing_ok=True)
        raise TemporaryDownloadError(f"Request error during stream download: {exc}")
    except Exception:
        dest.unlink(missing_ok=True)
        return False

    # If we got HTML content, parse and follow links outside the lock
    if html_content:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        soup = BeautifulSoup(html_content, "html.parser")
        candidates = []
        
        # 1. citation_pdf_url meta tags
        for meta in soup.find_all("meta", attrs={"name": "citation_pdf_url"}):
            if meta.get("content"):
                candidates.append(meta["content"])
                
        # 2. regular links
        for a in soup.find_all("a", href=True):
            href = a["href"]
            href_lower = href.lower()
            text_lower = a.get_text(strip=True).lower()
            if ".pdf" in href_lower or "pdf" in text_lower or "download" in text_lower:
                candidates.append(href)
                
        seen = set()
        resolved_candidates = []
        for c in candidates:
            full_url = urljoin(url, c)
            if full_url not in seen and full_url.startswith("http"):
                seen.add(full_url)
                resolved_candidates.append(full_url)
                
        for cand_url in resolved_candidates[:5]:
            try:
                # Attempt to download the candidate (do not follow nested HTML pages)
                if await _stream_download(client, cand_url, dest, follow_html_links=False):
                    return True
            except Exception:
                pass
                
    return False


async def _unpaywall_lookup(
    client: httpx.AsyncClient, doi: str, email: str
) -> Optional[str]:
    """Query Unpaywall for the best OA PDF URL (metered by the master router)."""
    from .api_router import get_router
    router = get_router()
    if not await router.acquire("unpaywall", max_wait=15.0):
        raise TemporaryDownloadError("Unpaywall lock timeout")
    try:
        async with network_slot("pdf_lookup", bucket="unpaywall"):
            resp = await client.get(
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": email},
            )
        router.report("unpaywall", resp.status_code, ok=(resp.status_code == 200))
        if resp.status_code in (429, 503, 504):
            raise TemporaryDownloadError(f"Unpaywall HTTP {resp.status_code} rate limit/server error")
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("is_oa"):
            return None
        best = data.get("best_oa_location") or {}
        return best.get("url_for_pdf") or best.get("url") or None
    except TemporaryDownloadError:
        raise
    except httpx.RequestError as exc:
        router.report("unpaywall", None, ok=False)
        raise TemporaryDownloadError(f"Unpaywall request error: {exc}")
    except Exception:
        router.report("unpaywall", None, ok=False)
        return None


async def _s2_oa_lookup(
    client: httpx.AsyncClient, ref: Reference, cfg
) -> Optional[str]:
    """Query Semantic Scholar for the openAccessPdf link (shares the S2 budget
    with enrichment via the master router, so PDF can't exhaust S2 on its own)."""
    from .api_router import get_router
    router = get_router()
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
        if not await router.acquire("semantic_scholar", max_wait=15.0):
            raise TemporaryDownloadError("Semantic Scholar lock timeout")

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
        router.report("semantic_scholar", resp.status_code, ok=(resp.status_code == 200))
        if resp.status_code in (429, 503, 504):
            raise TemporaryDownloadError(f"Semantic Scholar HTTP {resp.status_code} rate limit/server error")
        if resp.status_code != 200:
            return None
        data = resp.json()
        oa_pdf = data.get("openAccessPdf") or {}
        url = oa_pdf.get("url")
        if url and url.startswith("http"):
            return url
        return None
    except TemporaryDownloadError:
        raise
    except httpx.RequestError as exc:
        raise TemporaryDownloadError(f"Semantic Scholar request error: {exc}")
    except Exception:
        return None


async def _core_lookup(
    client: httpx.AsyncClient, ref: Reference
) -> Optional[str]:
    """Query CORE.ac.uk for a free full-text PDF link (metered by master router).

    CORE's free tier is tiny and 429s aggressively — the router caps it hard
    (6/min, 900/day) and cools it down on every denial so it can never become
    the daemon-stalling 900s-backoff sink it used to be.
    """
    from .api_router import get_router
    router = get_router()
    query = None
    if ref.doi:
        query = f'doi:"{ref.doi}"'
    elif ref.title and len(ref.title) > 15:
        query = f'title:"{ref.title}"'
    if not query:
        return None
    if not await router.acquire("core", max_wait=10.0):
        raise TemporaryDownloadError("CORE lock timeout")
    try:
        async with network_slot("pdf_lookup", bucket="core", min_interval=2.0):
            resp = await client.get(
                f"{_CORE_BASE}/search/works",
                params={"q": query, "limit": 3},
                headers={"Accept": "application/json"},
                timeout=8.0,
            )
        router.report("core", resp.status_code, ok=(resp.status_code == 200))
        if resp.status_code in (429, 503, 504):
            raise TemporaryDownloadError(f"CORE HTTP {resp.status_code} rate limit/server error")
        if resp.status_code != 200:
            return None

        results = resp.json().get("results", [])
        for result in results:
            download_url = result.get("downloadUrl")
            if download_url and download_url.startswith("http"):
                return download_url
            for link in result.get("links", []):
                if link.get("type") == "download":
                    return link.get("url")
        return None
    except TemporaryDownloadError:
        raise
    except httpx.RequestError as exc:
        router.report("core", None, ok=False)
        raise TemporaryDownloadError(f"CORE request error: {exc}")
    except Exception:
        router.report("core", None, ok=False)
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
    from .api_router import get_router
    router = get_router()

    mirrors = _SCIHUB_MIRRORS
    if _active_scihub_mirror:
        mirrors = [_active_scihub_mirror] + [m for m in _SCIHUB_MIRRORS if m != _active_scihub_mirror]

    had_temporary_error = False
    temp_error_msg = ""

    for mirror in mirrors:
        request_succeeded = False
        if not await router.acquire("scihub", max_wait=10.0):
            raise TemporaryDownloadError("Sci-Hub lock timeout")
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
            router.report("scihub", resp.status_code, ok=(resp.status_code == 200))

            if resp.status_code in (429, 503, 504):
                had_temporary_error = True
                temp_error_msg = f"Sci-Hub HTTP {resp.status_code} on {mirror}"
                continue

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

        except TemporaryDownloadError:
            raise
        except httpx.RequestError as exc:
            logger.debug("Sci-Hub mirror %s failed: %s", mirror, exc)
            had_temporary_error = True
            temp_error_msg = f"Sci-Hub connection error on {mirror}: {exc}"
        except Exception as e:
            logger.debug("Sci-Hub mirror %s failed: %s", mirror, e)

        # Polite delay between mirror attempts (only if request actually went through)
        if request_succeeded:
            await asyncio.sleep(_SCIHUB_DELAY)

    if had_temporary_error:
        raise TemporaryDownloadError(temp_error_msg)
    return False


async def _annas_archive_lookup(
    client: httpx.AsyncClient, ref: Reference, dest: Path
) -> bool:
    """Try Anna's Archive to find a PDF download link.

    Searches by DOI or ISBN, or falls back to title search.
    Rate-limited with 3s delays between attempts.
    """
    import asyncio
    import re
    from bs4 import BeautifulSoup
    from rapidfuzz import fuzz

    # Set up search queries in priority order: strong identifiers first, then title fallback
    queries = []
    if ref.doi:
        queries.append((ref.doi, True))
    if ref.isbn:
        queries.append((ref.isbn, True))
    if ref.title and len(ref.title) > 10:
        queries.append((ref.title, False))

    if not queries:
        return False

    global _active_annas_mirror
    from .api_router import get_router
    router = get_router()
    mirrors = _ANNAS_MIRRORS
    if _active_annas_mirror:
        mirrors = [_active_annas_mirror] + [m for m in _ANNAS_MIRRORS if m != _active_annas_mirror]

    had_temporary_error = False
    temp_error_msg = ""

    for mirror in mirrors:
        for search_query, is_identifier in queries:
            request_succeeded = False
            if not await router.acquire("annas", max_wait=10.0):
                raise TemporaryDownloadError("Anna's Archive lock timeout")
            try:
                # Search for the paper (omit content=book_any to support articles/journals)
                async with network_slot("gray_source", bucket=bucket_from_url(mirror), min_interval=_ANNAS_DELAY):
                    resp = await client.get(
                        f"{mirror}/search",
                        params={"q": search_query, "ext": "pdf"},
                        timeout=12.0,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                    )
                request_succeeded = True
                _active_annas_mirror = mirror

                if resp.status_code in (429, 503, 504):
                    had_temporary_error = True
                    temp_error_msg = f"Anna's Archive HTTP {resp.status_code} on {mirror}"
                    continue
                if resp.status_code != 200:
                    continue

                html = resp.text
                soup = BeautifulSoup(html, "html.parser")
                
                # Find all md5 result links and their texts
                results_found = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    # Matches /md5/ followed by 32 hex chars
                    if re.match(r'^/md5/[a-fA-F0-9]{32}$', href):
                        title_text = a.get_text(strip=True)
                        results_found.append((href, title_text))

                if not results_found:
                    continue

                # Find the best match
                best_href = None
                if is_identifier:
                    # If we have a strong identifier, take the first link
                    best_href = results_found[0][0]
                else:
                    # If searching by title, verify that it's a good match
                    target_title = ref.title.lower()
                    for href, result_title in results_found:
                        res_title_clean = result_title.lower()
                        # Calculate similarity score
                        score = fuzz.ratio(target_title, res_title_clean)
                        partial_score = fuzz.partial_ratio(target_title, res_title_clean)
                        if score >= 80 or partial_score >= 90:
                            best_href = href
                            break

                if not best_href:
                    continue

                # Follow the md5 link to get the download page
                detail_url = f"{mirror}{best_href}"
                await asyncio.sleep(_ANNAS_DELAY)

                async with network_slot("gray_source", bucket=bucket_from_url(detail_url), min_interval=_ANNAS_DELAY):
                    resp2 = await client.get(
                        detail_url,
                        timeout=12.0,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                    )

                if resp2.status_code in (429, 503, 504):
                    had_temporary_error = True
                    temp_error_msg = f"Anna's Archive detail page HTTP {resp2.status_code} on {mirror}"
                    continue
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

            except TemporaryDownloadError:
                raise
            except httpx.RequestError as exc:
                logger.debug("Anna's Archive mirror %s query failed: %s", mirror, exc)
                had_temporary_error = True
                temp_error_msg = f"Anna's Archive connection error on {mirror}: {exc}"
            except Exception as e:
                logger.debug("Anna's Archive mirror %s failed: %s", mirror, e)

            if request_succeeded:
                await asyncio.sleep(_ANNAS_DELAY)

    if had_temporary_error:
        raise TemporaryDownloadError(temp_error_msg)
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


# Circuit breaker for the web-search strategy: DuckDuckGo answered this network
# with a challenge page for weeks, and every ref queued ~40 s behind its rate
# lock for nothing (2026-09-25). After _WEB_TRIP empty searches in a row, skip
# it for _WEB_COOLDOWN seconds; any result closes the breaker again.
_WEB_TRIP, _WEB_COOLDOWN = 15, 3600
_web_state = {"fails": 0, "open_until": 0.0}


async def _ddg_pdf_search(
    client: httpx.AsyncClient, ref: Reference, dest: Path
) -> bool:
    """Search DuckDuckGo Lite for the paper title to find direct PDF download links."""
    import time as _t
    if _t.time() < _web_state["open_until"]:
        return False
    from .web_search import _search_duckduckgo
    from .api_router import get_router
    from rapidfuzz import fuzz
    import json
    
    router = get_router()

    if not ref.title or len(ref.title) < 10:
        return False

    # Try both quoted search and unquoted fallback search
    clean_title = ref.title.strip().replace('"', '')
    
    # 1. Quoted search query
    quoted_query = f'"{clean_title}" filetype:pdf'
    if ref.authors:
        try:
            authors_data = json.loads(ref.authors) if isinstance(ref.authors, str) else ref.authors
            if authors_data and isinstance(authors_data, list):
                family = authors_data[0].get("family", "")
                if family:
                    quoted_query = f'"{clean_title}" {family} filetype:pdf'
        except Exception:
            pass

    # 2. Unquoted fallback query
    unquoted_query = f'{clean_title} filetype:pdf'
    if ref.authors:
        try:
            authors_data = json.loads(ref.authors) if isinstance(ref.authors, str) else ref.authors
            if authors_data and isinstance(authors_data, list):
                family = authors_data[0].get("family", "")
                if family:
                    unquoted_query = f'{clean_title} {family} filetype:pdf'
        except Exception:
            pass

    # We will try both queries
    for query, is_quoted in [(quoted_query, True), (unquoted_query, False)]:
        if not await router.acquire("duckduckgo", max_wait=60.0):
            raise TemporaryDownloadError("DuckDuckGo lock acquisition timeout")
            
        logger.info("DDG PDF Search: searching for '%s'", query)
        try:
            results = await _search_duckduckgo(query, client, max_results=5)
            if not results:
                _web_state["fails"] += 1
                if _web_state["fails"] >= _WEB_TRIP:
                    _web_state["open_until"] = _t.time() + _WEB_COOLDOWN
                    logger.warning("Web search returned nothing %d times in a row; pausing it for %d s",
                                   _web_state["fails"], _WEB_COOLDOWN)
                    return False
                continue
            _web_state["fails"] = 0
                
            for res in results:
                url = res.get("url", "")
                res_title = res.get("title", "")
                if not url:
                    continue
                
                # Check similarity if it's an unquoted search or for extra safety
                if res_title and ref.title:
                    target_title_lower = ref.title.lower()
                    res_title_lower = res_title.lower()
                    score = fuzz.ratio(target_title_lower, res_title_lower)
                    partial_score = fuzz.partial_ratio(target_title_lower, res_title_lower)
                    if score < 75 and partial_score < 85:
                        logger.debug("DDG PDF Search: skipping '%s' due to low similarity (ratio: %d, partial: %d)", res_title, score, partial_score)
                        continue

                # Verify it looks like a direct PDF link
                if url.lower().endswith(".pdf") or "pdf" in url.lower():
                    # Try streaming the download
                    success = await _stream_download(client, url, dest)
                    if success:
                        logger.info("DDG PDF Search SUCCESS: found and downloaded PDF from %s", url)
                        return True
                    await asyncio.sleep(1.0)
        except TemporaryDownloadError:
            raise
        except Exception as err:
            logger.warning("DDG PDF Search failed: %s", err)
            if isinstance(err, (httpx.RequestError, httpx.HTTPStatusError)):
                raise TemporaryDownloadError(f"DuckDuckGo request error: {err}")
    return False

