import pytest
import sqlite3
from mouseion.db import RefDatabase
from mouseion.models import Author, Reference, RefType
from mouseion.enrich_daemon import _is_junk_title

@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_enrich.db"
    db = RefDatabase(path=db_file)
    db.open()
    yield db
    db.close()

def test_enqueue_refs_strategy_level_computation(test_db):
    # 1. Has DOI
    r1 = Reference(title="Some Long Enough Title", doi="10.1038/nature12373")
    # 2. Has URL but no DOI
    r2 = Reference(title="Some Long Enough Title", url="https://example.com/paper")
    # 3. Title + Meta (year) but no DOI/URL
    r3 = Reference(title="Some Long Enough Title", year=2024)
    # 4. Title only
    r4 = Reference(title="Some Long Enough Title")
    # 5. Junk/minimal title
    r5 = Reference(title="Junk")

    id1 = test_db.upsert(r1)
    id2 = test_db.upsert(r2)
    id3 = test_db.upsert(r3)
    id4 = test_db.upsert(r4)
    id5 = test_db.upsert(r5)

    test_db.enqueue_refs([id1, id2, id3, id4, id5])

    with test_db._db() as conn:
        rows = conn.execute("SELECT ref_id, strategy_level FROM enrich_queue").fetchall()
        strat_map = {row["ref_id"]: row["strategy_level"] for row in rows}

        assert strat_map[id1] == 0
        assert strat_map[id2] == 1
        assert strat_map[id3] == 2
        assert strat_map[id4] == 3
        assert strat_map[id5] == 4

def test_resurrection_updates_strategy_and_attempts(test_db):
    r1 = Reference(title="Some Long Enough Title", doi="10.1038/nature12373")
    id1 = test_db.upsert(r1)

    test_db.enqueue_refs([id1])

    # Mark as done
    with test_db._db() as conn:
        conn.execute("UPDATE enrich_queue SET status='done', strategy_level=4, attempts=3 WHERE ref_id=?", (id1,))

    # Resurrect
    test_db.enqueue_refs([id1])

    with test_db._db() as conn:
        row = conn.execute("SELECT status, strategy_level, attempts FROM enrich_queue WHERE ref_id=?", (id1,)).fetchone()
        assert row["status"] == "pending"
        assert row["strategy_level"] == 0  # Re-calculated dynamically from DOI
        assert row["attempts"] == 0        # Reset to 0

def test_complete_enrich_caps_level4_attempts(test_db):
    r1 = Reference(title="Some Title")
    id1 = test_db.upsert(r1)
    test_db.enqueue_refs([id1])

    # Move to strategy_level 4 and simulate 7 attempts
    with test_db._db() as conn:
        conn.execute("UPDATE enrich_queue SET strategy_level=4, attempts=7, status='active' WHERE ref_id=?", (id1,))

    # Complete with no improvement
    test_db.complete_enrich(id1, new_completeness=0.1, error="no match")

    with test_db._db() as conn:
        row = conn.execute("SELECT status, attempts, last_error FROM enrich_queue WHERE ref_id=?", (id1,)).fetchone()
        assert row["status"] == "failed"
        assert row["attempts"] == 8
        assert row["last_error"] == "no match"

def test_complete_enrich_under_cap_requeues(test_db):
    r1 = Reference(title="Some Title")
    id1 = test_db.upsert(r1)
    test_db.enqueue_refs([id1])

    # Move to strategy_level 4 and simulate 3 attempts
    with test_db._db() as conn:
        conn.execute("UPDATE enrich_queue SET strategy_level=4, attempts=3, status='active' WHERE ref_id=?", (id1,))

    # Complete with no improvement
    test_db.complete_enrich(id1, new_completeness=0.1, error="no match")

    with test_db._db() as conn:
        row = conn.execute("SELECT status, attempts, strategy_level FROM enrich_queue WHERE ref_id=?", (id1,)).fetchone()
        assert row["status"] == "pending"
        assert row["attempts"] == 4
        assert row["strategy_level"] == 4

def test_metadata_aware_junk_title():
    # 1. No metadata reference provided
    assert _is_junk_title("physics", None) is True
    assert _is_junk_title("regret", None) is True
    assert _is_junk_title("relay station", None) is True

    # 2. Reference WITH metadata (year or authors)
    r_with_year = Reference(title="physics", year=2024)
    r_with_author = Reference(title="regret", authors=[Author(family="Smith")])
    r_with_both = Reference(title="relay station", year=2020, authors=[Author(family="Jones")])

    assert _is_junk_title("physics", r_with_year) is False
    assert _is_junk_title("regret", r_with_author) is False
    assert _is_junk_title("relay station", r_with_both) is False

    # 3. Extremely short or single word but with no metadata
    assert _is_junk_title("short", None) is True
    assert _is_junk_title("shrt t", None) is True  # length 6, no metadata
    assert _is_junk_title("short title", None) is False  # length 11, has space, not a generic word
