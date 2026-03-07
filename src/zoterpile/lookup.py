"""
Lookup orchestrator.

This is the entry point for enriching a Reference (or a batch of them).
It coordinates all providers in parallel, collects results, and runs the
merge engine to produce one maximally complete output Reference per input.

Algorithm (per reference)
-------------------------
1.  For each provider, call provider.lookup(ref) concurrently.
2.  Collect all (result, confidence) pairs.
3.  Pass everything to merge.merge() together with the seed reference.
4.  Return the merged Reference.

For bulk processing, references are processed concurrently up to
`batch_concurrency` at a time so we don't overwhelm providers.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Iterable, List, Optional

from .merge import merge
from .models import Reference
from .providers.base import BaseProvider
from .providers import DEFAULT_PROVIDERS


async def enrich_one(
    ref: Reference,
    providers: Optional[List[BaseProvider]] = None,
) -> Reference:
    """
    Enrich a single Reference using all providers concurrently.
    Returns the merged, maximally complete Reference.
    """
    if providers is None:
        providers = DEFAULT_PROVIDERS

    # Fire all providers concurrently
    tasks = [asyncio.create_task(p.lookup(ref)) for p in providers]
    results_per_provider = await asyncio.gather(*tasks, return_exceptions=True)

    candidates = []
    for provider, result in zip(providers, results_per_provider):
        if isinstance(result, BaseException):
            # Log but don't crash on a single provider failure
            continue
        for candidate in result:
            conf = candidate.sources.get(provider.name, 0.7)
            candidates.append((candidate, conf))

    return merge(ref, candidates)


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
        providers = DEFAULT_PROVIDERS

    semaphore = asyncio.Semaphore(concurrency)
    total = len(refs)
    results: List[Optional[Reference]] = [None] * total

    async def _enrich_with_sem(i: int, ref: Reference) -> None:
        async with semaphore:
            result = await enrich_one(ref, providers)
            results[i] = result
            if progress_callback:
                progress_callback(i + 1, total, result)

    await asyncio.gather(*[_enrich_with_sem(i, ref) for i, ref in enumerate(refs)])

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
