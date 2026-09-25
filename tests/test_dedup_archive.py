"""maintenance_dedup: merges are archived (never destroyed) and vetoed when titles disagree.

2026-09-25 preview on the live library: the safe-mode ISBN rule would have fused
books with their own chapters (same ISBN) -- 15,394 rows -- and deleted the losers.
"""
import sqlite3

from mouseion.db import RefDatabase
from mouseion.maintenance_dedup import run_dedup_all
from mouseion.models import Author, Reference, RefType


def _add(db, title, ref_type=RefType.JOURNAL, year=2001, family="Smith"):
    return db.upsert(Reference(title=title, year=year, ref_type=ref_type,
                               authors=[Author(family=family, given="A.")]))


def test_dedup_archives_and_vetoes(tmp_path):
    path = tmp_path / "refs.db"
    with RefDatabase(path=path) as db:
        same_a = _add(db, "Quantum mechanics from self-interaction", year=1985, family="Hestenes")
        same_b = _add(db, "Quantum Mechanics from Self Interaction", year=1985, family="Hestenes")
        book = _add(db, "Scientific Realism", RefType.BOOK, 1984, "Leplin")
        chapter = _add(db, "The current status of scientific realism", RefType.BOOK_CHAPTER, 1984, "Boyd")
        wrong_a = _add(db, "Logic as Relation Lore", year=1893, family="Russell")
        wrong_b = _add(db, "A review of something else entirely", year=1893, family="Other")
        marked = _add(db, "Old flagged duplicate", year=1999, family="Doe")

    c = sqlite3.connect(path)
    c.execute("UPDATE refs SET doi='10.1007/BF00738738' WHERE id=?", (same_a,))
    c.execute("UPDATE refs SET doi='10.1007/bf00738738' WHERE id=?", (same_b,))
    c.execute("UPDATE refs SET isbn='9780520337442' WHERE id IN (?,?)", (book, chapter))
    c.execute("UPDATE refs SET doi='10.5840/monist18933240' WHERE id=?", (wrong_a,))
    c.execute("UPDATE refs SET doi='10.5840/MONIST18933240' WHERE id=?", (wrong_b,))   # UNIQUE is case-sensitive
    c.execute("UPDATE refs SET status='duplicate' WHERE id=?", (marked,))
    c.commit()
    c.close()

    report = run_dedup_all(db_path=path, mode="safe", restore_point=False,
                           report_path=tmp_path / "report.json")

    c = sqlite3.connect(path)
    live = {r[0] for r in c.execute("SELECT id FROM refs")}
    archived = dict(c.execute("SELECT id, duplicate_of FROM refs_duplicates").fetchall())
    # the true duplicate merged; the loser is archived with a pointer, not destroyed
    assert len({same_a, same_b} & live) == 1
    loser = ({same_a, same_b} - live).pop()
    assert archived[loser] in {same_a, same_b}
    assert c.execute("SELECT COUNT(*) FROM refs_dedup_keep_bak").fetchone()[0] >= 1
    # book and its chapter share an ISBN but are different works
    assert {book, chapter} <= live
    # same DOI, unrelated titles: vetoed, both kept
    assert {wrong_a, wrong_b} <= live
    # an already-flagged duplicate leaves the live table for the archive
    assert marked not in live and marked in archived
    assert any(p.get("vetoed_title_mismatch") for p in report["passes"])
