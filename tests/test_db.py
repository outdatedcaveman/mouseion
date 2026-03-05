"""Tests for the SQLite reference database (db.py)."""

import pytest
import tempfile
from pathlib import Path

from zoterpile.db import RefDatabase, _ref_id, _fts_query
from zoterpile.models import Author, Reference, RefType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ref(
    title: str = "Test Paper",
    doi: str | None = None,
    arxiv_id: str | None = None,
    pmid: str | None = None,
    year: int | None = 2024,
    abstract: str | None = None,
    authors: list[Author] | None = None,
    ref_type: RefType = RefType.JOURNAL,
    open_access: bool | None = None,
) -> Reference:
    return Reference(
        title=title,
        doi=doi,
        arxiv_id=arxiv_id,
        pmid=pmid,
        year=year,
        abstract=abstract,
        authors=authors or [Author(family="Smith", given="J.")],
        ref_type=ref_type,
        open_access=open_access,
    )


@pytest.fixture
def tmp_db(tmp_path):
    """A RefDatabase backed by a temporary file."""
    db_file = tmp_path / "test.db"
    db = RefDatabase(path=db_file)
    db.open()
    yield db
    db.close()


# ---------------------------------------------------------------------------
# _ref_id — deterministic ID generation
# ---------------------------------------------------------------------------

class TestRefId:
    def test_doi_keyed(self):
        r = _make_ref(doi="10.1038/nature12373")
        assert _ref_id(r) == _ref_id(_make_ref(doi="10.1038/nature12373"))

    def test_arxiv_keyed(self):
        r = _make_ref(arxiv_id="2310.00123")
        assert _ref_id(r) == _ref_id(_make_ref(arxiv_id="2310.00123"))

    def test_doi_case_insensitive(self):
        a = _make_ref(doi="10.1038/Nature12373")
        b = _make_ref(doi="10.1038/nature12373")
        assert _ref_id(a) == _ref_id(b)

    def test_different_dois_different_ids(self):
        a = _make_ref(doi="10.1038/abc")
        b = _make_ref(doi="10.1038/xyz")
        assert _ref_id(a) != _ref_id(b)

    def test_id_is_24_hex_chars(self):
        r = _make_ref(doi="10.1038/nature12373")
        rid = _ref_id(r)
        assert len(rid) == 24
        assert all(c in "0123456789abcdef" for c in rid)


# ---------------------------------------------------------------------------
# upsert / get
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_insert_and_retrieve(self, tmp_db):
        ref = _make_ref(doi="10.1038/nature12373", title="Test Paper")
        ref_id = tmp_db.upsert(ref)
        retrieved = tmp_db.get(ref_id)
        assert retrieved is not None
        assert retrieved.doi == "10.1038/nature12373"
        assert retrieved.title == "Test Paper"

    def test_upsert_updates_title(self, tmp_db):
        ref = _make_ref(doi="10.1038/nature12373", title="Old Title")
        tmp_db.upsert(ref)
        ref2 = _make_ref(doi="10.1038/nature12373", title="New Title")
        tmp_db.upsert(ref2)
        retrieved = tmp_db.get_by_doi("10.1038/nature12373")
        assert retrieved.title == "New Title"

    def test_upsert_returns_id(self, tmp_db):
        ref = _make_ref(doi="10.1038/abc")
        ref_id = tmp_db.upsert(ref)
        assert isinstance(ref_id, str)
        assert len(ref_id) == 24

    def test_count_increases(self, tmp_db):
        assert tmp_db.count() == 0
        tmp_db.upsert(_make_ref(doi="10.1000/a"))
        assert tmp_db.count() == 1
        tmp_db.upsert(_make_ref(doi="10.1000/b"))
        assert tmp_db.count() == 2

    def test_upsert_same_ref_no_duplicate(self, tmp_db):
        ref = _make_ref(doi="10.1000/dup")
        tmp_db.upsert(ref)
        tmp_db.upsert(ref)
        assert tmp_db.count() == 1

    def test_get_by_doi(self, tmp_db):
        ref = _make_ref(doi="10.1038/test999")
        tmp_db.upsert(ref)
        retrieved = tmp_db.get_by_doi("10.1038/test999")
        assert retrieved is not None
        assert retrieved.doi == "10.1038/test999"

    def test_get_nonexistent_returns_none(self, tmp_db):
        assert tmp_db.get("nonexistent_id") is None

    def test_get_by_doi_nonexistent_returns_none(self, tmp_db):
        assert tmp_db.get_by_doi("10.9999/nope") is None

    def test_ref_type_roundtrip(self, tmp_db):
        ref = _make_ref(doi="10.1000/preprint", ref_type=RefType.PREPRINT)
        rid = tmp_db.upsert(ref)
        retrieved = tmp_db.get(rid)
        assert retrieved.ref_type == RefType.PREPRINT

    def test_open_access_roundtrip(self, tmp_db):
        ref = _make_ref(doi="10.1000/oa", open_access=True)
        rid = tmp_db.upsert(ref)
        retrieved = tmp_db.get(rid)
        assert retrieved.open_access is True


# ---------------------------------------------------------------------------
# upsert_many
# ---------------------------------------------------------------------------

class TestUpsertMany:
    def test_bulk_insert(self, tmp_db):
        refs = [_make_ref(doi=f"10.1000/{i}") for i in range(5)]
        ids = tmp_db.upsert_many(refs)
        assert len(ids) == 5
        assert tmp_db.count() == 5

    def test_bulk_with_tags(self, tmp_db):
        refs = [_make_ref(doi="10.1000/a"), _make_ref(doi="10.1000/b")]
        ids = tmp_db.upsert_many(refs, tags_per_ref=[["ml", "ai"], ["physics"]])
        assert tmp_db.get_tags(ids[0]) == ["ai", "ml"]
        assert tmp_db.get_tags(ids[1]) == ["physics"]


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

class TestDelete:
    def test_delete_removes_ref(self, tmp_db):
        ref = _make_ref(doi="10.1000/del")
        rid = tmp_db.upsert(ref)
        assert tmp_db.count() == 1
        tmp_db.delete(rid)
        assert tmp_db.count() == 0
        assert tmp_db.get(rid) is None


# ---------------------------------------------------------------------------
# Tag management
# ---------------------------------------------------------------------------

class TestTags:
    def test_add_tags(self, tmp_db):
        ref = _make_ref(doi="10.1000/tag")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["machine-learning", "nlp"])
        tags = tmp_db.get_tags(rid)
        assert "machine-learning" in tags
        assert "nlp" in tags

    def test_tags_alphabetically_sorted(self, tmp_db):
        ref = _make_ref(doi="10.1000/sort")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["zebra", "apple", "mango"])
        tags = tmp_db.get_tags(rid)
        assert tags == sorted(tags)

    def test_add_tags_idempotent(self, tmp_db):
        ref = _make_ref(doi="10.1000/idem")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["ml"])
        tmp_db.add_tags(rid, ["ml"])
        assert tmp_db.get_tags(rid).count("ml") == 1

    def test_remove_tag(self, tmp_db):
        ref = _make_ref(doi="10.1000/rmtag")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["keep", "remove"])
        tmp_db.remove_tag(rid, "remove")
        tags = tmp_db.get_tags(rid)
        assert "remove" not in tags
        assert "keep" in tags

    def test_upsert_with_tags(self, tmp_db):
        ref = _make_ref(doi="10.1000/withtags")
        rid = tmp_db.upsert(ref, tags=["journal", "open-access"])
        tags = tmp_db.get_tags(rid)
        assert "journal" in tags
        assert "open-access" in tags

    def test_all_tags(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.1000/a1"), tags=["ml"])
        r2 = tmp_db.upsert(_make_ref(doi="10.1000/a2"), tags=["ml", "nlp"])
        all_tags = tmp_db.all_tags()
        names = [t["name"] for t in all_tags]
        assert "ml" in names
        assert "nlp" in names


# ---------------------------------------------------------------------------
# Search (FTS5)
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_finds_title_match(self, tmp_db):
        tmp_db.upsert(_make_ref(
            doi="10.1000/ml",
            title="Machine Learning Techniques",
            abstract="A review of ML methods."
        ))
        tmp_db.upsert(_make_ref(
            doi="10.1000/bio",
            title="Genomic Sequencing Methods",
            abstract="DNA analysis."
        ))
        results = tmp_db.search("machine learning")
        assert len(results) >= 1
        assert results[0][0].doi == "10.1000/ml"

    def test_search_finds_abstract_match(self, tmp_db):
        tmp_db.upsert(_make_ref(
            doi="10.1000/abstr",
            title="Irrelevant Title",
            abstract="Contains quantum computing discussion."
        ))
        results = tmp_db.search("quantum")
        assert len(results) >= 1

    def test_search_empty_query_returns_all(self, tmp_db):
        tmp_db.upsert(_make_ref(doi="10.1000/x1"))
        tmp_db.upsert(_make_ref(doi="10.1000/x2"))
        results = tmp_db.search("")
        assert len(results) == 2

    def test_search_year_filter(self, tmp_db):
        tmp_db.upsert(_make_ref(doi="10.1000/y2020", year=2020, title="Old Paper"))
        tmp_db.upsert(_make_ref(doi="10.1000/y2023", year=2023, title="New Paper"))
        results = tmp_db.search("", year_from=2022)
        assert all(r.year >= 2022 for r, _ in results)

    def test_search_type_filter(self, tmp_db):
        tmp_db.upsert(_make_ref(doi="10.1000/j1", ref_type=RefType.JOURNAL))
        tmp_db.upsert(_make_ref(doi="10.1000/p1", ref_type=RefType.PREPRINT))
        results = tmp_db.search("", ref_type="preprint")
        assert all(r.ref_type == RefType.PREPRINT for r, _ in results)

    def test_search_oa_filter(self, tmp_db):
        tmp_db.upsert(_make_ref(doi="10.1000/oa1", open_access=True))
        tmp_db.upsert(_make_ref(doi="10.1000/closed1", open_access=False))
        results = tmp_db.search("", oa_only=True)
        assert all(r.open_access is True for r, _ in results)

    def test_search_no_match_returns_empty(self, tmp_db):
        tmp_db.upsert(_make_ref(doi="10.1000/x", title="Python Programming"))
        results = tmp_db.search("zyxwvutsrqponm")
        assert results == []

    def test_search_limit(self, tmp_db):
        for i in range(10):
            tmp_db.upsert(_make_ref(doi=f"10.1000/lim{i}"))
        results = tmp_db.search("", limit=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------

class TestListAll:
    def test_list_all(self, tmp_db):
        tmp_db.upsert(_make_ref(doi="10.1000/la1"))
        tmp_db.upsert(_make_ref(doi="10.1000/la2"))
        refs = tmp_db.list_all()
        assert len(refs) == 2
        assert all(isinstance(r, Reference) for r in refs)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_stats_empty(self, tmp_db):
        s = tmp_db.stats()
        assert s["total"] == 0
        assert s["avg_completeness"] == 0.0

    def test_stats_counts(self, tmp_db):
        tmp_db.upsert(_make_ref(doi="10.1000/j1", ref_type=RefType.JOURNAL, open_access=True))
        tmp_db.upsert(_make_ref(doi="10.1000/j2", ref_type=RefType.JOURNAL, open_access=False))
        tmp_db.upsert(_make_ref(doi="10.1000/p1", ref_type=RefType.PREPRINT))
        s = tmp_db.stats()
        assert s["total"] == 3
        assert s["by_type"]["journal-article"] == 2
        assert s["open_access_count"] == 1


# ---------------------------------------------------------------------------
# low_completeness
# ---------------------------------------------------------------------------

class TestLowCompleteness:
    def test_low_completeness_filter(self, tmp_db):
        # Minimal ref → low completeness
        incomplete = Reference(title="Incomplete", doi="10.1000/inc")
        # Full ref → higher completeness
        complete = Reference(
            title="Full Reference",
            doi="10.1000/full",
            abstract="A" * 200,
            authors=[Author(family="Doe", given="J.")],
            year=2023,
            journal="Nature",
            volume="1",
            pages="1-10",
        )
        tmp_db.upsert(incomplete)
        tmp_db.upsert(complete)
        low = tmp_db.low_completeness(threshold=0.5)
        ids = [r.doi for r in low]
        assert "10.1000/inc" in ids

    def test_low_completeness_limit(self, tmp_db):
        for i in range(5):
            tmp_db.upsert(Reference(title=f"Paper {i}", doi=f"10.1000/lc{i}"))
        low = tmp_db.low_completeness(threshold=1.0, limit=3)
        assert len(low) <= 3


# ---------------------------------------------------------------------------
# update_integration_ids
# ---------------------------------------------------------------------------

class TestUpdateIntegrationIds:
    def test_update_notion_id(self, tmp_db):
        ref = _make_ref(doi="10.1000/notion")
        rid = tmp_db.upsert(ref)
        tmp_db.update_integration_ids(rid, notion_page_id="abc-123")
        extra = tmp_db.get_extra(rid)
        assert extra["notion_page_id"] == "abc-123"

    def test_update_zotero_key(self, tmp_db):
        ref = _make_ref(doi="10.1000/zotero")
        rid = tmp_db.upsert(ref)
        tmp_db.update_integration_ids(rid, zotero_item_key="ZKEY001")
        extra = tmp_db.get_extra(rid)
        assert extra["zotero_item_key"] == "ZKEY001"

    def test_update_pdf_path(self, tmp_db):
        ref = _make_ref(doi="10.1000/pdf")
        rid = tmp_db.upsert(ref)
        tmp_db.update_integration_ids(rid, pdf_local="/path/to/file.pdf")
        extra = tmp_db.get_extra(rid)
        assert extra["pdf_local"] == "/path/to/file.pdf"


# ---------------------------------------------------------------------------
# _fts_query helper
# ---------------------------------------------------------------------------

class TestFtsQuery:
    def test_single_word(self):
        q = _fts_query("machine")
        assert '"machine"*' in q

    def test_multi_word(self):
        q = _fts_query("machine learning")
        assert '"machine"*' in q
        assert '"learning"*' in q

    def test_empty_query(self):
        q = _fts_query("")
        assert q == ""
