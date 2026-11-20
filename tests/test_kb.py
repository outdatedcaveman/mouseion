"""mouseion.kb on a synthetic library: full text, citation graph, notes with provenance, MCP protocol."""

from __future__ import annotations

import io
import json

import pytest

fitz = pytest.importorskip("fitz")

from mouseion.db import RefDatabase
from mouseion.models import Author, Reference
from mouseion.kb import citations, fulltext, search
from mouseion.kb.mcp_server import serve
from mouseion.kb.store import open_kb, open_library


def _pdf(path, pages):
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 550, 800), text, fontsize=10)
    doc.save(str(path))
    doc.close()


@pytest.fixture()
def env(tmp_path):
    refs = {
        "a": Reference(
            title="Closure and coarse-graining of automata",
            doi="10.1/a",
            year=2020,
            authors=[Author(family="Rosas"), Author(family="Jensen")],
            abstract="Causal states and lumpability.",
        ),
        "b": Reference(
            title="Harmonic analysis of learned representations",
            doi="10.1/b",
            year=2022,
            authors=[Author(family="Chughtai")],
            abstract="Networks learn irreducible representations.",
        ),
        "c": Reference(
            title="Stability conditions on triangulated categories",
            doi="10.1/c",
            year=2007,
            authors=[Author(family="Bridgeland")],
        ),
    }
    with RefDatabase(path=str(tmp_path / "refs.db")) as db:
        ids = {k: db.upsert(r) for k, r in refs.items()}
    _pdf(
        tmp_path / "a.pdf",
        [
            "A coarse-graining is strongly lumpable when the block indicators span an invariant "
            "subspace of every input action. This is the Kemeny-Snell condition.",
            "Causal states are the minimal sufficient statistic of the past for the future.",
        ],
    )
    _pdf(
        tmp_path / "b.pdf",
        [
            "Grokking modular addition, networks build Fourier features; the leading frequency "
            "depends on the seed, a lottery over irreducible representations."
        ],
    )
    with RefDatabase(path=str(tmp_path / "refs.db")) as db:
        db.update_integration_ids(ids["a"], pdf_local=str(tmp_path / "a.pdf"))
        db.update_integration_ids(ids["b"], pdf_local=str(tmp_path / "b.pdf"))
        db.update_integration_ids(ids["c"], pdf_local=str(tmp_path / "missing.pdf"))
    lib, kb = open_library(str(tmp_path / "refs.db")), open_kb(str(tmp_path / "kb.db"))
    return tmp_path, ids, lib, kb


def test_fulltext_index_and_search(env):
    tmp, ids, lib, kb = env
    counts = fulltext.build(lib, kb)
    assert counts == {"ok": 2, "missing": 1}
    assert fulltext.build(lib, kb) == {"unchanged": 2, "missing": 1}  # incremental
    hits = search.search_passages(lib, kb, "strongly lumpable invariant subspace")
    assert hits and hits[0]["ref_id"] == ids["a"] and hits[0]["page"] == 1
    assert "Rosas" in hits[0]["citation"] and "p. 1" in hits[0]["citation"]
    assert (
        search.search_passages(lib, kb, "minimal sufficient statistic")[0]["page"] == 2
    )
    assert search.search_refs(lib, "stability conditions")[0]["ref_id"] == ids["c"]


def test_citation_graph(env):
    tmp, ids, lib, kb = env
    fake = {
        "10.1/a": {
            "id": "https://openalex.org/WA",
            "referenced_works": ["https://openalex.org/WC", "https://openalex.org/WX"],
        },
        "10.1/b": {
            "id": "https://openalex.org/WB",
            "referenced_works": [
                "https://openalex.org/WA",
                "https://openalex.org/WC",
                "https://openalex.org/WX",
            ],
        },
        "10.1/c": {"id": "https://openalex.org/WC", "referenced_works": []},
    }
    counts = citations.build(lib, kb, rate=1e6, fetcher=lambda doi: fake.get(doi))
    assert counts == {"ok": 3}
    assert (
        citations.build(lib, kb, rate=1e6, fetcher=lambda doi: 1 / 0) == {}
    )  # cached: nothing refetched
    assert {r["ref_id"] for r in citations.references(kb, ids["b"])} == {
        ids["a"],
        ids["c"],
        None,
    }
    assert set(citations.cited_by(kb, ids["c"])) == {ids["a"], ids["b"]}
    assert citations.coupling(kb, ids["a"])[0] == (ids["b"], 2)  # share WC and WX
    assert citations.frontier(kb)[0] == ("WX", 2)  # cited twice, not in the library


def test_notes_require_provenance(env):
    tmp, ids, lib, kb = env
    fulltext.build(lib, kb)
    n = search.add_note(
        lib,
        kb,
        "semplex",
        "Closure is invariance of the block span.",
        ids["a"],
        quote="block indicators span an invariant subspace",
    )
    assert n["chunk_id"] is not None  # passage located from the quote
    with pytest.raises(ValueError):
        search.add_note(lib, kb, "semplex", "x", "no-such-ref")
    with pytest.raises(ValueError):
        search.add_note(
            lib, kb, "semplex", "x", ids["a"], quote="text that is not in the paper"
        )
    n2 = search.add_note(
        lib,
        kb,
        "semplex",
        "Closure equals strong lumpability (Kemeny-Snell).",
        ids["a"],
        supersedes=n["id"],
    )
    act = search.search_notes(lib, kb, "lumpability closure")
    assert [x["id"] for x in act] == [n2["id"]]
    assert len(search.search_notes(lib, kb, "closure", include_inactive=True)) == 2


def test_mcp_protocol(env):
    tmp, ids, lib, kb = env
    fulltext.build(lib, kb)
    reqs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_fulltext",
                "arguments": {"query": "lottery seed"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "add_note",
                "arguments": {
                    "project": "semplex",
                    "claim": "x",
                    "ref_id": ids["b"],
                    "quote": "not in it",
                },
            },
        },
    ]
    out = io.StringIO()
    serve(
        str(tmp / "refs.db"),
        str(tmp / "kb.db"),
        io.StringIO("\n".join(json.dumps(r) for r in reqs)),
        out,
    )
    resp = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [r["id"] for r in resp] == [1, 2, 3, 4]  # notification gets no reply
    assert resp[0]["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in resp[1]["result"]["tools"]}
    assert {
        "search_refs",
        "search_fulltext",
        "get_ref",
        "citations",
        "search_notes",
        "add_note",
        "kb_stats",
    } <= names
    hit = json.loads(resp[2]["result"]["content"][0]["text"])
    assert hit[0]["ref_id"] == ids["b"]
    assert resp[3]["result"]["isError"] is True  # provenance check enforced over MCP


def test_library_is_read_only(env):
    tmp, ids, lib, kb = env
    with pytest.raises(Exception):
        lib.execute("DELETE FROM refs")


def test_daemon_cycle_writes_status(env, monkeypatch):
    tmp, ids, lib, kb = env
    from mouseion.kb import daemon

    monkeypatch.setenv("MOUSEION_DB_PATH", str(tmp / "refs.db"))
    rep = daemon.cycle(
        str(tmp / "refs.db"), str(tmp / "kb.db"), rate=1e6, fetcher=lambda doi: None
    )
    assert (
        rep["fulltext"] == {"ok": 2, "missing": 1}
        and rep["citations"] == {"not_found": 3}
        and not rep["errors"]
    )
    st = daemon.read_status(str(tmp / "refs.db"))
    assert st and st["stats"]["chunks"] >= 2


def test_web_kb_requires_key_and_searches(env, monkeypatch):
    tmp, ids, lib, kb = env
    fulltext.build(lib, kb)
    monkeypatch.setenv("MOUSEION_DB_PATH", str(tmp / "refs.db"))
    monkeypatch.setenv("MOUSEION_KB_PATH", str(tmp / "kb.db"))
    monkeypatch.setenv("MOUSEION_API_KEY", "k" * 32)
    from mouseion import web

    monkeypatch.setattr(web, "_api_key_cache", None)
    c = web.app.test_client()
    assert c.get("/kb?q=lumpable").status_code == 401
    assert c.get("/api/kb/search?q=lumpable").status_code == 401
    r = c.get("/kb?q=lumpable&api_key=" + "k" * 32)
    assert r.status_code == 200 and b"strongly lumpable" in r.data and b"p. 1" in r.data
    j = c.get(
        "/api/kb/search?q=lottery", headers={"Authorization": "Bearer " + "k" * 32}
    ).get_json()
    assert j["passages"][0]["ref_id"] == ids["b"]
    bad = c.post(
        "/api/kb/notes",
        json={
            "project": "semplex",
            "claim": "x",
            "ref_id": ids["b"],
            "quote": "absent text",
        },
        headers={"X-API-Key": "k" * 32},
    )
    assert bad.status_code == 400


def test_health_reports_knowledge_base(env, monkeypatch):
    tmp, ids, lib, kb = env
    from mouseion import health
    from mouseion.kb import daemon

    daemon.cycle(
        str(tmp / "refs.db"), str(tmp / "kb.db"), rate=1e6, fetcher=lambda doi: None
    )
    rep = health.run(str(tmp / "refs.db"))
    kbc = [x for x in rep["checks"] if x["check"] == "knowledge_base"][0]
    assert kbc["ok"] and kbc["value"]["passages"] >= 2
