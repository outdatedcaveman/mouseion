"""Tests for bibliographic data providers (mocked HTTP)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Optional

import pytest
import httpx

from zoterpile.models import Author, Reference, RefType
from zoterpile.providers.crossref import CrossRefProvider
from zoterpile.providers.arxiv import ArXivProvider
from zoterpile.providers.semantic_scholar import SemanticScholarProvider
from zoterpile.providers.base import BaseProvider


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
