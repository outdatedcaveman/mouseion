"""
Merge engine.

Given a list of References retrieved from multiple providers (each with an
associated confidence score), produce a single, maximally complete Reference.

Merge strategy  (net-positive: never lose non-trivial data)
--------------------------------------------------------------
1.  Filter out candidates whose title is a poor fuzzy match to the seed title
    (when a title-based search was used).  This prevents wrong-paper merges.
2.  Sort candidates by (confidence DESC, provider priority ASC).
3.  For each scalar field, pick the value from the highest-confidence source
    that has a non-empty value.  **Never overwrite an already-populated field
    with empty/None.**
4.  Special rules:
    - abstract: prefer the longest non-empty string (more detail = better)
    - keywords: union of all sources, deduplicated (case-insensitive), cap 30
    - authors:  merge author lists intelligently — take the longer/richer list,
      then back-fill ORCIDs and affiliations from other sources
    - editors:  same strategy as authors
    - identifiers (doi, pmid, arxiv_id): collect from all sources
    - citation_count: take the maximum
    - open_access / oa_url: any True wins
    - url: DOI URL > publisher URL > generic URL
5.  Carry over the original seed's cite_key if set.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from .models import Author, Reference, RefType


# Minimum title similarity (0–100) to accept a candidate as the same paper
_TITLE_MATCH_THRESHOLD = 75

# Provider priority order for field-level selection (lower = preferred)
_PROVIDER_PRIORITY: Dict[str, int] = {
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


def _best_source(ref: Reference) -> str:
    """Return the name of the highest-priority source on this ref."""
    if not ref.sources:
        return ""
    return min(ref.sources, key=lambda s: _PROVIDER_PRIORITY.get(s, 50))


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
        threshold = _TITLE_MATCH_THRESHOLD
        seed_has_context = bool(seed.year or seed.authors or seed.doi or seed.pmid or seed.arxiv_id or seed.isbn)
        # Short, generic title-only records are the riskiest tail case. A loose
        # token-sort match can otherwise turn "The foundations of physics" into
        # "Foundations of Physics Letters". Require near-exact agreement unless
        # another field anchors the match.
        if not seed_has_context and len(seed.title) < 45:
            threshold = 90
        if sim >= threshold:
            filtered.append((ref, conf))
    return filtered


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def _is_nonempty(val) -> bool:
    """True if val is a meaningful non-empty value."""
    if val is None:
        return False
    if isinstance(val, str) and not val.strip():
        return False
    if isinstance(val, list) and len(val) == 0:
        return False
    # RefType.UNKNOWN counts as empty for merge purposes
    if val is RefType.UNKNOWN:
        return False
    return True


def _effective_confidence(ref: Reference, conf: float) -> float:
    """Combine the per-candidate confidence with the provider priority."""
    # Lower priority number = more trusted.  Convert to a small bonus.
    prio = _priority(ref)
    # bonus ranges from ~0.05 (prio 1) to ~0.0 (prio 99)
    bonus = max(0.0, (100 - prio) / 2000.0)
    return conf + bonus


def _pick_best_scalar(
    field_name: str,
    candidates: List[Tuple[Reference, float]],
) -> object:
    """
    For a scalar field, return the value from the highest effective-confidence
    candidate that has a non-empty value.
    """
    best_val = None
    best_score = -1.0
    for ref, conf in candidates:
        val = getattr(ref, field_name, None)
        if not _is_nonempty(val):
            continue
        score = _effective_confidence(ref, conf)
        if score > best_score:
            best_score = score
            best_val = val
    return best_val


# ---------------------------------------------------------------------------
# Author merging
# ---------------------------------------------------------------------------

def _ensure_author(a) -> Author:
    """Coerce a dict or Author-like object to an Author dataclass."""
    if isinstance(a, Author):
        return a
    if isinstance(a, dict):
        return Author(
            family=a.get("family", ""),
            given=a.get("given", ""),
            orcid=a.get("orcid", ""),
            affiliation=a.get("affiliation", ""),
        )
    return Author(
        family=getattr(a, "family", ""),
        given=getattr(a, "given", ""),
        orcid=getattr(a, "orcid", ""),
        affiliation=getattr(a, "affiliation", ""),
    )


def _author_name_key(a: Author) -> str:
    """Normalised key for matching authors across sources."""
    return (a.family.lower().strip() + "|" + a.given.lower().strip()[:1])


def _author_richness(a: Author) -> int:
    """How much metadata this author entry carries."""
    score = 0
    if a.given:
        score += len(a.given)          # full given name > initial
    if a.family:
        score += len(a.family)
    if a.orcid:
        score += 20
    if a.affiliation:
        score += 10
    return score


def _merge_author_into(target: Author, donor: Author) -> None:
    """Back-fill empty fields on *target* from *donor* (in place)."""
    if not target.orcid and donor.orcid:
        target.orcid = donor.orcid
    if not target.affiliation and donor.affiliation:
        target.affiliation = donor.affiliation
    # Prefer longer given name (full name vs initial)
    if donor.given and len(donor.given) > len(target.given or ""):
        target.given = donor.given
    if donor.family and len(donor.family) > len(target.family or ""):
        target.family = donor.family


def _merge_author_lists(
    candidates: List[Tuple[Reference, float]],
    attr: str = "authors",
) -> List[Author]:
    """
    Merge author lists across candidates.

    *attr* is the Reference attribute to read ("authors" or "editors").

    Strategy:
    1. Score each candidate's author list by (orcid_count, completeness, length).
    2. Start with the best list as the base.
    3. For every other list, try to match authors by family+initial and
       back-fill ORCIDs/affiliations/full given names.
    4. If another list is longer AND its extra authors have real names,
       append the extras.
    """
    # Collect non-empty author lists with their confidence
    author_lists: List[Tuple[List[Author], float]] = []
    for ref, conf in candidates:
        alist = getattr(ref, attr, [])
        if alist:
            alist = [_ensure_author(a) for a in alist]
            author_lists.append((alist, _effective_confidence(ref, conf)))

    if not author_lists:
        return []

    # Score each list: orcid count * 100 + avg richness + length * 2
    def _list_score(authors: List[Author], conf: float) -> float:
        orcids = sum(1 for a in authors if a.orcid)
        richness = sum(_author_richness(a) for a in authors) / max(len(authors), 1)
        return orcids * 100 + richness + len(authors) * 2 + conf * 10

    author_lists.sort(key=lambda x: _list_score(x[0], x[1]), reverse=True)

    # Deep copy the best list so we don't mutate the original candidate
    base = [
        Author(
            family=a.family, given=a.given,
            orcid=a.orcid, affiliation=a.affiliation,
        )
        for a in author_lists[0][0]
    ]

    # Build a lookup by normalised key
    base_map: Dict[str, int] = {}
    for i, a in enumerate(base):
        key = _author_name_key(a)
        if key not in base_map:
            base_map[key] = i

    # Merge from remaining lists
    for authors, _conf in author_lists[1:]:
        for donor in authors:
            key = _author_name_key(donor)
            if key in base_map:
                _merge_author_into(base[base_map[key]], donor)
            else:
                # This author isn't in our base — if the donor list is longer
                # and this author has a real name, append.
                if donor.family or donor.given:
                    new_author = Author(
                        family=donor.family, given=donor.given,
                        orcid=donor.orcid, affiliation=donor.affiliation,
                    )
                    base_map[_author_name_key(new_author)] = len(base)
                    base.append(new_author)

    return base


# ---------------------------------------------------------------------------
# URL ranking
# ---------------------------------------------------------------------------

_DOI_URL_RE = re.compile(r"https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
_PUBLISHER_DOMAINS = {
    "sciencedirect", "springer", "wiley", "nature", "ieee",
    "acm", "oup", "cambridge", "tandf", "sage", "elsevier",
    "mdpi", "plos", "frontiersin", "hindawi", "bmc", "cell",
}


def _url_rank(url: Optional[str]) -> int:
    """Higher = more preferred.  DOI URLs > publisher > generic."""
    if not url:
        return 0
    if _DOI_URL_RE.search(url):
        return 30
    low = url.lower()
    for domain in _PUBLISHER_DOMAINS:
        if domain in low:
            return 20
    return 10


def _pick_best_url(
    candidates: List[Tuple[Reference, float]],
) -> Optional[str]:
    """Pick the most useful URL across all candidates."""
    best_url: Optional[str] = None
    best_rank = -1
    best_conf = -1.0
    for ref, conf in candidates:
        if not ref.url:
            continue
        rank = _url_rank(ref.url)
        eff = _effective_confidence(ref, conf)
        # Prefer higher rank; break ties by confidence
        if rank > best_rank or (rank == best_rank and eff > best_conf):
            best_url = ref.url
            best_rank = rank
            best_conf = eff
    return best_url


# ---------------------------------------------------------------------------
# Main merge function
# ---------------------------------------------------------------------------

def merge(
    seed: Reference,
    candidates: List[Tuple[Reference, float]],
) -> Reference:
    """
    Merge a list of (Reference, confidence) candidates into one Reference.

    `seed` is the original input reference.  It is always included as a
    low-confidence candidate so that any manually entered data is preserved
    when no provider has that field.

    Returns the merged Reference.  **Net-positive**: existing non-trivial data
    on the seed is never lost.
    """
    if not candidates:
        seed.normalize()
        return seed

    # Include seed at low confidence so its data is a last-resort fallback
    all_candidates: List[Tuple[Reference, float]] = list(candidates)
    seed_conf = 0.3
    for src in ("bibtex_input", "ris_input", "html_input"):
        if src in seed.sources:
            break
    else:
        seed_conf = 0.2
    all_candidates.append((seed, seed_conf))

    # Filter out clear mismatches
    all_candidates = _filter_candidates(seed, all_candidates)

    if not all_candidates:
        seed.normalize()
        return seed

    # Sort: effective confidence DESC (confidence + provider bonus)
    all_candidates.sort(
        key=lambda x: -_effective_confidence(x[0], x[1]),
    )

    merged = Reference()

    # -------------------------------------------------------------------
    # Weighted scalar fields — pick highest-confidence non-empty value
    # -------------------------------------------------------------------
    SCALAR_FIELDS = [
        "doi", "pmid", "pmcid", "arxiv_id", "isbn", "issn", "eissn",
        "title", "year", "month", "ref_type",
        "journal", "journal_abbrev", "container_title",
        "volume", "issue", "pages", "article_number", "event_name",
        "publisher", "place", "edition", "series", "num_pages",
        "language", "license",
        # url and citation_count handled separately
    ]
    for fname in SCALAR_FIELDS:
        val = _pick_best_scalar(fname, all_candidates)
        if _is_nonempty(val):
            setattr(merged, fname, val)

    # -------------------------------------------------------------------
    # URL — DOI URL > publisher URL > generic URL
    # -------------------------------------------------------------------
    merged.url = _pick_best_url(all_candidates)

    # -------------------------------------------------------------------
    # Abstract — prefer the longest non-empty string (more detail = better)
    # -------------------------------------------------------------------
    best_abstract = ""
    for ref, _conf in all_candidates:
        if ref.abstract and len(ref.abstract) > len(best_abstract):
            best_abstract = ref.abstract
    merged.abstract = best_abstract or None

    # -------------------------------------------------------------------
    # Authors — intelligent merge across sources
    # -------------------------------------------------------------------
    merged.authors = _merge_author_lists(all_candidates)

    # -------------------------------------------------------------------
    # Editors — same strategy as authors
    # -------------------------------------------------------------------
    editor_candidates = [
        (ref, conf) for ref, conf in all_candidates if ref.editors
    ]
    if editor_candidates:
        merged.editors = _merge_author_lists(editor_candidates, attr="editors")

    # -------------------------------------------------------------------
    # Keywords — union, deduplicated (case-insensitive), capped at 30
    # -------------------------------------------------------------------
    seen_kw: set = set()
    merged_kw: List[str] = []
    for ref, _conf in all_candidates:
        for kw in ref.keywords:
            kw_norm = kw.lower().strip()
            if kw_norm and kw_norm not in seen_kw:
                seen_kw.add(kw_norm)
                merged_kw.append(kw)
    merged.keywords = merged_kw[:30]

    # -------------------------------------------------------------------
    # Open access — any True wins; prefer best OA URL
    # -------------------------------------------------------------------
    for ref, _conf in all_candidates:
        if ref.open_access:
            merged.open_access = True
            break
    for ref, _conf in all_candidates:
        if ref.oa_url:
            merged.oa_url = ref.oa_url
            break

    # -------------------------------------------------------------------
    # Citation count — take the maximum
    # -------------------------------------------------------------------
    counts = [r.citation_count for r, _ in all_candidates if r.citation_count]
    merged.citation_count = max(counts) if counts else None

    # -------------------------------------------------------------------
    # Source provenance — merge all source dicts
    # -------------------------------------------------------------------
    for ref, conf in all_candidates:
        for src, c in ref.sources.items():
            merged.sources[src] = max(merged.sources.get(src, 0.0), c)

    # -------------------------------------------------------------------
    # Preserve seed's cite key if present
    # -------------------------------------------------------------------
    merged.cite_key = seed.cite_key or None

    # -------------------------------------------------------------------
    # Net-positive safety net: ensure seed's non-empty fields survive
    # -------------------------------------------------------------------
    _backfill_from_seed(merged, seed)

    merged.normalize()
    return merged


def _backfill_from_seed(merged: Reference, seed: Reference) -> None:
    """
    Final safety pass: if the seed had a non-empty value for any field and
    the merged result ended up with None/empty, restore the seed's value.
    This guarantees the merge is strictly net-positive.
    """
    # Scalar fields
    for fname in (
        "doi", "pmid", "pmcid", "arxiv_id", "isbn", "issn", "eissn", "url",
        "title", "year", "month", "abstract", "ref_type",
        "journal", "journal_abbrev", "container_title",
        "volume", "issue", "pages", "article_number", "event_name",
        "publisher", "place", "edition", "series", "num_pages",
        "language", "license", "open_access", "oa_url",
        "citation_count",
    ):
        seed_val = getattr(seed, fname, None)
        merged_val = getattr(merged, fname, None)
        if _is_nonempty(seed_val) and not _is_nonempty(merged_val):
            setattr(merged, fname, seed_val)

    # List fields — don't lose authors/editors/keywords the seed had
    if seed.authors and not merged.authors:
        merged.authors = list(seed.authors)
    if seed.editors and not merged.editors:
        merged.editors = list(seed.editors)
    if seed.keywords:
        existing = {kw.lower().strip() for kw in merged.keywords}
        for kw in seed.keywords:
            if kw.lower().strip() not in existing:
                merged.keywords.append(kw)
                existing.add(kw.lower().strip())


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
