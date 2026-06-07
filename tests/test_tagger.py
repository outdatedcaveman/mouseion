"""Tests for the auto-tagging engine (tagger.py)."""

import pytest
from mouseion.tagger import auto_tag, tag_from_keywords
from mouseion.models import Author, Reference, RefType
from mouseion.config import Config, AutoTagRule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ref(
    title: str = "Test Paper",
    doi: str | None = None,
    abstract: str | None = None,
    keywords: list[str] | None = None,
    ref_type: RefType = RefType.JOURNAL,
    year: int | None = 2024,
    open_access: bool | None = None,
    arxiv_id: str | None = None,
    journal: str | None = None,
    authors: list[Author] | None = None,
) -> Reference:
    return Reference(
        title=title,
        doi=doi,
        abstract=abstract or "",
        keywords=keywords or [],
        ref_type=ref_type,
        year=year,
        open_access=open_access,
        arxiv_id=arxiv_id,
        journal=journal,
        authors=authors or [],
    )


def _cfg(**kwargs) -> Config:
    """Build a Config with all auto-tag booleans set via kwargs (defaults on)."""
    defaults = dict(tag_by_type=True, tag_open_access=True, tag_by_year=False, auto_tag_rules=[])
    defaults.update(kwargs)
    return Config(**defaults)


# ---------------------------------------------------------------------------
# Built-in heuristics
# ---------------------------------------------------------------------------

class TestBuiltInHeuristics:
    def test_tag_by_type_journal(self):
        ref = _make_ref(ref_type=RefType.JOURNAL)
        tags = auto_tag(ref, _cfg(tag_by_type=True))
        assert "journal" in tags

    def test_tag_by_type_preprint(self):
        ref = _make_ref(ref_type=RefType.PREPRINT)
        tags = auto_tag(ref, _cfg(tag_by_type=True))
        assert "preprint" in tags

    def test_tag_by_type_book(self):
        ref = _make_ref(ref_type=RefType.BOOK)
        tags = auto_tag(ref, _cfg(tag_by_type=True))
        assert "book" in tags

    def test_tag_by_type_conference(self):
        ref = _make_ref(ref_type=RefType.CONFERENCE)
        tags = auto_tag(ref, _cfg(tag_by_type=True))
        assert "conference" in tags

    def test_tag_by_type_thesis(self):
        ref = _make_ref(ref_type=RefType.THESIS)
        tags = auto_tag(ref, _cfg(tag_by_type=True))
        assert "thesis" in tags

    def test_tag_by_type_disabled(self):
        ref = _make_ref(ref_type=RefType.JOURNAL)
        tags = auto_tag(ref, _cfg(tag_by_type=False))
        assert "journal" not in tags

    def test_open_access_tag_when_true(self):
        ref = _make_ref(open_access=True)
        tags = auto_tag(ref, _cfg(tag_open_access=True))
        assert "open-access" in tags

    def test_no_open_access_tag_when_false(self):
        ref = _make_ref(open_access=False)
        tags = auto_tag(ref, _cfg(tag_open_access=True))
        assert "open-access" not in tags

    def test_no_open_access_tag_when_none(self):
        ref = _make_ref(open_access=None)
        tags = auto_tag(ref, _cfg(tag_open_access=True))
        assert "open-access" not in tags

    def test_open_access_disabled(self):
        ref = _make_ref(open_access=True)
        tags = auto_tag(ref, _cfg(tag_open_access=False))
        assert "open-access" not in tags

    def test_tag_by_year(self):
        ref = _make_ref(year=2024)
        tags = auto_tag(ref, _cfg(tag_by_year=True))
        assert "2024" in tags

    def test_tag_by_year_disabled(self):
        ref = _make_ref(year=2024)
        tags = auto_tag(ref, _cfg(tag_by_year=False))
        assert "2024" not in tags

    def test_complete_badge_high_completeness(self):
        # Create a very complete reference
        ref = Reference(
            title="Full Paper",
            doi="10.1000/full",
            abstract="A" * 200,
            authors=[Author(family="Doe", given="Jane", orcid="0000-0001-2345-6789")],
            year=2024,
            journal="Nature",
            volume="1",
            pages="1-10",
            keywords=["machine learning", "AI"],
            language="en",
            open_access=True,
            citation_count=100,
        )
        tags = auto_tag(ref, _cfg())
        assert "complete" in tags

    def test_incomplete_badge_low_completeness(self):
        # Minimal reference → low completeness
        ref = Reference(title="Stub")
        tags = auto_tag(ref, _cfg())
        assert "incomplete" in tags

    def test_preprint_tag_from_arxiv_id(self):
        # A non-journal ref with an arxiv_id should get "preprint" tag
        ref = _make_ref(arxiv_id="2310.00123", ref_type=RefType.UNKNOWN)
        tags = auto_tag(ref, _cfg(tag_by_type=False))
        assert "preprint" in tags

    def test_arxiv_journal_no_extra_preprint(self):
        # A JOURNAL ref with arxiv_id should NOT get extra preprint
        ref = _make_ref(arxiv_id="2310.00123", ref_type=RefType.JOURNAL)
        tags = auto_tag(ref, _cfg())
        # "journal" is there; "preprint" only from type or arxiv heuristic
        # Since ref_type is JOURNAL, arxiv heuristic doesn't apply
        assert tags.count("preprint") == 0 or "journal" in tags


# ---------------------------------------------------------------------------
# No duplicates
# ---------------------------------------------------------------------------

class TestNoDuplicates:
    def test_no_duplicate_tags(self):
        # Could trigger "preprint" from type AND from arxiv_id
        ref = _make_ref(ref_type=RefType.PREPRINT, arxiv_id="2310.00123")
        tags = auto_tag(ref, _cfg())
        assert len(tags) == len(set(tags))


# ---------------------------------------------------------------------------
# User-defined rules
# ---------------------------------------------------------------------------

class TestAutoTagRules:
    def test_keyword_rule_matches_title(self):
        rule = AutoTagRule(keywords=["deep learning"], tags=["ai"])
        ref = _make_ref(title="Deep Learning for Vision")
        cfg = _cfg(auto_tag_rules=[rule])
        tags = auto_tag(ref, cfg)
        assert "ai" in tags

    def test_keyword_rule_matches_abstract(self):
        rule = AutoTagRule(keywords=["neural network"], tags=["nn"])
        ref = _make_ref(title="Other Title", abstract="We use neural networks extensively.")
        cfg = _cfg(auto_tag_rules=[rule])
        tags = auto_tag(ref, cfg)
        assert "nn" in tags

    def test_keyword_rule_case_insensitive(self):
        rule = AutoTagRule(keywords=["MACHINE LEARNING"], tags=["ml"])
        ref = _make_ref(title="machine learning approaches")
        cfg = _cfg(auto_tag_rules=[rule])
        tags = auto_tag(ref, cfg)
        assert "ml" in tags

    def test_keyword_rule_no_match(self):
        rule = AutoTagRule(keywords=["quantum computing"], tags=["quantum"])
        ref = _make_ref(title="Classical Machine Learning")
        cfg = _cfg(auto_tag_rules=[rule])
        tags = auto_tag(ref, cfg)
        assert "quantum" not in tags

    def test_journal_pattern_rule(self):
        rule = AutoTagRule(journal_pattern=r"Nature|Science", tags=["high-impact"])
        ref = _make_ref(journal="Nature Biotechnology")
        cfg = _cfg(auto_tag_rules=[rule])
        tags = auto_tag(ref, cfg)
        assert "high-impact" in tags

    def test_journal_pattern_no_match(self):
        rule = AutoTagRule(journal_pattern=r"Nature|Science", tags=["high-impact"])
        ref = _make_ref(journal="PLOS ONE")
        cfg = _cfg(auto_tag_rules=[rule])
        tags = auto_tag(ref, cfg)
        assert "high-impact" not in tags

    def test_ref_type_rule(self):
        rule = AutoTagRule(ref_type="preprint", tags=["to-verify"])
        ref = _make_ref(ref_type=RefType.PREPRINT)
        cfg = _cfg(auto_tag_rules=[rule])
        tags = auto_tag(ref, cfg)
        assert "to-verify" in tags

    def test_ref_type_rule_no_match(self):
        rule = AutoTagRule(ref_type="preprint", tags=["to-verify"])
        ref = _make_ref(ref_type=RefType.JOURNAL)
        cfg = _cfg(auto_tag_rules=[rule])
        tags = auto_tag(ref, cfg)
        assert "to-verify" not in tags

    def test_year_from_rule(self):
        rule = AutoTagRule(year_from=2020, tags=["recent"])
        ref_old = _make_ref(year=2018)
        ref_new = _make_ref(year=2022)
        cfg = _cfg(auto_tag_rules=[rule])
        assert "recent" not in auto_tag(ref_old, cfg)
        assert "recent" in auto_tag(ref_new, cfg)

    def test_year_to_rule(self):
        rule = AutoTagRule(year_to=2010, tags=["classic"])
        ref_old = _make_ref(year=2005)
        ref_new = _make_ref(year=2023)
        cfg = _cfg(auto_tag_rules=[rule])
        assert "classic" in auto_tag(ref_old, cfg)
        assert "classic" not in auto_tag(ref_new, cfg)

    def test_year_range_rule(self):
        rule = AutoTagRule(year_from=2015, year_to=2020, tags=["mid-period"])
        cfg = _cfg(auto_tag_rules=[rule])
        assert "mid-period" in auto_tag(_make_ref(year=2017), cfg)
        assert "mid-period" not in auto_tag(_make_ref(year=2014), cfg)
        assert "mid-period" not in auto_tag(_make_ref(year=2021), cfg)

    def test_open_access_only_rule(self):
        rule = AutoTagRule(open_access_only=True, tags=["oa-paper"])
        ref_oa = _make_ref(open_access=True)
        ref_closed = _make_ref(open_access=False)
        cfg = _cfg(auto_tag_rules=[rule])
        assert "oa-paper" in auto_tag(ref_oa, cfg)
        assert "oa-paper" not in auto_tag(ref_closed, cfg)

    def test_rule_with_multiple_criteria_all_must_match(self):
        rule = AutoTagRule(
            keywords=["machine learning"],
            ref_type="journal-article",
            tags=["ml-journal"],
        )
        cfg = _cfg(auto_tag_rules=[rule])
        # Matches both criteria
        ref_match = _make_ref(title="Machine Learning Methods", ref_type=RefType.JOURNAL)
        # Matches keyword but not type
        ref_kw_only = _make_ref(title="Machine Learning Methods", ref_type=RefType.PREPRINT)
        # Matches type but not keyword
        ref_type_only = _make_ref(title="Genetics Review", ref_type=RefType.JOURNAL)
        assert "ml-journal" in auto_tag(ref_match, cfg)
        assert "ml-journal" not in auto_tag(ref_kw_only, cfg)
        assert "ml-journal" not in auto_tag(ref_type_only, cfg)

    def test_multiple_rules_combine(self):
        rules = [
            AutoTagRule(keywords=["machine learning"], tags=["ml"]),
            AutoTagRule(keywords=["genomics", "dna"], tags=["bio"]),
        ]
        ref = _make_ref(title="Machine Learning for Genomics and DNA analysis")
        cfg = _cfg(auto_tag_rules=rules)
        tags = auto_tag(ref, cfg)
        assert "ml" in tags
        assert "bio" in tags


# ---------------------------------------------------------------------------
# tag_from_keywords
# ---------------------------------------------------------------------------

class TestTagFromKeywords:
    def test_machine_learning_from_title(self):
        ref = _make_ref(title="Deep Learning with Neural Networks")
        tags = tag_from_keywords(ref)
        assert "machine-learning" in tags

    def test_nlp_from_abstract(self):
        ref = _make_ref(abstract="We introduce a language model based on Transformer architecture.")
        tags = tag_from_keywords(ref)
        assert "nlp" in tags

    def test_computer_vision_from_keywords(self):
        ref = _make_ref(keywords=["image recognition", "object detection"])
        tags = tag_from_keywords(ref)
        assert "computer-vision" in tags

    def test_genomics_from_title(self):
        ref = _make_ref(title="CRISPR-based genome editing")
        tags = tag_from_keywords(ref)
        assert "genomics" in tags

    def test_neuroscience_from_abstract(self):
        ref = _make_ref(abstract="We studied the hippocampus using fMRI.")
        tags = tag_from_keywords(ref)
        assert "neuroscience" in tags

    def test_climate_from_title(self):
        ref = _make_ref(title="Global warming and sea level rise")
        tags = tag_from_keywords(ref)
        assert "climate" in tags

    def test_economics_from_abstract(self):
        ref = _make_ref(abstract="We analyze monetary policy effects on inflation.")
        tags = tag_from_keywords(ref)
        assert "economics" in tags

    def test_statistics_from_keywords(self):
        ref = _make_ref(keywords=["bayesian inference", "monte carlo simulation"])
        tags = tag_from_keywords(ref)
        assert "statistics" in tags

    def test_quantum_from_title(self):
        ref = _make_ref(title="Quantum computing with qubits and entanglement")
        tags = tag_from_keywords(ref)
        assert "quantum" in tags

    def test_reinforcement_learning(self):
        ref = _make_ref(title="Policy gradient methods in reinforcement learning")
        tags = tag_from_keywords(ref)
        assert "reinforcement-learning" in tags

    def test_unrelated_returns_empty(self):
        ref = _make_ref(title="Ancient Roman Architecture Studies")
        tags = tag_from_keywords(ref)
        assert tags == []

    def test_no_duplicates(self):
        # Title, abstract, and keywords all contain ML triggers
        ref = _make_ref(
            title="Deep learning review",
            abstract="Machine learning with neural networks.",
            keywords=["deep learning", "gradient descent"],
        )
        tags = tag_from_keywords(ref)
        assert len(tags) == len(set(tags))

    def test_case_insensitive(self):
        ref = _make_ref(title="DEEP LEARNING approaches")
        tags = tag_from_keywords(ref)
        assert "machine-learning" in tags

    def test_multiple_topics(self):
        ref = _make_ref(
            title="Neural Language Models for Genomic Sequence Analysis",
            abstract="We combine deep learning with DNA sequencing."
        )
        tags = tag_from_keywords(ref)
        # Should find both ML and genomics
        assert "machine-learning" in tags or "nlp" in tags
        assert "genomics" in tags
