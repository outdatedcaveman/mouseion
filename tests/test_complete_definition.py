"""The definition of a complete reference (2026-09-24): title + author + an
identifier or delivery (DOI, ISBN, arXiv, PMID, URL, PDF)."""
from mouseion.db import RefDatabase
from mouseion.models import Author, Reference


def _ref(**kw):
    return Reference(title=kw.pop("title", "A Title"), authors=kw.pop("authors", [Author(family="Doe", given="J")]), **kw)


def test_any_identifier_or_delivery_completes():
    assert _ref(doi="10.1/x").is_complete
    assert _ref(isbn="9780000000000").is_complete
    assert _ref(url="https://example.org/paper").is_complete
    r = _ref(); r.extras = {"pdf_local": "G:/My Drive/Mouseion PDFs/x.pdf"}
    assert r.is_complete


def test_title_author_and_identifier_are_all_required():
    assert not _ref().is_complete                                  # no identifier
    assert not _ref(authors=[], doi="10.1/x").is_complete          # no author
    assert not _ref(title="", doi="10.1/x").is_complete            # no title


def test_sql_predicate_mentions_every_accepted_identifier():
    sql = RefDatabase.COMPLETE_SQL
    for col in ("doi", "isbn", "arxiv_id", "pmid", "url", "oa_url", "pdf_local", "pdf_drive_id", "title", "authors"):
        assert col in sql
