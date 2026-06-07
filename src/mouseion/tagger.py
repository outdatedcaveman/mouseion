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
    from mouseion.tagger import auto_tag

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
        # ── Computer Science / AI ──
        ("machine-learning",  ["machine learning", "deep learning", "neural network",
                                "gradient descent", "backpropagation", "convolutional",
                                "random forest", "support vector", "xgboost", "ensemble"]),
        ("nlp",               ["natural language", "language model", "transformer",
                                "bert", "gpt", "text classification", "sentiment analysis",
                                "named entity", "question answering", "summarization",
                                "word embedding", "tokenization", "parsing"]),
        ("computer-vision",   ["image recognition", "object detection", "segmentation",
                                "convolutional neural", "visual", "image classification",
                                "generative adversarial", "diffusion model", "vit"]),
        ("reinforcement-learning", ["reinforcement learning", "markov decision",
                                    "reward function", "policy gradient", "q-learning",
                                    "actor-critic", "multi-agent"]),
        ("large-language-models", ["large language model", "llm", "chatgpt", "gpt-4",
                                   "instruction tuning", "rlhf", "chain of thought",
                                   "prompt engineering", "in-context learning"]),
        ("graph-neural-networks", ["graph neural", "graph convolutional", "gnn",
                                   "knowledge graph", "graph attention"]),
        ("algorithms",        ["algorithm", "computational complexity", "dynamic programming",
                                "sorting", "data structure", "approximation algorithm"]),
        ("systems",           ["distributed system", "operating system", "file system",
                                "network protocol", "cloud computing", "kubernetes",
                                "microservice", "database", "consistency"]),
        ("security",          ["cybersecurity", "cryptography", "vulnerability", "malware",
                                "intrusion detection", "privacy", "differential privacy",
                                "adversarial attack", "federated learning"]),
        ("hci",               ["human-computer interaction", "user interface", "usability",
                                "user experience", "accessibility", "interface design"]),
        # ── Biology / Medicine ──
        ("genomics",          ["genome", "genomic", "sequencing", "dna", "rna",
                                "crispr", "gene expression", "transcriptome", "epigenetics"]),
        ("neuroscience",      ["neuroscience", "neural circuit", "synaptic", "cortex",
                                "hippocampus", "fmri", "neural plasticity", "connectome"]),
        ("immunology",        ["immune", "antibody", "t cell", "b cell", "cytokine",
                                "vaccine", "inflammation", "autoimmune"]),
        ("oncology",          ["cancer", "tumor", "oncology", "metastasis", "chemotherapy",
                                "immunotherapy", "biomarker"]),
        ("epidemiology",      ["epidemiology", "cohort study", "disease incidence", "disease prevalence",
                                "public health", "pandemic", "randomized controlled trial"]),
        ("drug-discovery",    ["drug discovery", "pharmacology", "clinical trial",
                                "drug target", "molecular docking", "protein folding"]),
        # ── Physical sciences ──
        ("climate",           ["climate change", "global warming", "greenhouse gas",
                                "carbon emissions", "sea level", "climate model"]),
        ("quantum",           ["quantum computing", "qubit", "entanglement",
                                "quantum mechanics", "superposition", "quantum error correction"]),
        ("materials-science", ["material science", "nanomaterial", "semiconductor",
                                "polymer", "composite", "crystallography", "superconductor"]),
        ("astrophysics",      ["black hole", "galaxy", "cosmology", "dark matter",
                                "gravitational wave", "exoplanet", "stellar"]),
        # ── Social sciences / Humanities ──
        ("economics",         ["economic", "gdp", "monetary policy", "inflation",
                                "labour market", "fiscal", "econometrics", "market equilibrium"]),
        ("psychology",        ["cognitive", "behavioral", "mental health", "personality",
                                "social psychology", "memory", "attention", "perception"]),
        ("education",         ["pedagogy", "learning outcome", "curriculum", "higher education",
                                "student performance", "e-learning", "educational technology"]),
        ("sociology",         ["social inequality", "race", "gender", "migration",
                                "social network", "urban", "demographics"]),
        # ── Statistics / Math ──
        ("statistics",        ["bayesian", "markov chain", "monte carlo", "regression",
                                "statistical inference", "hypothesis test", "causal inference",
                                "propensity score", "survival analysis"]),
        ("optimization",      ["convex optimization", "stochastic gradient", "convergence",
                                "loss function", "regularization", "hyperparameter"]),
        # ── Engineering ──
        ("robotics",          ["robotics", "autonomous vehicle", "path planning",
                                "simultaneous localization", "robot arm", "drone"]),
        ("energy",            ["renewable energy", "solar", "wind energy", "battery",
                                "energy storage", "photovoltaic", "smart grid"]),
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
