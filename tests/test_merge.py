"""Tests for the merge engine."""

import pytest
from src.zoterpile.merge import merge, match_score, _title_similarity
from src.zoterpile.models import Author, RefType, Reference


def _make_ref(
    title="Test Paper",
    doi=None,
    year=2023,
    authors=None,
    journal=None,
    abstract=None,
    keywords=None,
    source="test",
) -> Reference:
    ref = Reference(
        title=title,
        doi=doi,
        year=year,
        authors=authors or [Author(family="Smith", given="J.")],
        journal=journal,
        abstract=abstract,
        keywords=keywords or [],
    )
    ref.sources[source] = 1.0
    return ref


class TestMerge:
    def test_empty_candidates_returns_seed(self):
        seed = _make_ref(doi="10.1234/test")
        result = merge(seed, [])
        assert result.doi == "10.1234/test"

    def test_first_wins_for_doi(self):
        seed = _make_ref(title="Test Paper")
        c1 = _make_ref(doi="10.1111/first", source="crossref")
        c1.sources["crossref"] = 1.0
        c2 = _make_ref(doi="10.2222/second", source="openalex")
        c2.sources["openalex"] = 1.0

        result = merge(seed, [(c1, 1.0), (c2, 0.9)])
        # Higher confidence wins
        assert result.doi == "10.1111/first"

    def test_abstract_prefers_longest(self):
        seed = _make_ref()
        short_ref = _make_ref(abstract="Short.")
        long_ref  = _make_ref(abstract="A " * 50 + "longer abstract.")

        result = merge(seed, [(short_ref, 1.0), (long_ref, 0.9)])
        assert result.abstract == long_ref.abstract

    def test_keywords_merged_from_all_sources(self):
        seed = _make_ref()
        c1 = _make_ref(keywords=["machine learning", "deep learning"])
        c2 = _make_ref(keywords=["neural networks", "machine learning"])  # duplicate

        result = merge(seed, [(c1, 1.0), (c2, 0.9)])
        kw_lower = [k.lower() for k in result.keywords]
        # All unique keywords should be present
        assert "machine learning" in kw_lower
        assert "deep learning" in kw_lower
        assert "neural networks" in kw_lower
        # No duplicates
        assert len(result.keywords) == len(set(k.lower() for k in result.keywords))

    def test_authors_prefers_orcid(self):
        seed = _make_ref()
        c_no_orcid  = _make_ref(authors=[Author(family="Smith", given="J.")])
        c_with_orcid = _make_ref(authors=[
            Author(family="Smith", given="John", orcid="0000-0001-2345-6789")
        ])

        result = merge(seed, [(c_no_orcid, 1.0), (c_with_orcid, 0.9)])
        assert result.authors[0].orcid == "0000-0001-2345-6789"

    def test_open_access_any_true_wins(self):
        seed = _make_ref()
        c_closed = _make_ref()
        c_closed.open_access = False
        c_open   = _make_ref()
        c_open.open_access = True
        c_open.oa_url = "https://example.com/paper.pdf"

        result = merge(seed, [(c_closed, 1.0), (c_open, 0.8)])
        assert result.open_access is True
        assert result.oa_url == "https://example.com/paper.pdf"

    def test_citation_count_takes_max(self):
        seed = _make_ref()
        c1 = _make_ref()
        c1.citation_count = 100
        c2 = _make_ref()
        c2.citation_count = 500

        result = merge(seed, [(c1, 1.0), (c2, 0.9)])
        assert result.citation_count == 500

    def test_title_mismatch_filtered(self):
        seed = _make_ref(title="Machine Learning in Healthcare")
        unrelated = _make_ref(title="Completely Different Topic About Chemistry")
        unrelated.doi = "10.9999/wrong"

        result = merge(seed, [(unrelated, 1.0)])
        # Should not have taken the unrelated DOI
        assert result.doi != "10.9999/wrong"

    def test_cite_key_preserved_from_seed(self):
        seed = _make_ref()
        seed.cite_key = "smith2023test"
        candidate = _make_ref(doi="10.1234/found")

        result = merge(seed, [(candidate, 1.0)])
        assert result.cite_key == "smith2023test"

    def test_sources_merged(self):
        seed = _make_ref()
        c1 = _make_ref()
        c1.sources = {"crossref": 1.0}
        c2 = _make_ref()
        c2.sources = {"openalex": 0.9}

        result = merge(seed, [(c1, 1.0), (c2, 0.9)])
        assert "crossref" in result.sources
        assert "openalex" in result.sources


class TestMatchScore:
    def test_doi_match_is_definitive(self):
        a = _make_ref(doi="10.1234/test")
        b = _make_ref(doi="10.1234/test")
        assert match_score(a, b) == 1.0

    def test_doi_mismatch_is_zero(self):
        a = _make_ref(doi="10.1234/test")
        b = _make_ref(doi="10.9999/other")
        assert match_score(a, b) == 0.0

    def test_same_title_high_score(self):
        a = _make_ref(doi=None, title="Attention Is All You Need")
        b = _make_ref(doi=None, title="Attention Is All You Need")
        score = match_score(a, b)
        assert score > 0.8

    def test_different_title_low_score(self):
        # Use different authors and years too, so only title is compared
        a = _make_ref(doi=None, title="Deep Learning for NLP",
                      authors=[Author(family="Smith", given="J.")], year=2020)
        b = _make_ref(doi=None, title="Quantum Computing Advances",
                      authors=[Author(family="Jones", given="A.")], year=2018)
        score = match_score(a, b)
        assert score < 0.5


class TestTitleSimilarity:
    def test_identical(self):
        assert _title_similarity("Attention Is All You Need", "Attention Is All You Need") == 100.0

    def test_case_insensitive(self):
        assert _title_similarity("attention is all you need", "ATTENTION IS ALL YOU NEED") == 100.0

    def test_completely_different(self):
        score = _title_similarity("Deep Learning", "Quantum Mechanics of Black Holes")
        assert score < 50

    def test_none_returns_zero(self):
        assert _title_similarity(None, "Some Title") == 0.0
        assert _title_similarity("Some Title", None) == 0.0
