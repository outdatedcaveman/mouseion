"""The search index is rowid-coupled to refs: rebuilt rows must carry refs.rowid,
or every later update (trigger: DELETE ... WHERE rowid = old.rowid) leaves a stale
row behind and the ref shows up twice (2026-09-25: rebuild_fts and the dedup
engine inserted without rowid)."""
import sqlite3

from mouseion.db import RefDatabase
from mouseion.models import Author, Reference


def test_rebuild_then_update_keeps_one_row_per_ref(tmp_path):
    path = tmp_path / "refs.db"
    with RefDatabase(path=path) as db:
        rid = db.upsert(Reference(title="A study of things", year=2001, authors=[Author(family="Doe")]))
        db.upsert(Reference(title="Another study", year=2002, authors=[Author(family="Roe")]))
    RefDatabase(path=path).rebuild_fts()
    c = sqlite3.connect(path)
    c.execute("UPDATE refs SET title = 'A study of things, revised' WHERE id = ?", (rid,))
    c.commit()
    per_ref = dict(c.execute("SELECT ref_id, COUNT(*) FROM refs_fts GROUP BY ref_id").fetchall())
    assert per_ref == {r[0]: 1 for r in c.execute("SELECT id FROM refs")}
    assert c.execute("SELECT ref_id FROM refs_fts WHERE refs_fts MATCH 'revised'").fetchone()[0] == rid


def test_null_ids_rejected_at_the_door(tmp_path):
    import pytest
    path = tmp_path / "refs.db"
    with RefDatabase(path=path) as db:
        db.upsert(Reference(title="Something", year=2001, authors=[Author(family="Doe")]))
    c = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO refs (id, title) VALUES (NULL, 'x')")
    c.execute("INSERT INTO enrich_queue (ref_id) VALUES (NULL)")          # silently ignored
    assert c.execute("SELECT COUNT(*) FROM enrich_queue WHERE ref_id IS NULL").fetchone()[0] == 0
