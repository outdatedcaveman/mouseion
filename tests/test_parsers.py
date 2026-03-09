"""Tests for input parsers (RIS, BibTeX, HTML)."""

import pytest
from src.zoterpile.parsers.ris    import parse_ris_string
from src.zoterpile.parsers.bibtex import parse_bibtex_string
from src.zoterpile.parsers.html   import parse_html_string, _ids_from_url
from src.zoterpile.models         import RefType


# ---------------------------------------------------------------------------
# RIS parser
# ---------------------------------------------------------------------------

SAMPLE_RIS = """\
TY  - JOUR
TI  - Attention Is All You Need
AU  - Vaswani, Ashish
AU  - Shazeer, Noam
AU  - Parmar, Niki
PY  - 2017
DO  - 10.48550/arXiv.1706.03762
AB  - The dominant sequence transduction models are based on complex RNNs.
JO  - Advances in Neural Information Processing Systems
VL  - 30
ER  -
"""

SAMPLE_RIS_BOOK = """\
TY  - BOOK
TI  - Deep Learning
AU  - Goodfellow, Ian
AU  - Bengio, Yoshua
AU  - Courville, Aaron
PY  - 2016
PB  - MIT Press
SN  - 9780262035613
ER  -
"""


class TestRISParser:
    def test_basic_article(self):
        refs = parse_ris_string(SAMPLE_RIS)
        assert len(refs) == 1
        ref = refs[0]
        assert "Attention" in ref.title
        assert len(ref.authors) == 3
        assert ref.authors[0].family == "Vaswani"
        assert ref.year == 2017
        assert ref.journal is not None
        assert ref.volume == "30"
        assert ref.ref_type == RefType.JOURNAL

    def test_abstract_parsed(self):
        refs = parse_ris_string(SAMPLE_RIS)
        assert refs[0].abstract is not None
        assert "RNNs" in refs[0].abstract

    def test_doi_parsed(self):
        refs = parse_ris_string(SAMPLE_RIS)
        assert refs[0].doi is not None
        assert refs[0].doi.startswith("10.")

    def test_book_type(self):
        refs = parse_ris_string(SAMPLE_RIS_BOOK)
        assert len(refs) == 1
        ref = refs[0]
        assert ref.ref_type == RefType.BOOK
        assert "Goodfellow" in ref.authors[0].family
        assert ref.publisher == "MIT Press"

    def test_multiple_entries(self):
        double = SAMPLE_RIS + "\n" + SAMPLE_RIS_BOOK
        refs = parse_ris_string(double)
        assert len(refs) == 2

    def test_conference_t2_as_event_name(self):
        refs = parse_ris_string(SAMPLE_RIS_CONFERENCE)
        ref = refs[0]
        assert ref.event_name == "IEEE Conference on Computer Vision and Pattern Recognition"

    def test_conference_m1_as_article_number(self):
        refs = parse_ris_string(SAMPLE_RIS_CONFERENCE)
        assert refs[0].article_number == "7780459"

    def test_conference_m3_as_license(self):
        refs = parse_ris_string(SAMPLE_RIS_CONFERENCE)
        assert refs[0].license == "CC BY 4.0"


# ---------------------------------------------------------------------------
# BibTeX parser
# ---------------------------------------------------------------------------

SAMPLE_BIB = r"""
@article{vaswani2017attention,
  title   = {Attention Is All You Need},
  author  = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki},
  journal = {Advances in Neural Information Processing Systems},
  year    = {2017},
  volume  = {30},
  doi     = {10.48550/arXiv.1706.03762},
  abstract = {The dominant sequence transduction models are based on complex RNNs.},
}
"""

SAMPLE_BIB_INPROCEEDINGS = r"""
@inproceedings{he2016deep,
  title     = {Deep Residual Learning for Image Recognition},
  author    = {He, Kaiming and Zhang, Xiangyu and Ren, Shaoqing and Sun, Jian},
  booktitle = {Proceedings of CVPR},
  year      = {2016},
  pages     = {770--778},
}
"""

SAMPLE_BIB_CONFERENCE_FULL = r"""
@inproceedings{he2016resnet,
  title      = {Deep Residual Learning},
  author     = {He, Kaiming},
  editor     = {Smith, Alice},
  booktitle  = {IEEE CVPR 2016},
  year       = {2016},
  eid        = {7780459},
  issn       = {1063-6919},
  eissn      = {2575-7075},
  license    = {CC BY 4.0},
}
"""

SAMPLE_RIS_CONFERENCE = """\
TY  - CONF
TI  - Deep Residual Learning
AU  - He, Kaiming
T2  - IEEE Conference on Computer Vision and Pattern Recognition
PY  - 2016
DO  - 10.1109/CVPR.2016.90
M1  - 7780459
M3  - CC BY 4.0
ER  -
"""


class TestBibTeXParser:
    def test_basic_article(self):
        refs = parse_bibtex_string(SAMPLE_BIB)
        assert len(refs) == 1
        ref = refs[0]
        assert "Attention" in ref.title
        assert len(ref.authors) == 3
        assert ref.authors[0].family == "Vaswani"
        assert ref.year == 2017
        assert ref.volume == "30"
        assert ref.ref_type == RefType.JOURNAL

    def test_cite_key_preserved(self):
        refs = parse_bibtex_string(SAMPLE_BIB)
        assert refs[0].cite_key == "vaswani2017attention"

    def test_doi_stripped(self):
        refs = parse_bibtex_string(SAMPLE_BIB)
        doi = refs[0].doi
        assert doi is not None
        assert not doi.startswith("https://")

    def test_inproceedings(self):
        refs = parse_bibtex_string(SAMPLE_BIB_INPROCEEDINGS)
        ref = refs[0]
        assert ref.ref_type == RefType.CONFERENCE
        assert "770" in (ref.pages or "")

    def test_inproceedings_booktitle_as_event_name(self):
        refs = parse_bibtex_string(SAMPLE_BIB_INPROCEEDINGS)
        ref = refs[0]
        assert ref.event_name == "Proceedings of CVPR"

    def test_conference_full_editors(self):
        refs = parse_bibtex_string(SAMPLE_BIB_CONFERENCE_FULL)
        assert len(refs[0].editors) == 1
        assert refs[0].editors[0].family == "Smith"

    def test_conference_full_eid_as_article_number(self):
        refs = parse_bibtex_string(SAMPLE_BIB_CONFERENCE_FULL)
        assert refs[0].article_number == "7780459"

    def test_conference_full_eissn(self):
        refs = parse_bibtex_string(SAMPLE_BIB_CONFERENCE_FULL)
        assert refs[0].eissn == "2575-7075"
        assert refs[0].issn == "1063-6919"

    def test_conference_full_license(self):
        refs = parse_bibtex_string(SAMPLE_BIB_CONFERENCE_FULL)
        assert refs[0].license == "CC BY 4.0"

    def test_multiple_entries(self):
        double = SAMPLE_BIB + SAMPLE_BIB_INPROCEEDINGS
        refs = parse_bibtex_string(double)
        assert len(refs) == 2


# ---------------------------------------------------------------------------
# HTML / URL parser
# ---------------------------------------------------------------------------

SAMPLE_HTML_HIGHWIRE = """<!DOCTYPE html>
<html>
<head>
  <meta name="citation_title" content="Attention Is All You Need">
  <meta name="citation_author" content="Vaswani, Ashish">
  <meta name="citation_author" content="Shazeer, Noam">
  <meta name="citation_doi" content="10.48550/arXiv.1706.03762">
  <meta name="citation_publication_date" content="2017/12">
  <meta name="citation_journal_title" content="NeurIPS">
  <meta name="citation_volume" content="30">
</head>
<body><h1>Attention Is All You Need</h1></body>
</html>
"""

SAMPLE_HTML_OG = """<!DOCTYPE html>
<html>
<head>
  <meta property="og:title" content="Some Web Article">
  <meta property="og:description" content="An interesting article.">
</head>
<body></body>
</html>
"""


class TestHTMLParser:
    def test_highwire_tags(self):
        ref = parse_html_string(SAMPLE_HTML_HIGHWIRE)
        assert "Attention" in ref.title
        assert len(ref.authors) == 2
        assert ref.authors[0].family == "Vaswani"
        assert ref.year == 2017
        assert ref.doi is not None
        assert ref.journal == "NeurIPS"
        assert ref.volume == "30"

    def test_opengraph_fallback(self):
        ref = parse_html_string(SAMPLE_HTML_OG)
        assert ref.title == "Some Web Article"
        assert "interesting" in ref.abstract

    def test_doi_from_url(self):
        ref = parse_html_string("", source_url="https://doi.org/10.1234/test.2023")
        assert ref.doi == "10.1234/test.2023"

    def test_arxiv_from_url(self):
        ref = parse_html_string("", source_url="https://arxiv.org/abs/1706.03762")
        assert ref.arxiv_id == "1706.03762"

    def test_pubmed_from_url(self):
        ref = parse_html_string("", source_url="https://pubmed.ncbi.nlm.nih.gov/12345678/")
        assert ref.pmid == "12345678"


class TestIdsFromUrl:
    def test_doi_org(self):
        ids = _ids_from_url("https://doi.org/10.1234/test")
        assert ids["doi"] == "10.1234/test"

    def test_arxiv_abs(self):
        ids = _ids_from_url("https://arxiv.org/abs/1706.03762")
        assert ids["arxiv_id"] == "1706.03762"

    def test_arxiv_pdf(self):
        ids = _ids_from_url("https://arxiv.org/pdf/1706.03762v3")
        assert ids["arxiv_id"] == "1706.03762"

    def test_pubmed(self):
        ids = _ids_from_url("https://pubmed.ncbi.nlm.nih.gov/98765432/")
        assert ids["pmid"] == "98765432"

    def test_empty_url(self):
        ids = _ids_from_url("")
        assert ids == {}
