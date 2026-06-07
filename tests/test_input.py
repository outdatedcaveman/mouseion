"""Tests for the universal input parser (input.py)."""

import pytest
from mouseion.input import (
    detect_item_type,
    parse_input,
    DOI, ARXIV, PMID, PMCID, ISBN, URL, TITLE,
    FILE_BIB, FILE_RIS, FILE_HTML, FILE_PDF,
)
from mouseion.models import Reference


# ---------------------------------------------------------------------------
# detect_item_type — DOI variants
# ---------------------------------------------------------------------------

class TestDetectDOI:
    def test_bare_doi(self):
        t, v = detect_item_type("10.1038/nature12373")
        assert t == DOI
        assert v == "10.1038/nature12373"

    def test_doi_url(self):
        t, v = detect_item_type("https://doi.org/10.1038/nature12373")
        assert t == DOI
        assert v == "10.1038/nature12373"

    def test_doi_dx_url(self):
        t, v = detect_item_type("https://dx.doi.org/10.1016/j.cell.2023.01.001")
        assert t == DOI
        assert v == "10.1016/j.cell.2023.01.001"

    def test_doi_prefix(self):
        t, v = detect_item_type("doi:10.1126/science.abc1234")
        assert t == DOI
        assert v == "10.1126/science.abc1234"

    def test_doi_prefix_uppercase(self):
        t, v = detect_item_type("DOI: 10.1126/science.abc1234")
        assert t == DOI
        assert v == "10.1126/science.abc1234"

    def test_doi_trailing_punctuation_stripped(self):
        t, v = detect_item_type("10.1038/nature12373.")
        assert t == DOI
        assert not v.endswith(".")


# ---------------------------------------------------------------------------
# detect_item_type — arXiv variants
# ---------------------------------------------------------------------------

class TestDetectArXiv:
    def test_bare_arxiv_new_format(self):
        t, v = detect_item_type("2310.00123")
        assert t == ARXIV
        assert v == "2310.00123"

    def test_arxiv_prefix(self):
        t, v = detect_item_type("arXiv:2310.00123")
        assert t == ARXIV
        assert v == "2310.00123"

    def test_arxiv_url(self):
        t, v = detect_item_type("https://arxiv.org/abs/2310.00123")
        assert t == ARXIV
        assert v == "2310.00123"

    def test_arxiv_url_with_version(self):
        t, v = detect_item_type("https://arxiv.org/abs/2310.00123v2")
        assert t == ARXIV
        assert v == "2310.00123"

    def test_arxiv_pdf_url(self):
        t, v = detect_item_type("https://arxiv.org/pdf/1706.03762")
        assert t == ARXIV
        assert v == "1706.03762"


# ---------------------------------------------------------------------------
# detect_item_type — PubMed / PMC
# ---------------------------------------------------------------------------

class TestDetectPubMed:
    def test_pmid_prefix(self):
        t, v = detect_item_type("PMID:12345678")
        assert t == PMID
        assert v == "12345678"

    def test_pmid_url(self):
        t, v = detect_item_type("https://pubmed.ncbi.nlm.nih.gov/12345678/")
        assert t == PMID
        assert v == "12345678"

    def test_pmcid(self):
        t, v = detect_item_type("PMC1234567")
        assert t == PMCID
        assert v == "1234567"


# ---------------------------------------------------------------------------
# detect_item_type — ISBN
# ---------------------------------------------------------------------------

class TestDetectISBN:
    def test_isbn13(self):
        t, v = detect_item_type("9780306406157")
        assert t == ISBN
        assert v == "9780306406157"

    def test_isbn_with_hyphens(self):
        t, v = detect_item_type("978-0-306-40615-7")
        assert t == ISBN
        # Hyphens stripped
        assert "-" not in v

    def test_isbn_prefix(self):
        t, v = detect_item_type("ISBN: 9780306406157")
        assert t == ISBN


# ---------------------------------------------------------------------------
# detect_item_type — generic URL
# ---------------------------------------------------------------------------

class TestDetectURL:
    def test_https_url(self):
        t, v = detect_item_type("https://example.com/paper")
        assert t == URL
        assert v == "https://example.com/paper"

    def test_http_url(self):
        t, v = detect_item_type("http://example.org/article")
        assert t == URL


# ---------------------------------------------------------------------------
# detect_item_type — title fallback
# ---------------------------------------------------------------------------

class TestDetectTitle:
    def test_plain_text(self):
        t, v = detect_item_type("Attention Is All You Need")
        assert t == TITLE
        assert v == "Attention Is All You Need"

    def test_short_text(self):
        t, v = detect_item_type("quantum computing")
        assert t == TITLE


# ---------------------------------------------------------------------------
# parse_input — single items
# ---------------------------------------------------------------------------

class TestParseInputSingle:
    def test_doi_returns_ref_with_doi(self):
        refs = parse_input("10.1038/nature12373")
        assert len(refs) == 1
        assert refs[0].doi == "10.1038/nature12373"

    def test_arxiv_returns_ref_with_arxiv_id(self):
        refs = parse_input("2310.00123")
        assert len(refs) == 1
        assert refs[0].arxiv_id == "2310.00123"

    def test_pmid_returns_ref_with_pmid(self):
        refs = parse_input("PMID:12345678")
        assert len(refs) == 1
        assert refs[0].pmid == "12345678"

    def test_url_with_doi_seeds_doi(self):
        refs = parse_input("https://doi.org/10.1038/nature12373")
        assert len(refs) == 1
        assert refs[0].doi == "10.1038/nature12373"

    def test_url_with_arxiv_seeds_arxiv_id(self):
        refs = parse_input("https://arxiv.org/abs/1706.03762")
        assert len(refs) == 1
        assert refs[0].arxiv_id == "1706.03762"

    def test_title_returns_ref_with_title(self):
        refs = parse_input("Attention Is All You Need")
        assert len(refs) == 1
        assert refs[0].title == "Attention Is All You Need"

    def test_empty_string_returns_empty(self):
        assert parse_input("") == []
        assert parse_input("   ") == []


# ---------------------------------------------------------------------------
# parse_input — multi-item splitting
# ---------------------------------------------------------------------------

class TestParseInputMulti:
    def test_newline_separated_dois(self):
        text = "10.1038/nature12373\n10.1016/j.cell.2023.01.001"
        refs = parse_input(text)
        assert len(refs) == 2
        assert refs[0].doi == "10.1038/nature12373"
        assert refs[1].doi == "10.1016/j.cell.2023.01.001"

    def test_semicolon_separated(self):
        text = "10.1038/nature12373;10.1016/j.cell.2023.01.001"
        refs = parse_input(text)
        assert len(refs) == 2

    def test_comma_separated_dois(self):
        text = "10.1038/nature12373,10.1016/j.cell.2023.01.001"
        refs = parse_input(text)
        assert len(refs) == 2

    def test_blank_line_separation(self):
        text = "10.1038/nature12373\n\n10.1016/j.cell.2023.01.001"
        refs = parse_input(text)
        assert len(refs) == 2

    def test_comma_not_split_for_titles(self):
        # A plain title should NOT be split on commas
        text = "Machine learning, neural networks, and AI"
        refs = parse_input(text)
        assert len(refs) == 1

    def test_mixed_types(self):
        text = "10.1038/nature12373\narXiv:2310.00123\nPMID:12345678"
        refs = parse_input(text)
        assert len(refs) == 3
        assert refs[0].doi is not None
        assert refs[1].arxiv_id is not None
        assert refs[2].pmid is not None


# ---------------------------------------------------------------------------
# parse_input — BibTeX string detection
# ---------------------------------------------------------------------------

class TestParseInputBibTeX:
    _BIB = """@article{smith2024,
  author  = {Smith, John},
  title   = {Deep Learning Review},
  journal = {Nature},
  year    = {2024},
  doi     = {10.1038/nature12373},
}"""

    def test_bibtex_string_detected(self):
        refs = parse_input(self._BIB)
        assert len(refs) >= 1
        assert refs[0].title is not None

    def test_bibtex_string_extracts_doi(self):
        refs = parse_input(self._BIB)
        assert refs[0].doi == "10.1038/nature12373"


# ---------------------------------------------------------------------------
# parse_input — RIS string detection
# ---------------------------------------------------------------------------

class TestParseInputRIS:
    _RIS = """TY  - JOUR
TI  - A Test Article
AU  - Jones, Alice
PY  - 2023
DO  - 10.1234/test.001
ER  - """

    def test_ris_string_detected(self):
        refs = parse_input(self._RIS)
        assert len(refs) >= 1

    def test_ris_string_extracts_title(self):
        refs = parse_input(self._RIS)
        assert refs[0].title == "A Test Article"


# ---------------------------------------------------------------------------
# parse_input — Chrome bookmarks string detection
# ---------------------------------------------------------------------------

class TestParseInputBookmarks:
    _BOOKMARKS = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3>Papers</H3>
    <DL><p>
        <DT><A HREF="https://doi.org/10.1038/nature12373" ADD_DATE="1690000000">Nature Paper</A>
        <DT><A HREF="https://arxiv.org/abs/1706.03762" ADD_DATE="1690000001">Attention Paper</A>
    </DL><p>
</DL><p>"""

    def test_bookmarks_detected(self):
        refs = parse_input(self._BOOKMARKS)
        assert len(refs) == 2

    def test_bookmarks_extract_doi(self):
        refs = parse_input(self._BOOKMARKS)
        doi_refs = [r for r in refs if r.doi]
        assert len(doi_refs) == 1
        assert doi_refs[0].doi == "10.1038/nature12373"

    def test_bookmarks_extract_arxiv(self):
        refs = parse_input(self._BOOKMARKS)
        arxiv_refs = [r for r in refs if r.arxiv_id]
        assert len(arxiv_refs) == 1
        assert arxiv_refs[0].arxiv_id == "1706.03762"
