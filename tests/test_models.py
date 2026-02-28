"""Tests for the Reference data model."""

import pytest
from src.zoterpile.models import Author, RefType, Reference, _normalize_doi


class TestAuthor:
    def test_full_name(self):
        a = Author(family="Smith", given="John")
        assert a.full_name == "John Smith"

    def test_full_name_family_only(self):
        a = Author(family="Smith")
        assert a.full_name == "Smith"

    def test_to_bibtex_str(self):
        a = Author(family="Smith", given="John A.")
        assert a.to_bibtex_str() == "Smith, John A."

    def test_from_bibtex_str_family_given(self):
        a = Author.from_bibtex_str("Smith, John")
        assert a.family == "Smith"
        assert a.given  == "John"

    def test_from_bibtex_str_first_last(self):
        a = Author.from_bibtex_str("John Smith")
        assert a.family == "Smith"
        assert a.given  == "John"

    def test_from_bibtex_str_single_name(self):
        a = Author.from_bibtex_str("Anonymous")
        assert a.family == "Anonymous"

    def test_from_crossref(self):
        data = {
            "family": "Vaswani",
            "given": "Ashish",
            "ORCID": "https://orcid.org/0000-0001-2345-6789",
            "affiliation": [{"name": "Google Brain"}],
        }
        a = Author.from_crossref(data)
        assert a.family == "Vaswani"
        assert a.given  == "Ashish"
        assert a.orcid  == "0000-0001-2345-6789"
        assert a.affiliation == "Google Brain"


class TestRefType:
    def test_from_crossref_known(self):
        assert RefType.from_crossref("journal-article") == RefType.JOURNAL
        assert RefType.from_crossref("book")            == RefType.BOOK
        assert RefType.from_crossref("posted-content")  == RefType.PREPRINT

    def test_from_crossref_unknown(self):
        assert RefType.from_crossref("mystery-type") == RefType.UNKNOWN

    def test_to_bibtex_type(self):
        assert RefType.JOURNAL.to_bibtex_type()    == "article"
        assert RefType.BOOK.to_bibtex_type()       == "book"
        assert RefType.CONFERENCE.to_bibtex_type() == "inproceedings"
        assert RefType.THESIS.to_bibtex_type()     == "phdthesis"

    def test_to_ris_type(self):
        assert RefType.JOURNAL.to_ris_type()      == "JOUR"
        assert RefType.BOOK.to_ris_type()         == "BOOK"
        assert RefType.CONFERENCE.to_ris_type()   == "CONF"


class TestReference:
    def test_completeness_empty(self):
        ref = Reference()
        assert ref.completeness == 0.0

    def test_completeness_full(self):
        ref = Reference(
            title="Test Paper",
            authors=[Author(family="Smith", given="J.")],
            year=2023,
            doi="10.1234/test",
            journal="Test Journal",
            volume="1",
            issue="2",
            pages="10-20",
            abstract="An abstract.",
            publisher="Test Publisher",
        )
        assert ref.completeness == pytest.approx(1.0)

    def test_has_identifier_with_doi(self):
        ref = Reference(doi="10.1234/test")
        assert ref.has_identifier() is True

    def test_has_identifier_empty(self):
        ref = Reference()
        assert ref.has_identifier() is False

    def test_normalize_doi_strips_prefix(self):
        ref = Reference(doi="https://doi.org/10.1234/test")
        ref.normalize()
        assert ref.doi == "10.1234/test"

    def test_normalize_doi_dx_prefix(self):
        ref = Reference(doi="https://dx.doi.org/10.1234/test.2023")
        ref.normalize()
        assert ref.doi == "10.1234/test.2023"

    def test_normalize_title_whitespace(self):
        ref = Reference(title="  A  title  with   extra   spaces  ")
        ref.normalize()
        assert ref.title == "A title with extra spaces"

    def test_normalize_abstract_strips_label(self):
        ref = Reference(abstract="Abstract: This is the abstract.")
        ref.normalize()
        assert not ref.abstract.startswith("Abstract:")

    def test_auto_cite_key(self):
        ref = Reference(
            authors=[Author(family="Vaswani", given="Ashish")],
            year=2017,
            title="Attention Is All You Need",
        )
        key = ref.auto_cite_key()
        assert "vaswani" in key
        assert "2017" in key
        assert "attention" in key

    def test_auto_cite_key_empty(self):
        ref = Reference()
        assert ref.auto_cite_key() == "ref"


class TestNormalizeDoi:
    def test_bare(self):
        assert _normalize_doi("10.1234/abc") == "10.1234/abc"

    def test_https_doi_org(self):
        assert _normalize_doi("https://doi.org/10.1234/abc") == "10.1234/abc"

    def test_dx_doi_org(self):
        assert _normalize_doi("http://dx.doi.org/10.1234/abc") == "10.1234/abc"

    def test_strips_whitespace(self):
        assert _normalize_doi("  10.1234/abc  ") == "10.1234/abc"
