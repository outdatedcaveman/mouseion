"""mouseion.kb: bridge-term expansion (multi-hop), literature-based discovery, typed mathematical statements."""

from __future__ import annotations

import pytest

from mouseion.kb import expand, statements
from mouseion.kb.store import open_kb

PASSAGES = {
    "r1": [
        "Erik Hort was a baseball player. Erik Hort was born in Montebello and played for several minor league clubs.",
        "Definition 2.3 (Univalent category). A precategory is univalent when the map from identities to isomorphisms is an equivalence.",
    ],
    "r2": [
        "Montebello is a village located in Rockland, a suburb north of the city. The village has parks.",
        "Theorem 4.1. Every univalent category is equivalent to its Rezk completion. Proof. Standard.",
    ],
    "r3": [
        "Strong lumpability requires the block indicators to span an invariant subspace of the transition operator.",
        "Causal states give the minimal sufficient statistic of the past for predicting the future of a process.",
    ],
    "r4": [
        "The minimal sufficient statistic of a stochastic process is unique up to relabelling of its values.",
        "Lemma 3 (Relabelling). Any two minimal sufficient statistics agree up to a measurable bijection here.",
        "A sufficient statistic compresses the past; the sufficient statistic of a hidden process is its state.",
    ],
    "r5": ["Rockland County lies in the state of New York and borders New Jersey along the Hudson valley."],
}
FILLER = [f"Filler passage number {i} about unrelated gardening topics, tomatoes and soil." for i in range(30)]


@pytest.fixture()
def kb(tmp_path):
    kb = open_kb(str(tmp_path / "kb.db"))
    ordn = 0
    for ref, texts in list(PASSAGES.items()) + [("filler", FILLER)]:
        for i, t in enumerate(texts):
            kb.execute("INSERT INTO chunks(ref_id,page,ord,text) VALUES (?,?,?,?)", (ref, i + 1, ordn, t))
            ordn += 1
    kb.commit()
    return kb


def test_candidate_terms_keep_names_as_phrases():
    tf = expand.candidate_terms("Erik Hort was born in Montebello. The Barratt-Priddy-Quillen theorem holds.")
    assert "erik hort" in tf and "barratt-priddy-quillen" in " ".join(tf)
    assert "the" not in tf and "was" not in tf


def test_expand_reaches_second_hop_through_bridge_term(kb):
    q = "Which county is the birthplace of Erik Hort part of?"
    out = expand.expand(kb, q, B=3, K=2, n=10)  # small pool: the toy corpus is tiny
    refs = [p["ref_id"] for p in out["passages"]]
    assert "r1" in refs and "r2" in refs  # r2 shares no content word with q: reached via "montebello"
    via = {p.get("via") for p in out["passages"] if p["ref_id"] == "r2"}
    assert "montebello" in via
    assert "r5" in refs  # third hop, via "rockland"
    assert out["trace"] and any(t["term"] == "montebello" for t in out["trace"][0]["terms"])


def test_expand_with_agent_evidence_excludes_the_evidence(kb):
    first = kb.execute("SELECT id FROM chunks WHERE ref_id='r1' AND page=1").fetchone()[0]
    out = expand.expand(kb, "Erik Hort birthplace county", evidence=[first], n=10)
    assert first not in [p["chunk_id"] for p in out["passages"]]
    assert "r2" in [p["ref_id"] for p in out["passages"]]


def test_expand_with_judge(kb):
    def judge(q, cands, ctx):
        return {c["chunk_id"]: (1.0 if ("Montebello" in c["text"] or "Rockland" in c["text"]) else 0.0) for c in cands}

    out = expand.expand(kb, "Erik Hort birthplace county", judge=judge, n=5)
    assert {p["ref_id"] for p in out["passages"][:3]} >= {"r1", "r2"}


def test_closed_discovery_finds_shared_intermediate(kb):
    out = expand.bridges(kb, "causal states lumpability", "relabelling measurable bijection")
    terms = [b["term"] for b in out["bridges"]]
    assert "sufficient statistic" in terms  # a technical phrase shared by both literatures
    assert "minimal" not in terms  # ordinary English words are not reported as bridges
    assert out["cooccurring_passages"] == 0


def test_statements_extract_and_search(kb, tmp_path):
    s = statements.extract("Definition 2.3 (Univalent category). A precategory is univalent when it holds.")
    assert s == [{"kind": "definition", "label": "2.3", "name": "Univalent category",
                  "text": "A precategory is univalent when it holds."}]
    assert statements.extract("We use this theorem. It is classical.") == []  # no number, no name
    counts = statements.build(kb)
    assert counts["statements"] == 3
    assert statements.build(kb) == {"references": 0, "statements": 0}  # incremental

    class Lib:  # minimal stand-in: cite() falls back to the ref id
        def execute(self, *a):
            class R:
                def fetchone(self_inner):
                    return None
            return R()

    hits = statements.search(Lib(), kb, "univalent", kind="definition")
    assert [h["name"] for h in hits] == ["Univalent category"]
    assert statements.search(Lib(), kb, "univalent", kind="theorem")[0]["label"] == "4.1"


def test_ligatures_normalised_and_notes_survive_reextraction(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    from mouseion.db import RefDatabase
    from mouseion.models import Reference
    from mouseion.kb import fulltext, search
    from mouseion.kb.store import open_library

    pdf = tmp_path / "lig.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((50, 72), "A deﬁnition of the diﬀerential is given here in full.", fontsize=10)
    doc.save(str(pdf))
    doc.close()
    with RefDatabase(path=str(tmp_path / "refs.db")) as db:
        rid = db.upsert(Reference(title="Ligatures", year=2020))
        db.update_integration_ids(rid, pdf_local=str(pdf))
    lib, kb = open_library(str(tmp_path / "refs.db")), open_kb(str(tmp_path / "kb.db"))
    # PyMuPDF returns LaTeX ligatures as single characters; base-14 test fonts cannot draw them, so simulate it
    monkeypatch.setattr(fulltext, "extract", lambda path: [(1, "A de\ufb01nition of the di\ufb00erential is given here in full.")])
    assert fulltext.build(lib, kb) == {"ok": 1}
    assert search.search_passages(lib, kb, "definition differential")  # plain query finds ligature text
    note = search.add_note(lib, kb, "p", "c", rid, quote="definition of the differential")
    old = note["chunk_id"]
    # simulate a passage indexed by the old extractor, then re-extract
    kb.execute("UPDATE pdf_versions SET version=1 WHERE ref_id=?", (rid,))
    kb.commit()
    assert fulltext.build(lib, kb) == {"ok": 1}
    new = kb.execute("SELECT chunk_id FROM notes WHERE id=?", (note["id"],)).fetchone()[0]
    assert new != old and kb.execute("SELECT 1 FROM chunks WHERE id=?", (new,)).fetchone()
    assert kb.execute("SELECT text FROM chunks_archive WHERE id=?", (old,)).fetchone()  # the old passage is kept
    assert fulltext.build(lib, kb) == {"unchanged": 1}


def _lib_with_pdfs(tmp_path, n=2):
    from mouseion.db import RefDatabase
    from mouseion.models import Reference
    from mouseion.kb.store import open_library

    ids = []
    with RefDatabase(path=str(tmp_path / "refs.db")) as db:
        for i in range(n):
            p = tmp_path / f"p{i}.pdf"
            p.write_bytes(b"%PDF-1.4 fake")
            rid = db.upsert(Reference(title=f"Paper {i}", year=2020))
            db.update_integration_ids(rid, pdf_local=str(p))
            ids.append(rid)
    return ids, open_library(str(tmp_path / "refs.db")), open_kb(str(tmp_path / "kb.db"))


def _slow_or_ok(path):
    import time

    if path.name == "p0.pdf":
        time.sleep(30)
    return [(1, "Some page text long enough to make a passage about lumpability and causal states here.")]


def test_hung_pdf_times_out_and_is_not_retried(tmp_path, monkeypatch):
    from mouseion.kb import fulltext

    ids, lib, kb = _lib_with_pdfs(tmp_path)
    monkeypatch.setattr(fulltext, "extract", _slow_or_ok)
    counts = fulltext.build(lib, kb, timeout=1.0)
    assert counts == {"error": 1, "ok": 1}  # the hung file did not stall the run
    st = dict(kb.execute("SELECT ref_id, status FROM pdf_files").fetchall())
    assert st[ids[0]] == "error:TimeoutError" and st[ids[1]] == "ok"
    assert fulltext.build(lib, kb, timeout=1.0) == {"skipped_error": 1, "unchanged": 1}  # no re-stall
    monkeypatch.setattr(fulltext, "extract", lambda p: [(1, "Recovered text of the formerly hung file, long enough to index.")])
    assert fulltext.build(lib, kb, timeout=None, retry_errors=True) == {"ok": 1, "unchanged": 1}


def _probe_lock(path):
    import sqlite3

    con = sqlite3.connect(str(path.parent / "kb.db"), timeout=0)
    con.execute("BEGIN IMMEDIATE")  # raises 'database is locked' if the indexer holds the write lock
    con.rollback()
    con.close()
    return [(1, "Text extracted while another writer could take the lock, long enough to index.")]


def test_parsing_does_not_hold_the_write_lock(tmp_path, monkeypatch):
    from mouseion.kb import fulltext

    ids, lib, kb = _lib_with_pdfs(tmp_path)
    monkeypatch.setattr(fulltext, "extract", _probe_lock)
    assert fulltext.build(lib, kb, timeout=10.0) == {"ok": 2}


def test_missing_pymupdf_changes_nothing(tmp_path, monkeypatch):
    from mouseion.kb import fulltext

    ids, lib, kb = _lib_with_pdfs(tmp_path)
    monkeypatch.setattr(fulltext, "pymupdf_available", lambda: False)
    out = fulltext.build(lib, kb)
    assert "PyMuPDF" in out["error"] and "nothing was changed" in out["error"]
    assert kb.execute("SELECT count(*) FROM pdf_files").fetchone()[0] == 0


def test_environment_errors_are_retried(tmp_path, monkeypatch):
    from mouseion.kb import fulltext

    ids, lib, kb = _lib_with_pdfs(tmp_path, 1)
    p = tmp_path / "p0.pdf"
    kb.execute("INSERT INTO pdf_files(ref_id,path,size,mtime,status) VALUES (?,?,?,?,?)",
               (ids[0], str(p), p.stat().st_size, p.stat().st_mtime, "error:ModuleNotFoundError"))
    kb.commit()
    monkeypatch.setattr(fulltext, "extract", lambda q: [(1, "Now the parser is installed and this long text is indexed fine.")])
    assert fulltext.build(lib, kb, timeout=None) == {"ok": 1}
