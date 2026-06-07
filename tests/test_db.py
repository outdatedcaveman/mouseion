"""Tests for the SQLite reference database (db.py)."""

import pytest
import tempfile
from pathlib import Path

from mouseion.db import RefDatabase, _ref_id, _fts_query
from mouseion.models import Author, Reference, RefType


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


# ---------------------------------------------------------------------------
# Collection CRUD
# ---------------------------------------------------------------------------

class TestCollections:
    def test_create_and_list(self, tmp_db):
        cid = tmp_db.create_collection("My Papers")
        assert isinstance(cid, int)
        colls = tmp_db.get_collections()
        names = [c["name"] for c in colls]
        assert "My Papers" in names

    def test_create_strips_whitespace(self, tmp_db):
        cid = tmp_db.create_collection("  Trimmed  ")
        colls = tmp_db.get_collections()
        c = next(c for c in colls if c["id"] == cid)
        assert c["name"] == "Trimmed"

    def test_rename(self, tmp_db):
        cid = tmp_db.create_collection("Old Name")
        tmp_db.rename_collection(cid, "New Name")
        colls = tmp_db.get_collections()
        c = next(c for c in colls if c["id"] == cid)
        assert c["name"] == "New Name"

    def test_delete(self, tmp_db):
        cid = tmp_db.create_collection("Delete Me")
        tmp_db.delete_collection(cid)
        ids = [c["id"] for c in tmp_db.get_collections()]
        assert cid not in ids

    def test_add_ref_and_list(self, tmp_db):
        ref = _make_ref(doi="10.coll/1")
        rid = tmp_db.upsert(ref)
        cid = tmp_db.create_collection("Science")
        tmp_db.add_to_collection(rid, cid)
        refs = tmp_db.list_collection_refs(cid)
        assert any(r.doi == "10.coll/1" for r in refs)

    def test_remove_from_collection(self, tmp_db):
        ref = _make_ref(doi="10.coll/2")
        rid = tmp_db.upsert(ref)
        cid = tmp_db.create_collection("Temp Coll")
        tmp_db.add_to_collection(rid, cid)
        tmp_db.remove_from_collection(rid, cid)
        refs = tmp_db.list_collection_refs(cid)
        assert not any(r.doi == "10.coll/2" for r in refs)

    def test_get_ref_collections(self, tmp_db):
        ref = _make_ref(doi="10.coll/3")
        rid = tmp_db.upsert(ref)
        cid1 = tmp_db.create_collection("A")
        cid2 = tmp_db.create_collection("B")
        tmp_db.add_to_collection(rid, cid1)
        tmp_db.add_to_collection(rid, cid2)
        colls = tmp_db.get_ref_collections(rid)
        names = [c["name"] for c in colls]
        assert "A" in names and "B" in names

    def test_add_duplicate_is_idempotent(self, tmp_db):
        ref = _make_ref(doi="10.coll/4")
        rid = tmp_db.upsert(ref)
        cid = tmp_db.create_collection("No Dupe")
        tmp_db.add_to_collection(rid, cid)
        tmp_db.add_to_collection(rid, cid)  # should not raise
        refs = tmp_db.list_collection_refs(cid)
        assert len([r for r in refs if r.doi == "10.coll/4"]) == 1

    def test_ref_count_in_collection(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.coll/5"))
        r2 = tmp_db.upsert(_make_ref(doi="10.coll/6"))
        cid = tmp_db.create_collection("Counted")
        tmp_db.add_to_collection(r1, cid)
        tmp_db.add_to_collection(r2, cid)
        colls = tmp_db.get_collections()
        c = next(c for c in colls if c["id"] == cid)
        assert c["ref_count"] == 2

    def test_subcollection_parent_id(self, tmp_db):
        parent = tmp_db.create_collection("Parent")
        child  = tmp_db.create_collection("Child", parent_id=parent)
        colls  = tmp_db.get_collections()
        child_row = next(c for c in colls if c["id"] == child)
        assert child_row["parent_id"] == parent

    def test_empty_collection_ref_count_zero(self, tmp_db):
        cid = tmp_db.create_collection("Empty")
        colls = tmp_db.get_collections()
        c = next(c for c in colls if c["id"] == cid)
        assert c["ref_count"] == 0


# ---------------------------------------------------------------------------
# Tag CRUD
# ---------------------------------------------------------------------------

class TestTags:
    def test_add_and_get_tags(self, tmp_db):
        ref = _make_ref(doi="10.tag/1")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["ml", "nlp"])
        tags = tmp_db.get_tags(rid)
        assert "ml" in tags
        assert "nlp" in tags

    def test_remove_tag(self, tmp_db):
        ref = _make_ref(doi="10.tag/2")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["keep", "drop"])
        tmp_db.remove_tag(rid, "drop")
        tags = tmp_db.get_tags(rid)
        assert "keep" in tags
        assert "drop" not in tags

    def test_all_tags_lists_all(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.tag/3"))
        r2 = tmp_db.upsert(_make_ref(doi="10.tag/4"))
        tmp_db.add_tags(r1, ["alpha"])
        tmp_db.add_tags(r2, ["beta"])
        tag_names = [t["name"] for t in tmp_db.all_tags()]
        assert "alpha" in tag_names
        assert "beta" in tag_names

    def test_duplicate_tag_is_idempotent(self, tmp_db):
        ref = _make_ref(doi="10.tag/5")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["dup"])
        tmp_db.add_tags(rid, ["dup"])  # should not raise or duplicate
        tags = tmp_db.get_tags(rid)
        assert tags.count("dup") == 1

    def test_tags_batch(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.tag/6"))
        r2 = tmp_db.upsert(_make_ref(doi="10.tag/7"))
        tmp_db.add_tags(r1, ["x"])
        tmp_db.add_tags(r2, ["y"])
        result = tmp_db.get_tags_batch([r1, r2])
        assert "x" in result[r1]
        assert "y" in result[r2]

    def test_upsert_with_tags(self, tmp_db):
        ref = _make_ref(doi="10.tag/8")
        rid = tmp_db.upsert(ref, tags=["auto1", "auto2"])
        tags = tmp_db.get_tags(rid)
        assert "auto1" in tags
        assert "auto2" in tags

    def test_rename_tag(self, tmp_db):
        ref = _make_ref(doi="10.tag/9")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["oldname"])
        result = tmp_db.rename_tag("oldname", "newname")
        assert result is True
        tags = tmp_db.get_tags(rid)
        assert "newname" in tags
        assert "oldname" not in tags

    def test_rename_tag_collision_returns_false(self, tmp_db):
        ref = _make_ref(doi="10.tag/10")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["tagA", "tagB"])
        result = tmp_db.rename_tag("tagA", "tagB")  # tagB exists
        assert result is False
        tags = tmp_db.get_tags(rid)
        assert "tagA" in tags  # unchanged

    def test_set_tag_color(self, tmp_db):
        ref = _make_ref(doi="10.tag/11")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["colored"])
        tmp_db.set_tag_color("colored", "#ff6b6b")
        all_t = {t["name"]: t for t in tmp_db.all_tags()}
        assert all_t["colored"]["color"] == "#ff6b6b"

    def test_delete_tag_by_name(self, tmp_db):
        ref = _make_ref(doi="10.tag/12")
        rid = tmp_db.upsert(ref)
        tmp_db.add_tags(rid, ["gone"])
        result = tmp_db.delete_tag_by_name("gone")
        assert result is True
        tags = tmp_db.get_tags(rid)
        assert "gone" not in tags
        tag_names = [t["name"] for t in tmp_db.all_tags()]
        assert "gone" not in tag_names

    def test_delete_tag_not_found_returns_false(self, tmp_db):
        result = tmp_db.delete_tag_by_name("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# Saved searches
# ---------------------------------------------------------------------------

class TestSavedSearches:
    def test_create_and_list(self, tmp_db):
        sid = tmp_db.create_saved_search("My Search", "neural networks", "{}")
        assert isinstance(sid, int)
        searches = tmp_db.list_saved_searches()
        names = [s["name"] for s in searches]
        assert "My Search" in names

    def test_list_is_ordered_by_name(self, tmp_db):
        tmp_db.create_saved_search("Zebra", "z", "{}")
        tmp_db.create_saved_search("Apple", "a", "{}")
        names = [s["name"] for s in tmp_db.list_saved_searches()]
        assert names.index("Apple") < names.index("Zebra")

    def test_delete_saved_search(self, tmp_db):
        sid = tmp_db.create_saved_search("To Delete", "delete", "{}")
        tmp_db.delete_saved_search(sid)
        ids = [s["id"] for s in tmp_db.list_saved_searches()]
        assert sid not in ids

    def test_query_and_filters_stored(self, tmp_db):
        sid = tmp_db.create_saved_search("Complex", "deep learning", '{"year": 2020}')
        s = next(s for s in tmp_db.list_saved_searches() if s["id"] == sid)
        assert s["query"] == "deep learning"
        assert s["filters"] == '{"year": 2020}'

    def test_empty_list_initially(self, tmp_db):
        assert tmp_db.list_saved_searches() == []


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

class TestAnalytics:
    def test_status_counts_empty(self, tmp_db):
        counts = tmp_db.status_counts()
        assert counts == {}

    def test_status_counts_default_unread(self, tmp_db):
        tmp_db.upsert(_make_ref(doi="10.ana/1"))
        counts = tmp_db.status_counts()
        assert counts.get("unread", 0) == 1

    def test_status_counts_mixed(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.ana/2"))
        r2 = tmp_db.upsert(_make_ref(doi="10.ana/3"))
        r3 = tmp_db.upsert(_make_ref(doi="10.ana/4"))
        tmp_db.update_ref_fields(r2, status="reading")
        tmp_db.update_ref_fields(r3, status="read")
        counts = tmp_db.status_counts()
        assert counts.get("reading", 0) >= 1
        assert counts.get("read", 0) >= 1

    def test_count_read_since_future_date(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.ana/5"))
        tmp_db.update_ref_fields(r1, status="read")
        count = tmp_db.count_read_since("2099-01-01")
        assert count == 0

    def test_count_read_since_past_date(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.ana/6"))
        tmp_db.update_ref_fields(r1, status="read")
        count = tmp_db.count_read_since("2000-01-01")
        assert count >= 1

    def test_list_recent(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.ana/7", title="First"))
        r2 = tmp_db.upsert(_make_ref(doi="10.ana/8", title="Second"))
        recent = tmp_db.list_recent(2)
        # Second was inserted last, so it should appear first
        assert recent[0].doi == "10.ana/8"
        assert recent[1].doi == "10.ana/7"

    def test_list_recent_respects_limit(self, tmp_db):
        for i in range(5):
            tmp_db.upsert(_make_ref(doi=f"10.ana/r{i}"))
        recent = tmp_db.list_recent(3)
        assert len(recent) == 3

    def test_delete_ref(self, tmp_db):
        rid = tmp_db.upsert(_make_ref(doi="10.ana/del1"))
        deleted = tmp_db.delete_ref(rid)
        assert deleted is True
        assert tmp_db.get(rid) is None

    def test_delete_ref_nonexistent_returns_false(self, tmp_db):
        deleted = tmp_db.delete_ref("doi:nonexistent")
        assert deleted is False

    def test_find_by_cite_key_found(self, tmp_db):
        ref = _make_ref(doi="10.ana/ck1")
        rid = tmp_db.upsert(ref)
        # Manually set a cite_key
        tmp_db.update_ref_fields(rid, cite_key="smith2024test")
        result = tmp_db.find_by_cite_key("smith2024test")
        assert result == rid

    def test_find_by_cite_key_not_found(self, tmp_db):
        result = tmp_db.find_by_cite_key("nonexistent_key")
        assert result is None
    def test_get_many_returns_all(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.gm/1", title="First"))
        r2 = tmp_db.upsert(_make_ref(doi="10.gm/2", title="Second"))
        r3 = tmp_db.upsert(_make_ref(doi="10.gm/3", title="Third"))
        refs = tmp_db.get_many([r1, r2, r3])
        dois = {r.doi for r in refs}
        assert "10.gm/1" in dois
        assert "10.gm/2" in dois
        assert "10.gm/3" in dois

    def test_get_many_preserves_order(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.gm/4", title="Alpha"))
        r2 = tmp_db.upsert(_make_ref(doi="10.gm/5", title="Beta"))
        refs = tmp_db.get_many([r2, r1])
        assert refs[0].doi == "10.gm/5"
        assert refs[1].doi == "10.gm/4"

    def test_get_many_skips_missing(self, tmp_db):
        r1 = tmp_db.upsert(_make_ref(doi="10.gm/6"))
        refs = tmp_db.get_many([r1, "doi:nonexistent"])
        assert len(refs) == 1

    def test_get_many_empty_list(self, tmp_db):
        refs = tmp_db.get_many([])
        assert refs == []


# ---------------------------------------------------------------------------
# Extended field round-trip tests
# ---------------------------------------------------------------------------

class TestExtendedFields:
    def test_article_number_roundtrip(self, tmp_db):
        from mouseion.models import Reference, RefType
        ref = Reference(
            title="Journal Article",
            doi="10.ext/1",
            article_number="e12345",
            ref_type=RefType.JOURNAL,
        )
        rid = tmp_db.upsert(ref)
        stored = tmp_db.get(rid)
        assert stored.article_number == "e12345"

    def test_event_name_roundtrip(self, tmp_db):
        from mouseion.models import Reference, RefType
        ref = Reference(
            title="Conference Paper",
            doi="10.ext/2",
            event_name="NeurIPS 2024",
            ref_type=RefType.CONFERENCE,
        )
        rid = tmp_db.upsert(ref)
        stored = tmp_db.get(rid)
        assert stored.event_name == "NeurIPS 2024"

    def test_editors_roundtrip(self, tmp_db):
        from mouseion.models import Author, Reference, RefType
        ref = Reference(
            title="Edited Book",
            doi="10.ext/3",
            editors=[Author(family="Editor", given="Ed")],
            ref_type=RefType.BOOK,
        )
        rid = tmp_db.upsert(ref)
        stored = tmp_db.get(rid)
        assert len(stored.editors) == 1
        assert stored.editors[0].family == "Editor"

    def test_container_title_roundtrip(self, tmp_db):
        from mouseion.models import Reference, RefType
        ref = Reference(
            title="Book Chapter",
            doi="10.ext/4",
            container_title="Handbook of AI",
            ref_type=RefType.BOOK_CHAPTER,
        )
        rid = tmp_db.upsert(ref)
        stored = tmp_db.get(rid)
        assert stored.container_title == "Handbook of AI"

    def test_eissn_roundtrip(self, tmp_db):
        from mouseion.models import Reference, RefType
        ref = Reference(
            title="Journal",
            doi="10.ext/5",
            eissn="1234-5679",
            ref_type=RefType.JOURNAL,
        )
        rid = tmp_db.upsert(ref)
        stored = tmp_db.get(rid)
        assert stored.eissn == "1234-5679"

    def test_license_roundtrip(self, tmp_db):
        from mouseion.models import Reference, RefType
        ref = Reference(
            title="OA Paper",
            doi="10.ext/6",
            license="https://creativecommons.org/licenses/by/4.0/",
            ref_type=RefType.JOURNAL,
        )
        rid = tmp_db.upsert(ref)
        stored = tmp_db.get(rid)
        assert stored.license == "https://creativecommons.org/licenses/by/4.0/"

    def test_num_pages_roundtrip(self, tmp_db):
        from mouseion.models import Reference, RefType
        ref = Reference(
            title="Long Book",
            isbn="978-0-123-456789",
            num_pages=450,
            ref_type=RefType.BOOK,
        )
        rid = tmp_db.upsert(ref)
        stored = tmp_db.get(rid)
        assert stored.num_pages == 450
