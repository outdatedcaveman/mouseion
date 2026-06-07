"""
Batch lookup module for mass reference enrichment.

Uses batch/bulk endpoints from major APIs to resolve hundreds of references
per request instead of one at a time:

  - Semantic Scholar /paper/batch: 500 IDs per POST request
  - OpenAlex pipe-separated DOI filter: 100 DOIs per GET request
  - PubMed EPost+EFetch: 10,000 PMIDs per request

Each function takes a list of References and returns a dict mapping
ref_id -> list of (Reference, confidence) candidates for merging.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from .semaphore import SafeSemaphore
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .models import Author, RefType, Reference
from .merge import merge
from .network_budget import network_slot

logger = logging.getLogger("mouseion.batch_lookup")

# ---------------------------------------------------------------------------
# Shared HTTP client config
# ---------------------------------------------------------------------------

_USER_AGENT = "Mouseion/1.0 (academic reference manager; mailto:mouseion-user@example.com)"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _make_client(**kwargs) -> httpx.AsyncClient:
    headers = {"User-Agent": _USER_AGENT, **kwargs.pop("headers", {})}
    return httpx.AsyncClient(
        headers=headers,
        timeout=_TIMEOUT,
        follow_redirects=True,
        http2=True,
        **kwargs,
    )


def _clean_ref_ids(refs: List[Reference]) -> None:
    """
    Fix misclassified identifiers and clean DOIs in place.

    Common problems in imported data:
    - arXiv IDs stored as DOIs: "arxiv:2310.17410v1"
    - DOIs with \\r\\n, URL fragments, query strings
    - Wildcard/placeholder DOIs: "10.4236/***.2024"
    - DOIs that are actually URLs: "https://doi.org/10.xxx"
    """
    for ref in refs:
        if ref.doi:
            doi = ref.doi.strip()
            # Remove control characters
            doi = re.sub(r'[\r\n\t]', '', doi)

            # arXiv ID stored as DOI — move it
            if doi.lower().startswith('arxiv:') or re.match(r'^\d{4}\.\d{4,5}', doi):
                arxiv_id = doi.replace('arxiv:', '').replace('ARXIV:', '').strip()
                # Strip version suffix for cleanliness
                if not ref.arxiv_id:
                    ref.arxiv_id = re.sub(r'v\d+$', '', arxiv_id)
                ref.doi = None
                continue

            # Strip URL prefix
            doi = re.sub(r'^https?://doi\.org/', '', doi)
            doi = re.sub(r'^doi:', '', doi, flags=re.I)

            # Remove fragments and query strings
            doi = doi.split('#')[0]
            doi = doi.split('?')[0]

            # Remove trailing punctuation
            doi = doi.rstrip('.,;:) ')

            # Validate: DOI must start with "10."
            if not doi.startswith('10.'):
                ref.doi = None
                continue

            # Reject obvious junk DOIs
            if '*' in doi or doi.endswith('/') or len(doi) < 7:
                ref.doi = None
                continue

            ref.doi = doi


# ---------------------------------------------------------------------------
# Semantic Scholar batch: 500 IDs per POST request
# ---------------------------------------------------------------------------

_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_S2_FIELDS = (
    "paperId,externalIds,title,abstract,tldr,authors,year,venue,journal,"
    "publicationVenue,publicationDate,publicationTypes,openAccessPdf,"
    "citationCount,fieldsOfStudy,s2FieldsOfStudy"
)
_S2_BATCH_SIZE = 500   # standard POST limit is 500; faster throughput
_S2_MIN_INTERVAL = 1.1  # 1 req/s with API key
_S2_MAX_RETRIES = 3    # retry on 403/429 with exponential backoff


def _s2_id_string(ref: Reference) -> Optional[str]:
    """Convert a Reference to an S2-compatible ID string.
    Note: URL identifiers are not supported by the S2 POST /paper/batch API and cause HTTP 400.
    """
    if ref.doi:
        return f"DOI:{ref.doi}"
    if ref.arxiv_id:
        return f"ARXIV:{ref.arxiv_id}"
    if ref.pmid:
        return f"PMID:{ref.pmid}"
    return None


def _s2_parse_paper(data: dict) -> Reference:
    """Parse S2 paper JSON into a Reference (reuse SemanticScholarProvider logic)."""
    from .providers.semantic_scholar import SemanticScholarProvider
    return SemanticScholarProvider._parse_paper(data)


async def batch_semantic_scholar(
    refs: List[Reference],
    api_key: str = "",
) -> Dict[str, List[Tuple[Reference, float]]]:
    """
    Batch lookup via Semantic Scholar POST /paper/batch endpoint.

    Args:
        refs: List of References to look up.
        api_key: Optional S2 API key for higher rate limits.

    Returns:
        Dict mapping ref._batch_id -> list of (enriched_ref, confidence) pairs.
    """
    results: Dict[str, List[Tuple[Reference, float]]] = {}

    from .cache import get_default_cache
    cache = get_default_cache()
    cooldown = cache.get_cooldown("semantic_scholar")
    if cooldown and time.time() < cooldown:
        logger.info("Semantic Scholar is cooled down, skipping batch lookup")
        return results

    # Build ID list, tracking which ref each ID came from
    id_to_ref: Dict[str, Reference] = {}
    ids: List[str] = []
    for ref in refs:
        s2_id = _s2_id_string(ref)
        if s2_id:
            id_to_ref[s2_id] = ref
            ids.append(s2_id)

    if not ids:
        return results

    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    async with _make_client(headers=headers) as client:
        # Process in chunks of 500
        for i in range(0, len(ids), _S2_BATCH_SIZE):
            # Check if cooled down
            cooldown = cache.get_cooldown("semantic_scholar")
            if cooldown and time.time() < cooldown:
                logger.info("Semantic Scholar is cooled down, skipping remaining chunks")
                break
            chunk = ids[i:i + _S2_BATCH_SIZE]
            chunk_start = time.monotonic()

            try:
                resp = None
                from .quota import get_default_quota_manager
                qm = get_default_quota_manager()
                for attempt in range(_S2_MAX_RETRIES):
                    # Check if cooled down
                    cooldown = cache.get_cooldown("semantic_scholar")
                    if cooldown and time.time() < cooldown:
                        break
                    async with qm.acquire("semantic_scholar"):
                        async with network_slot("metadata", bucket="semantic_scholar", min_interval=0.5):
                            resp = await client.post(
                            f"{_S2_BASE}/paper/batch",
                            params={"fields": _S2_FIELDS},
                            json={"ids": chunk},
                        )

                    if resp.status_code in (429, 403):
                        try:
                            retry_after = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                        except (TypeError, ValueError):
                            retry_after = float(2 ** (attempt + 1))
                        wait_time = max(retry_after, 2 ** (attempt + 1))  # exponential backoff
                        cache.set_cooldown("semantic_scholar", time.time() + min(wait_time, 900.0))
                        logger.warning("S2 batch: HTTP %d for chunk %d-%d, waiting %.0fs (attempt %d/%d)",
                                       resp.status_code, i, i + len(chunk), wait_time, attempt + 1, _S2_MAX_RETRIES)
                        await asyncio.sleep(wait_time)
                        continue
                    break  # success or non-retryable error

                if resp is None or resp.status_code != 200:
                    status = resp.status_code if resp else "no response"
                    if resp and resp.status_code in (429, 403):
                        cache.set_cooldown("semantic_scholar", time.time() + 300.0)
                    logger.warning("S2 batch: HTTP %s for chunk %d-%d (exhausted retries)",
                                   status, i, i + len(chunk))
                    continue

                data_list = resp.json()
                resolved = 0
                for s2_id, paper_data in zip(chunk, data_list):
                    if paper_data is None:
                        continue
                    ref = id_to_ref.get(s2_id)
                    if not ref:
                        continue
                    try:
                        enriched = _s2_parse_paper(paper_data)
                        if ref._batch_id not in results:
                            results[ref._batch_id] = []
                        results[ref._batch_id].append((enriched, 0.9))
                        resolved += 1
                    except Exception as e:
                        logger.debug("S2 batch: parse error for %s: %s", s2_id, e)

                logger.info("S2 batch: resolved %d/%d in chunk %d-%d",
                           resolved, len(chunk), i, i + len(chunk))

            except Exception as e:
                logger.warning("S2 batch: error for chunk %d-%d: %s", i, i + len(chunk), e)

            # Rate limit: 1 req/s (or 10 req/s with key)
            elapsed = time.monotonic() - chunk_start
            min_interval = 0.1 if api_key else _S2_MIN_INTERVAL
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)

    return results


# ---------------------------------------------------------------------------
# OpenAlex batch: 100 DOIs per pipe-separated filter
# ---------------------------------------------------------------------------

_OA_BASE = "https://api.openalex.org"
_OA_BATCH_SIZE = 50  # OpenAlex documented max for OR filters; halves chunk count vs 25
_OA_MIN_INTERVAL = 0.15  # ~7 req/s to stay under 10 req/s limit


def _oa_parse_work(data: dict) -> Reference:
    """Parse OpenAlex work JSON into a Reference."""
    from .providers.openalex import OpenAlexProvider
    return OpenAlexProvider._parse_work(data)
async def batch_openalex(
    refs: List[Reference],
    email: str = "",
    api_key: str = "",
) -> Dict[str, List[Tuple[Reference, float]]]:
    """
    Batch lookup via OpenAlex pipe-separated DOI filter.

    Only works for refs with DOIs. Uses:
      GET /works?filter=doi:DOI1|DOI2|...|DOI50&per_page=50

    Args:
        refs: List of References (only those with DOIs are processed).
        email: Email for polite pool.
        api_key: Optional OpenAlex API key.

    Returns:
        Dict mapping ref._batch_id -> list of (enriched_ref, confidence) pairs.
    """
    results: Dict[str, List[Tuple[Reference, float]]] = {}

    from .cache import get_default_cache
    cache = get_default_cache()
    cooldown = cache.get_cooldown("openalex")
    if cooldown and time.time() < cooldown:
        logger.info("OpenAlex is cooled down, skipping batch lookup")
        return results

    # Group refs by DOI
    doi_to_ref: Dict[str, Reference] = {}
    for ref in refs:
        if ref.doi:
            doi_to_ref[ref.doi.lower()] = ref

    if not doi_to_ref:
        return results

    dois = list(doi_to_ref.keys())
    concurrency = 16 if (api_key or email) else 4
    sem = SafeSemaphore(concurrency)

    async def _lookup_chunk(client, chunk, chunk_idx):
        # Check if already cooled down by a sibling chunk
        cooldown = cache.get_cooldown("openalex")
        if cooldown and time.time() < cooldown:
            return
        async with sem:
            # Check cooldown again inside semaphore
            cooldown = cache.get_cooldown("openalex")
            if cooldown and time.time() < cooldown:
                return
            # Build pipe-separated DOI filter
            doi_filter = "|".join(f"https://doi.org/{d}" for d in chunk)
            params: dict = {
                "filter": f"doi:{doi_filter}",
                "per_page": len(chunk),
                "select": "id,doi,title,authorships,publication_year,publication_date,"
                          "type,primary_location,locations,open_access,cited_by_count,"
                          "biblio,concepts,topics,keywords,language,abstract_inverted_index",
            }
            if api_key:
                params["api_key"] = api_key
            elif email:
                params["mailto"] = email

            # Stagger starting requests to avoid overloading OpenAlex pool
            stagger = 0.02 if (api_key or email) else 0.1
            await asyncio.sleep(chunk_idx * stagger)

            try:
                from .quota import get_default_quota_manager
                qm = get_default_quota_manager()
                async with qm.acquire("openalex"):
                    async with network_slot("metadata", bucket="openalex", min_interval=0.2):
                        resp = await client.get(f"{_OA_BASE}/works", params=params)

                if resp.status_code == 429:
                    try:
                        retry_after = float(resp.headers.get("Retry-After", 60.0))
                    except (TypeError, ValueError):
                        retry_after = 60.0
                    cooldown_time = time.time() + min(max(retry_after, 60.0), 900.0)
                    cache.set_cooldown("openalex", cooldown_time)
                    logger.warning("OpenAlex batch: 429 Too Many Requests, cooling down for %.0fs, skipping chunk", retry_after)
                    return

                if resp.status_code != 200:
                    logger.warning("OpenAlex batch: HTTP %d for chunk %d",
                                   resp.status_code, chunk_idx)
                    return

                works = resp.json().get("results", [])
                resolved = 0
                for work in works:
                    work_doi = (work.get("doi") or "").replace("https://doi.org/", "").lower()
                    ref = doi_to_ref.get(work_doi)
                    if not ref:
                        continue
                    try:
                        enriched = _oa_parse_work(work)
                        if ref._batch_id not in results:
                            results[ref._batch_id] = []
                        results[ref._batch_id].append((enriched, 0.85))
                        resolved += 1
                    except Exception as e:
                        logger.debug("OpenAlex batch: parse error for %s: %s", work_doi, e)

                logger.info("OpenAlex batch: resolved %d/%d in chunk %d",
                            resolved, len(chunk), chunk_idx)

            except Exception as e:
                logger.warning("OpenAlex batch: error for chunk %d: %s", chunk_idx, e)

    async with _make_client() as client:
        tasks = []
        for chunk_idx, i in enumerate(range(0, len(dois), _OA_BATCH_SIZE)):
            chunk = dois[i:i + _OA_BATCH_SIZE]
            tasks.append(_lookup_chunk(client, chunk, chunk_idx))
        await asyncio.gather(*tasks)

    return results


# ---------------------------------------------------------------------------
# CrossRef batch: filter-based DOI lookup
# ---------------------------------------------------------------------------

_CR_BASE = "https://api.crossref.org"
_CR_CONCURRENCY = 5    # parallel requests to CrossRef (polite pool ~50/s max)
_CR_MIN_INTERVAL = 0.25  # per-request minimum gap → ~4 effective req/s


def _cr_parse_work(data: dict) -> Reference:
    """Parse CrossRef work JSON into a Reference."""
    from .providers.crossref import CrossRefProvider
    return CrossRefProvider._parse_work(data)


async def batch_crossref(
    refs: List[Reference],
    email: str = "",
) -> Dict[str, List[Tuple[Reference, float]]]:
    """
    Batch lookup via parallel CrossRef individual DOI lookups.

    CrossRef doesn't support OR-based multi-DOI filters, so we use
    concurrent GET /works/{doi} requests with a semaphore for rate limiting.

    Returns:
        Dict mapping ref._batch_id -> list of (enriched_ref, confidence) pairs.
    """
    results: Dict[str, List[Tuple[Reference, float]]] = {}

    from .cache import get_default_cache
    cache = get_default_cache()
    cooldown = cache.get_cooldown("crossref")
    if cooldown and time.time() < cooldown:
        logger.info("CrossRef is cooled down, skipping batch lookup")
        return results

    doi_to_ref: Dict[str, Reference] = {}
    for ref in refs:
        if ref.doi:
            doi_to_ref[ref.doi.lower()] = ref

    if not doi_to_ref:
        return results

    headers = {}
    if email:
        headers["User-Agent"] = f"Mouseion/1.0 (mailto:{email})"

    concurrency = 30 if email else 10
    sem = SafeSemaphore(concurrency)

    async def _lookup_one(client: httpx.AsyncClient, doi: str, ref: Reference):
        # Check cooldown
        cooldown = cache.get_cooldown("crossref")
        if cooldown and time.time() < cooldown:
            return
        async with sem:
            cooldown = cache.get_cooldown("crossref")
            if cooldown and time.time() < cooldown:
                return
            try:
                encoded_doi = quote(doi, safe="/")
                resp = None
                from .quota import get_default_quota_manager
                qm = get_default_quota_manager()
                for attempt in range(3):
                    # Check cooldown
                    cooldown = cache.get_cooldown("crossref")
                    if cooldown and time.time() < cooldown:
                        break
                    async with qm.acquire("crossref"):
                        async with network_slot("metadata", bucket="crossref", min_interval=0.15):
                            resp = await client.get(f"{_CR_BASE}/works/{encoded_doi}", timeout=httpx.Timeout(5.0, connect=2.0))

                    if resp.status_code == 429:
                        try:
                            retry_after = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                        except (TypeError, ValueError):
                            retry_after = float(2 ** (attempt + 1))
                        wait_time = max(retry_after, 2 ** (attempt + 1))
                        cache.set_cooldown("crossref", time.time() + min(wait_time, 900.0))
                        logger.debug("CrossRef: 429 for %s, waiting %.0fs (attempt %d/3)",
                                     doi[:30], wait_time, attempt + 1)
                        await asyncio.sleep(wait_time)
                        continue
                    break

                if resp is None or resp.status_code != 200:
                    return

                item = resp.json().get("message", {})
                enriched = _cr_parse_work(item)
                if ref._batch_id not in results:
                    results[ref._batch_id] = []
                results[ref._batch_id].append((enriched, 0.95))

            except Exception as e:
                logger.debug("CrossRef: error for %s: %s", doi[:30], e)

            min_interval = 0.025 if email else 0.1
            await asyncio.sleep(min_interval)

    async with _make_client(headers=headers) as client:
        tasks = [
            _lookup_one(client, doi, ref)
            for doi, ref in doi_to_ref.items()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    resolved = sum(1 for v in results.values() if v)
    logger.info("CrossRef batch: resolved %d/%d", resolved, len(doi_to_ref))

    return results


# ---------------------------------------------------------------------------
# Open Library batch: multiple ISBNs via /api/books?bibkeys=ISBN:X,ISBN:Y
# ---------------------------------------------------------------------------

_OL_BASE = "https://openlibrary.org"
_OL_BATCH_SIZE = 50  # conservative; comma-separated bibkeys
_OL_MIN_INTERVAL = 0.35  # ~3 req/s identified


async def batch_openlibrary(
    refs: List[Reference],
) -> Dict[str, List[Tuple[Reference, float]]]:
    """
    Batch lookup via Open Library /api/books endpoint.

    Uses: GET /api/books?bibkeys=ISBN:X,ISBN:Y,...&format=json&jscmd=data

    Only processes refs with ISBNs.

    Returns:
        Dict mapping ref._batch_id -> list of (enriched_ref, confidence) pairs.
    """
    results: Dict[str, List[Tuple[Reference, float]]] = {}

    from .cache import get_default_cache
    cache = get_default_cache()
    cooldown = cache.get_cooldown("openlibrary")
    if cooldown and time.time() < cooldown:
        logger.info("Open Library is cooled down, skipping batch lookup")
        return results

    isbn_to_ref: Dict[str, Reference] = {}
    for ref in refs:
        if ref.isbn:
            isbn_to_ref[ref.isbn.strip()] = ref

    if not isbn_to_ref:
        return results

    isbns = list(isbn_to_ref.keys())
    sem = SafeSemaphore(4)

    async def _lookup_chunk(client, chunk, chunk_idx):
        # Check cooldown
        cooldown = cache.get_cooldown("openlibrary")
        if cooldown and time.time() < cooldown:
            return
        async with sem:
            cooldown = cache.get_cooldown("openlibrary")
            if cooldown and time.time() < cooldown:
                return
            bibkeys = ",".join(f"ISBN:{isbn}" for isbn in chunk)
            params = {
                "bibkeys": bibkeys,
                "format": "json",
                "jscmd": "data",
            }
            await asyncio.sleep(chunk_idx * 0.1)
            try:
                from .quota import get_default_quota_manager
                qm = get_default_quota_manager()
                async with qm.acquire("openlibrary"):
                    async with network_slot("metadata", bucket="openlibrary", min_interval=0.2):
                        resp = await client.get(f"{_OL_BASE}/api/books", params=params)

                if resp.status_code in (429, 403):
                    try:
                        retry_after = float(resp.headers.get("Retry-After", 60.0))
                    except (TypeError, ValueError):
                        retry_after = 60.0
                    cache.set_cooldown("openlibrary", time.time() + 600.0)
                    logger.warning("OpenLibrary batch: HTTP %d, cooling down, skipping chunk", resp.status_code)
                    return

                if resp.status_code != 200:
                    logger.warning("OpenLibrary batch: HTTP %d for chunk %d",
                                   resp.status_code, chunk_idx)
                    return

                data = resp.json()
                resolved = 0
                for isbn in chunk:
                    key = f"ISBN:{isbn}"
                    book_data = data.get(key)
                    if not book_data:
                        continue
                    ref = isbn_to_ref.get(isbn)
                    if not ref:
                        continue
                    try:
                        enriched = _ol_parse_book(book_data, isbn)
                        if enriched:
                            if ref._batch_id not in results:
                                results[ref._batch_id] = []
                            results[ref._batch_id].append((enriched, 0.85))
                            resolved += 1
                    except Exception as e:
                        logger.debug("OpenLibrary batch: parse error for %s: %s", isbn, e)

                logger.info("OpenLibrary batch: resolved %d/%d in chunk %d",
                            resolved, len(chunk), chunk_idx)

            except Exception as e:
                logger.warning("OpenLibrary batch: error for chunk %d: %s", chunk_idx, e)

    async with _make_client() as client:
        tasks = []
        for chunk_idx, i in enumerate(range(0, len(isbns), _OL_BATCH_SIZE)):
            chunk = isbns[i:i + _OL_BATCH_SIZE]
            tasks.append(_lookup_chunk(client, chunk, chunk_idx))
        await asyncio.gather(*tasks)

    return results


def _ol_parse_book(data: dict, isbn: str) -> Optional[Reference]:
    """Parse Open Library /api/books jscmd=data response into a Reference."""
    ref = Reference()
    ref.ref_type = RefType.BOOK
    ref.isbn = isbn

    ref.title = data.get("title", "").strip() or None
    subtitle = data.get("subtitle", "").strip()
    if subtitle and ref.title:
        ref.title = f"{ref.title}: {subtitle}"

    # Authors
    for a in data.get("authors", []):
        name = a.get("name", "")
        if name:
            parts = name.rsplit(" ", 1)
            if len(parts) == 2:
                ref.authors.append(Author(family=parts[1], given=parts[0]))
            else:
                ref.authors.append(Author(family=name))

    # Year from publish_date
    import re as _re
    pub_date = data.get("publish_date", "")
    m = _re.search(r"\d{4}", pub_date)
    if m:
        ref.year = int(m.group(0))

    # Publisher
    publishers = data.get("publishers", [])
    if publishers:
        name = publishers[0].get("name", "") if isinstance(publishers[0], dict) else str(publishers[0])
        if name:
            ref.publisher = name

    # Pages
    pages = data.get("number_of_pages")
    if pages:
        ref.pages = str(pages)

    # URL
    ref.url = data.get("url") or None

    # Subjects → keywords
    subjects = data.get("subjects", [])
    ref.keywords = [s.get("name", "") for s in subjects[:10] if isinstance(s, dict)]

    if not ref.title:
        return None

    ref.sources["openlibrary"] = 0.85
    return ref


# ---------------------------------------------------------------------------
# Orchestrator: enrich a batch using all batch APIs
# ---------------------------------------------------------------------------

async def enrich_batch_by_id(
    refs: List[Reference],
    s2_api_key: str = "",
    oa_email: str = "",
    oa_api_key: str = "",
    cr_email: str = "",
) -> Dict[str, Reference]:
    """
    Enrich a batch of references using batch API endpoints.

    Runs S2 batch, OpenAlex batch, and CrossRef batch concurrently,
    then merges results per-reference.

    Args:
        refs: List of References to enrich.
        s2_api_key: Semantic Scholar API key.
        oa_email: OpenAlex polite pool email.
        oa_api_key: OpenAlex API key.
        cr_email: CrossRef polite pool email.

    Returns:
        Dict mapping ref._batch_id -> merged enriched Reference.
    """
    if not refs:
        return {}

    # Clean and fix misclassified identifiers before sending to APIs
    _clean_ref_ids(refs)

    # Run all batch lookups with bulk/batch endpoints concurrently
    s2_task = batch_semantic_scholar(refs, api_key=s2_api_key)
    oa_task = batch_openalex(refs, email=oa_email, api_key=oa_api_key)
    ol_task = batch_openlibrary(refs)  # ISBN refs

    s2_results, oa_results, ol_results = await asyncio.gather(
        s2_task, oa_task, ol_task,
        return_exceptions=True,
    )

    # Handle exceptions
    if isinstance(s2_results, Exception):
        logger.warning("S2 batch failed: %s", s2_results)
        s2_results = {}
    if isinstance(oa_results, Exception):
        logger.warning("OpenAlex batch failed: %s", oa_results)
        oa_results = {}
    if isinstance(ol_results, Exception):
        logger.warning("OpenLibrary batch failed: %s", ol_results)
        ol_results = {}

    # Identify references still unresolved (no candidate from S2, OpenAlex, or OpenLibrary)
    unresolved_refs = []
    for ref in refs:
        has_candidate = (
            ref._batch_id in s2_results
            or ref._batch_id in oa_results
            or ref._batch_id in ol_results
        )
        if not has_candidate and ref.doi:
            unresolved_refs.append(ref)

    # Fall back to CrossRef only for those unresolved references with DOIs
    cr_results = {}
    if unresolved_refs:
        try:
            logger.info("CrossRef batch: falling back for %d unresolved refs", len(unresolved_refs))
            cr_results = await batch_crossref(unresolved_refs, email=cr_email)
        except Exception as e:
            logger.warning("CrossRef batch failed: %s", e)

    # Merge per-reference
    enriched: Dict[str, Reference] = {}
    for ref in refs:
        candidates: List[Tuple[Reference, float]] = []
        candidates.extend(s2_results.get(ref._batch_id, []))
        candidates.extend(oa_results.get(ref._batch_id, []))
        candidates.extend(cr_results.get(ref._batch_id, []))
        candidates.extend(ol_results.get(ref._batch_id, []))

        if not candidates:
            # No API found a match — don't write back to the result dict.
            # _process_tier will mark this as "no match" without touching the DB.
            continue

        try:
            enriched[ref._batch_id] = merge(ref, candidates)
        except Exception as e:
            logger.debug("Merge error for %s: %s", ref._batch_id[:8], e)
            # Fallback to best candidate
            candidates.sort(key=lambda c: c[1], reverse=True)
            enriched[ref._batch_id] = candidates[0][0]

    return enriched
