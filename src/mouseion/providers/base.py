"""
BaseProvider — abstract base class for all academic database providers.

Responsibilities
----------------
* Define the interface every provider must implement
* Manage per-provider rate limiting (asyncio.Semaphore + min interval)
* Provide shared HTTP client with connection pooling
* Provide a unified `lookup()` entry point that dispatches to the correct
  method based on what identifiers the reference has
* Cache raw API responses via the cache layer
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from ..semaphore import SafeSemaphore
from typing import List, Optional

import httpx

import re as _re
import unicodedata as _ud

from ..models import Reference


def clean_query_title(title: str) -> str:
    """
    Normalize a title for use as a search query against academic APIs.

    Must handle every kind of garbage that ends up in a title field:
    URLs, DOIs, HTML entities, file paths, notification badges, site names,
    journal volume/issue suffixes, parenthetical noise, smart quotes, etc.

    The goal is a clean human-readable title string that academic APIs
    (CrossRef, OpenAlex, S2) can match against.  If nothing usable remains,
    falls back to the original string.
    """
    if not title:
        return ""
    s = title.strip()

    # ── 0. Decode HTML entities (&#39; &amp; &lt; etc.) ──
    import html as _html
    s = _html.unescape(s)

    # ── 0a. Strip encoding-garbled characters ──
    # Unicode replacement chars (U+FFFD), black diamonds (◆ U+25C6), white squares (□ U+25A1),
    # and other common mojibake artifacts that appear when non-Latin text is mis-decoded
    s = _re.sub(r'[\ufffd\u25a0-\u25ff\u2700-\u27bf]+', ' ', s)
    # If title starts with garbled chars followed by a colon/comma, strip the prefix
    # e.g. "◆◆◆◆◆◆◆◆: Z. Ognjanovic, Logics with..." → "Z. Ognjanovic, Logics with..."
    # e.g. "은은숙, Pedagogical Implication..." → keep (valid Korean)
    s = _re.sub(r'^\s*[\s\ufffd\u25a0-\u25ff\u2700-\u27bf]+\s*[,:;]\s*', '', s)
    # Strip runs of box-drawing / geometric shapes at the start
    s = _re.sub(r'^[\u2500-\u257f\u2580-\u259f\u25a0-\u25ff\s]{3,}[,:;]?\s*', '', s)

    # ── 0b. Handle "Author, Title" pattern (common in RIS/BibTeX title fields) ──
    # "LastName, F., Real Title Here" or "P. Masani, Masani P.. Title..."
    # Also: "Neil Barton, Is (Un)Countabilism Restrictive"
    m = _re.match(r'^([A-Z][a-z]+(?:\s+[A-Z]\.?)+)\s*,\s+(.{15,})$', s)
    if m:
        s = m.group(2)
    # "P. Author, Author P.. Title..." or "Author P., Title..."
    m = _re.match(r'^(?:[A-Z]\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z]\.?)*\s*,\s*(?:[A-Z][a-z]+\s+[A-Z]\.?\s*\.?\s*\.?\s*,?\s*)?(.{15,})$', s)
    if m:
        s = m.group(1)
    # "Firstname Lastname, Title..." (two-word author name before comma)
    m = _re.match(r'^[A-Z][a-z]+\s+[A-Z][a-z]+\s*,\s+(.{15,})$', s)
    if m:
        s = m.group(1)

    # ── 0c. Strip LaTeX commands ──
    # e.g. "${\mathrm{CFT}}_{{D}}$ from ${\mathrm{T..." → "CFT D from T..."
    s = _re.sub(r'\$[^$]*\$', lambda m: _re.sub(r'\\(?:mathrm|mathbf|mathcal|mathit|text)\{([^}]*)\}', r'\1',
                _re.sub(r'[\\${}_^]', ' ', m.group(0))), s)
    s = _re.sub(r'\\(?:mathrm|mathbf|mathcal|mathit|text)\{([^}]*)\}', r'\1', s)
    s = _re.sub(r'[{}\\$^_]', ' ', s) if '\\' in s or '{' in s else s

    # ── 0d. Reject if the entire "title" is a URL ──
    if _re.match(r'^https?://\S+$', s) or _re.match(r'^www\.\S+$', s):
        return ""

    # ── 0e. Strip arXiv ID prefixes ──
    # "[2107.14031] Title" or "2107.14031] Title" or "arXiv:2107.14031 Title"
    s = _re.sub(r'^\[?\s*\d{4}\.\d{4,5}(?:v\d+)?\s*\]?\s*', '', s)
    s = _re.sub(r'^arXiv\s*:\s*\d{4}\.\d{4,5}(?:v\d+)?\s*', '', s, flags=_re.IGNORECASE)

    # ── 0f. Detect and reject junk/placeholder titles ──
    # These are Cloudflare interstitials, CAPTCHA pages, cookie banners, etc.
    _JUNK_TITLES = {
        "just a moment", "please wait", "access denied", "page not found",
        "404 not found", "403 forbidden", "error", "untitled",
        "error: doi not found", "doi not found",
        "accept terms and conditions on jstor",
        "subscribe to read", "sign in", "log in", "loading",
        "verify you are human", "one moment please", "checking your browser",
        "attention required", "please enable javascript",
    }
    if s.strip().lower() in _JUNK_TITLES or len(s.strip()) < 5:
        return ""

    # ── 1. Remove URLs anywhere in the string ──
    # Handles http(s), ftp, doi.org links, bare DOIs, file paths
    s = _re.sub(r'https?://\S+', ' ', s)
    s = _re.sub(r'ftp://\S+', ' ', s)
    s = _re.sub(r'www\.\S+', ' ', s)
    # Bare DOIs (10.xxxx/...) — but only when they look like a DOI, not part of a title
    s = _re.sub(r'\b10\.\d{4,}/\S+', ' ', s)
    # Windows/Unix file paths
    s = _re.sub(r'[A-Z]:\\[\w\\. -]+', ' ', s)
    s = _re.sub(r'/(?:home|Users|tmp|var|mnt)/\S+', ' ', s)

    # ── 2. Remove leading noise ──
    # Notification counts: "(7) ", "(123) "
    s = _re.sub(r'^\(\d+\)\s*', '', s)
    # Tags: "[PDF]", "PDF |", "Full text |", "[HTML]", etc.
    s = _re.sub(r'^\[?\s*(?:PDF|FULL\s*TEXT|HTML|ABSTRACT|CITATION)\s*\]?\s*[|:\-–—]\s*',
                '', s, flags=_re.IGNORECASE)
    # Leading "Download", "View", "Read" verbs from link text
    s = _re.sub(r'^(?:Download|View|Read|Open|Get|Access)\s*(?:PDF|Full\s*Text|Article)?\s*[:\-–—|]?\s*',
                '', s, flags=_re.IGNORECASE)

    # ── 3. Remove trailing site/platform names ──
    # Explicit platform names first (greedy — removes everything after the separator)
    _PLATFORMS = (
        r'Academia\.edu|ResearchGate|Google\s*Scholar|Semantic\s*Scholar|'
        r'PubMed|PubMed\s*Central|PMC|JSTOR|SSRN|NBER|arXiv|bioRxiv|medRxiv|'
        r'Springer(?:Link)?|Wiley|Elsevier|ScienceDirect|Taylor\s*&?\s*Francis|'
        r'IEEE\s*Xplore|ACM\s*Digital|SAGE|Cambridge\s*(?:Core|University\s*Press)|'
        r'Oxford\s*(?:Academic|University\s*Press)|De\s*Gruyter|MDPI|Hindawi|'
        r'Frontiers|PLOS|Nature|Science(?:Mag)?|The\s*Lancet|BMJ|Cell\s*Press|'
        r'Annual\s*Reviews|Review\s*of|Quarterly\s*Journal|'
        r'Wikipedia|YouTube|Goodreads|PhilPapers|Phil\s*Archive|MathSciNet|'
        r'zbMATH|Zentralblatt|Project\s*Euclid|DBLP|HAL|EconPapers|'
        r'IDEAS\s*RePEc|RePEc|CORE|Scopus|Web\s*of\s*Science|'
        r'American\s*Economic\s*(?:Association|Review)|Full\s*[Aa]rticle'
    )
    s = _re.sub(r'\s*[\-–—|]\s*(?:' + _PLATFORMS + r').*$', '', s, flags=_re.IGNORECASE)
    # Also handle ": JournalName" suffix (e.g. "Title: Cell", "Title: Cell Systems")
    s = _re.sub(r'\s*:\s*(?:' + _PLATFORMS + r').*$', '', s, flags=_re.IGNORECASE)
    # Short journal suffixes after colon (": Cell", ": PNAS", ": eLife")
    m = _re.match(r'^(.{15,}?)\s*:\s*([A-Z][a-z]*(?:\s+[A-Z][a-z]*)?)$', s)
    if m and len(m.group(2).split()) <= 3:
        s = m.group(1).strip()

    # ── 4. Remove trailing journal volume/issue info ──
    # "Vol 125, No 4", ": Volume 12, Issue 3 (2019)", "vol. 3 no. 2 pp. 45-67"
    s = _re.sub(
        r'\s*[:\-–—]?\s*'
        r'(?:Vol(?:ume)?\.?\s*\d+[\s,;]*(?:No\.?\s*\d+)?[\s,;]*'
        r'(?:(?:Issue|Iss)\.?\s*\d+)?[\s,;]*'
        r'(?:pp?\.?\s*\d[\d\-–, ]*)?'
        r'(?:\s*\(\d{4}\))?)\s*$',
        '', s, flags=_re.IGNORECASE
    )
    # Also strip inline journal citation suffix:
    # "journal name, vol. N no. N, pp. NN-NN"
    s = _re.sub(
        r',\s*[Vv]ol\.?\s*\d+\s*(?:[Nn]o\.?\s*\d[\d\-]*)?'
        r'(?:\s*,\s*pp?\.?\s*\d[\d\-–, ]*)?\s*$',
        '', s
    )

    # ── 5. Remove trailing " - Short Tail" if it looks like a site/section name ──
    m = _re.match(r'^(.{10,}?)\s*[\-–—|]\s*(.{1,50})$', s)
    if m:
        tail = m.group(2).strip()
        tail_lower = tail.lower()
        # Strip if the tail has very few words (likely a site name) or contains
        # domain-like patterns (.com, .org, .edu)
        is_site = (
            len(tail.split()) <= 4
            and (
                _re.search(r'\.\w{2,4}$', tail)  # ends with .com/.org/.edu
                or _re.search(r'(?:journal|review|press|university|online|library|archive)',
                              tail_lower)
                or tail_lower.rstrip('.') == tail_lower.rstrip('.').title().lower()  # Title Case = likely proper name
            )
        )
        if is_site:
            s = m.group(1).strip()

    # ── 6. Remove parenthetical noise at end ──
    # "(pdf)", "(full text)", "(2019)", "(accessed 2024-01-01)", "(preprint)"
    s = _re.sub(r'\s*\(\s*(?:pdf|full\s*text|html|preprint|forthcoming|in\s*press|'
                r'accessed\s*[^)]*|\d{4}(?:\s*[,;]\s*\w+)*)\s*\)\s*$',
                '', s, flags=_re.IGNORECASE)

    # ── 7. Normalize Unicode ──
    s = _ud.normalize('NFKD', s)
    s = ''.join(c for c in s if not _ud.combining(c))
    s = _ud.normalize('NFC', s)

    # ── 8. Normalize quotes and dashes ──
    s = s.replace('\u2018', "'").replace('\u2019', "'")
    s = s.replace('\u201c', '"').replace('\u201d', '"')
    s = s.replace('\u2013', '-').replace('\u2014', '-')
    s = s.replace('\u00a0', ' ')

    # ── 9. Strip leading/trailing punctuation that isn't part of a title ──
    s = s.strip(' \t\n\r\x0b\x0c.,;:!?*#@~`|/\\()[]{}')
    s = s.strip('"\'""''')

    # ── 10. Collapse whitespace ──
    s = _re.sub(r'\s+', ' ', s).strip()

    # ── 11. Too short = probably junk ──
    if len(s) < 5:
        return ""

    return s


# Shared user-agent string used by all providers
_USER_AGENT = (
    "mouseion/0.1 (https://github.com/outdatedcaveman/mouseion; "
    "reference enrichment tool)"
)


class BaseProvider(ABC):
    """
    Abstract base for a single bibliographic data source.

    Subclasses MUST implement:
        name            — short identifier, e.g. "crossref"
        priority        — int, lower = higher priority in merge
        lookup_by_doi() — or return None if unsupported
        search()        — title-based search fallback

    Subclasses MAY implement:
        lookup_by_pmid()
        lookup_by_arxiv_id()
        lookup_by_isbn()
    """

    # --- Subclass-defined constants ---
    name: str = "base"
    priority: int = 99          # lower is better; set in each subclass

    # Rate limiting: max concurrent requests to this provider.
    # 8 keeps crossref/openalex (polite-pool limits ~50/s and 10/s, daily
    # budgets 40k/50k) comfortably fed at the daemon clamp of 10 without
    # exceeding each provider's own quota-manager budget.
    _max_concurrent: int = 8
    # Minimum seconds between requests (0 = no enforced delay)
    _min_interval: float = 0.0

    def __init__(self) -> None:
        self._semaphore = SafeSemaphore(self._max_concurrent)
        # Use a Lock to serialize the min_interval check (prevents burst)
        self._rate_lock = asyncio.Lock()
        self._last_request_time: float = 0.0
        self._shared_client: Optional[httpx.AsyncClient] = None
        # Circuit breaker: skip provider after N consecutive 429s
        self._consecutive_429s: int = 0
        self._circuit_open_until: float = 0.0  # monotonic timestamp
        self._register_quota_limits()

    def _register_quota_limits(self) -> None:
        """Register provider's limits dynamically with QuotaManager."""
        try:
            from ..quota import get_default_quota_manager, ProviderLimits
            qm = get_default_quota_manager()
            
            # Keep the persisted quota manager conservative. Provider
            # _min_interval handles short spacing, but the sliding windows are
            # what keep long daemon runs from turning a large batch into a 429
            # storm. These numbers are intentionally below advertised maxima
            # because title-search endpoints are usually stricter than direct
            # identifier lookups.
            req_per_min = 60
            req_per_hour = 2_000
            req_per_day = 20_000
            
            # Specific daily limit tuning based on provider credentials/tier
            if self.name == "crossref":
                if self._min_interval <= 0.05:  # polite pool
                    req_per_day = 80_000
                    req_per_min = 300
                    req_per_hour = 12_000
                else:
                    req_per_day = 40_000
                    req_per_min = 180
                    req_per_hour = 7_200
            elif self.name == "openalex":
                if self._min_interval <= 0.1:  # polite pool / API key (strictly 10 req/s)
                    req_per_day = 100_000
                    req_per_min = 300
                    req_per_hour = 12_000
                else:
                    req_per_day = 60_000
                    req_per_min = 180
                    req_per_hour = 7_200
            elif self.name == "semantic_scholar":
                # IMPORTANT: free and standard-key S2 BOTH use _min_interval≈1.05
                # (only _max_concurrent differs), so the tier cannot be inferred
                # from _min_interval alone — doing so left standard keys stuck on
                # free-tier quota (20/min, 10k/day), silently neutralizing the key.
                # Detect the key directly instead.
                has_key = bool(getattr(self, "_api_key", ""))
                if self._min_interval <= 0.05:  # premium key (~100 req/s)
                    req_per_day = 1_000_000
                    req_per_min = 3_000
                    req_per_hour = 120_000
                elif has_key:  # standard API key: ~1 req/s sustained
                    req_per_day = 40_000
                    req_per_min = 60
                    req_per_hour = 3_000
                else: # free/anonymous: strictly 100 req per 5 minutes = 20 req/minute average
                    req_per_day = 10_000
                    req_per_min = 20
                    req_per_hour = 1000
            elif self.name == "pubmed":
                if self._min_interval <= 0.15:
                    req_per_day = 100_000
                else:
                    req_per_day = 20_000
            elif self.name in ("arxiv_api", "arxiv"):
                req_per_day = 20_000
            
            qm.update_limits(self.name, ProviderLimits(
                requests_per_minute=req_per_min,
                requests_per_hour=req_per_hour,
                requests_per_day=req_per_day,
                min_interval_seconds=max(0.0, self._min_interval * 0.5)
            ))
        except Exception:
            pass  # robust fallback if quota manager is not initialized or fails


    # -----------------------------------------------------------------------
    # HTTP client — shared across lookups for connection pooling
    # -----------------------------------------------------------------------

    def _make_client(self, **kwargs) -> httpx.AsyncClient:
        headers = {"User-Agent": _USER_AGENT, **kwargs.pop("headers", {})}
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=True,
            http2=True,
            **kwargs,
        )

    async def _get_shared_client(self) -> httpx.AsyncClient:
        """Return a long-lived client for this provider (connection pooling)."""
        if self._shared_client is None or self._shared_client.is_closed:
            self._shared_client = self._make_client()
        return self._shared_client

    async def close(self) -> None:
        """Close the shared HTTP client (call at shutdown)."""
        if self._shared_client and not self._shared_client.is_closed:
            await self._shared_client.aclose()
            self._shared_client = None

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        max_retries: int = 1,
    ) -> Optional[httpx.Response]:
        """
        Rate-limited GET with concurrency cap and exponential-backoff retry.

        * 404 / 410 → return None immediately (resource doesn't exist)
        * 429 / 503 / 502 / 500 → retry with backoff up to max_retries
        * Network errors → retry with backoff
        * Other 4xx → return None

        Also consults the global QuotaManager before every request so that
        bulk jobs respect per-provider daily/hourly budgets.
        """
        from ..cache import get_default_cache
        from ..quota import get_default_quota_manager
        cache = get_default_cache()
        quota = get_default_quota_manager()

        cooldown_until = cache.get_cooldown(self.name)
        if cooldown_until and time.time() < cooldown_until:
            return None

        # Circuit breaker: if we've seen too many consecutive errors, skip
        if self._circuit_open_until > 0:
            if time.monotonic() < self._circuit_open_until:
                return None  # circuit open — skip silently
            # Circuit half-open: allow one request through
            self._circuit_open_until = 0.0

        # Quota check happens *before* the semaphore so we don't hold a
        # concurrency slot while waiting for the budget to clear.
        async with quota.acquire(self.name):
            for attempt in range(max_retries + 1):
                # Re-check cooldown and circuit breaker before acquiring the semaphore
                cooldown_until = cache.get_cooldown(self.name)
                if cooldown_until and time.time() < cooldown_until:
                    return None
                if self._circuit_open_until > 0 and time.monotonic() < self._circuit_open_until:
                    return None

                async with self._semaphore:
                    # Enforce minimum interval between requests.
                    # IMPORTANT: claim the next time slot atomically while
                    # holding the lock, then sleep *outside* the lock so
                    # other waiters can claim their own slots concurrently
                    # (they just get pushed further into the future).
                    # This prevents the lock from being held for the entire
                    # sleep duration, which would force all waiters to
                    # queue up sequentially behind each sleep.
                    if self._min_interval > 0:
                        async with self._rate_lock:
                            elapsed = time.monotonic() - self._last_request_time
                            sleep_for = max(0.0, self._min_interval - elapsed)
                            # Advance the "next available slot" timestamp so the
                            # next waiter queues up 1 interval after us.
                            self._last_request_time = time.monotonic() + sleep_for
                        if sleep_for > 0:
                            await asyncio.sleep(sleep_for)

                    # Re-check cooldown and circuit breaker inside the semaphore
                    cooldown_until = cache.get_cooldown(self.name)
                    if cooldown_until and time.time() < cooldown_until:
                        return None
                    if self._circuit_open_until > 0 and time.monotonic() < self._circuit_open_until:
                        return None

                    try:
                        cached = cache.get_http(self.name, url, params)
                        if cached is not None:
                            return httpx.Response(
                                status_code=int(cached.get("status_code", 200)),
                                headers=cached.get("headers") or {},
                                content=cached.get("content") or b"",
                                request=httpx.Request("GET", url),
                            )

                        from ..network_budget import network_slot
                        async with network_slot(
                            "metadata",
                            bucket=self.name,
                            min_interval=max(0.0, self._min_interval * 0.5),
                        ):
                            resp = await client.get(url, params=params, headers=headers or {})
                        if self._min_interval <= 0:
                            self._last_request_time = time.monotonic()

                        if resp.status_code in (404, 410):
                            self._consecutive_429s = 0
                            cache.set_http(self.name, url, params, resp.status_code, dict(resp.headers), resp.content, ttl=24 * 3600)
                            return None  # permanent miss

                        if resp.status_code == 429:
                            self._consecutive_429s += 1
                            try:
                                retry_after = float(resp.headers.get("Retry-After", 60.0))
                            except (TypeError, ValueError):
                                retry_after = 60.0
                            cache.set_cooldown(self.name, time.time() + min(max(retry_after, 60.0), 900.0))
                            if self._consecutive_429s >= 3:
                                # Open circuit for 60s after 3 consecutive 429s
                                backoff = min(60 * self._consecutive_429s, 300)
                                self._circuit_open_until = time.monotonic() + backoff
                                cache.set_cooldown(self.name, time.time() + backoff)
                            return None  # return None immediately on 429

                        if resp.status_code in (500, 502, 503, 504) and attempt < max_retries:
                            # We break out of semaphore context to sleep outside
                            pass
                        else:
                            resp.raise_for_status()
                            self._consecutive_429s = 0
                            if resp.status_code == 200:
                                cache.set_http(self.name, url, params, resp.status_code, dict(resp.headers), resp.content)
                            return resp

                    except httpx.HTTPStatusError:
                        if attempt >= max_retries:
                            return None
                    except httpx.RequestError:
                        # Count network errors (timeouts, connection refused, etc.)
                        # toward the circuit breaker so a flaky/down provider
                        # doesn't stall the daemon for 20+ minutes.
                        self._consecutive_429s += 1
                        if self._consecutive_429s >= 5:
                            backoff = min(30 * self._consecutive_429s, 180)
                            self._circuit_open_until = time.monotonic() + backoff
                            cache.set_cooldown(self.name, time.time() + backoff)
                            return None
                        if attempt >= max_retries:
                            return None

                # Sleep outside the semaphore context for 5xx/network errors
                await asyncio.sleep(2 ** attempt)
        return None

    # -----------------------------------------------------------------------
    # Abstract interface
    # -----------------------------------------------------------------------

    @abstractmethod
    async def lookup_by_doi(
        self, doi: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        """Look up by DOI.  Return None if not found."""

    @abstractmethod
    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        """
        Title-based search.  Return up to ~5 candidate References,
        ordered by relevance.  Return [] if nothing found.
        """

    # Optional methods — default to None / []
    async def lookup_by_pmid(
        self, pmid: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        return None

    async def lookup_by_arxiv_id(
        self, arxiv_id: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        return None

    async def lookup_by_isbn(
        self, isbn: str, client: httpx.AsyncClient
    ) -> Optional[Reference]:
        return None

    # -----------------------------------------------------------------------
    # Unified dispatch
    # -----------------------------------------------------------------------

    async def lookup(self, ref: Reference) -> List[Reference]:
        """
        Try all available identifier-based lookups for `ref`, then fall back
        to title search.  Returns a list of candidate References.

        Uses the disk cache to avoid redundant API calls for the same
        identifier (critical for bulk re-enrichment).

        This is the main entry point called by the lookup orchestrator.
        """
        from ..cache import get_default_cache
        import json as _json

        cache = get_default_cache()

        # Determine the best cache key for this ref
        cache_type = None
        cache_value = None
        if ref.doi:
            cache_type, cache_value = "doi", ref.doi
        elif ref.pmid:
            cache_type, cache_value = "pmid", ref.pmid
        elif ref.arxiv_id:
            cache_type, cache_value = "arxiv", ref.arxiv_id
        elif ref.isbn:
            cache_type, cache_value = "isbn", ref.isbn

        # Check cache for identifier-based lookups
        if cache_type:
            cached = cache.get(self.name, cache_type, cache_value)
            if cached is not None:
                try:
                    ref_data = _json.loads(cached) if isinstance(cached, str) else cached
                    from ..models import Reference as Ref
                    r = Ref(**ref_data)
                    r.sources[self.name] = 1.0
                    return [r]
                except Exception:
                    pass  # cache corrupt; fall through to live lookup

        results: List[Reference] = []
        client = await self._get_shared_client()

        # 1. DOI lookup (highest fidelity)
        if ref.doi:
            r = await self.lookup_by_doi(ref.doi, client)
            if r:
                r.sources[self.name] = 1.0
                results.append(r)
                self._cache_result(cache, "doi", ref.doi, r)
                return results

        # 2. PMID
        if ref.pmid and not results:
            r = await self.lookup_by_pmid(ref.pmid, client)
            if r:
                r.sources[self.name] = 0.95
                results.append(r)
                self._cache_result(cache, "pmid", ref.pmid, r)
                return results

        # 3. arXiv ID
        if ref.arxiv_id and not results:
            r = await self.lookup_by_arxiv_id(ref.arxiv_id, client)
            if r:
                r.sources[self.name] = 0.90
                results.append(r)
                self._cache_result(cache, "arxiv", ref.arxiv_id, r)
                return results

        # 4. ISBN
        if ref.isbn and not results:
            r = await self.lookup_by_isbn(ref.isbn, client)
            if r:
                r.sources[self.name] = 0.90
                results.append(r)
                self._cache_result(cache, "isbn", ref.isbn, r)
                return results

        # 5. Title search fallback (not cached — too variable)
        if ref.title and not results:
            cleaned_title = clean_query_title(ref.title)
            author_names = [a.family for a in ref.authors if a.family]
            candidates = await self.search(
                cleaned_title,
                authors=author_names or None,
                year=ref.year,
                client=client,
            )
            for c in candidates:
                c.sources[self.name] = 0.70
            results.extend(candidates)

        return results

    def _cache_result(self, cache, lookup_type: str, value: str, ref: Reference) -> None:
        """Store a provider result in the disk cache."""
        import json as _json
        import dataclasses
        try:
            data = dataclasses.asdict(ref)
            cache.set(self.name, lookup_type, value, _json.dumps(data, default=str))
        except Exception:
            pass  # caching is best-effort

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} priority={self.priority}>"
