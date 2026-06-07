"""
Lookup orchestrator.

This is the entry point for enriching a Reference (or a batch of them).
It coordinates all providers in parallel, collects results, and runs the
merge engine to produce one maximally complete output Reference per input.

Algorithm (per reference)
-------------------------
1.  Select relevant providers based on available identifiers.
2.  For each provider, call provider.lookup(ref) with a per-provider timeout.
3.  Collect all (result, confidence) pairs as they arrive.
4.  Pass everything to merge.merge() together with the seed reference.
5.  Return the merged Reference.

For bulk processing, references are processed concurrently up to
`batch_concurrency` at a time so we don't overwhelm providers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Iterable, List, Optional
from .semaphore import SafeSemaphore

from .merge import merge
from .models import Reference
from .providers.base import BaseProvider
from .providers import DEFAULT_PROVIDERS, _make_providers
from .local_resolver import find_local_candidates

logger = logging.getLogger("mouseion.lookup")

# Per-provider timeout: don't let a slow/dead provider block everything.
_PROVIDER_TIMEOUT = 90.0  # seconds per provider (increased to 90s to accommodate rate-limiting queue/sleep times under concurrency)

# Retry settings for transient provider failures.
# Keep low — the provider _get() already retries 429/5xx internally.
_MAX_RETRIES = 0  # no retries at lookup level — provider _get already retries
_RETRY_DELAYS = ()  # unused with 0 retries


def _select_providers(
    ref: Reference,
    providers: List[BaseProvider],
) -> List[BaseProvider]:
    """
    Pick which providers to query based on what identifiers the ref has.

    - DOI present: CrossRef + DOI.org + OpenAlex + Semantic Scholar + Unpaywall
    - PMID present: PubMed + CrossRef + Semantic Scholar (+Unpaywall if DOI)
    - arXiv ID: arXiv + arXiv API + Semantic Scholar + OpenAlex
    - ISBN: OpenLibrary + Google Books + CrossRef
    - Title only: CrossRef + OpenAlex + Semantic Scholar (skip slow niche ones)

    Unpaywall is appended for any ref that has a DOI (to find OA URLs).
    """
    by_name = {p.name: p for p in providers}

    if ref.doi:
        pick = ["crossref", "doi_org", "openalex", "semantic_scholar", "unpaywall"]
    elif ref.pmid:
        pick = ["pubmed", "crossref", "semantic_scholar"]
    elif ref.arxiv_id:
        pick = ["arxiv", "arxiv_api", "semantic_scholar", "openalex"]
    elif ref.isbn:
        pick = ["openlibrary", "google_books", "crossref"]
    else:
        # Title-only: use the 3 fastest providers with broadest coverage
        pick = ["crossref", "openalex", "semantic_scholar"]

    # If the ref has a DOI but wasn't in the DOI branch (e.g. PMID with DOI),
    # append unpaywall to get OA URLs.
    if ref.doi and "unpaywall" not in pick:
        pick.append("unpaywall")

    standard_names = {
        "crossref", "doi_org", "openalex", "semantic_scholar", "pubmed",
        "arxiv_api", "dblp", "arxiv", "openlibrary", "google_books", "unpaywall"
    }

    # Include picked standard providers, and any custom/mock provider (e.g. for testing)
    selected = []
    for p in providers:
        if p.name not in standard_names or p.name in pick:
            selected.append(p)
    return selected


async def enrich_one(
    ref: Reference,
    providers: Optional[List[BaseProvider]] = None,
) -> Reference:
    """
    Enrich a single Reference using selected providers concurrently.
    Each provider gets a timeout so one slow provider can't block the rest.
    Returns the merged, maximally complete Reference.
    """
    if providers is None:
        providers = _make_providers()

    local_candidates = find_local_candidates(ref)
    if local_candidates:
        try:
            ref = merge(ref, local_candidates)
        except Exception as exc:
            logger.debug("local resolver merge failed ref=%s: %s", ref.title or ref.doi, exc)

    selected = _select_providers(ref, providers)

    ref_id = ref.doi or ref.pmid or ref.arxiv_id or ref.isbn or ref.title or "unknown"

    async def _lookup_with_retry(p: BaseProvider) -> list:
        for attempt in range(_MAX_RETRIES + 1):
            logger.debug("provider=%s ref=%s attempt=%d", p.name, ref_id, attempt + 1)
            try:
                return await asyncio.wait_for(p.lookup(ref), timeout=_PROVIDER_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "provider=%s timed out (attempt %d/%d) ref=%s",
                    p.name, attempt + 1, _MAX_RETRIES + 1, ref_id,
                )
            except Exception as exc:
                logger.warning(
                    "provider=%s error (attempt %d/%d) ref=%s: %s",
                    p.name, attempt + 1, _MAX_RETRIES + 1, ref_id, exc,
                )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_DELAYS[attempt])
        return []

    # Fire selected providers concurrently
    tasks = [asyncio.create_task(_lookup_with_retry(p)) for p in selected]
    results_per_provider = await asyncio.gather(*tasks)

    candidates = list(local_candidates)
    for provider, result_list in zip(selected, results_per_provider):
        for candidate in result_list:
            conf = candidate.sources.get(provider.name, 0.7)
            candidates.append((candidate, conf))

    try:
        return merge(ref, candidates)
    except Exception as exc:
        logger.error("merge failed ref=%s: %s", ref_id, exc)
        # Fall back to the highest-confidence candidate, or the seed ref.
        if candidates:
            candidates.sort(key=lambda c: c[1], reverse=True)
            return candidates[0][0]
        return ref


async def enrich_batch(
    refs: List[Reference],
    providers: Optional[List[BaseProvider]] = None,
    concurrency: int = 20,
    progress_callback: Optional[Callable[[int, int, Reference], None]] = None,
) -> List[Reference]:
    """
    Enrich a list of References with bounded concurrency.

    `concurrency`: how many references to process simultaneously.
    `progress_callback(index, total, result)`: called after each reference
        completes, useful for progress bars.
    """
    if providers is None:
        providers = _make_providers()

    logger.info("Starting enrichment of %d references (concurrency=%d)", len(refs), concurrency)

    semaphore = SafeSemaphore(concurrency)
    total = len(refs)
    results: List[Optional[Reference]] = [None] * total

    async def _enrich_with_sem(i: int, ref: Reference) -> None:
        async with semaphore:
            result = await enrich_one(ref, providers)
            results[i] = result
            if progress_callback:
                progress_callback(i + 1, total, result)

    await asyncio.gather(*[_enrich_with_sem(i, ref) for i, ref in enumerate(refs)])

    # Clean up shared HTTP clients
    for p in providers:
        await p.close()

    # Replace any None results (failed lookups) with the original reference
    return [r if r is not None else refs[i] for i, r in enumerate(results)]


def enrich_batch_sync(
    refs: List[Reference],
    providers: Optional[List[BaseProvider]] = None,
    concurrency: int = 20,
    progress_callback: Optional[Callable[[int, int, Reference], None]] = None,
) -> List[Reference]:
    """Synchronous wrapper around enrich_batch for non-async callers."""
    return asyncio.run(
        enrich_batch(refs, providers, concurrency, progress_callback)
    )


def enrich_one_sync(
    ref: Reference,
    providers: Optional[List[BaseProvider]] = None,
) -> Reference:
    """Synchronous wrapper around enrich_one."""
    return asyncio.run(enrich_one(ref, providers))
