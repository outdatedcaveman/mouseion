"""
Persistent SQLite database for enriched references.

Features
--------
* UPSERT by DOI (or by normalized title+year+author when no DOI)
* FTS5 full-text search (title, abstract, authors, keywords, journal)
* Tag system (manual + auto-assigned)
* Integration tracking (Notion page ID, Zotero item key)
* Configurable path — point to a Dropbox / Drive folder for multi-device sync

Schema notes
------------
The `refs` table stores every field in the Reference model as flat columns
plus JSON blobs for lists/dicts.  This keeps queries fast and simple.
FTS5 is maintained via triggers.

Thread safety: each call creates a short-lived connection with WAL mode
for safe concurrent read access (the typical pattern for a single-user tool).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from .models import Author, RefType, Reference

# Per-process set of DB paths whose schema has already been initialised.
# Running executescript() (which includes PRAGMA journal_mode=WAL) on every
# request caused the 4th+ connection in a process to hang indefinitely after
# prior WAL writes accumulated.  Tracking per-path (rather than a single bool)
# lets tests use independent temporary databases without skipping schema init.
_db_initialized: set[str] = set()
_db_init_lock = threading.Lock()


# ---------------------------------------------------------------------------
# FTS5 trigger DDL — stored as a module-level constant so upsert_many can
# recreate the triggers after dropping them for bulk-insert optimisation.
# ---------------------------------------------------------------------------

_FTS_TRIGGER_DDL = """
    CREATE TRIGGER IF NOT EXISTS refs_ai AFTER INSERT ON refs BEGIN
        INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal)
        VALUES (
            new.id, new.title, new.abstract,
            (SELECT group_concat(json_extract(value, '$.family') || ' ' ||
                                  IFNULL(json_extract(value, '$.given'), ''), ' ')
             FROM json_each(IFNULL(new.authors, '[]'))),
            new.keywords,
            new.journal
        );
    END;

    CREATE TRIGGER IF NOT EXISTS refs_au AFTER UPDATE ON refs BEGIN
        DELETE FROM refs_fts WHERE ref_id = old.id;
        INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal)
        VALUES (
            new.id, new.title, new.abstract,
            (SELECT group_concat(json_extract(value, '$.family') || ' ' ||
                                  IFNULL(json_extract(value, '$.given'), ''), ' ')
             FROM json_each(IFNULL(new.authors, '[]'))),
            new.keywords,
            new.journal
        );
    END;

    CREATE TRIGGER IF NOT EXISTS refs_ad AFTER DELETE ON refs BEGIN
        DELETE FROM refs_fts WHERE ref_id = old.id;
    END;
"""


def _bulk_fts_update(conn: sqlite3.Connection, ids: List[str]) -> None:
    """
    Batch-rebuild FTS5 entries for the given ref IDs.
    Dramatically faster than N individual per-row trigger updates for large
    batches because SQLite processes both the DELETE and the INSERT in a single
    B-tree traversal per operation rather than N separate ones.
    """
    if not ids:
        return
    ph = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM refs_fts WHERE ref_id IN ({ph})", ids)
    conn.execute(
        f"""
        INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal)
        SELECT
            id, title, abstract,
            (SELECT group_concat(
                json_extract(value, '$.family') || ' ' ||
                IFNULL(json_extract(value, '$.given'), ''), ' ')
             FROM json_each(IFNULL(authors, '[]'))),
            keywords,
            journal
        FROM refs WHERE id IN ({ph})
        """,
        ids,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ref_id(ref: Reference) -> str:
    """Generate a stable, deterministic ID for a reference."""
    if ref.doi:
        key = "doi:" + ref.doi.lower().strip()
    elif ref.arxiv_id:
        key = "arxiv:" + ref.arxiv_id.lower().strip()
    elif ref.pmid:
        key = "pmid:" + ref.pmid.strip()
    elif ref.isbn:
        key = "isbn:" + re.sub(r"[-\s]", "", ref.isbn)
    else:
        # Title + year + first author family name
        title = (ref.title or "").lower().strip()
        year  = str(ref.year or "")
        auth  = ref.authors[0].family.lower() if ref.authors else ""
        key   = f"title:{title}:{year}:{auth}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]





def _authors_json(authors: List[Author]) -> str:
    return json.dumps([
        {"family": a.family, "given": a.given,
         "orcid": a.orcid, "affiliation": a.affiliation}
        for a in authors
    ])


def _authors_from_json(s: str) -> List[Author]:
    if not s:
        return []
    try:
        return [Author(**d) for d in json.loads(s)]
    except Exception:
        return []


def _safe_json_list(s: Optional[str]) -> list:
    if not s:
        return []
    try:
        result = json.loads(s)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def _safe_json_dict(s: Optional[str]) -> dict:
    if not s:
        return {}
    try:
        result = json.loads(s)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _ref_to_row(ref: Reference) -> Dict[str, Any]:
    return {
        "id":             _ref_id(ref),
        "doi":            ref.doi,
        "pmid":           ref.pmid,
        "pmcid":          ref.pmcid,
        "arxiv_id":       ref.arxiv_id,
        "isbn":           ref.isbn,
        "issn":           ref.issn,
        "url":            ref.url,
        "oa_url":         ref.oa_url,
        "title":          ref.title or "",
        "authors":        _authors_json(ref.authors),
        "year":           ref.year,
        "month":          ref.month,
        "abstract":       ref.abstract,
        "ref_type":       ref.ref_type.value,
        "journal":        ref.journal,
        "journal_abbrev": ref.journal_abbrev,
        "volume":         ref.volume,
        "issue":          ref.issue,
        "pages":          ref.pages,
        "publisher":      ref.publisher,
        "place":          ref.place,
        "edition":        ref.edition,
        "series":         ref.series,
        "keywords":        json.dumps(ref.keywords),
        "language":        ref.language,
        "open_access":     int(ref.open_access) if ref.open_access is not None else None,
        "citation_count":  ref.citation_count,
        "sources":         json.dumps(ref.sources),
        "completeness":    ref.completeness,
        "cite_key":        ref.cite_key or ref.auto_cite_key(),
        "updated_at":      _now(),
        # Extended fields (added via migrations)
        "eissn":           ref.eissn,
        "container_title": ref.container_title,
        "article_number":  ref.article_number,
        "event_name":      ref.event_name,
        "editors":         _authors_json(ref.editors) if ref.editors else None,
        "num_pages":       ref.num_pages,
        "license":         ref.license,
    }


def _row_col(row: sqlite3.Row, col: str, default=None):
    """Safely read a column that may not exist in older DB versions."""
    try:
        return row[col]
    except (IndexError, KeyError):
        return default


def _row_to_ref(row: sqlite3.Row) -> Reference:
    ref = Reference(
        doi             = row["doi"],
        pmid            = row["pmid"],
        pmcid           = row["pmcid"],
        arxiv_id        = row["arxiv_id"],
        isbn            = row["isbn"],
        issn            = row["issn"],
        eissn           = _row_col(row, "eissn"),
        url             = row["url"],
        oa_url          = row["oa_url"],
        title           = row["title"] or None,
        authors         = _authors_from_json(row["authors"] or ""),
        year            = row["year"],
        month           = row["month"],
        abstract        = row["abstract"],
        ref_type        = RefType(row["ref_type"]) if row["ref_type"] else RefType.UNKNOWN,
        journal         = row["journal"],
        journal_abbrev  = row["journal_abbrev"],
        container_title = _row_col(row, "container_title"),
        volume          = row["volume"],
        issue           = row["issue"],
        pages           = row["pages"],
        article_number  = _row_col(row, "article_number"),
        event_name      = _row_col(row, "event_name"),
        publisher       = row["publisher"],
        place           = row["place"],
        edition         = row["edition"],
        editors         = _authors_from_json(_row_col(row, "editors") or ""),
        series          = row["series"],
        num_pages       = _row_col(row, "num_pages"),
        keywords        = _safe_json_list(row["keywords"]),
        language        = row["language"],
        open_access     = bool(row["open_access"]) if row["open_access"] is not None else None,
        license         = _row_col(row, "license"),
        citation_count  = row["citation_count"],
        sources         = _safe_json_dict(row["sources"]),
        cite_key        = row["cite_key"],
    )
    return ref


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class RefDatabase:
    """
    Persistent store for enriched references.

    Usage
    -----
        with RefDatabase() as db:
            db.upsert(ref)
            results = db.search("machine learning")
    """

    _SCHEMA = """
    PRAGMA journal_mode = WAL;
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS refs (
        id              TEXT PRIMARY KEY,
        doi             TEXT,
        pmid            TEXT,
        pmcid           TEXT,
        arxiv_id        TEXT,
        isbn            TEXT,
        issn            TEXT,
        url             TEXT,
        oa_url          TEXT,
        title           TEXT NOT NULL DEFAULT '',
        authors         TEXT,       -- JSON [{family,given,orcid,affiliation}]
        year            INTEGER,
        month           INTEGER,
        abstract        TEXT,
        ref_type        TEXT,
        journal         TEXT,
        journal_abbrev  TEXT,
        volume          TEXT,
        issue           TEXT,
        pages           TEXT,
        publisher       TEXT,
        place           TEXT,
        edition         TEXT,
        series          TEXT,
        keywords        TEXT,       -- JSON [str]
        language        TEXT,
        open_access     INTEGER,    -- 0/1/NULL
        citation_count  INTEGER,
        sources         TEXT,       -- JSON {provider:score}
        completeness    REAL,
        cite_key        TEXT,
        pdf_local       TEXT,
        pdf_drive_id    TEXT,
        notion_page_id  TEXT,
        zotero_item_key TEXT,
        created_at      TEXT DEFAULT (datetime('now')),
        updated_at      TEXT DEFAULT (datetime('now')),
        UNIQUE (doi)
    );

    CREATE INDEX IF NOT EXISTS idx_doi        ON refs (doi)        WHERE doi IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_arxiv      ON refs (arxiv_id)   WHERE arxiv_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_pmid       ON refs (pmid)       WHERE pmid IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_year       ON refs (year);
    CREATE INDEX IF NOT EXISTS idx_type       ON refs (ref_type);
    CREATE INDEX IF NOT EXISTS idx_complete   ON refs (completeness);
    CREATE INDEX IF NOT EXISTS idx_created_at ON refs (created_at);
    CREATE INDEX IF NOT EXISTS idx_updated_at ON refs (updated_at);
    CREATE INDEX IF NOT EXISTS idx_oa         ON refs (open_access) WHERE open_access = 1;

    CREATE TABLE IF NOT EXISTS tags (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        name  TEXT NOT NULL UNIQUE,
        color TEXT DEFAULT '#6366f1',
        auto  INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS ref_tags (
        ref_id  TEXT NOT NULL REFERENCES refs(id)  ON DELETE CASCADE,
        tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (ref_id, tag_id)
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS refs_fts USING fts5 (
        ref_id   UNINDEXED,
        title,
        abstract,
        authors_text,
        keywords_text,
        journal,
        tokenize = 'porter unicode61'
    );

    CREATE TRIGGER IF NOT EXISTS refs_ai AFTER INSERT ON refs BEGIN
        INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal)
        VALUES (
            new.id, new.title, new.abstract,
            (SELECT group_concat(json_extract(value, '$.family') || ' ' ||
                                  json_extract(value, '$.given'), ' ')
             FROM json_each(IFNULL(new.authors, '[]'))),
            new.keywords,
            new.journal
        );
    END;

    CREATE TRIGGER IF NOT EXISTS refs_au AFTER UPDATE ON refs BEGIN
        DELETE FROM refs_fts WHERE ref_id = old.id;
        INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal)
        VALUES (
            new.id, new.title, new.abstract,
            (SELECT group_concat(json_extract(value, '$.family') || ' ' ||
                                  json_extract(value, '$.given'), ' ')
             FROM json_each(IFNULL(new.authors, '[]'))),
            new.keywords,
            new.journal
        );
    END;

    CREATE TRIGGER IF NOT EXISTS refs_ad AFTER DELETE ON refs BEGIN
        DELETE FROM refs_fts WHERE ref_id = old.id;
    END;

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS collections (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        parent_id  INTEGER REFERENCES collections(id) ON DELETE CASCADE,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS ref_collections (
        ref_id        TEXT    NOT NULL REFERENCES refs(id) ON DELETE CASCADE,
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        PRIMARY KEY (ref_id, collection_id)
    );
    """

    # Incremental migrations: run on every open(), errors ignored if already applied.
    _MIGRATIONS = [
        "ALTER TABLE refs ADD COLUMN notes  TEXT",
        "ALTER TABLE refs ADD COLUMN status TEXT DEFAULT 'unread'",
        "CREATE INDEX IF NOT EXISTS idx_status ON refs (status)",
        """CREATE TABLE IF NOT EXISTS saved_searches (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            query      TEXT NOT NULL DEFAULT '',
            filters    TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        # Reference model fields missing from initial schema
        "ALTER TABLE refs ADD COLUMN eissn           TEXT",
        "ALTER TABLE refs ADD COLUMN container_title  TEXT",
        "ALTER TABLE refs ADD COLUMN article_number   TEXT",
        "ALTER TABLE refs ADD COLUMN event_name       TEXT",
        "ALTER TABLE refs ADD COLUMN editors          TEXT",  # JSON list
        "ALTER TABLE refs ADD COLUMN num_pages        INTEGER",
        "ALTER TABLE refs ADD COLUMN license          TEXT",
    ]

    def __init__(self, path: Optional[str | Path] = None) -> None:
        from .config import get_config
        db_path = path or get_config().db_path
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    # -----------------------------------------------------------------------
    # Context manager
    # -----------------------------------------------------------------------

    def __enter__(self) -> "RefDatabase":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._conn and exc_type is None:
            self._conn.commit()
        self.close()

    def open(self) -> None:
        global _db_initialized
        print(f"[db.open] connecting to {self._path}", file=sys.stderr, flush=True)
        self._conn = sqlite3.connect(str(self._path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        print("[db.open] connected; setting busy_timeout", file=sys.stderr, flush=True)
        self._conn.execute("PRAGMA busy_timeout = 10000")  # 10 s max lock wait
        db_key = str(self._path)
        if db_key not in _db_initialized:
            print("[db.open] running schema init", file=sys.stderr, flush=True)
            with _db_init_lock:
                if db_key not in _db_initialized:
                    # Run full schema + migrations once per process per path.
                    # Calling executescript() (which issues PRAGMA journal_mode=WAL)
                    # on every request caused the Nth connection to hang after
                    # prior WAL writes had accumulated.
                    print("[db.open] executescript start", file=sys.stderr, flush=True)
                    self._conn.executescript(self._SCHEMA)
                    print("[db.open] executescript done; running migrations", file=sys.stderr, flush=True)
                    for stmt in self._MIGRATIONS:
                        try:
                            self._conn.execute(stmt)
                        except sqlite3.OperationalError:
                            pass
                    self._conn.commit()
                    # Force a full WAL checkpoint so subsequent connections don't
                    # inherit accumulated WAL pages that cause the 4th+ connection
                    # to hang indefinitely when mmap or other PRAGMAs touch the WAL.
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    _db_initialized.add(db_key)
                    print("[db.open] schema init complete", file=sys.stderr, flush=True)
        # Per-connection performance settings (safe to re-apply each time).
        import os
        _low_mem = bool(os.environ.get("RENDER") or os.environ.get("LOW_MEMORY"))
        self._conn.execute(f"PRAGMA cache_size = {-8192 if _low_mem else -65536}")
        # mmap_size is intentionally omitted: setting it after WAL writes accumulate
        # causes the 4th+ connection to hang indefinitely (WAL shm deadlock).
        self._conn.execute("PRAGMA synchronous    = NORMAL")
        self._conn.execute("PRAGMA temp_store     = MEMORY")
        self._conn.execute("PRAGMA wal_autocheckpoint = 1000")
        print("[db.open] done", file=sys.stderr, flush=True)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        if self._conn:
            yield self._conn
        else:
            conn = sqlite3.connect(str(self._path))
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # Core write operations
    # -----------------------------------------------------------------------

    def upsert(self, ref: Reference, tags: Optional[List[str]] = None) -> str:
        """
        Insert or update a reference.  Returns the record ID.

        Deduplication strategy:
        - Primary key is the content-addressed ``id`` (hash of DOI / arXiv / …).
        - If a record with the *same DOI* already exists under a *different* id
          (e.g. the paper was first saved by title, then re-looked-up by DOI),
          we reuse the existing id so the UPSERT updates that row rather than
          creating a duplicate.  SQLite does not support multiple ON CONFLICT
          clauses, so we handle the DOI check explicitly.
        """
        row = _ref_to_row(ref)
        ref_id = row["id"]

        with self._db() as conn:
            # Resolve DOI-based ID collision before the UPSERT
            if ref.doi:
                existing = conn.execute(
                    "SELECT id FROM refs WHERE doi = ?", (ref.doi,)
                ).fetchone()
                if existing and existing["id"] != ref_id:
                    ref_id = existing["id"]
                    row["id"] = ref_id

            cols = list(row.keys())
            placeholders = ", ".join(f":{c}" for c in cols)
            updates = ", ".join(
                f"{c} = excluded.{c}"
                for c in cols
                if c not in ("id", "created_at")
            )
            sql = f"""
                INSERT INTO refs ({', '.join(cols)})
                VALUES ({placeholders})
                ON CONFLICT(id) DO UPDATE SET {updates}
            """
            if "created_at" not in row:
                row["created_at"] = _now()
            conn.execute(sql, row)

            if tags:
                self._apply_tags(conn, ref_id, tags)

        return ref_id

    def upsert_many(
        self,
        refs: List[Reference],
        tags_per_ref: Optional[List[Optional[List[str]]]] = None,
        deferred_fts: Optional[bool] = None,
    ) -> List[str]:
        """
        Bulk upsert.  Returns list of IDs in the same order as `refs`.

        Optimised for large batches:
        - One SELECT to batch-resolve DOI collisions instead of N individual SELECTs.
        - One executemany for all inserts/updates instead of N individual executes.
        - ``deferred_fts=True`` (auto-enabled for batches > 200): drops the
          per-row FTS triggers for the duration of the insert, then rebuilds FTS
          for all affected rows in a single batch pass — 5-20× faster for
          large imports. ``executescript`` commits the transaction atomically
          when recreating the triggers.
        """
        if not refs:
            return []

        # Auto-enable deferred FTS for large batches; triggers are efficient for
        # small ones but become the bottleneck at 200+ rows.
        if deferred_fts is None:
            deferred_fts = len(refs) > 200

        rows = [_ref_to_row(r) for r in refs]
        for row in rows:
            if "created_at" not in row:
                row["created_at"] = _now()

        with self._db() as conn:
            # --- Batch DOI collision detection ---
            # Map doi → list of row indices so we handle duplicate DOIs in batch.
            dois_to_idxs: Dict[str, List[int]] = {}
            for i, row in enumerate(rows):
                doi = row.get("doi")
                if doi:
                    dois_to_idxs.setdefault(doi, []).append(i)

            if dois_to_idxs:
                placeholders = ", ".join("?" * len(dois_to_idxs))
                cur = conn.execute(
                    f"SELECT doi, id FROM refs WHERE doi IN ({placeholders})",
                    list(dois_to_idxs.keys()),
                )
                for existing_doi, existing_id in cur.fetchall():
                    for row_idx in dois_to_idxs[existing_doi]:
                        if rows[row_idx]["id"] != existing_id:
                            rows[row_idx]["id"] = existing_id

            # --- Optionally drop FTS triggers for bulk performance ---
            if deferred_fts:
                conn.execute("DROP TRIGGER IF EXISTS refs_ai")
                conn.execute("DROP TRIGGER IF EXISTS refs_au")

            # --- Bulk upsert with executemany ---
            cols = list(rows[0].keys())
            placeholders_str = ", ".join(f":{c}" for c in cols)
            updates = ", ".join(
                f"{c} = excluded.{c}"
                for c in cols
                if c not in ("id", "created_at")
            )
            sql = (
                f"INSERT INTO refs ({', '.join(cols)}) "
                f"VALUES ({placeholders_str}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}"
            )
            conn.executemany(sql, rows)

            if deferred_fts:
                # Batch-rebuild FTS for all affected rows, then restore triggers.
                # executescript issues a COMMIT first, which atomically persists
                # both the bulk insert and the FTS batch update.
                _bulk_fts_update(conn, [r["id"] for r in rows])
                conn.executescript(_FTS_TRIGGER_DDL)

            # --- Tags ---
            if tags_per_ref:
                for i, row in enumerate(rows):
                    if i < len(tags_per_ref) and tags_per_ref[i]:
                        self._apply_tags(conn, row["id"], tags_per_ref[i])

        return [row["id"] for row in rows]

    def rebuild_fts(self) -> None:
        """
        Full FTS5 rebuild from scratch.

        Use after a very large initial import (100k+ rows) to ensure the search
        index is correct and compact.  This is a single-pass operation and is
        dramatically faster than waiting for per-row triggers to catch up.
        Requires SQLite 3.23.0+ (released 2018-04-02).
        """
        with self._db() as conn:
            conn.execute("INSERT INTO refs_fts(refs_fts) VALUES('delete-all')")
            conn.execute(
                """
                INSERT INTO refs_fts
                    (ref_id, title, abstract, authors_text, keywords_text, journal)
                SELECT
                    id, title, abstract,
                    (SELECT group_concat(
                        json_extract(value, '$.family') || ' ' ||
                        IFNULL(json_extract(value, '$.given'), ''), ' ')
                     FROM json_each(IFNULL(authors, '[]'))),
                    keywords,
                    journal
                FROM refs
                """
            )

    def delete(self, ref_id: str) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM refs WHERE id = ?", (ref_id,))

    def update_integration_ids(
        self,
        ref_id: str,
        notion_page_id: Optional[str] = None,
        zotero_item_key: Optional[str] = None,
        pdf_local: Optional[str] = None,
        pdf_drive_id: Optional[str] = None,
    ) -> None:
        sets = []
        params: Dict[str, Any] = {"id": ref_id}
        if notion_page_id is not None:
            sets.append("notion_page_id = :notion_page_id")
            params["notion_page_id"] = notion_page_id
        if zotero_item_key is not None:
            sets.append("zotero_item_key = :zotero_item_key")
            params["zotero_item_key"] = zotero_item_key
        if pdf_local is not None:
            sets.append("pdf_local = :pdf_local")
            params["pdf_local"] = pdf_local
        if pdf_drive_id is not None:
            sets.append("pdf_drive_id = :pdf_drive_id")
            params["pdf_drive_id"] = pdf_drive_id
        if sets:
            with self._db() as conn:
                conn.execute(
                    f"UPDATE refs SET {', '.join(sets)} WHERE id = :id", params
                )

    def update_ref_fields(
        self,
        ref_id: str,
        notes: Optional[str] = None,
        status: Optional[str] = None,
        # Bibliographic metadata edits
        title: Optional[str] = None,
        year: Optional[int] = None,
        journal: Optional[str] = None,
        volume: Optional[str] = None,
        issue: Optional[str] = None,
        pages: Optional[str] = None,
        abstract: Optional[str] = None,
        cite_key: Optional[str] = None,
        authors_json: Optional[str] = None,
    ) -> None:
        """Update user-editable fields on a reference.

        Pass only the fields you want to change; None means «leave unchanged».
        The FTS5 index is updated automatically via the refs_au trigger.
        """
        _VALID_STATUS = {"unread", "reading", "read"}
        sets: List[str] = []
        params: Dict[str, Any] = {"id": ref_id}

        def _add(col: str, val) -> None:
            if val is not None:
                sets.append(f"{col} = :{col}")
                params[col] = val

        _add("notes",    notes)
        _add("title",    title)
        _add("year",     year)
        _add("journal",  journal)
        _add("volume",   volume)
        _add("issue",    issue)
        _add("pages",    pages)
        _add("abstract", abstract)
        _add("cite_key", cite_key)
        _add("authors",  authors_json)

        if status is not None:
            if status not in _VALID_STATUS:
                raise ValueError(f"status must be one of {_VALID_STATUS}")
            sets.append("status = :status")
            params["status"] = status

        if sets:
            sets.append("updated_at = :updated_at")
            params["updated_at"] = _now()
            with self._db() as conn:
                conn.execute(
                    f"UPDATE refs SET {', '.join(sets)} WHERE id = :id", params
                )

    # -----------------------------------------------------------------------
    # Collections
    # -----------------------------------------------------------------------

    def get_collections(self) -> List[Dict[str, Any]]:
        """Return all collections with their reference counts."""
        with self._db() as conn:
            cur = conn.execute(
                """SELECT c.id, c.name, c.parent_id,
                          COUNT(rc.ref_id) AS ref_count
                   FROM collections c
                   LEFT JOIN ref_collections rc ON rc.collection_id = c.id
                   GROUP BY c.id
                   ORDER BY c.name"""
            )
            return [dict(r) for r in cur.fetchall()]

    def create_collection(self, name: str, parent_id: Optional[int] = None) -> int:
        """Create a new collection. Returns the new collection ID."""
        with self._db() as conn:
            cur = conn.execute(
                "INSERT INTO collections (name, parent_id) VALUES (?, ?)",
                (name.strip(), parent_id),
            )
            return cur.lastrowid

    def rename_collection(self, collection_id: int, name: str) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE collections SET name = ? WHERE id = ?",
                (name.strip(), collection_id),
            )

    def delete_collection(self, collection_id: int) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))

    def add_to_collection(self, ref_id: str, collection_id: int) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ref_collections (ref_id, collection_id) VALUES (?, ?)",
                (ref_id, collection_id),
            )

    def remove_from_collection(self, ref_id: str, collection_id: int) -> None:
        with self._db() as conn:
            conn.execute(
                "DELETE FROM ref_collections WHERE ref_id = ? AND collection_id = ?",
                (ref_id, collection_id),
            )

    def get_ref_collections(self, ref_id: str) -> List[Dict[str, Any]]:
        """Return collections that contain the given reference."""
        with self._db() as conn:
            cur = conn.execute(
                """SELECT c.id, c.name FROM collections c
                   JOIN ref_collections rc ON rc.collection_id = c.id
                   WHERE rc.ref_id = ?
                   ORDER BY c.name""",
                (ref_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_collection_refs(
        self,
        collection_id: int,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Reference]:
        """Return all references in a collection."""
        with self._db() as conn:
            cur = conn.execute(
                """SELECT refs.* FROM refs
                   JOIN ref_collections rc ON rc.ref_id = refs.id
                   WHERE rc.collection_id = ?
                   ORDER BY refs.year DESC, refs.title ASC
                   LIMIT ? OFFSET ?""",
                (collection_id, limit, offset),
            )
            return [_row_to_ref(r) for r in cur.fetchall()]

    # -----------------------------------------------------------------------
    # Tag management
    # -----------------------------------------------------------------------

    def _get_or_create_tag(self, conn: sqlite3.Connection, name: str, auto: bool = False) -> int:
        cur = conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO tags (name, auto) VALUES (?, ?)", (name, int(auto))
        )
        return cur.lastrowid

    def _apply_tags(
        self, conn: sqlite3.Connection, ref_id: str, tags: List[str], auto: bool = False
    ) -> None:
        for tag in tags:
            tag_id = self._get_or_create_tag(conn, tag.strip(), auto=auto)
            conn.execute(
                "INSERT OR IGNORE INTO ref_tags (ref_id, tag_id) VALUES (?, ?)",
                (ref_id, tag_id),
            )

    def add_tags(self, ref_id: str, tags: List[str], auto: bool = False) -> None:
        with self._db() as conn:
            self._apply_tags(conn, ref_id, tags, auto=auto)

    def remove_tag(self, ref_id: str, tag_name: str) -> None:
        with self._db() as conn:
            conn.execute(
                """DELETE FROM ref_tags WHERE ref_id = ? AND
                   tag_id = (SELECT id FROM tags WHERE name = ?)""",
                (ref_id, tag_name),
            )

    def get_tags(self, ref_id: str) -> List[str]:
        with self._db() as conn:
            cur = conn.execute(
                """SELECT t.name FROM tags t
                   JOIN ref_tags rt ON rt.tag_id = t.id
                   WHERE rt.ref_id = ?
                   ORDER BY t.name""",
                (ref_id,),
            )
            return [r["name"] for r in cur.fetchall()]

    def get_tags_batch(self, ref_ids: List[str]) -> Dict[str, List[str]]:
        """
        Return a mapping of ref_id → [tag_name, …] for all given ref_ids
        in a single query.  Far more efficient than calling get_tags in a loop.
        """
        if not ref_ids:
            return {}
        placeholders = ", ".join("?" * len(ref_ids))
        with self._db() as conn:
            cur = conn.execute(
                f"""SELECT rt.ref_id, t.name
                    FROM ref_tags rt
                    JOIN tags t ON t.id = rt.tag_id
                    WHERE rt.ref_id IN ({placeholders})
                    ORDER BY rt.ref_id, t.name""",
                ref_ids,
            )
            result: Dict[str, List[str]] = {rid: [] for rid in ref_ids}
            for row in cur.fetchall():
                result[row["ref_id"]].append(row["name"])
        return result

    def all_tags(self) -> List[Dict[str, Any]]:
        with self._db() as conn:
            cur = conn.execute(
                """SELECT t.name, t.color, t.auto,
                          COUNT(rt.ref_id) AS count
                   FROM tags t
                   LEFT JOIN ref_tags rt ON rt.tag_id = t.id
                   GROUP BY t.id
                   ORDER BY count DESC, t.name"""
            )
            return [dict(r) for r in cur.fetchall()]

    def rename_tag(self, old_name: str, new_name: str) -> bool:
        """Rename a tag. Returns False if new_name already exists, True on success."""
        with self._db() as conn:
            existing = conn.execute(
                "SELECT id FROM tags WHERE name = ?", (new_name,)
            ).fetchone()
            if existing:
                return False
            conn.execute(
                "UPDATE tags SET name = ? WHERE name = ?", (new_name, old_name)
            )
        return True

    def set_tag_color(self, tag_name: str, color: str) -> None:
        """Update the color of a tag (hex string, e.g. '#ff6b6b')."""
        with self._db() as conn:
            conn.execute(
                "UPDATE tags SET color = ? WHERE name = ?", (color, tag_name)
            )

    def delete_tag_by_name(self, tag_name: str) -> bool:
        """Delete a tag and all its associations. Returns False if not found."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT id FROM tags WHERE name = ?", (tag_name,)
            ).fetchone()
            if not row:
                return False
            tag_id = row["id"]
            conn.execute("DELETE FROM ref_tags WHERE tag_id = ?", (tag_id,))
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        return True

    # -----------------------------------------------------------------------
    # Read operations
    # -----------------------------------------------------------------------

    def get(self, ref_id: str) -> Optional[Reference]:
        with self._db() as conn:
            cur = conn.execute("SELECT * FROM refs WHERE id = ?", (ref_id,))
            row = cur.fetchone()
            return _row_to_ref(row) if row else None

    def get_many(self, ref_ids: List[str]) -> List[Reference]:
        """Return references for a list of IDs in a single query.

        Preserves the order of ref_ids; missing IDs are silently skipped.
        """
        if not ref_ids:
            return []
        ph = ",".join("?" * len(ref_ids))
        with self._db() as conn:
            cur = conn.execute(
                f"SELECT * FROM refs WHERE id IN ({ph})", ref_ids
            )
            by_id = {row["id"]: _row_to_ref(row) for row in cur.fetchall()}
        return [by_id[i] for i in ref_ids if i in by_id]

    def get_by_doi(self, doi: str) -> Optional[Reference]:
        with self._db() as conn:
            cur = conn.execute("SELECT * FROM refs WHERE doi = ?", (doi.strip(),))
            row = cur.fetchone()
            return _row_to_ref(row) if row else None

    def get_extra(self, ref_id: str) -> Dict[str, Any]:
        """Return integration/PDF/notes/status fields not in the Reference model."""
        with self._db() as conn:
            cur = conn.execute(
                "SELECT pdf_local, pdf_drive_id, notion_page_id, zotero_item_key, "
                "notes, status, cite_key, created_at, updated_at FROM refs WHERE id = ?",
                (ref_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else {}

    def get_extras_bulk(self, ref_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Return integration/PDF/notes/status metadata for multiple refs in a single query.
        Far more efficient than calling ``get_extra`` in a loop.
        Returns a mapping of ref_id → extras dict (missing IDs are absent).
        """
        if not ref_ids:
            return {}
        ph = ",".join("?" * len(ref_ids))
        with self._db() as conn:
            cur = conn.execute(
                f"SELECT id, pdf_local, pdf_drive_id, notion_page_id, "
                f"zotero_item_key, notes, status, cite_key, created_at, updated_at "
                f"FROM refs WHERE id IN ({ph})",
                ref_ids,
            )
            return {row["id"]: dict(row) for row in cur.fetchall()}

    def search(
        self,
        query: str,
        tags: Optional[List[str]] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        ref_type: Optional[str] = None,
        oa_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Tuple[Reference, float]]:
        """
        Full-text search + optional filters.
        Returns list of (Reference, relevance_score) tuples.
        """
        params: Dict[str, Any] = {}
        joins: List[str] = []
        wheres: List[str] = []

        use_fts = bool(query.strip())
        if use_fts:
            joins.append(
                "JOIN refs_fts ON refs_fts.ref_id = refs.id"
            )
            wheres.append("refs_fts MATCH :query")
            params["query"] = _fts_query(query)
            order = "ORDER BY rank"
        else:
            order = "ORDER BY year DESC, title ASC"

        if tags:
            for i, tag in enumerate(tags):
                alias = f"rt{i}"
                joins.append(
                    f"JOIN ref_tags {alias} ON {alias}.ref_id = refs.id "
                    f"JOIN tags t{i} ON t{i}.id = {alias}.tag_id AND t{i}.name = :tag{i}"
                )
                params[f"tag{i}"] = tag

        if year_from is not None:
            wheres.append("year >= :year_from")
            params["year_from"] = year_from
        if year_to is not None:
            wheres.append("year <= :year_to")
            params["year_to"] = year_to
        if ref_type:
            wheres.append("ref_type = :ref_type")
            params["ref_type"] = ref_type
        if oa_only:
            wheres.append("open_access = 1")

        where_clause = f"WHERE {' AND '.join(wheres)}" if wheres else ""
        join_clause  = " ".join(joins)
        rank_col = "refs_fts.rank AS fts_rank" if use_fts else "NULL AS fts_rank"

        sql = f"""
            SELECT refs.*, {rank_col}
            FROM refs {join_clause}
            {where_clause}
            {order}
            LIMIT :limit OFFSET :offset
        """
        params["limit"]  = limit
        params["offset"] = offset

        # Optionally fetch FTS5 snippet for abstract column (index 1)
        snippet_sql = ""
        if use_fts:
            snippet_sql = """
                SELECT refs_fts.ref_id,
                       snippet(refs_fts, 1, '<mark>', '</mark>', '…', 20) AS snip
                FROM refs_fts
                WHERE refs_fts MATCH :query
                LIMIT :limit OFFSET :offset
            """

        with self._db() as conn:
            try:
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                # FTS query might be malformed; fall back to LIKE
                return self._search_fallback(query, limit, offset)

            # Build snippet map if FTS
            snippet_map: Dict[str, str] = {}
            if use_fts and snippet_sql:
                try:
                    snip_rows = conn.execute(snippet_sql, params).fetchall()
                    snippet_map = {r["ref_id"]: r["snip"] for r in snip_rows}
                except Exception:
                    pass

        results = []
        for row in rows:
            ref   = _row_to_ref(row)
            score = abs(row["fts_rank"]) if "fts_rank" in row.keys() and row["fts_rank"] else 0.5
            # Attach snippet as transient attribute using the DB row ref_id
            row_ref_id = row["id"] if "id" in row.keys() else _ref_id(ref)
            snip = snippet_map.get(row_ref_id or "")
            if snip:
                setattr(ref, "_snippet", snip)
            results.append((ref, score))
        return results

    def _search_fallback(self, query: str, limit: int, offset: int) -> List[Tuple[Reference, float]]:
        like = f"%{query}%"
        with self._db() as conn:
            cur = conn.execute(
                "SELECT * FROM refs WHERE title LIKE ? OR abstract LIKE ? "
                "ORDER BY year DESC LIMIT ? OFFSET ?",
                (like, like, limit, offset),
            )
            return [(_row_to_ref(r), 0.5) for r in cur.fetchall()]

    def list_all(
        self,
        tags: Optional[List[str]] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        ref_type: Optional[str] = None,
        oa_only: bool = False,
        limit: int = 1000,
        offset: int = 0,
    ) -> List[Reference]:
        return [r for r, _ in self.search(
            "", tags=tags, year_from=year_from, year_to=year_to,
            ref_type=ref_type, oa_only=oa_only, limit=limit, offset=offset,
        )]

    def iter_all(
        self,
        chunk_size: int = 500,
        tags: Optional[List[str]] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        ref_type: Optional[str] = None,
        oa_only: bool = False,
    ) -> Generator[List[Reference], None, None]:
        """
        Stream all matching references in chunks using cursor-based (keyset)
        pagination on the primary key.

        Unlike OFFSET pagination — where each page requires scanning from the
        start of the result set — each page access here is O(log N) regardless
        of how many pages have already been consumed.  Safe and fast at 1M+
        rows.  Suitable for re-indexing, bulk export, and large sync jobs.
        """
        cursor = ""  # empty string sorts before all hex IDs
        while True:
            params: Dict[str, Any] = {"cursor": cursor, "limit": chunk_size}
            joins: List[str] = []
            wheres: List[str] = ["refs.id > :cursor"]

            for i, tag in enumerate(tags or []):
                joins.append(
                    f"JOIN ref_tags rt{i} ON rt{i}.ref_id = refs.id "
                    f"JOIN tags t{i} ON t{i}.id = rt{i}.tag_id AND t{i}.name = :tag{i}"
                )
                params[f"tag{i}"] = tag

            if year_from is not None:
                wheres.append("year >= :year_from")
                params["year_from"] = year_from
            if year_to is not None:
                wheres.append("year <= :year_to")
                params["year_to"] = year_to
            if ref_type:
                wheres.append("ref_type = :ref_type")
                params["ref_type"] = ref_type
            if oa_only:
                wheres.append("open_access = 1")

            sql = (
                f"SELECT refs.* FROM refs {' '.join(joins)} "
                f"WHERE {' AND '.join(wheres)} "
                f"ORDER BY refs.id LIMIT :limit"
            )

            with self._db() as conn:
                rows = conn.execute(sql, params).fetchall()

            if not rows:
                break
            yield [_row_to_ref(row) for row in rows]
            if len(rows) < chunk_size:
                break
            cursor = rows[-1]["id"]

    def count(self) -> int:
        with self._db() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM refs")
            return cur.fetchone()[0]

    def stats(self) -> Dict[str, Any]:
        with self._db() as conn:
            counts_by_type = dict(conn.execute(
                "SELECT ref_type, COUNT(*) FROM refs GROUP BY ref_type"
            ).fetchall())
            avg_completeness = conn.execute(
                "SELECT AVG(completeness) FROM refs"
            ).fetchone()[0] or 0.0
            oa_count = conn.execute(
                "SELECT COUNT(*) FROM refs WHERE open_access = 1"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        return {
            "total": total,
            "by_type": counts_by_type,
            "avg_completeness": avg_completeness,
            "open_access_count": oa_count,
        }

    # -----------------------------------------------------------------------
    # Low-completeness iterator (for re-enrichment)
    # -----------------------------------------------------------------------

    def low_completeness(
        self, threshold: float = 0.5, limit: int = 500
    ) -> List[Reference]:
        with self._db() as conn:
            cur = conn.execute(
                "SELECT * FROM refs WHERE completeness < ? ORDER BY completeness ASC LIMIT ?",
                (threshold, limit),
            )
            return [_row_to_ref(r) for r in cur.fetchall()]

    # -----------------------------------------------------------------------
    # Persistent key-value settings (sync state, version cursors, etc.)
    # -----------------------------------------------------------------------

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieve a persisted setting value, or ``default`` if absent."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Persist a setting value, overwriting any existing value for ``key``."""
        with self._db() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    # -----------------------------------------------------------------------
    # Saved searches
    # -----------------------------------------------------------------------

    def list_saved_searches(self) -> List[Dict[str, Any]]:
        with self._db() as conn:
            cur = conn.execute(
                "SELECT id, name, query, filters, created_at FROM saved_searches ORDER BY name"
            )
            return [dict(r) for r in cur.fetchall()]

    def create_saved_search(self, name: str, query: str, filters: str) -> int:
        with self._db() as conn:
            cur = conn.execute(
                "INSERT INTO saved_searches (name, query, filters) VALUES (?, ?, ?)",
                (name, query, filters),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def delete_saved_search(self, search_id: int) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))

    def status_counts(self) -> Dict[str, int]:
        """Return a mapping of status → count for all references."""
        with self._db() as conn:
            cur = conn.execute(
                "SELECT COALESCE(status,'unread') AS s, COUNT(*) AS c FROM refs GROUP BY s"
            )
            return {row["s"]: row["c"] for row in cur.fetchall()}

    def count_read_since(self, since_date: str) -> int:
        """Return count of refs with status='read' and updated_at >= since_date (YYYY-MM-DD)."""
        with self._db() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM refs WHERE status='read' AND updated_at >= ?",
                (since_date,),
            )
            return cur.fetchone()[0]

    def list_recent(self, n: int = 20) -> List[Reference]:
        """Return the N most recently inserted references (by rowid DESC)."""
        with self._db() as conn:
            cur = conn.execute(
                "SELECT * FROM refs ORDER BY rowid DESC LIMIT ?", (min(n, 1000),)
            )
            return [_row_to_ref(r) for r in cur.fetchall()]

    def delete_ref(self, ref_id: str) -> bool:
        """Delete a reference by ID. Returns True if a row was deleted."""
        with self._db() as conn:
            cur = conn.execute("DELETE FROM refs WHERE id = ?", (ref_id,))
            return cur.rowcount > 0

    def find_by_cite_key(self, cite_key: str) -> Optional[str]:
        """Return the ref_id that uses the given cite_key, or None."""
        with self._db() as conn:
            cur = conn.execute(
                "SELECT id FROM refs WHERE cite_key = ? LIMIT 1", (cite_key,)
            )
            row = cur.fetchone()
            return row["id"] if row else None


# ---------------------------------------------------------------------------
# FTS query helper
# ---------------------------------------------------------------------------

def _fts_query(query: str) -> str:
    """
    Convert a user query to FTS5 syntax.
    Simple approach: each word becomes a prefix term.
    """
    words = [w.strip() for w in query.split() if w.strip()]
    return " ".join(f'"{w}"*' for w in words)
