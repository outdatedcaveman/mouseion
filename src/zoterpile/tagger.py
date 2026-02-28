"""
Auto-tagging engine.

Assigns tags to a Reference based on configurable rules and built-in heuristics.

Rule types (from config)
------------------------
  keywords       — list of strings to match in title + abstract + keywords (case-insensitive)
  journal_pattern — regex matched against journal / container_title
  ref_type        — exact match against ref.ref_type.value ("journal-article", etc.)
  year_from/to    — numeric year range
  open_access_only — only apply to OA refs

Built-in heuristics (always applied)
--------------------------------------
  tag_by_type     — "journal", "conference", "preprint", "book", "thesis", …
  tag_open_access — "open-access" tag when ref.open_access is True
  tag_by_year     — year as a tag (e.g. "2024")

Usage
-----
    from zoterpile.tagger import auto_tag

    tags = auto_tag(ref)         # uses global config
    tags = auto_tag(ref, cfg)    # explicit config
"""

from __future__ import annotations

import re
from typing import List, Optional, TYPE_CHECKING

from .models import RefType, Reference

if TYPE_CHECKING:
    from .config import Config


# ---------------------------------------------------------------------------
# Built-in type → tag name map
# ---------------------------------------------------------------------------

_TYPE_TAGS = {
    RefType.JOURNAL:      "journal",
    RefType.BOOK:         "book",
    RefType.BOOK_CHAPTER: "book-chapter",
    RefType.CONFERENCE:   "conference",
    RefType.PREPRINT:     "preprint",
    RefType.THESIS:       "thesis",
    RefType.DATASET:      "dataset",
    RefType.REPORT:       "report",
    RefType.WEBSITE:      "website",
}


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------

def _rule_matches(rule, ref: Reference) -> bool:
    """Return True if `rule` matches this Reference."""
    # keywords: any keyword must appear in title, abstract, or keywords list
    if rule.keywords:
        haystack = " ".join(filter(None, [
            ref.title or "",
            ref.abstract or "",
            " ".join(ref.keywords),
        ])).lower()
        if not any(kw.lower() in haystack for kw in rule.keywords):
            return False

    # journal_pattern: regex on journal / container_title
    if rule.journal_pattern:
        venue = ref.journal or ref.container_title or ""
        if not re.search(rule.journal_pattern, venue, re.IGNORECASE):
            return False

    # ref_type: exact match
    if rule.ref_type:
        if ref.ref_type.value != rule.ref_type:
            return False

    # year range
    if rule.year_from is not None and (ref.year is None or ref.year < rule.year_from):
        return False
    if rule.year_to is not None and (ref.year is None or ref.year > rule.year_to):
        return False

    # open access filter
    if rule.open_access_only and not ref.open_access:
        return False

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def auto_tag(ref: Reference, cfg: Optional["Config"] = None) -> List[str]:
    """
    Return a list of tags to apply to `ref` based on config rules + heuristics.
    Tags are lowercase strings; no duplicates.
    """
    from .config import get_config
    if cfg is None:
        cfg = get_config()

    tags: List[str] = []
    seen: set = set()

    def add(tag: str) -> None:
        tag = tag.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)

    # --- Built-in heuristics ---
    if cfg.tag_by_type:
        type_tag = _TYPE_TAGS.get(ref.ref_type)
        if type_tag:
            add(type_tag)

    if cfg.tag_open_access and ref.open_access:
        add("open-access")

    if cfg.tag_by_year and ref.year:
        add(str(ref.year))

    # High-completeness badge
    if ref.completeness >= 0.9:
        add("complete")
    elif ref.completeness < 0.4:
        add("incomplete")

    # arXiv preprint flag
    if ref.arxiv_id and ref.ref_type != RefType.JOURNAL:
        add("preprint")

    # --- User-defined rules ---
    for rule in cfg.auto_tag_rules:
        if _rule_matches(rule, ref):
            for tag in rule.tags:
                add(tag)

    return tags


def tag_from_keywords(ref: Reference) -> List[str]:
    """
    Infer topic tags from the reference's own keywords using a built-in
    taxonomy of common academic fields.

    This supplements (not replaces) user-defined rules.
    """
    _TAXONOMY: List[tuple] = [
        # (tag, [trigger keywords])
        ("machine-learning",  ["machine learning", "deep learning", "neural network",
                                "gradient descent", "backpropagation", "convolutional"]),
        ("nlp",               ["natural language", "language model", "transformer",
                                "bert", "gpt", "text classification"]),
        ("computer-vision",   ["image recognition", "object detection", "segmentation",
                                "convolutional neural", "visual"]),
        ("reinforcement-learning", ["reinforcement learning", "markov decision",
                                    "reward function", "policy gradient", "q-learning"]),
        ("genomics",          ["genome", "genomic", "sequencing", "dna", "rna",
                                "crispr", "gene expression"]),
        ("neuroscience",      ["neuroscience", "neural circuit", "synaptic", "cortex",
                                "hippocampus", "fmri"]),
        ("climate",           ["climate change", "global warming", "greenhouse gas",
                                "carbon emissions", "sea level"]),
        ("economics",         ["economic", "gdp", "monetary policy", "inflation",
                                "labour market", "fiscal"]),
        ("statistics",        ["bayesian", "markov chain", "monte carlo", "regression",
                                "statistical inference", "hypothesis test"]),
        ("quantum",           ["quantum computing", "qubit", "entanglement",
                                "quantum mechanics", "superposition"]),
    ]

    haystack = " ".join(filter(None, [
        (ref.title or "").lower(),
        (ref.abstract or "").lower(),
        " ".join(ref.keywords).lower(),
    ]))

    tags: List[str] = []
    seen: set = set()
    for tag, triggers in _TAXONOMY:
        if tag not in seen and any(t in haystack for t in triggers):
            tags.append(tag)
            seen.add(tag)

    return tags
