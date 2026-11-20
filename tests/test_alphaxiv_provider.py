"""AlphaXivProvider: opt-in, title-exact matching (a wrong arXiv id is worse than none)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mouseion.models import RefType
from mouseion.providers import alphaxiv as ax
from mouseion.providers.alphaxiv import AlphaXivProvider, title_similarity

HITS = [
    {"paperId": "2610.29585v2", "title": "Any colour you like, as long as it's UIP: Semi-simplicial types from weakenings",
     "abstract": "  We construct   semi-simplicial types. ", "publicationDate": "2026-10-03T00:00:00.000Z", "votes": 3,
     "snippets": [{"pageNumber": 1, "snippet": "..."}]},
    {"paperId": "2609.20919", "title": "Massive type 0A string theory and M-theory", "abstract": "x",
     "publicationDate": "2026-09-17T00:00:00.000Z"},
]


def _resp(data):
    r = MagicMock(); r.status_code = 200; r.json = MagicMock(return_value=data); return r


@pytest.mark.asyncio
async def test_exact_title_match_returns_arxiv_id():
    p = AlphaXivProvider()
    with patch.object(p, "_get", new=AsyncMock(return_value=_resp(HITS))):
        async with p._make_client() as c:
            refs = await p.search("Any colour you like, as long as it's UIP: Semi-simplicial types from weakenings", client=c)
    assert len(refs) == 1
    r = refs[0]
    assert r.arxiv_id == "2610.29585"                      # version stripped
    assert r.year == 2026 and r.month == 10
    assert r.abstract == "We construct semi-simplicial types."
    assert r.ref_type == RefType.PREPRINT
    assert r.url == "https://www.alphaxiv.org/abs/2610.29585"
    assert r.sources["alphaxiv"] >= ax.MATCH_THRESHOLD


@pytest.mark.asyncio
async def test_full_text_mentions_are_not_matches():
    p = AlphaXivProvider()
    with patch.object(p, "_get", new=AsyncMock(return_value=_resp(HITS))):
        async with p._make_client() as c:
            refs = await p.search("Two-level type theory and applications", client=c)
    assert refs == []


@pytest.mark.asyncio
async def test_failed_request_and_bad_payload_return_empty():
    p = AlphaXivProvider()
    for ret in (None, _resp({"error": "x"})):
        with patch.object(p, "_get", new=AsyncMock(return_value=ret)):
            async with p._make_client() as c:
                assert await p.search("A sufficiently long paper title here", client=c) == []


@pytest.mark.asyncio
async def test_no_doi_lookup():
    p = AlphaXivProvider()
    async with p._make_client() as c:
        assert await p.lookup_by_doi("10.1000/x", c) is None


def test_similarity_normalizes_case_and_punctuation():
    assert title_similarity("Attention Is All You Need", "attention is all you need.") == 1.0
    assert title_similarity("Attention Is All You Need", "Massive type 0A string theory") < 0.5


@pytest.mark.asyncio
async def test_record_without_subtitle_matches_main_title():
    hits = [dict(HITS[0], title="Any colour you like, as long as it's UIP")]
    p = AlphaXivProvider()
    with patch.object(p, "_get", new=AsyncMock(return_value=_resp(hits))):
        async with p._make_client() as c:
            refs = await p.search("Any colour you like, as long as it's UIP: Semi-simplicial types from weakenings", client=c)
    assert [r.arxiv_id for r in refs] == ["2610.29585"]


def test_short_main_title_is_not_enough():
    assert ax.title_match("Introduction", "Introduction: a survey of everything") < ax.MATCH_THRESHOLD


def test_opt_in_default_off():
    class C: alphaxiv_enabled = False
    assert ax.enabled(C()) is False
    C.alphaxiv_enabled = True
    assert ax.enabled(C()) is True


def test_registered_only_when_enabled():
    from mouseion import providers as P
    with patch.object(ax, "enabled", return_value=False):
        assert not any(isinstance(x, AlphaXivProvider) for x in P._make_providers())
    with patch.object(ax, "enabled", return_value=True):
        assert any(isinstance(x, AlphaXivProvider) for x in P._make_providers())
