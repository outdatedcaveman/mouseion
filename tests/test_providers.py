"""Tests for bibliographic data providers (mocked HTTP)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Optional

import pytest
import httpx

from mouseion.models import Author, Reference, RefType
from mouseion.providers.crossref import CrossRefProvider
from mouseion.providers.arxiv import ArXivProvider
from mouseion.providers.semantic_scholar import SemanticScholarProvider
from mouseion.providers.openalex import OpenAlexProvider, _reconstruct_abstract
from mouseion.providers.pubmed import PubMedProvider
from mouseion.providers.dblp import DBLPProvider
from mouseion.providers.base import BaseProvider


# ---------------------------------------------------------------------------
# Helpers: fake API responses
# ---------------------------------------------------------------------------

def _crossref_work(
    doi="10.1038/nature12345",
    title="Fake Paper",
    authors=None,
    year=2023,
    journal="Nature",
    abstract="A fake abstract.",
    citation_count=42,
) -> dict:
    authors = authors or [{"given": "Jane", "family": "Doe", "sequence": "first"}]
    return {
        "message": {
            "DOI": doi,
            "title": [title],
            "author": authors,
            "type": "journal-article",
            "published": {"date-parts": [[year, 3]]},
            "container-title": [journal],
            "volume": "500",
            "page": "100-110",
            "abstract": abstract,
            "is-referenced-by-count": citation_count,
            "publisher": "Springer Nature",
        }
    }


_ARXIV_ATOM = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>Fake arXiv Paper</title>
    <summary>A fake arXiv abstract.</summary>
    <published>2023-01-01T00:00:00Z</published>
    <author><name>Alice Smith</name></author>
    <author><name>Bob Jones</name></author>
    <arxiv:doi xmlns:arxiv="http://arxiv.org/schemas/atom">10.1234/fake</arxiv:doi>
  </entry>
</feed>
"""

def _ss_paper(
    doi="10.1234/fake",
    title="Fake SS Paper",
    year=2022,
    abstract="Semantic scholar abstract.",
    citation_count=7,
) -> dict:
    return {
        "paperId": "abc123",
        "title": title,
        "abstract": abstract,
        "year": year,
        "externalIds": {"DOI": doi, "ArXiv": "2201.00001"},
        "citationCount": citation_count,
        "authors": [
            {"name": "Alice Smith", "authorId": "1"},
            {"name": "Bob Jones",  "authorId": "2"},
        ],
        "venue": "NeurIPS",
        "isOpenAccess": True,
        "openAccessPdf": {"url": "https://example.com/paper.pdf"},
    }


def _mock_resp(data, status=200) -> MagicMock:
    """Build a fake httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    if isinstance(data, str):
        resp.text = data
        resp.content = data.encode()
        resp.json = MagicMock(side_effect=json.JSONDecodeError("", "", 0))
    else:
        payload = json.dumps(data)
        resp.text = payload
        resp.content = payload.encode()
        resp.json = MagicMock(return_value=data)
    return resp


# ---------------------------------------------------------------------------
# CrossRef provider tests
# ---------------------------------------------------------------------------

class TestCrossRefProvider:
    @pytest.fixture
    def provider(self):
        return CrossRefProvider()

    @pytest.mark.asyncio
    async def test_lookup_by_doi_returns_reference(self, provider):
        fake_resp = _mock_resp(_crossref_work())
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.1038/nature12345", client)
        assert ref is not None
        assert isinstance(ref, Reference)

    @pytest.mark.asyncio
    async def test_lookup_by_doi_title(self, provider):
        fake_resp = _mock_resp(_crossref_work(title="My Great Paper"))
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.1038/nature12345", client)
        assert ref.title == "My Great Paper"

    @pytest.mark.asyncio
    async def test_lookup_by_doi_authors(self, provider):
        fake_resp = _mock_resp(_crossref_work(authors=[
            {"given": "Alice", "family": "Wonderland", "sequence": "first"},
        ]))
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.xxx/1", client)
        assert ref.authors[0].family == "Wonderland"

    @pytest.mark.asyncio
    async def test_lookup_by_doi_year(self, provider):
        fake_resp = _mock_resp(_crossref_work(year=2021))
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.xxx/1", client)
        assert ref.year == 2021

    @pytest.mark.asyncio
    async def test_lookup_by_doi_citation_count(self, provider):
        fake_resp = _mock_resp(_crossref_work(citation_count=99))
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.xxx/1", client)
        assert ref.citation_count == 99

    @pytest.mark.asyncio
    async def test_lookup_by_doi_returns_none_on_404(self, provider):
        with patch.object(provider, "_get", new=AsyncMock(return_value=None)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.xxx/missing", client)
        assert ref is None

    @pytest.mark.asyncio
    async def test_lookup_by_doi_abstract_stripped_of_tags(self, provider):
        work = _crossref_work(abstract="<jats:p>Some <b>bold</b> text.</jats:p>")
        fake_resp = _mock_resp(work)
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.xxx/1", client)
        assert "<" not in (ref.abstract or "")

    @pytest.mark.asyncio
    async def test_lookup_by_doi_ref_type_journal(self, provider):
        fake_resp = _mock_resp(_crossref_work())
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.xxx/1", client)
        assert ref.ref_type == RefType.JOURNAL

    @pytest.mark.asyncio
    async def test_parse_work_doi_normalized(self, provider):
        work_data = _crossref_work(doi="https://doi.org/10.1038/nature12345")["message"]
        ref = provider._parse_work(work_data)
        # Should strip the https://doi.org/ prefix
        assert not ref.doi.startswith("http")
        assert "10.1038" in ref.doi

    def test_provider_name(self, provider):
        assert provider.name == "crossref"

    def test_provider_priority_is_1(self, provider):
        assert provider.priority == 1


# ---------------------------------------------------------------------------
# ArXiv provider tests
# ---------------------------------------------------------------------------

class TestArXivProvider:
    @pytest.fixture
    def provider(self):
        return ArXivProvider()

    @pytest.mark.asyncio
    async def test_lookup_by_arxiv_id_returns_reference(self, provider):
        fake_resp = _mock_resp(_ARXIV_ATOM)
        fake_resp.text = _ARXIV_ATOM
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_arxiv_id("2301.00001", client)
        assert ref is not None
        assert ref.title == "Fake arXiv Paper"

    @pytest.mark.asyncio
    async def test_lookup_by_arxiv_id_year(self, provider):
        fake_resp = _mock_resp(_ARXIV_ATOM)
        fake_resp.text = _ARXIV_ATOM
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_arxiv_id("2301.00001", client)
        assert ref.year == 2023

    @pytest.mark.asyncio
    async def test_lookup_by_arxiv_id_authors(self, provider):
        fake_resp = _mock_resp(_ARXIV_ATOM)
        fake_resp.text = _ARXIV_ATOM
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_arxiv_id("2301.00001", client)
        assert len(ref.authors) == 2
        family_names = {a.family for a in ref.authors}
        assert "Smith" in family_names or "Alice" in family_names

    @pytest.mark.asyncio
    async def test_lookup_by_arxiv_id_ref_type_preprint(self, provider):
        fake_resp = _mock_resp(_ARXIV_ATOM)
        fake_resp.text = _ARXIV_ATOM
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_arxiv_id("2301.00001", client)
        assert ref.ref_type == RefType.PREPRINT

    @pytest.mark.asyncio
    async def test_lookup_by_arxiv_id_returns_none_on_404(self, provider):
        with patch.object(provider, "_get", new=AsyncMock(return_value=None)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_arxiv_id("9999.99999", client)
        assert ref is None

    def test_provider_name(self, provider):
        assert provider.name == "arxiv"

    def test_provider_priority_lower_than_crossref(self, provider):
        crossref = CrossRefProvider()
        assert provider.priority > crossref.priority


# ---------------------------------------------------------------------------
# Semantic Scholar provider tests
# ---------------------------------------------------------------------------

class TestSemanticScholarProvider:
    @pytest.fixture
    def provider(self):
        return SemanticScholarProvider()

    @pytest.mark.asyncio
    async def test_lookup_by_doi_returns_reference(self, provider):
        fake_resp = _mock_resp(_ss_paper())
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.1234/fake", client)
        assert ref is not None
        assert ref.title == "Fake SS Paper"

    @pytest.mark.asyncio
    async def test_lookup_by_doi_open_access(self, provider):
        fake_resp = _mock_resp(_ss_paper())
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.1234/fake", client)
        assert ref.open_access is True
        assert ref.oa_url == "https://example.com/paper.pdf"

    @pytest.mark.asyncio
    async def test_lookup_by_doi_citation_count(self, provider):
        fake_resp = _mock_resp(_ss_paper(citation_count=555))
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.1234/fake", client)
        assert ref.citation_count == 555

    @pytest.mark.asyncio
    async def test_lookup_by_doi_returns_none_on_404(self, provider):
        with patch.object(provider, "_get", new=AsyncMock(return_value=None)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.xxx/missing", client)
        assert ref is None

    def test_provider_name(self, provider):
        assert provider.name == "semantic_scholar"


# ---------------------------------------------------------------------------
# BaseProvider rate-limiting / dispatch tests
# ---------------------------------------------------------------------------

class _ConcreteProvider(BaseProvider):
    """Minimal concrete provider for testing base class behavior."""
    name = "test_provider"
    priority = 10

    async def lookup_by_doi(self, doi, client):
        ref = Reference(doi=doi, title="Mock result", ref_type=RefType.JOURNAL)
        return ref

    async def search(self, title, authors=None, year=None, client=None):
        ref = Reference(title=title, ref_type=RefType.JOURNAL)
        return [ref]


class TestBaseProvider:
    @pytest.fixture
    def provider(self):
        return _ConcreteProvider()

    @pytest.mark.asyncio
    async def test_lookup_dispatches_doi(self, provider):
        ref = Reference(doi="10.xxx/1", ref_type=RefType.JOURNAL)
        results = await provider.lookup(ref)
        assert len(results) == 1
        assert results[0].doi == "10.xxx/1"

    @pytest.mark.asyncio
    async def test_lookup_doi_hits_add_confidence_1(self, provider):
        ref = Reference(doi="10.xxx/1", ref_type=RefType.JOURNAL)
        results = await provider.lookup(ref)
        assert results[0].sources.get("test_provider") == 1.0

    @pytest.mark.asyncio
    async def test_lookup_falls_back_to_title_search(self, provider):
        ref = Reference(title="Some paper", ref_type=RefType.JOURNAL)
        results = await provider.lookup(ref)
        assert len(results) >= 1
        assert results[0].title == "Some paper"

    @pytest.mark.asyncio
    async def test_lookup_title_search_confidence(self, provider):
        ref = Reference(title="Some paper", ref_type=RefType.JOURNAL)
        results = await provider.lookup(ref)
        assert results[0].sources.get("test_provider") == 0.70

    def test_repr(self, provider):
        assert "test_provider" in repr(provider) or "priority" in repr(provider)


# ---------------------------------------------------------------------------
# CrossRef: new-field parsing tests
# ---------------------------------------------------------------------------

class TestCrossRefNewFields:
    @pytest.fixture
    def provider(self):
        return CrossRefProvider()

    def _conference_work(self) -> dict:
        return {
            "DOI": "10.1109/CVPR.2016.90",
            "type": "proceedings-article",
            "title": ["Deep Residual Learning"],
            "author": [{"given": "Kaiming", "family": "He"}],
            "editor": [{"given": "Alice", "family": "Smith"}],
            "published": {"date-parts": [[2016, 6]]},
            "event": {"name": "IEEE CVPR 2016"},
            "article-number": "7780459",
            "ISSN": ["1063-6919", "2575-7075"],
            "issn-type": [
                {"value": "1063-6919", "type": "print"},
                {"value": "2575-7075", "type": "electronic"},
            ],
            "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
        }

    def test_event_name_parsed(self, provider):
        ref = provider._parse_work(self._conference_work())
        assert ref.event_name == "IEEE CVPR 2016"

    def test_article_number_parsed(self, provider):
        ref = provider._parse_work(self._conference_work())
        assert ref.article_number == "7780459"

    def test_eissn_parsed(self, provider):
        ref = provider._parse_work(self._conference_work())
        assert ref.eissn == "2575-7075"

    def test_issn_print_parsed(self, provider):
        ref = provider._parse_work(self._conference_work())
        assert ref.issn == "1063-6919"

    def test_license_url_parsed(self, provider):
        ref = provider._parse_work(self._conference_work())
        assert "creativecommons" in (ref.license or "")

    def test_open_access_inferred_from_cc_license(self, provider):
        ref = provider._parse_work(self._conference_work())
        assert ref.open_access is True

    def test_editors_parsed(self, provider):
        ref = provider._parse_work(self._conference_work())
        assert len(ref.editors) == 1
        assert ref.editors[0].family == "Smith"

    def test_ref_type_conference(self, provider):
        ref = provider._parse_work(self._conference_work())
        assert ref.ref_type == RefType.CONFERENCE


# ---------------------------------------------------------------------------
# OpenAlex provider tests
# ---------------------------------------------------------------------------

def _openalex_work(
    title="OpenAlex Paper",
    doi="10.1234/oa",
    year=2023,
    citation_count=15,
    is_oa=False,
    oa_url=None,
    issns=None,
) -> dict:
    issns = issns or ["1234-5678"]
    return {
        "title": title,
        "doi": f"https://doi.org/{doi}",
        "publication_year": year,
        "type": "journal-article",
        "cited_by_count": citation_count,
        "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/12345678/"},
        "authorships": [
            {"author": {"display_name": "Alice Smith", "orcid": None}, "institutions": []},
        ],
        "abstract_inverted_index": {"Hello": [0], "world": [1]},
        "primary_location": {
            "source": {
                "display_name": "Journal of Fake Science",
                "issn_l": issns[0],
                "issn": issns,
                "host_organization_name": "Elsevier",
            }
        },
        "biblio": {"volume": "10", "issue": "2", "first_page": "100", "last_page": "110"},
        "open_access": {"is_oa": is_oa, "oa_url": oa_url},
        "keywords": [{"display_name": "machine learning"}],
        "language": "en",
        "locations": [],
    }


class TestOpenAlexProvider:
    @pytest.fixture
    def provider(self):
        return OpenAlexProvider()

    def test_parse_title(self, provider):
        ref = provider._parse_work(_openalex_work(title="Test Title"))
        assert ref.title == "Test Title"

    def test_parse_doi_normalized(self, provider):
        ref = provider._parse_work(_openalex_work(doi="10.1234/oa"))
        assert ref.doi == "10.1234/oa"
        assert not (ref.doi or "").startswith("http")

    def test_parse_year(self, provider):
        ref = provider._parse_work(_openalex_work(year=2021))
        assert ref.year == 2021

    def test_parse_citation_count(self, provider):
        ref = provider._parse_work(_openalex_work(citation_count=77))
        assert ref.citation_count == 77

    def test_parse_open_access(self, provider):
        ref = provider._parse_work(_openalex_work(is_oa=True, oa_url="https://example.com/pdf"))
        assert ref.open_access is True
        assert ref.oa_url == "https://example.com/pdf"

    def test_parse_authors(self, provider):
        ref = provider._parse_work(_openalex_work())
        assert len(ref.authors) == 1
        assert "Smith" in ref.authors[0].family or "Smith" in ref.authors[0].given

    def test_parse_journal(self, provider):
        ref = provider._parse_work(_openalex_work())
        assert ref.journal == "Journal of Fake Science"

    def test_parse_volume_issue_pages(self, provider):
        ref = provider._parse_work(_openalex_work())
        assert ref.volume == "10"
        assert ref.issue == "2"
        assert ref.pages == "100-110"

    def test_parse_abstract_from_inverted_index(self, provider):
        ref = provider._parse_work(_openalex_work())
        assert ref.abstract == "Hello world"

    def test_parse_issn(self, provider):
        ref = provider._parse_work(_openalex_work(issns=["1234-5678"]))
        assert ref.issn == "1234-5678"

    def test_parse_eissn_second_issn(self, provider):
        ref = provider._parse_work(_openalex_work(issns=["1234-5678", "8765-4321"]))
        assert ref.eissn == "8765-4321"

    def test_parse_pmid(self, provider):
        ref = provider._parse_work(_openalex_work())
        assert ref.pmid == "12345678"

    def test_provider_name(self, provider):
        assert provider.name == "openalex"

    def test_provider_priority(self, provider):
        assert provider.priority == 2

    @pytest.mark.asyncio
    async def test_lookup_by_doi_calls_api(self, provider):
        fake_resp = _mock_resp(_openalex_work())
        with patch.object(provider, "_get", new=AsyncMock(return_value=fake_resp)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.1234/oa", client)
        assert ref is not None
        assert ref.title == "OpenAlex Paper"

    @pytest.mark.asyncio
    async def test_lookup_by_doi_returns_none_on_404(self, provider):
        with patch.object(provider, "_get", new=AsyncMock(return_value=None)):
            async with provider._make_client() as client:
                ref = await provider.lookup_by_doi("10.xxx/missing", client)
        assert ref is None


# ---------------------------------------------------------------------------
# _reconstruct_abstract helper
# ---------------------------------------------------------------------------

class TestReconstructAbstract:
    def test_basic(self):
        inv = {"Hello": [0], "world": [1]}
        assert _reconstruct_abstract(inv) == "Hello world"

    def test_out_of_order_positions(self):
        inv = {"second": [1], "first": [0]}
        assert _reconstruct_abstract(inv) == "first second"

    def test_multiple_positions_same_word(self):
        inv = {"the": [0, 2], "cat": [1]}
        result = _reconstruct_abstract(inv)
        words = result.split()
        assert words[0] == "the"
        assert words[1] == "cat"
        assert words[2] == "the"

    def test_empty(self):
        assert _reconstruct_abstract({}) == ""


# ---------------------------------------------------------------------------
# PubMed provider tests
# ---------------------------------------------------------------------------

_PUBMED_XML = """\
<PubmedArticleSet>
<PubmedArticle>
  <MedlineCitation>
    <PMID>12345678</PMID>
    <Article>
      <ArticleTitle>A PubMed Paper</ArticleTitle>
      <Abstract>
        <AbstractText>This is the abstract.</AbstractText>
      </Abstract>
      <AuthorList>
        <Author>
          <LastName>Brown</LastName>
          <ForeName>Alice</ForeName>
          <Identifier Source="ORCID">https://orcid.org/0000-0001-2345-6789</Identifier>
        </Author>
        <Author>
          <LastName>Jones</LastName>
          <ForeName>Bob</ForeName>
        </Author>
      </AuthorList>
      <Journal>
        <Title>Journal of Science</Title>
        <ISOAbbreviation>J. Sci.</ISOAbbreviation>
        <ISSN IssnType="Print">1234-5678</ISSN>
        <ISSN IssnType="Electronic">8765-4321</ISSN>
        <JournalIssue>
          <Volume>10</Volume>
          <Issue>2</Issue>
          <PubDate><Year>2022</Year><Month>Mar</Month></PubDate>
        </JournalIssue>
      </Journal>
      <Pagination><MedlinePgn>100-110</MedlinePgn></Pagination>
      <Language>eng</Language>
    </Article>
    <KeywordList>
      <Keyword>neuroscience</Keyword>
      <Keyword>brain</Keyword>
    </KeywordList>
  </MedlineCitation>
  <PubmedData>
    <ArticleIdList>
      <ArticleId IdType="doi">10.1234/fake.doi</ArticleId>
      <ArticleId IdType="pmc">PMC1234567</ArticleId>
    </ArticleIdList>
  </PubmedData>
</PubmedArticle>
</PubmedArticleSet>
"""


class TestPubMedProvider:
    @pytest.fixture
    def provider(self):
        return PubMedProvider()

    def test_parse_title(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref is not None
        assert ref.title == "A PubMed Paper"

    def test_parse_pmid(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.pmid == "12345678"

    def test_parse_doi(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.doi == "10.1234/fake.doi"

    def test_parse_pmcid(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.pmcid == "PMC1234567"

    def test_parse_authors(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert len(ref.authors) == 2
        assert ref.authors[0].family == "Brown"
        assert ref.authors[0].given == "Alice"

    def test_parse_orcid(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.authors[0].orcid == "0000-0001-2345-6789"

    def test_parse_year_month(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.year == 2022
        assert ref.month == 3

    def test_parse_journal(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.journal == "Journal of Science"
        assert ref.journal_abbrev == "J. Sci."

    def test_parse_volume_issue(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.volume == "10"
        assert ref.issue == "2"

    def test_parse_pages(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.pages == "100-110"

    def test_parse_issn_print(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.issn == "1234-5678"

    def test_parse_eissn_electronic(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.eissn == "8765-4321"

    def test_parse_keywords(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert "neuroscience" in ref.keywords

    def test_parse_language(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert ref.language == "eng"

    def test_parse_abstract(self, provider):
        ref = provider._parse_xml(_PUBMED_XML)
        assert "abstract" in (ref.abstract or "").lower()

    def test_invalid_xml_returns_none(self, provider):
        ref = provider._parse_xml("<not valid xml>")
        assert ref is None

    def test_provider_name(self, provider):
        assert provider.name == "pubmed"

    def test_provider_priority(self, provider):
        assert provider.priority == 4


# ---------------------------------------------------------------------------
# DBLP provider tests
# ---------------------------------------------------------------------------

def _dblp_hit(
    title="DBLP Paper",
    doi="10.9999/dblp.1",
    year="2021",
    venue="ICML",
    pub_type="Conference and Workshop Papers",
) -> dict:
    return {
        "info": {
            "title": title,
            "doi": doi,
            "year": year,
            "venue": venue,
            "type": pub_type,
            "volume": "5",
            "pages": "200-210",
            "authors": {
                "author": [
                    {"text": "Alice Smith"},
                    {"text": "Bob Jones"},
                ]
            },
            "url": "https://dblp.org/rec/conf/icml/2021",
            "access": "open",
        }
    }


class TestDBLPProvider:
    @pytest.fixture
    def provider(self):
        return DBLPProvider()

    def test_parse_title(self, provider):
        ref = provider._parse_hit(_dblp_hit())
        assert ref is not None
        assert ref.title == "DBLP Paper"

    def test_parse_doi(self, provider):
        ref = provider._parse_hit(_dblp_hit())
        assert ref.doi == "10.9999/dblp.1"

    def test_parse_year(self, provider):
        ref = provider._parse_hit(_dblp_hit())
        assert ref.year == 2021

    def test_parse_authors(self, provider):
        ref = provider._parse_hit(_dblp_hit())
        assert len(ref.authors) == 2

    def test_parse_journal_type(self, provider):
        ref = provider._parse_hit(_dblp_hit(pub_type="Journal Articles"))
        assert ref.ref_type == RefType.JOURNAL

    def test_parse_conference_type(self, provider):
        ref = provider._parse_hit(_dblp_hit())
        assert ref.ref_type == RefType.CONFERENCE

    def test_parse_venue(self, provider):
        ref = provider._parse_hit(_dblp_hit())
        assert ref.journal == "ICML"

    def test_parse_volume_pages(self, provider):
        ref = provider._parse_hit(_dblp_hit())
        assert ref.volume == "5"
        assert ref.pages == "200-210"

    def test_parse_open_access(self, provider):
        ref = provider._parse_hit(_dblp_hit())
        assert ref.open_access is True

    def test_title_trailing_dot_stripped(self, provider):
        ref = provider._parse_hit(_dblp_hit(title="A Paper."))
        assert ref.title == "A Paper"

    def test_empty_info_returns_none(self, provider):
        ref = provider._parse_hit({})
        assert ref is None

    def test_provider_name(self, provider):
        assert provider.name == "dblp"

    def test_provider_priority(self, provider):
        assert provider.priority == 5
