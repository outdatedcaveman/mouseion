"""mouseion.kb: notes backfilled from project bibliographies, the retrieval evaluation, OCR of scanned PDFs."""

from __future__ import annotations

import json

import pytest

fitz = pytest.importorskip("fitz")

from mouseion.db import RefDatabase
from mouseion.models import Author, Reference
from mouseion.kb import backfill, evaluate, fulltext, search
from mouseion.kb.store import open_kb, open_library

BIB = r"""
% comment line
@article{hedberg1998coherence,
  author  = {Hedberg, Michael},
  title   = {A Coherence Theorem for {Martin-Lof}'s Type Theory},
  year    = {1998},
  doi     = {10.1017/S0956796898003153},
  note    = {Decidable equality implies sethood; used in the C3 finite-set cover}
}
@misc{ahrens2021univalence,
  title         = {The Univalence Principle},
  eprint        = {2102.06275},
  archiveprefix = {arXiv},
  note          = "Structure identity for higher structures"
}
@book{nowhere2000,
  title = {A Book The Library Does Not Have},
  note  = {never matched}
}
@book{nonote2001, title = {Grokking Without A Note Field At All}}
"""

TABLE = """
| Key | Reference | Level | Used in | Used for |
|---|---|---|---|---|
| Liu22 | Z. Liu, E. Michaud, M. Tegmark, "Omnigrok: Grokking Beyond Algorithmic Data", arXiv:2210.01117 | INDEX | S10b | Grokking from large init plus weight decay |
| Pawlak82 | Z. Pawlak, "Rough sets", 1982 | MEMORY | S01 | Rough sets |
| Hed | M. Hedberg, "A coherence theorem for Martin-Lof's type theory", JFP | ABSTRACT | T1 | Hedberg's theorem |

Text after the table.
"""


def _pdf(path, pages):
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(fitz.Rect(50, 50, 550, 800), text, fontsize=10)
    doc.save(str(path))
    doc.close()


@pytest.fixture()
def env(tmp_path):
    refs = {
        "hed": Reference(title="A coherence theorem for Martin-Löf's type theory", doi="10.1017/s0956796898003153",
                         year=1998, authors=[Author(family="Hedberg")]),
        "uni": Reference(title="The Univalence Principle", arxiv_id="2102.06275v3", year=2021,
                         authors=[Author(family="Ahrens")]),
        "omni": Reference(title="Omnigrok: Grokking Beyond Algorithmic Data", year=2022,
                          authors=[Author(family="Liu")], abstract="Grokking on non-algorithmic datasets."),
        "scan": Reference(title="A scanned monograph on lumpability", year=1960, authors=[Author(family="Kemeny")]),
    }
    with RefDatabase(path=str(tmp_path / "refs.db")) as db:
        ids = {k: db.upsert(r) for k, r in refs.items()}
    _pdf(tmp_path / "hed.pdf", ["Decidable equality implies that a type is a set. This is Hedberg's theorem."])
    _pdf(tmp_path / "scan.pdf", ["", ""])  # no text layer
    with RefDatabase(path=str(tmp_path / "refs.db")) as db:
        db.update_integration_ids(ids["hed"], pdf_local=str(tmp_path / "hed.pdf"))
        db.update_integration_ids(ids["scan"], pdf_local=str(tmp_path / "scan.pdf"))
    lib, kb = open_library(str(tmp_path / "refs.db")), open_kb(str(tmp_path / "kb.db"))
    return tmp_path, ids, lib, kb


def test_parsers():
    b = {r["key"]: r for r in backfill.parse_bibtex(BIB)}
    assert b["hedberg1998coherence"]["doi"] == "10.1017/s0956796898003153"
    assert b["ahrens2021univalence"]["arxiv"] == "2102.06275"
    assert b["ahrens2021univalence"]["use"] == "Structure identity for higher structures"
    assert b["nonote2001"]["use"] == ""
    t = {r["key"]: r for r in backfill.parse_reference_table(TABLE)}
    assert set(t) == {"Liu22", "Pawlak82", "Hed"}
    assert t["Liu22"]["arxiv"] == "2210.01117" and t["Liu22"]["title"].startswith("Omnigrok")
    assert t["Pawlak82"]["level"] == "MEMORY"


def test_backfill_matches_and_is_idempotent(env):
    tmp, ids, lib, kb = env
    rep = backfill.backfill(lib, kb, "synesism", backfill.parse_bibtex(BIB))
    assert rep["added"] == 2 and rep["no_use"] == 1
    assert rep["matched_by"] == {"doi": 1, "arxiv": 1}
    assert [u["key"] for u in rep["unmatched"]] == ["nowhere2000"]
    assert backfill.backfill(lib, kb, "synesism", backfill.parse_bibtex(BIB))["existing"] == 2

    rep = backfill.backfill(lib, kb, "semplex", backfill.parse_reference_table(TABLE))
    assert rep["skipped_memory"] == 1 and rep["added"] == 2
    assert rep["matched_by"] == {"title": 2}  # Omnigrok has no arXiv id in the library; Hedberg by title
    notes = search.search_notes(lib, kb, "weight decay")
    assert notes and notes[0]["ref_id"] == ids["omni"]
    row = kb.execute("SELECT kind, quote, author FROM notes WHERE ref_id=?", (ids["omni"],)).fetchone()
    assert (row["kind"], row["quote"], row["author"]) == ("project-use", None, "backfill")

    dry = backfill.backfill(lib, kb, "other", backfill.parse_bibtex(BIB), dry_run=True)
    assert dry["added"] == 2
    assert kb.execute("SELECT count(*) FROM notes WHERE project='other'").fetchone()[0] == 0


def test_ocr_empty_indexes_scanned_pdfs(env):
    tmp, ids, lib, kb = env
    assert fulltext.build(lib, kb) == {"ok": 1, "empty": 1}
    assert fulltext.build(lib, kb) == {"unchanged": 2}  # an empty file is not re-extracted every cycle
    pages = ["", "The block indicators span an invariant subspace; this is strong lumpability."]
    assert fulltext.ocr_empty(lib, kb, ocr=lambda p: pages) == {"ocr": 1}
    hit = search.search_passages(lib, kb, "strong lumpability invariant subspace")[0]
    assert hit["ref_id"] == ids["scan"] and hit["page"] == 2
    assert fulltext.build(lib, kb) == {"unchanged": 2}  # OCR text survives the next build
    assert fulltext.ocr_empty(lib, kb, ocr=lambda p: 1 / 0) == {}  # nothing left to OCR


def test_ocr_empty_reports_when_engine_missing(env, monkeypatch):
    tmp, ids, lib, kb = env
    fulltext.build(lib, kb)
    from mouseion import ocr

    monkeypatch.setattr(ocr, "available", lambda: False)
    assert fulltext.ocr_empty(lib, kb) == {"unavailable": 1}
    assert fulltext.ocr_empty(lib, kb, ocr=lambda p: [""]) == {"empty": 1}  # blank OCR keeps 'empty'


def test_eval_classifies_misses(env, tmp_path):
    _, ids, lib, kb = env
    fulltext.build(lib, kb)
    qs = tmp_path / "q.jsonl"
    qs.write_text(
        "# comment\n"
        + "\n".join(json.dumps(x) for x in [
            {"q": "decidable equality implies set", "expect": [{"doi": "10.1017/S0956796898003153"}]},
            {"q": "grokking weight decay", "expect": [{"arxiv": "2210.01117"}, {"title": "Omnigrok: Grokking Beyond Algorithmic Data"}]},
            {"q": "anything", "expect": [{"doi": "10.9/none"}]},
            {"q": "homotopy coherent nerve", "expect": [{"doi": "10.1017/S0956796898003153"}]},
        ]),
        encoding="utf-8",
    )
    out = evaluate.run(lib, kb, evaluate.load(str(qs)))
    why = [r["why"] for r in out["rows"]]
    assert why == ["hit", "not_indexed", "not_in_library", "retrieval_miss"]
    assert out["rows"][1]["ref_rank"] == 1  # found by title/abstract even though it has no PDF
    s = out["summary"]
    assert s["questions"] == 4 and s["passages"]["hit@1"] == 0.25
    assert s["misses"] == {"retrieval_miss": 1, "not_indexed": 1, "not_in_library": 1}
    assert s["verdict"].startswith("do not add dense retrieval")
