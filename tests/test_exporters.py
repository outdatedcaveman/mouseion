"""Tests for BibTeX, RIS, Markdown and Zotero RDF exporters."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from zoterpile.exporters.bibtex import to_bibtex_string, export_bibtex_file
from zoterpile.exporters.ris import to_ris_string, export_ris_file
from zoterpile.exporters.markdown import to_markdown_string, export_markdown_file
from zoterpile.exporters.zotero_rdf import to_zotero_rdf_string
from zoterpile.models import Author, Reference, RefType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_author(family: str, given: str = "", orcid: str = "") -> Author:
    return Author(family=family, given=given, orcid=orcid or None)


def _journal_ref() -> Reference:
    return Reference(
        title="Attention Is All You Need",
        doi="10.48550/arXiv.1706.03762",
        arxiv_id="1706.03762",
        year=2017,
        month=6,
        journal="Advances in Neural Information Processing Systems",
        journal_abbrev="NeurIPS",
        volume="30",
        pages="5998-6008",
        abstract="The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.",
        keywords=["transformer", "attention", "NLP"],
        open_access=True,
        oa_url="https://arxiv.org/pdf/1706.03762",
        citation_count=80000,
        ref_type=RefType.JOURNAL,
        authors=[
            _make_author("Vaswani", "Ashish"),
            _make_author("Shazeer", "Noam"),
        ],
        sources={"arxiv": 0.9, "crossref": 1.0},
    )


def _book_ref() -> Reference:
    return Reference(
        title="Deep Learning",
        year=2016,
        publisher="MIT Press",
        place="Cambridge, MA",
        isbn="978-0262035613",
        ref_type=RefType.BOOK,
        authors=[
            _make_author("Goodfellow", "Ian"),
            _make_author("Bengio", "Yoshua"),
        ],
    )


def _minimal_ref() -> Reference:
    return Reference(title="Minimal Paper", ref_type=RefType.UNKNOWN)


def _conference_ref() -> Reference:
    return Reference(
        title="Deep Residual Learning for Image Recognition",
        doi="10.1109/CVPR.2016.90",
        year=2016,
        ref_type=RefType.CONFERENCE,
        event_name="IEEE Conference on Computer Vision and Pattern Recognition",
        article_number="7780459",
        eissn="2575-7075",
        issn="1063-6919",
        license="CC BY 4.0",
        authors=[
            _make_author("He", "Kaiming"),
            _make_author("Zhang", "Xiangyu"),
        ],
        editors=[
            _make_author("Smith", "Alice"),
        ],
    )


# ---------------------------------------------------------------------------
# BibTeX tests
# ---------------------------------------------------------------------------

class TestBibTeX:
    def test_entry_type_journal(self):
        bib = to_bibtex_string(_journal_ref())
        assert bib.startswith("@article{")

    def test_entry_type_book(self):
        bib = to_bibtex_string(_book_ref())
        assert bib.startswith("@book{")

    def test_cite_key_generated(self):
        bib = to_bibtex_string(_journal_ref())
        assert re.search(r"@article\{[a-z]+\d{4}", bib)

    def test_title_present(self):
        bib = to_bibtex_string(_journal_ref())
        assert "Attention Is All You Need" in bib

    def test_authors_formatted(self):
        bib = to_bibtex_string(_journal_ref())
        assert "Vaswani" in bib
        assert " and " in bib

    def test_doi_field(self):
        bib = to_bibtex_string(_journal_ref())
        assert "10.48550" in bib

    def test_arxiv_fields(self):
        bib = to_bibtex_string(_journal_ref())
        assert "arXiv" in bib
        assert "1706.03762" in bib

    def test_abstract_included(self):
        bib = to_bibtex_string(_journal_ref())
        assert "dominant sequence transduction" in bib

    def test_keywords_field(self):
        bib = to_bibtex_string(_journal_ref())
        assert "transformer" in bib

    def test_latex_escape_ampersand(self):
        ref = Reference(
            title="A & B",
            ref_type=RefType.JOURNAL,
            authors=[_make_author("Smith", "John")],
        )
        bib = to_bibtex_string(ref)
        assert r"\&" in bib

    def test_latex_escape_percent(self):
        ref = Reference(
            title="50% Improvement",
            ref_type=RefType.JOURNAL,
            authors=[_make_author("Smith", "John")],
        )
        bib = to_bibtex_string(ref)
        assert r"\%" in bib

    def test_title_case_protection(self):
        bib = to_bibtex_string(_journal_ref())
        assert "{{" in bib

    def test_isbn_in_book(self):
        bib = to_bibtex_string(_book_ref())
        assert "978-0262035613" in bib

    def test_list_of_refs(self):
        bib = to_bibtex_string([_journal_ref(), _book_ref()])
        assert bib.count("@") == 2

    def test_minimal_ref_no_crash(self):
        bib = to_bibtex_string(_minimal_ref())
        assert "@" in bib

    def test_export_to_file(self, tmp_path):
        dest = tmp_path / "refs.bib"
        export_bibtex_file([_journal_ref()], dest)
        content = dest.read_text(encoding="utf-8")
        assert "@article{" in content
        assert content.endswith("\n")

    def test_missing_authors_no_crash(self):
        ref = Reference(title="Authorless", ref_type=RefType.JOURNAL)
        bib = to_bibtex_string(ref)
        assert "Authorless" in bib

    def test_single_ref_accepted(self):
        ref = _journal_ref()
        bib_single = to_bibtex_string(ref)
        bib_list   = to_bibtex_string([ref])
        assert bib_single == bib_list

    def test_conference_booktitle_uses_event_name(self):
        bib = to_bibtex_string(_conference_ref())
        assert "IEEE Conference on Computer Vision" in bib
        assert "booktitle" in bib

    def test_article_number_as_pages_fallback(self):
        bib = to_bibtex_string(_conference_ref())
        assert "7780459" in bib

    def test_editors_field(self):
        bib = to_bibtex_string(_conference_ref())
        assert "editor" in bib
        assert "Smith" in bib


# ---------------------------------------------------------------------------
# RIS tests
# ---------------------------------------------------------------------------

class TestRIS:
    def test_ty_line_first(self):
        ris = to_ris_string(_journal_ref())
        first_line = ris.strip().splitlines()[0]
        assert first_line.startswith("TY  -")

    def test_er_line_present(self):
        ris = to_ris_string(_journal_ref())
        assert "ER  -" in ris

    def test_title_present(self):
        ris = to_ris_string(_journal_ref())
        assert "TI  - Attention Is All You Need" in ris

    def test_doi_field(self):
        ris = to_ris_string(_journal_ref())
        assert "DO  - 10.48550" in ris

    def test_year_field(self):
        ris = to_ris_string(_journal_ref())
        assert "PY  - 2017" in ris

    def test_abstract_present(self):
        ris = to_ris_string(_journal_ref())
        assert "AB  -" in ris
        assert "dominant sequence" in ris

    def test_authors_au_lines(self):
        ris = to_ris_string(_journal_ref())
        au_lines = [l for l in ris.splitlines() if l.startswith("AU  -")]
        assert len(au_lines) == 2

    def test_keywords_kw_lines(self):
        ris = to_ris_string(_journal_ref())
        kw_lines = [l for l in ris.splitlines() if l.startswith("KW  -")]
        assert len(kw_lines) == 3

    def test_list_of_refs(self):
        ris = to_ris_string([_journal_ref(), _book_ref()])
        assert ris.count("TY  -") == 2
        assert ris.count("ER  -") == 2

    def test_minimal_ref_no_crash(self):
        ris = to_ris_string(_minimal_ref())
        assert "TY  -" in ris
        assert "ER  -" in ris

    def test_export_to_file(self, tmp_path):
        dest = tmp_path / "refs.ris"
        export_ris_file([_journal_ref()], dest)
        content = dest.read_text(encoding="utf-8")
        assert "TY  -" in content

    def test_pages_split_sp_ep(self):
        ris = to_ris_string(_journal_ref())
        assert "SP  - 5998" in ris
        assert "EP  - 6008" in ris

    def test_no_url_field_when_absent(self):
        ref = Reference(title="No URL", ref_type=RefType.JOURNAL,
                        authors=[_make_author("Smith", "J")])
        ris = to_ris_string(ref)
        assert "TY  -" in ris

    def test_single_ref_accepted(self):
        ref = _journal_ref()
        assert to_ris_string(ref) == to_ris_string([ref])

    def test_conference_t2_is_event_name(self):
        ris = to_ris_string(_conference_ref())
        assert "T2  - IEEE Conference on Computer Vision" in ris

    def test_article_number_in_m1(self):
        ris = to_ris_string(_conference_ref())
        assert "M1  - 7780459" in ris

    def test_eissn_in_sn(self):
        ris = to_ris_string(_conference_ref())
        sn_lines = [l for l in ris.splitlines() if l.startswith("SN  -")]
        assert any("2575-7075" in l for l in sn_lines)

    def test_license_in_m3(self):
        ris = to_ris_string(_conference_ref())
        assert "M3  - CC BY 4.0" in ris


# ---------------------------------------------------------------------------
# Markdown tests
# ---------------------------------------------------------------------------

class TestMarkdown:
    def test_title_heading(self):
        md = to_markdown_string(_journal_ref())
        assert "Attention Is All You Need" in md

    def test_completeness_bar(self):
        md = to_markdown_string(_journal_ref())
        assert "Completeness:" in md

    def test_authors_listed(self):
        md = to_markdown_string(_journal_ref())
        assert "Vaswani" in md

    def test_doi_link(self):
        md = to_markdown_string(_journal_ref())
        assert "doi.org/10.48550" in md

    def test_abstract_present(self):
        md = to_markdown_string(_journal_ref())
        assert "dominant sequence transduction" in md

    def test_open_access_marked(self):
        md = to_markdown_string(_journal_ref())
        assert "Open Access" in md

    def test_keywords_listed(self):
        md = to_markdown_string(_journal_ref())
        assert "transformer" in md

    def test_citation_count(self):
        md = to_markdown_string(_journal_ref())
        # 80000 may be rendered with or without comma formatting
        assert "80" in md and "000" in md

    def test_list_numbered(self):
        md = to_markdown_string([_journal_ref(), _book_ref()])
        assert "## 1." in md
        assert "## 2." in md

    def test_minimal_ref_no_crash(self):
        md = to_markdown_string(_minimal_ref())
        assert "Minimal Paper" in md

    def test_sources_listed(self):
        md = to_markdown_string(_journal_ref())
        assert "crossref" in md or "arxiv" in md

    def test_cite_key_shown(self):
        md = to_markdown_string(_journal_ref())
        assert "Cite key:" in md

    def test_export_to_file(self, tmp_path):
        dest = tmp_path / "refs.md"
        export_markdown_file([_journal_ref()], dest)
        content = dest.read_text(encoding="utf-8")
        assert "Attention Is All You Need" in content

    def test_single_ref_no_index(self):
        """Single ref passed as object (not list) should not get a numbered heading."""
        md = to_markdown_string(_journal_ref())
        assert "## 1." not in md

    def test_no_crash_empty_list(self):
        md = to_markdown_string([])
        assert md == ""

    def test_conference_event_name_shown(self):
        md = to_markdown_string(_conference_ref())
        assert "Conference:" in md
        assert "IEEE Conference on Computer Vision" in md

    def test_editors_shown(self):
        md = to_markdown_string(_conference_ref())
        assert "Editors:" in md
        assert "Smith" in md

    def test_license_shown(self):
        md = to_markdown_string(_conference_ref())
        assert "License:" in md
        assert "CC BY 4.0" in md


# ---------------------------------------------------------------------------
# Zotero RDF tests
# ---------------------------------------------------------------------------

class TestZoteroRDF:
    def test_xml_declaration(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert rdf.startswith('<?xml version="1.0" encoding="UTF-8"?>')

    def test_rdf_root_element(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert "<rdf:RDF" in rdf
        assert "</rdf:RDF>" in rdf

    def test_namespace_declarations(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert 'xmlns:rdf=' in rdf
        assert 'xmlns:dc=' in rdf
        assert 'xmlns:bib=' in rdf
        assert 'xmlns:foaf=' in rdf

    def test_article_type(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert "<bib:Article" in rdf
        assert "</bib:Article>" in rdf

    def test_book_type(self):
        rdf = to_zotero_rdf_string([_book_ref()])
        assert "<bib:Book" in rdf

    def test_title_element(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert "<dc:title>Attention Is All You Need</dc:title>" in rdf

    def test_authors_present(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert "<foaf:surname>Vaswani</foaf:surname>" in rdf
        assert "<foaf:firstName>Ashish</foaf:firstName>" in rdf

    def test_doi_uri_used_as_rdf_about(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert 'rdf:about="https://doi.org/10.48550/arXiv.1706.03762"' in rdf

    def test_doi_identifier_element(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert "<dc:identifier>DOI 10.48550" in rdf

    def test_isbn_identifier(self):
        rdf = to_zotero_rdf_string([_book_ref()])
        assert "<dc:identifier>ISBN 978-0262035613</dc:identifier>" in rdf

    def test_year_date_element(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert "<dc:date>2017-06</dc:date>" in rdf

    def test_year_only_date(self):
        ref = Reference(title="T", ref_type=RefType.JOURNAL, year=2020)
        rdf = to_zotero_rdf_string([ref])
        assert "<dc:date>2020</dc:date>" in rdf

    def test_journal_is_part_of(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert "<dcterms:isPartOf>" in rdf
        assert "Advances in Neural Information Processing Systems" in rdf

    def test_abstract_element(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert "<dcterms:abstract>" in rdf
        assert "dominant sequence transduction" in rdf

    def test_keywords_as_dc_subject(self):
        rdf = to_zotero_rdf_string([_journal_ref()])
        assert "<dc:subject>transformer</dc:subject>" in rdf
        assert "<dc:subject>attention</dc:subject>" in rdf

    def test_multiple_refs(self):
        rdf = to_zotero_rdf_string([_journal_ref(), _book_ref()])
        assert rdf.count("<bib:Article") == 1
        assert rdf.count("<bib:Book") == 1

    def test_empty_list(self):
        rdf = to_zotero_rdf_string([])
        assert "<rdf:RDF" in rdf
        assert "</rdf:RDF>" in rdf

    def test_xml_escaping_in_title(self):
        ref = Reference(title="A & B <test>", ref_type=RefType.JOURNAL)
        rdf = to_zotero_rdf_string([ref])
        assert "A &amp; B &lt;test&gt;" in rdf

    def test_minimal_ref_no_crash(self):
        rdf = to_zotero_rdf_string([_minimal_ref()])
        assert "<bib:Document" in rdf

    def test_urn_fallback_uri(self):
        """Refs without DOI or URL get a urn:zoterpile: URI."""
        ref = Reference(title="No ID Ref", ref_type=RefType.JOURNAL, year=2020)
        rdf = to_zotero_rdf_string([ref])
        assert 'rdf:about="urn:zoterpile:' in rdf

    def test_url_used_as_uri_when_no_doi(self):
        ref = Reference(
            title="Web Only",
            ref_type=RefType.WEBSITE,
            url="https://example.com/page",
        )
        rdf = to_zotero_rdf_string([ref])
        assert 'rdf:about="https://example.com/page"' in rdf

    def test_conference_event_name(self):
        rdf = to_zotero_rdf_string([_conference_ref()])
        assert "<bib:Conference>" in rdf
        assert "IEEE Conference on Computer Vision" in rdf

    def test_article_number_as_pages_fallback(self):
        rdf = to_zotero_rdf_string([_conference_ref()])
        assert "<bib:pages>7780459</bib:pages>" in rdf

    def test_editors_in_rdf(self):
        rdf = to_zotero_rdf_string([_conference_ref()])
        assert "<bib:editors>" in rdf
        assert "<foaf:surname>Smith</foaf:surname>" in rdf

    def test_license_as_dc_rights(self):
        rdf = to_zotero_rdf_string([_conference_ref()])
        assert "<dc:rights>CC BY 4.0</dc:rights>" in rdf

    def test_eissn_as_dc_identifier(self):
        rdf = to_zotero_rdf_string([_conference_ref()])
        assert "ISSN 2575-7075" in rdf
