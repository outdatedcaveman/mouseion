"""
Merge engine.

Given a list of References retrieved from multiple providers (each with an
associated confidence score), produce a single, maximally complete Reference.

Merge strategy
--------------
1.  Filter out candidates whose title is a poor fuzzy match to the seed title
    (when a title-based search was used).  This prevents wrong-paper merges.
2.  Sort candidates by (confidence DESC, provider priority ASC).
3.  For each field, take the value from the first (highest-priority) candidate
    that has a non-empty value.
4.  Special rules:
    - abstract: prefer the longest non-empty string (more detail = better)
    - keywords: union of all sources, deduplicated, capped at 20
    - authors:  prefer the list with ORCID data; fallback to longest list
    - identifiers (doi, pmid, arxiv_id): collect from all sources; flag conflicts
    - citation_count: take the maximum
    - open_access / oa_url: any True wins
5.  Carry over the original seed's cite_key if set.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from rapidfuzz import fuzz

from .models import Author, Reference
from .providers.base import BaseProvider


# Minimum title similarity (0–100) to accept a candidate as the same paper
_TITLE_MATCH_THRESHOLD = 75

# Provider priority order for field-level selection (lower = preferred)
_PROVIDER_PRIORITY = {
    "crossref":         1,
    "openalex":         2,
    "semantic_scholar": 3,
    "pubmed":           4,
    "dblp":             5,
    "arxiv":            6,
    "html_input":       90,
    "bibtex_input":     90,
    "ris_input":        90,
}


def _priority(ref: Reference) -> int:
    """Return the best (lowest) provider priority for a Reference."""
    if not ref.sources:
        return 99
    return min(_PROVIDER_PRIORITY.get(src, 50) for src in ref.sources)


def _title_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Token-sort ratio between two titles, ignoring case and punctuation."""
    if not a or not b:
        return 0.0
    return fuzz.token_sort_ratio(a.lower(), b.lower())


def _filter_candidates(
    seed: Reference,
    candidates: List[Tuple[Reference, float]],
) -> List[Tuple[Reference, float]]:
    """
    Remove candidates that are clearly a different paper.

    If the seed has no title, we can't filter — return all.
    If a candidate has no title, keep it (might fill in the title).
    """
    if not seed.title:
        return candidates

    filtered = []
    for ref, conf in candidates:
        if not ref.title:
            filtered.append((ref, conf))
            continue
        sim = _title_similarity(seed.title, ref.title)
        if sim >= _TITLE_MATCH_THRESHOLD:
            filtered.append((ref, conf))
    return filtered


def merge(
    seed: Reference,
    candidates: List[Tuple[Reference, float]],
) -> Reference:
    """
    Merge a list of (Reference, confidence) candidates into one Reference.

    `seed` is the original input reference.  It is always included as a
    low-confidence candidate so that any manually entered data is preserved
    when no provider has that field.

    Returns the merged Reference.
    """
    if not candidates:
        seed.normalize()
        return seed

    # Include seed at low confidence so its data is a last-resort fallback
    all_candidates: List[Tuple[Reference, float]] = list(candidates)
    for src in ("bibtex_input", "ris_input", "html_input"):
        if src in seed.sources:
            all_candidates.append((seed, 0.3))
            break
    else:
        # Even if no explicit input source tag, include seed at very low prio
        all_candidates.append((seed, 0.2))

    # Filter out clear mismatches
    all_candidates = _filter_candidates(seed, all_candidates)

    if not all_candidates:
        seed.normalize()
        return seed

    # Sort: confidence DESC first, then provider priority ASC
    all_candidates.sort(key=lambda x: (-x[1], _priority(x[0])))

    merged = Reference()

    # -----------------------------------------------------------------------
    # Simple "first wins" scalar fields
    # -----------------------------------------------------------------------
    SCALAR_FIELDS = [
        "doi", "pmid", "pmcid", "arxiv_id", "isbn", "issn", "eissn",
        "title", "year", "month", "ref_type",
        "journal", "journal_abbrev", "container_title",
        "volume", "issue", "pages", "article_number", "event_name",
        "publisher", "place", "edition", "series", "num_pages",
        "language", "license", "url",
        # Note: citation_count is handled separately (we take the max)
    ]
    for field in SCALAR_FIELDS:
        for ref, _conf in all_candidates:
            val = getattr(ref, field, None)
            if val is not None and val != "" and val != []:
                setattr(merged, field, val)
                break

    # -----------------------------------------------------------------------
    # Abstract — prefer longest
    # -----------------------------------------------------------------------
    best_abstract = ""
    for ref, _conf in all_candidates:
        if ref.abstract and len(ref.abstract) > len(best_abstract):
            best_abstract = ref.abstract
    merged.abstract = best_abstract or None

    # -----------------------------------------------------------------------
    # Authors — prefer list with most ORCID data; fallback to longest list
    # -----------------------------------------------------------------------
    best_authors: List[Author] = []
    best_orcid_count = -1
    for ref, _conf in all_candidates:
        if not ref.authors:
            continue
        orcid_count = sum(1 for a in ref.authors if a.orcid)
        if orcid_count > best_orcid_count or (
            orcid_count == best_orcid_count and len(ref.authors) > len(best_authors)
        ):
            best_authors = ref.authors
            best_orcid_count = orcid_count
    merged.authors = best_authors

    # -----------------------------------------------------------------------
    # Editors — same as authors logic
    # -----------------------------------------------------------------------
    for ref, _conf in all_candidates:
        if ref.editors:
            merged.editors = ref.editors
            break

    # -----------------------------------------------------------------------
    # Keywords — union, deduplicated (case-insensitive), capped at 20
    # -----------------------------------------------------------------------
    seen_kw: set = set()
    merged_kw: List[str] = []
    for ref, _conf in all_candidates:
        for kw in ref.keywords:
            kw_norm = kw.lower().strip()
            if kw_norm and kw_norm not in seen_kw:
                seen_kw.add(kw_norm)
                merged_kw.append(kw)
    merged.keywords = merged_kw[:20]

    # -----------------------------------------------------------------------
    # Open access — any True wins; prefer best OA URL
    # -----------------------------------------------------------------------
    for ref, _conf in all_candidates:
        if ref.open_access:
            merged.open_access = True
            break
    for ref, _conf in all_candidates:
        if ref.oa_url:
            merged.oa_url = ref.oa_url
            break

    # -----------------------------------------------------------------------
    # Citation count — take the maximum
    # -----------------------------------------------------------------------
    counts = [r.citation_count for r, _ in all_candidates if r.citation_count]
    merged.citation_count = max(counts) if counts else None

    # -----------------------------------------------------------------------
    # Source provenance — merge all source dicts
    # -----------------------------------------------------------------------
    for ref, conf in all_candidates:
        for src, c in ref.sources.items():
            merged.sources[src] = max(merged.sources.get(src, 0.0), c)

    # -----------------------------------------------------------------------
    # Preserve seed's cite key if present
    # -----------------------------------------------------------------------
    merged.cite_key = seed.cite_key or None

    merged.normalize()
    return merged


# ---------------------------------------------------------------------------
# Convenience: score how well two References match (for ranking)
# ---------------------------------------------------------------------------

def match_score(a: Reference, b: Reference) -> float:
    """
    Return a similarity score 0.0–1.0 between two References.
    Useful for deduplication of an output list.
    """
    score = 0.0
    checks = 0

    # DOI exact match is definitive
    if a.doi and b.doi:
        return 1.0 if a.doi.lower() == b.doi.lower() else 0.0

    if a.title and b.title:
        score += _title_similarity(a.title, b.title) / 100.0
        checks += 1

    if a.year and b.year:
        score += 1.0 if a.year == b.year else 0.0
        checks += 1

    if a.authors and b.authors:
        # Compare first author family names
        fa = (a.authors[0].family or "").lower()
        fb = (b.authors[0].family or "").lower()
        score += fuzz.ratio(fa, fb) / 100.0
        checks += 1

    return score / checks if checks else 0.0
