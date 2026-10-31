"""BASE (base-search.net) provider: parsing, matching, access refusal, PDF-chain wiring.

All HTTP is mocked: BASE answers only registered IP + User-Agent pairs (or an
apikey), so its responses are reproduced here from the Interface Guide v1.27.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from mouseion.models import Author, Reference
from mouseion.providers import base_search as B


DENIED = {"error": "Access denied for IP address 203.0.113.9 and user agent x."}

DOC_REPO = {
    "dctitle": "Moduli of curves in positive characteristic",
    "dccreator": ["Doe, Jane", "Roe, Rick"],
    "dcyear": 2019,
    "dcdoi": ["10.1234/ABC.5"],
    "dclink": "https://repo.example.edu/handle/123/456",
    "dcidentifier": ["https://repo.example.edu/handle/123/456",
                     "https://repo.example.edu/bitstream/123/456/1/thesis.pdf",
                     "https://doi.org/10.1234/abc.5"],
    "dcoa": 1,
    "dccollection": "ftexampleuniv",
    "dcprovider": "Example University Repository",
    "dctypenorm": ["183"],
}


def _json(docs):
    return json.dumps({"response": {"numFound": len(docs), "start": 0, "docs": docs}})


@pytest.fixture(autouse=True)
def _fresh_access(monkeypatch):
    monkeypatch.setitem(B._ACCESS, "denied_until", 0.0)
    monkeypatch.setitem(B._ACCESS, "message", "")

    from mouseion import api_router
    monkeypatch.setattr(api_router, "get_router", lambda: _Router())


class _Router:
    """Router stand-in: always grants, remembers recorded attempts."""
    def __init__(self):
        self.recorded = []

    async def acquire(self, api, max_wait=10.0):
        return True

    def report(self, *a, **k):
        pass

    def entry_hash(self, ref):
        return "h"

    def was_tried(self, *a):
        return False

    def record_attempt(self, *a):
        self.recorded.append(a)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- parsing

def test_parse_json_docs():
    docs = B.parse_response(_json([DOC_REPO]))
    assert docs[0]["dccollection"] == "ftexampleuniv"


def test_parse_documented_xml():
    xml = """<?xml version="1.0" encoding="UTF-8"?><response>
      <result name="response" numFound="1" start="0"><doc>
        <str name="dctitle">10 years BASE</str>
        <arr name="dccreator"><str>Pieper, Dirk</str><str>Summann, Friedrich</str></arr>
        <str name="dclink">https://pub.uni-bielefeld.de/record/2710028</str>
        <int name="dcoa">1</int><str name="dccollection">ftubbiepub</str>
      </doc></result></response>"""
    d = B.parse_response(xml)[0]
    assert d["dccreator"] == ["Pieper, Dirk", "Summann, Friedrich"]
    assert d["dcoa"] == "1"


@pytest.mark.parametrize("body", [json.dumps(DENIED),
                                  "<error>Access denied for IP address 1.2.3.4 and user agent x.</error>"])
def test_refusal_is_access_denied_not_no_results(body):
    with pytest.raises(B.BaseAccessDenied):
        B.parse_response(body)


def test_no_results_message_is_empty():
    assert B.parse_response(json.dumps({"error": "No results found!"})) == []


# ---------------------------------------------------------------- queries + matching

def test_doi_query_normalises_and_quotes():
    assert B.doi_query("https://doi.org/10.1234/ABC.5") == 'dcdoi:"10.1234/abc.5"'
    assert '"' not in B.doi_query('10.1/a"b')[7:-1]


def test_title_query_has_author_and_year_window():
    q = B.title_query("Moduli of curves in positive characteristic", ["Doe"], 2019)
    assert 'dctitle:"Moduli of curves in positive characteristic"' in q
    assert 'dccreator:"Doe"' in q and "dcyear:[2018 TO 2020]" in q
    assert len(B.title_query("x" * 5000, ["y"], 2000)) <= 1000


def test_matching():
    assert B.doc_matches_doi(DOC_REPO, "10.1234/abc.5")
    assert not B.doc_matches_doi(DOC_REPO, "10.1234/abc.6")
    assert B.doc_matches_title(DOC_REPO, "Moduli of Curves in Positive Characteristic", ["Doe"], 2019)
    assert not B.doc_matches_title(DOC_REPO, "Moduli of curves in positive characteristic", ["Smith"], 2019)
    assert not B.doc_matches_title(DOC_REPO, "Moduli of curves in positive characteristic", ["Doe"], 2012)
    assert not B.doc_matches_title(DOC_REPO, "Moduli of surfaces in mixed characteristic", ["Doe"], 2019)


def test_copies_rank_files_first_drop_resolvers_and_closed():
    copies = B.rank_copies(B.copies_from_doc(DOC_REPO))
    assert [c.url for c in copies] == ["https://repo.example.edu/bitstream/123/456/1/thesis.pdf",
                                       "https://repo.example.edu/handle/123/456"]
    assert copies[0].collection == "ftexampleuniv" and copies[0].provider.startswith("Example")
    assert B.copies_from_doc({**DOC_REPO, "dcoa": 0}) == []


def test_doc_to_reference():
    r = B.doc_to_reference(DOC_REPO)
    assert r.doi == "10.1234/abc.5" and r.year == 2019 and r.authors[0].family == "Doe"
    assert r.open_access and r.oa_url.endswith("thesis.pdf")
    assert r.extras["base_search"]["collection"] == "ftexampleuniv"


# ---------------------------------------------------------------- HTTP behaviour

def test_doi_first_then_title_fallback():
    seen = []

    def handler(req):
        q = req.url.params["query"]
        seen.append(q)
        assert req.headers["user-agent"] == B.USER_AGENT
        assert req.url.params["func"] == "PerformSearch" and req.url.params["format"] == "json"
        if q.startswith("dcdoi:"):
            return httpx.Response(200, text=_json([]))
        return httpx.Response(200, text=_json([{**DOC_REPO, "dcdoi": []}]))

    ref = Reference(doi="10.1234/abc.5", title="Moduli of curves in positive characteristic",
                    authors=[Author(family="Doe", given="J")], year=2019)

    async def go():
        async with _client(handler) as c:
            return await B.repository_copies(c, ref, api_key="")
    copies = asyncio.run(go())
    assert seen[0].startswith("dcdoi:") and seen[1].startswith("dctitle:")
    assert copies and copies[0].matched_by == "title"


def test_title_hit_with_a_different_doi_is_rejected():
    def handler(req):
        if req.url.params["query"].startswith("dcdoi:"):
            return httpx.Response(200, text=_json([]))
        return httpx.Response(200, text=_json([{**DOC_REPO, "dcdoi": ["10.9999/other"]}]))

    ref = Reference(doi="10.1234/abc.5", title="Moduli of curves in positive characteristic",
                    authors=[Author(family="Doe")], year=2019)

    async def go():
        async with _client(handler) as c:
            return await B.repository_copies(c, ref, api_key="")
    assert asyncio.run(go()) == []


def test_apikey_is_sent_when_set():
    keys = []

    def handler(req):
        keys.append(req.url.params.get("apikey"))
        return httpx.Response(200, text=_json([DOC_REPO]))

    async def go():
        async with _client(handler) as c:
            await B.perform_search(c, B.doi_query("10.1234/abc.5"), api_key="k123")
    asyncio.run(go())
    assert keys == ["k123"]


def test_denial_parks_access_and_stops_asking():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, text=json.dumps(DENIED))

    async def go():
        async with _client(handler) as c:
            for _ in range(3):
                with pytest.raises(B.BaseAccessDenied):
                    await B.perform_search(c, "dcdoi:\"10.1/x\"", api_key="")
    asyncio.run(go())
    assert len(calls) == 1 and B.access_parked()


def test_unaskable_is_none_not_empty(monkeypatch):
    from mouseion import api_router

    class _Busy:
        async def acquire(self, api, max_wait=10.0):
            return False

    monkeypatch.setattr(api_router, "get_router", lambda: _Busy())

    async def go():
        async with _client(lambda r: httpx.Response(500)) as c:
            return await B.repository_copies(c, Reference(doi="10.1/x", title="A long enough title here"),
                                             api_key="")
    assert asyncio.run(go()) is None


def test_provider_lookup_by_doi():
    p = B.BaseSearchProvider(api_key="")

    async def go():
        async with _client(lambda r: httpx.Response(200, text=_json([DOC_REPO]))) as c:
            return await p.lookup_by_doi("10.1234/ABC.5", c)
    r = asyncio.run(go())
    assert r and r.title.startswith("Moduli")


# ---------------------------------------------------------------- config + PDF chain

def test_flag_off_by_default_and_env_bool(monkeypatch):
    from mouseion import config as C
    assert C.Config().base_search_enabled is False
    cfg = C.Config()
    monkeypatch.setenv("MOUSEION_BASE_SEARCH_ENABLED", "0")
    monkeypatch.setenv("MOUSEION_VPN_ENABLED", "0")
    C._apply_env(cfg)
    assert cfg.base_search_enabled is False and cfg.vpn_enabled is False
    monkeypatch.setenv("MOUSEION_BASE_SEARCH_ENABLED", "1")
    C._apply_env(cfg)
    assert cfg.base_search_enabled is True


def test_ledger_key_separates_base_runs(monkeypatch):
    from mouseion import pdf_manager as PM
    monkeypatch.setattr(PM, "_base_search_on", lambda: False)
    off = PM._pdf_ledger_key()
    monkeypatch.setattr(PM, "_base_search_on", lambda: True)
    assert PM._pdf_ledger_key() != off and "_base" in PM._pdf_ledger_key()


def test_pdf_chain_downloads_the_repository_copy(monkeypatch, tmp_path):
    from mouseion import pdf_manager as PM
    from mouseion.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "base_search_enabled", True)
    monkeypatch.setattr(PM, "get_pdf_dir", lambda: tmp_path)
    monkeypatch.setenv("MOUSEION_PDF_SHADOW", "0")
    pdf = b"%PDF-1.5\n" + b"0" * 4096

    def handler(req):
        if req.url.host == "api.base-search.net":
            return httpx.Response(200, text=_json([DOC_REPO]))
        if req.url.path.endswith("thesis.pdf"):
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    ref = Reference(doi="10.1234/abc.5", title="Moduli of curves in positive characteristic",
                    authors=[Author(family="Doe")], year=2019)

    async def go():
        async with _client(handler) as c:
            return await PM._download_pdf_impl(ref, c)
    name = asyncio.run(go())
    assert name and (tmp_path / name).read_bytes().startswith(b"%PDF")
    assert ref.extras["pdf_provenance"]["repository"] == "ftexampleuniv"


def test_pdf_chain_refusal_is_not_recorded_as_a_miss(monkeypatch, tmp_path):
    from mouseion import pdf_manager as PM
    from mouseion.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "base_search_enabled", True)
    monkeypatch.setattr(PM, "get_pdf_dir", lambda: tmp_path)
    monkeypatch.setenv("MOUSEION_PDF_SHADOW", "0")
    router = _Router()
    from mouseion import api_router
    monkeypatch.setattr(api_router, "get_router", lambda: router)

    def handler(req):
        if req.url.host == "api.base-search.net":
            return httpx.Response(200, text=json.dumps(DENIED))
        return httpx.Response(404)

    ref = Reference(doi="10.1234/zzz", title="Some title that is long enough")
    ref._db_id = 42

    async def go():
        async with _client(handler) as c:
            return await PM._download_pdf_impl(ref, c)
    assert asyncio.run(go()) is None
    assert router.recorded == []


def test_control_chain_without_base_does_record_the_miss(monkeypatch, tmp_path):
    """Witness for the test above: the same chain with BASE off records a miss,
    so 'nothing recorded' there is caused by the refusal handling."""
    from mouseion import pdf_manager as PM
    from mouseion.config import get_config
    monkeypatch.setattr(get_config(), "base_search_enabled", False)
    monkeypatch.setattr(PM, "get_pdf_dir", lambda: tmp_path)
    monkeypatch.setenv("MOUSEION_PDF_SHADOW", "0")
    router = _Router()
    from mouseion import api_router
    monkeypatch.setattr(api_router, "get_router", lambda: router)
    ref = Reference(doi="10.1234/zzz", title="Some title that is long enough")
    ref._db_id = 42

    async def go():
        async with _client(lambda r: httpx.Response(404)) as c:
            return await PM._download_pdf_impl(ref, c)
    assert asyncio.run(go()) is None
    assert router.recorded and router.recorded[0][-1] == "miss"


def test_unknown_json_shape_is_an_error_not_no_copies():
    with pytest.raises(ValueError):
        B.parse_response(json.dumps({"result": {"items": []}}))
