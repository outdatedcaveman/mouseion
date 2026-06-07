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


def _dbg(msg: str) -> None:
    """Debug print to stderr — silently ignores I/O errors (e.g. detached console)."""
    try:
        print(msg, file=sys.stderr, flush=True)
    except OSError:
        pass

# Per-process set of DB paths whose schema has already been initialised.
# Running executescript() (which includes PRAGMA journal_mode=WAL) on every
# request caused the 4th+ connection in a process to hang indefinitely after
# prior WAL writes accumulated.  Tracking per-path (rather than a single bool)
# lets tests use independent temporary databases without skipping schema init.
_db_initialized: set[str] = set()
_db_init_lock = threading.Lock()

# Cache for slow stats queries (avg completeness) to prevent DB locking
_stats_completeness_cache = {
    "last_update": 0.0,
    "avg_done": 0.0,
    "avg_pending": 0.0,
    "enriched_success": 0,
}
_stats_cache_lock = threading.Lock()



# ---------------------------------------------------------------------------
# FTS5 trigger DDL — stored as a module-level constant so upsert_many can
# recreate the triggers after dropping them for bulk-insert optimisation.
# ---------------------------------------------------------------------------

_FTS_IDENTIFIERS_EXPR = "IFNULL(new.doi,'') || ' ' || IFNULL(new.url,'') || ' ' || IFNULL(new.isbn,'') || ' ' || IFNULL(new.pmid,'') || ' ' || IFNULL(new.arxiv_id,'') || ' ' || IFNULL(new.publisher,'')"

_FTS_TRIGGER_DDL = f"""
    CREATE TRIGGER IF NOT EXISTS refs_ai AFTER INSERT ON refs BEGIN
        INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal, identifiers)
        VALUES (
            new.id, new.title, new.abstract,
            (SELECT group_concat(json_extract(value, '$.family') || ' ' ||
                                  IFNULL(json_extract(value, '$.given'), ''), ' ')
             FROM json_each(IFNULL(new.authors, '[]'))),
            new.keywords,
            new.journal,
            {_FTS_IDENTIFIERS_EXPR}
        );
    END;

    CREATE TRIGGER IF NOT EXISTS refs_au AFTER UPDATE ON refs BEGIN
        DELETE FROM refs_fts WHERE ref_id = old.id;
        INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal, identifiers)
        VALUES (
            new.id, new.title, new.abstract,
            (SELECT group_concat(json_extract(value, '$.family') || ' ' ||
                                  IFNULL(json_extract(value, '$.given'), ''), ' ')
             FROM json_each(IFNULL(new.authors, '[]'))),
            new.keywords,
            new.journal,
            {_FTS_IDENTIFIERS_EXPR}
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
        INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal, identifiers)
        SELECT
            id, title, abstract,
            (SELECT group_concat(
                json_extract(value, '$.family') || ' ' ||
                IFNULL(json_extract(value, '$.given'), ''), ' ')
             FROM json_each(IFNULL(authors, '[]'))),
            keywords,
            journal,
            IFNULL(doi,'') || ' ' || IFNULL(url,'') || ' ' || IFNULL(isbn,'') || ' ' || IFNULL(pmid,'') || ' ' || IFNULL(arxiv_id,'') || ' ' || IFNULL(publisher,'')
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
    """Generate a stable, deterministic ID for a reference.

    Uses the strongest available identifier.  For title-based fallback,
    includes ALL authors + URL to minimise false collisions between
    papers by the same first author in the same year.
    """
    if ref.doi:
        key = "doi:" + ref.doi.lower().strip()
    elif ref.arxiv_id:
        key = "arxiv:" + ref.arxiv_id.lower().strip()
    elif ref.pmid:
        key = "pmid:" + ref.pmid.strip()
    elif ref.isbn:
        key = "isbn:" + re.sub(r"[-\s]", "", ref.isbn)
    else:
        # Title + year + ALL authors + URL for maximum discrimination
        title = re.sub(r"\s+", " ", (ref.title or "").lower().strip())
        year  = str(ref.year or "")
        def _auth_key(a):
            if isinstance(a, dict):
                return (a.get("family", "") or "").lower() + "," + (a.get("given", "") or "").lower()
            return (a.family or "").lower() + "," + (a.given or "").lower()
        auths = "|".join(_auth_key(a) for a in ref.authors) if ref.authors else ""
        url   = (ref.url or "").lower().strip()
        key   = f"title:{title}:{year}:{auths}:{url}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]





def _authors_json(authors: List[Author]) -> str:
    def _to_dict(a):
        if isinstance(a, dict):
            return {"family": a.get("family", ""), "given": a.get("given", ""),
                    "orcid": a.get("orcid", ""), "affiliation": a.get("affiliation", "")}
        return {"family": a.family, "given": a.given,
                "orcid": a.orcid, "affiliation": a.affiliation}
    return json.dumps([_to_dict(a) for a in authors])


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
        "ref_type":       ref.ref_type.value if hasattr(ref.ref_type, 'value') else str(ref.ref_type),
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
        "pdf_path":        ref.pdf_path,
        "extras":          json.dumps(ref.extras) if getattr(ref, "extras", None) else None,
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
        pdf_path        = _row_col(row, "pdf_path"),
        sources         = _safe_json_dict(row["sources"]),
        cite_key        = row["cite_key"],
        extras          = _safe_json_dict(_row_col(row, "extras") or ""),
    )
    # Carry the canonical stored primary key so downstream code updates the
    # RIGHT row. Without this, code that recomputes an id from content
    # (_ref_id) diverges from refs.id for any ref whose content changed after
    # insert (e.g. gained a DOI via enrichment) — ~52% of refs — causing
    # pdf_local/tag/status writes to silently miss. _db_id is authoritative.
    try:
        ref._db_id = row["id"]
    except (KeyError, IndexError):
        pass
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
    CREATE INDEX IF NOT EXISTS idx_journal    ON refs (journal);
    CREATE INDEX IF NOT EXISTS idx_publisher  ON refs (publisher);
    CREATE INDEX IF NOT EXISTS idx_language   ON refs (language);

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
        identifiers,
        tokenize = 'porter unicode61'
    );

    CREATE TRIGGER IF NOT EXISTS refs_ai AFTER INSERT ON refs BEGIN
        INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal, identifiers)
        VALUES (
            new.id, new.title, new.abstract,
            (SELECT group_concat(json_extract(value, '$.family') || ' ' ||
                                  IFNULL(json_extract(value, '$.given'), ''), ' ')
             FROM json_each(IFNULL(new.authors, '[]'))),
            new.keywords,
            new.journal,
            IFNULL(new.doi,'') || ' ' || IFNULL(new.url,'') || ' ' || IFNULL(new.isbn,'') || ' ' || IFNULL(new.pmid,'') || ' ' || IFNULL(new.arxiv_id,'') || ' ' || IFNULL(new.publisher,'')
        );
    END;

    CREATE TRIGGER IF NOT EXISTS refs_au AFTER UPDATE ON refs BEGIN
        DELETE FROM refs_fts WHERE ref_id = old.id;
        INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal, identifiers)
        VALUES (
            new.id, new.title, new.abstract,
            (SELECT group_concat(json_extract(value, '$.family') || ' ' ||
                                  IFNULL(json_extract(value, '$.given'), ''), ' ')
             FROM json_each(IFNULL(new.authors, '[]'))),
            new.keywords,
            new.journal,
            IFNULL(new.doi,'') || ' ' || IFNULL(new.url,'') || ' ' || IFNULL(new.isbn,'') || ' ' || IFNULL(new.pmid,'') || ' ' || IFNULL(new.arxiv_id,'') || ' ' || IFNULL(new.publisher,'')
        );
    END;

    CREATE TRIGGER IF NOT EXISTS refs_ad AFTER DELETE ON refs BEGIN
        DELETE FROM refs_fts WHERE ref_id = old.id;
    END;

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS enrich_queue (
        ref_id         TEXT PRIMARY KEY REFERENCES refs(id) ON DELETE CASCADE,
        priority       REAL    DEFAULT 0,
        difficulty     INTEGER DEFAULT 0,
        strategy_level INTEGER DEFAULT 0,
        attempts       INTEGER DEFAULT 0,
        last_attempt   TEXT,
        status         TEXT    DEFAULT 'pending',
        last_error     TEXT,
        completeness_before REAL DEFAULT 0,
        created_at     TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_eq_status_prio ON enrich_queue (status, priority DESC);

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
        "CREATE INDEX IF NOT EXISTS idx_ref_tags_tag ON ref_tags (tag_id)",
        "CREATE INDEX IF NOT EXISTS idx_ref_coll_coll ON ref_collections (collection_id)",
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
        "ALTER TABLE refs ADD COLUMN pdf_path         TEXT",   # relative path to local PDF
        "ALTER TABLE refs ADD COLUMN extras           TEXT",   # JSON dict for dynamic/unknown fields
        # Rebuild FTS with identifiers column (DOI, URL, ISBN, PMID, arXiv, publisher)
        "DROP TRIGGER IF EXISTS refs_ai",
        "DROP TRIGGER IF EXISTS refs_au",
        "DROP TRIGGER IF EXISTS refs_ad",
        "DROP TABLE IF EXISTS refs_fts",
        # Re-create FTS table with identifiers column
        """CREATE VIRTUAL TABLE IF NOT EXISTS refs_fts USING fts5 (
            ref_id   UNINDEXED,
            title,
            abstract,
            authors_text,
            keywords_text,
            journal,
            identifiers,
            tokenize = 'porter unicode61'
        )""",
        # Enrichment queue table (migration for existing DBs)
        """CREATE TABLE IF NOT EXISTS enrich_queue (
            ref_id         TEXT PRIMARY KEY REFERENCES refs(id) ON DELETE CASCADE,
            priority       REAL    DEFAULT 0,
            difficulty     INTEGER DEFAULT 0,
            strategy_level INTEGER DEFAULT 0,
            attempts       INTEGER DEFAULT 0,
            last_attempt   TEXT,
            status         TEXT    DEFAULT 'pending',
            last_error     TEXT,
            completeness_before REAL DEFAULT 0,
            created_at     TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_eq_status_prio ON enrich_queue (status, priority DESC)",
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
        _dbg(f"[db.open] connecting to {self._path}")
        self._conn = sqlite3.connect(str(self._path), timeout=30, isolation_level="IMMEDIATE")
        self._conn.row_factory = sqlite3.Row
        _dbg("[db.open] connected; setting busy_timeout")
        self._conn.execute("PRAGMA busy_timeout = 30000")  # 10 s max lock wait
        db_key = str(self._path)
        if db_key not in _db_initialized:
            _dbg("[db.open] running schema init")
            with _db_init_lock:
                if db_key not in _db_initialized:
                    # Run full schema + migrations once per process per path.
                    # Calling executescript() (which issues PRAGMA journal_mode=WAL)
                    # on every request caused the Nth connection to hang after
                    # prior WAL writes had accumulated.
                    _dbg("[db.open] executescript start")
                    self._conn.executescript(self._SCHEMA)
                    _dbg("[db.open] executescript done; running migrations")
                    # Track which migrations have been applied via settings table
                    try:
                        applied = int(self._conn.execute(
                            "SELECT value FROM settings WHERE key = 'migration_version'"
                        ).fetchone()[0])
                    except (TypeError, sqlite3.OperationalError):
                        applied = 0
                    for i, stmt in enumerate(self._MIGRATIONS):
                        if i < applied:
                            continue
                        try:
                            self._conn.execute(stmt)
                        except sqlite3.OperationalError:
                            pass
                    new_version = len(self._MIGRATIONS)
                    if new_version > applied:
                        self._conn.execute(
                            "INSERT OR REPLACE INTO settings (key, value) VALUES ('migration_version', ?)",
                            (str(new_version),)
                        )
                    # Re-create FTS triggers (may have been dropped by migration)
                    try:
                        self._conn.executescript(_FTS_TRIGGER_DDL)
                    except Exception:
                        pass
                    self._conn.commit()
                    # Force a full WAL checkpoint so subsequent connections don't
                    # inherit accumulated WAL pages that cause the 4th+ connection
                    # to hang indefinitely when mmap or other PRAGMAs touch the WAL.
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.OperationalError:
                        # Checkpoint may fail if another connection is active;
                        # a PASSIVE checkpoint is always safe.
                        try:
                            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                        except sqlite3.OperationalError:
                            pass
                    # If FTS table is empty but refs exist, rebuild FTS index
                    try:
                        fts_count = self._conn.execute("SELECT count(*) FROM refs_fts").fetchone()[0]
                        ref_count = self._conn.execute("SELECT count(*) FROM refs").fetchone()[0]
                        if ref_count > 0 and fts_count == 0:
                            _dbg(f"[db.open] rebuilding FTS index for {ref_count} refs…")
                            self._conn.execute("""
                                INSERT INTO refs_fts (ref_id, title, abstract, authors_text, keywords_text, journal, identifiers)
                                SELECT id, title, abstract,
                                    (SELECT group_concat(json_extract(value, '$.family') || ' ' ||
                                                          IFNULL(json_extract(value, '$.given'), ''), ' ')
                                     FROM json_each(IFNULL(refs.authors, '[]'))),
                                    keywords, journal,
                                    IFNULL(doi,'') || ' ' || IFNULL(url,'') || ' ' || IFNULL(isbn,'') || ' ' || IFNULL(pmid,'') || ' ' || IFNULL(arxiv_id,'') || ' ' || IFNULL(publisher,'')
                                FROM refs
                            """)
                            self._conn.commit()
                            _dbg("[db.open] FTS rebuild complete")
                    except Exception as e:
                        _dbg(f"[db.open] FTS rebuild failed: {e}")
                    _db_initialized.add(db_key)
                    _dbg("[db.open] schema init complete")
        # Per-connection performance settings (safe to re-apply each time).
        import os
        _low_mem = bool(os.environ.get("RENDER") or os.environ.get("LOW_MEMORY"))
        self._conn.execute(f"PRAGMA cache_size = {-8192 if _low_mem else -65536}")
        # mmap_size is intentionally omitted: setting it after WAL writes accumulate
        # causes the 4th+ connection to hang indefinitely (WAL shm deadlock).
        self._conn.execute("PRAGMA synchronous    = NORMAL")
        self._conn.execute("PRAGMA temp_store     = MEMORY")
        self._conn.execute("PRAGMA wal_autocheckpoint = 1000")
        _dbg("[db.open] done")

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        if self._conn:
            yield self._conn
        else:
            conn = sqlite3.connect(str(self._path), timeout=30, isolation_level="IMMEDIATE")
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout = 30000")
                import os
                _low_mem = bool(os.environ.get("RENDER") or os.environ.get("LOW_MEMORY"))
                conn.execute(f"PRAGMA cache_size = {-8192 if _low_mem else -65536}")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute("PRAGMA temp_store = MEMORY")
                yield conn
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
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

    def replace_ref(self, ref_id: str, ref: Reference, tags: Optional[List[str]] = None) -> str:
        """Update an existing reference in place, preserving its row id.

        Re-enrichment must improve the selected row rather than creating a new
        title/url-derived row when providers return different fallback metadata.
        If a newly discovered DOI already belongs to another row, keep this
        row's existing DOI to avoid violating the unique DOI constraint.
        """
        row = _ref_to_row(ref)
        row["id"] = ref_id

        with self._db() as conn:
            if row.get("doi"):
                existing = conn.execute(
                    "SELECT id FROM refs WHERE doi = ? AND id != ?",
                    (row["doi"], ref_id),
                ).fetchone()
                if existing:
                    current = conn.execute(
                        "SELECT doi FROM refs WHERE id = ?", (ref_id,),
                    ).fetchone()
                    row["doi"] = current["doi"] if current else None

            cols = list(row.keys())
            placeholders = ", ".join(f":{c}" for c in cols)
            updates = ", ".join(
                f"{c} = excluded.{c}"
                for c in cols
                if c not in ("id", "created_at")
            )
            conn.execute(
                f"""
                INSERT INTO refs ({', '.join(cols)})
                VALUES ({placeholders})
                ON CONFLICT(id) DO UPDATE SET {updates}
                """,
                row,
            )
            if tags:
                self._apply_tags(conn, ref_id, tags)

        return ref_id

    def merge_duplicate_refs(self, keep_id: str, drop_id: str) -> str:
        """Merge two duplicate rows and delete the weaker row.

        Bibliographic metadata is merged with the same net-positive merge engine
        used for enrichment. Tags, collections, integration IDs, PDF pointers,
        notes, and queue state are preserved where possible.
        """
        if keep_id == drop_id:
            raise ValueError("keep_id and drop_id must differ")

        from .merge import merge as merge_refs

        with self._db() as conn:
            keep_row = conn.execute("SELECT * FROM refs WHERE id = ?", (keep_id,)).fetchone()
            drop_row = conn.execute("SELECT * FROM refs WHERE id = ?", (drop_id,)).fetchone()
            if keep_row is None or drop_row is None:
                raise ValueError("Reference not found")

            keep = _row_to_ref(keep_row)
            drop = _row_to_ref(drop_row)
            # Give drop a confidence proportional to its completeness so
            # the merge engine correctly prefers the more complete entry
            # when resolving field conflicts (net-positive: never lose data).
            keep_comp = getattr(keep, 'completeness', 0) or 0
            drop_comp = getattr(drop, 'completeness', 0) or 0
            drop_cc = getattr(drop, 'citation_count', 0) or 0
            keep_cc = getattr(keep, 'citation_count', 0) or 0
            # If drop is more complete or has more citations, boost its
            # confidence so the merge engine will prefer its values.
            if drop_comp > keep_comp or drop_cc > keep_cc:
                drop_conf = 0.5
            else:
                drop_conf = 0.1
            merged = merge_refs(keep, [(drop, drop_conf)])
            row = _ref_to_row(merged)
            row["id"] = keep_id

            if row.get("doi"):
                conflict = conn.execute(
                    "SELECT id FROM refs WHERE doi = ? AND id NOT IN (?, ?)",
                    (row["doi"], keep_id, drop_id),
                ).fetchone()
                if conflict:
                    row["doi"] = keep_row["doi"]

            cols = list(row.keys())
            placeholders = ", ".join(f":{c}" for c in cols)
            updates = ", ".join(
                f"{c} = excluded.{c}"
                for c in cols
                if c not in ("id", "created_at")
            )
            conn.execute(
                f"""
                INSERT INTO refs ({', '.join(cols)})
                VALUES ({placeholders})
                ON CONFLICT(id) DO UPDATE SET {updates}
                """,
                row,
            )

            for col in ("notes", "pdf_local", "pdf_drive_id", "pdf_path",
                        "notion_page_id", "zotero_item_key"):
                if col in keep_row.keys() and col in drop_row.keys() and not keep_row[col] and drop_row[col]:
                    conn.execute(f"UPDATE refs SET {col} = ? WHERE id = ?", (drop_row[col], keep_id))

            if "status" in keep_row.keys() and "status" in drop_row.keys():
                rank = {"unread": 0, "reading": 1, "read": 2}
                keep_status = keep_row["status"] or "unread"
                drop_status = drop_row["status"] or "unread"
                if rank.get(drop_status, 0) > rank.get(keep_status, 0):
                    conn.execute("UPDATE refs SET status = ? WHERE id = ?", (drop_status, keep_id))

            conn.execute(
                "INSERT OR IGNORE INTO ref_tags (ref_id, tag_id) SELECT ?, tag_id FROM ref_tags WHERE ref_id = ?",
                (keep_id, drop_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO ref_collections (ref_id, collection_id) SELECT ?, collection_id FROM ref_collections WHERE ref_id = ?",
                (keep_id, drop_id),
            )

            drop_q = conn.execute("SELECT * FROM enrich_queue WHERE ref_id = ?", (drop_id,)).fetchone()
            if drop_q:
                conn.execute(
                    """INSERT INTO enrich_queue
                       (ref_id, priority, difficulty, strategy_level, attempts, last_attempt, status, last_error, completeness_before, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(ref_id) DO UPDATE SET
                         priority = MAX(priority, excluded.priority),
                         difficulty = MAX(difficulty, excluded.difficulty),
                         attempts = MAX(attempts, excluded.attempts),
                         last_error = COALESCE(enrich_queue.last_error, excluded.last_error)
                    """,
                    (keep_id, drop_q["priority"], drop_q["difficulty"], drop_q["strategy_level"],
                     drop_q["attempts"], drop_q["last_attempt"], drop_q["status"], drop_q["last_error"],
                     drop_q["completeness_before"], drop_q["created_at"]),
                )

            conn.execute("DELETE FROM refs WHERE id = ?", (drop_id,))

        return keep_id

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

            # --- Tags (batch) ---
            if tags_per_ref:
                batch_ids = [row["id"] for row in rows]
                # Pad tags_per_ref to match rows length
                padded = list(tags_per_ref) + [[] for _ in range(len(rows) - len(tags_per_ref))]
                self._apply_tags_batch(conn, batch_ids, padded)

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
                    (ref_id, title, abstract, authors_text, keywords_text, journal, identifiers)
                SELECT
                    id, title, abstract,
                    (SELECT group_concat(
                        json_extract(value, '$.family') || ' ' ||
                        IFNULL(json_extract(value, '$.given'), ''), ' ')
                     FROM json_each(IFNULL(authors, '[]'))),
                    keywords,
                    journal,
                    IFNULL(doi,'') || ' ' || IFNULL(url,'') || ' ' || IFNULL(isbn,'') || ' ' || IFNULL(pmid,'') || ' ' || IFNULL(arxiv_id,'') || ' ' || IFNULL(publisher,'')
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
        pdf_path: Optional[str] = None,
        extras: Optional[dict] = None,
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
        if pdf_path is not None:
            sets.append("pdf_path = :pdf_path")
            params["pdf_path"] = pdf_path
        if extras is not None:
            import json
            sets.append("extras = :extras")
            params["extras"] = json.dumps(extras)
        if sets:
            # Retry on transient "database is locked" — when the PDF engine and
            # the enrichment daemon write concurrently, a save can briefly lose
            # the write lock. Without this, the PDF download is silently dropped
            # and re-fetched forever. Retry with backoff so the save always lands.
            import time as _t
            sql = f"UPDATE refs SET {', '.join(sets)} WHERE id = :id"
            last_err = None
            for attempt in range(6):
                try:
                    with self._db() as conn:
                        conn.execute(sql, params)
                    return
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                        raise
                    last_err = e
                    _t.sleep(0.5 * (attempt + 1))  # 0.5,1,1.5,2,2.5s backoff
            _dbg(f"update_integration_ids gave up after lock retries: {last_err}")

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
        # Identifier and extra metadata edits
        doi: Optional[str] = None,
        url: Optional[str] = None,
        publisher: Optional[str] = None,
        issn: Optional[str] = None,
        isbn: Optional[str] = None,
        pmid: Optional[str] = None,
        arxiv_id: Optional[str] = None,
        language: Optional[str] = None,
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
        _add("doi",       doi)
        _add("url",       url)
        _add("publisher", publisher)
        _add("issn",      issn)
        _add("isbn",      isbn)
        _add("pmid",      pmid)
        _add("arxiv_id",  arxiv_id)
        _add("language",  language)

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
        limit: int = 10_000_000,
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

    def _apply_tags_batch(
        self,
        conn: sqlite3.Connection,
        ref_ids: List[str],
        tags_per_ref: List[List[str]],
        auto: bool = False,
    ) -> None:
        """Batch tag assignment — resolves all unique tags in one pass."""
        # Collect all unique tag names across every ref
        all_tags = set()
        for tags in tags_per_ref:
            for t in tags:
                all_tags.add(t.strip().lower())
        if not all_tags:
            return

        # Ensure all tags exist (batch insert, ignore existing)
        conn.executemany(
            "INSERT OR IGNORE INTO tags (name, auto) VALUES (?, ?)",
            [(t, int(auto)) for t in all_tags],
        )

        # Fetch all tag IDs in one query
        placeholders = ", ".join("?" * len(all_tags))
        cur = conn.execute(
            f"SELECT name, id FROM tags WHERE name IN ({placeholders})",
            list(all_tags),
        )
        tag_id_map = {row["name"]: row["id"] for row in cur.fetchall()}

        # Batch insert ref_tags
        ref_tag_rows = []
        for ref_id, tags in zip(ref_ids, tags_per_ref):
            for t in tags:
                tid = tag_id_map.get(t.strip().lower())
                if tid:
                    ref_tag_rows.append((ref_id, tid))
        if ref_tag_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO ref_tags (ref_id, tag_id) VALUES (?, ?)",
                ref_tag_rows,
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
                "SELECT pdf_local, pdf_drive_id, pdf_path, notion_page_id, zotero_item_key, "
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
                f"SELECT id, pdf_local, pdf_drive_id, pdf_path, notion_page_id, "
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
        limit: int = 10_000_000,
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
        limit: int = 10_000_000,
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
        self, threshold: float = 0.5, limit: int = 10_000_000
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

    # -------------------------------------------------------------------
    # Enrichment queue operations
    # -------------------------------------------------------------------

    def enqueue_refs(self, ref_ids: List[str], completeness_map: Optional[Dict[str, float]] = None) -> int:
        """Add refs to the enrichment queue.  Bulk-optimised."""
        if not ref_ids:
            return 0
        with self._db() as conn:
            # Batch-fetch ref metadata for priority calculation
            BATCH = 500
            added = 0
            for i in range(0, len(ref_ids), BATCH):
                batch = ref_ids[i:i + BATCH]
                ph = ",".join("?" * len(batch))
                rows = conn.execute(
                    f"SELECT id, doi, pmid, arxiv_id, isbn, url, title, completeness, year, authors FROM refs WHERE id IN ({ph})",
                    batch,
                ).fetchall()
                queue_rows = []
                for row in rows:
                    comp = row["completeness"] or 0.0
                    has_id = bool(row["doi"] or row["pmid"] or row["arxiv_id"] or row["isbn"])
                    has_url = bool(row["url"])
                    title = row["title"] or ""
                    # Title quality heuristic: skip very short or filename-like titles
                    title_len = len(title)
                    has_spaces = " " in title
                    looks_like_title = title_len > 10 and has_spaces
                    has_meta = bool(row["year"] or (row["authors"] and row["authors"] != "[]"))
                    # Priority: gap × ease × title_quality
                    gap  = max(0, 1.0 - comp)
                    if has_id:
                        ease = 10.0  # strong identifiers = fast wins
                        strategy_level = 0
                    elif has_url and looks_like_title:
                        ease = 3.0
                        strategy_level = 1
                    elif looks_like_title:
                        ease = 1.0
                        strategy_level = 2 if has_meta else 3
                    else:
                        ease = 0.05  # garbage title, no identifiers — bottom of pile
                        strategy_level = 4
                    prio = gap * ease
                    difficulty = 0 if has_id else (1 if looks_like_title else 3)
                    queue_rows.append((row["id"], round(prio, 3), difficulty, strategy_level, round(comp, 3)))

                conn.executemany(
                    """INSERT INTO enrich_queue (ref_id, priority, difficulty, strategy_level, completeness_before)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(ref_id) DO UPDATE SET
                         priority = CASE WHEN status = 'done' THEN excluded.priority ELSE MAX(priority, excluded.priority) END,
                         status   = CASE WHEN status = 'done' THEN 'pending' ELSE status END,
                         strategy_level = CASE WHEN status = 'done' THEN excluded.strategy_level ELSE strategy_level END,
                         attempts = CASE WHEN status = 'done' THEN 0 ELSE attempts END,
                         last_error = CASE WHEN status = 'done' THEN 'resurrected for deep repair' ELSE last_error END
                    """,
                    queue_rows,
                )
                added += len(queue_rows)
            return added

    def enqueue_incomplete(
        self,
        threshold: float = 0.8,
        skip_done: bool = True,
        min_tier: int = 1,
    ) -> int:
        """Queue refs below a completeness threshold.

        Args:
            threshold: Only enqueue refs with completeness below this value.
            skip_done: Exclude refs with 'done'/'failed' queue entries.
            min_tier: Only queue refs classified as this tier or higher.
                1 = all, 2 = skip T1(ID), 3 = skip T1+T2, 4 = T4+T5 only, etc.
                Uses the same tier classification as tier_breakdown().
        """
        tc = self.TIER_CASE_SQL
        tier_filter = ""
        if min_tier > 1:
            tier_filter = f" AND {tc} >= {min_tier}".format(tc=tc, min_tier=min_tier)

        with self._db() as conn:
            if skip_done:
                rows = conn.execute(
                    "SELECT id FROM refs WHERE completeness < ?"
                    " AND id NOT IN "
                    "(SELECT ref_id FROM enrich_queue"
                    " WHERE status IN ('pending','active','done','failed'))"
                    + tier_filter,
                    (threshold,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM refs WHERE completeness < ?"
                    " AND id NOT IN "
                    "(SELECT ref_id FROM enrich_queue"
                    " WHERE status IN ('pending','active'))"
                    + tier_filter,
                    (threshold,),
                ).fetchall()
            ref_ids = [r["id"] for r in rows]
        return self.enqueue_refs(ref_ids)


    def dequeue_batch(self, batch_size: int = 20) -> List[Dict[str, Any]]:
        """
        Get the next batch of refs to enrich, ordered by priority (highest first).
        Marks them as 'active'.
        """
        with self._db() as conn:
            rows = conn.execute(
                """SELECT eq.ref_id, eq.priority, eq.difficulty, eq.strategy_level,
                          eq.attempts, r.doi, r.pmid, r.arxiv_id, r.isbn,
                          r.title, r.completeness
                   FROM enrich_queue eq
                   JOIN refs r ON r.id = eq.ref_id
                   WHERE eq.status = 'pending'
                   ORDER BY eq.priority DESC
                   LIMIT ?""",
                (batch_size,),
            ).fetchall()
            ids = [r["ref_id"] for r in rows]
            if ids:
                ph = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE enrich_queue SET status = 'active', last_attempt = datetime('now') WHERE ref_id IN ({ph})",
                    ids,
                )
            return [dict(r) for r in rows]

    def complete_enrich(self, ref_id: str, new_completeness: float, error: Optional[str] = None) -> None:
        """Mark a queued ref as done or update its difficulty/priority for retry.

        Completion rules:
        - Improved by >5% OR reached ≥80% → done
        - Exhausted all strategy levels (L4) with no improvement → done (accept current state)
        - Too many failures (≥5 with errors) → failed
        - Otherwise → re-queue with escalated strategy
        """
        with self._db() as conn:
            row = conn.execute(
                "SELECT attempts, strategy_level, completeness_before FROM enrich_queue WHERE ref_id = ?",
                (ref_id,),
            ).fetchone()
            if not row:
                return
            if error and error.startswith("cooldown"):
                # Skipped due to provider cooldown — do not escalate or increment attempts
                conn.execute(
                    "UPDATE enrich_queue SET status = 'pending', last_error = ? WHERE ref_id = ?",
                    (error, ref_id),
                )
                return
            attempts = row["attempts"] + 1
            old_comp = row["completeness_before"]
            level = row["strategy_level"]
            improved = new_completeness > old_comp + 0.05  # meaningful improvement

            if improved or new_completeness >= 0.8:
                # Success — mark done
                conn.execute(
                    "UPDATE enrich_queue SET status = 'done', attempts = ?, last_error = NULL WHERE ref_id = ?",
                    (attempts, ref_id),
                )
            elif level >= 4 and not improved:
                # Exhausted the current strategy ladder with no improvement.
                # Keep it eligible for future resolver/web/provider upgrades,
                # but lower its priority so the daemon does not spin on hard
                # tail refs while easier recoveries are still pending.
                if attempts >= 8:
                    conn.execute(
                        "UPDATE enrich_queue SET status = 'failed', attempts = ?, last_error = ? WHERE ref_id = ?",
                        (attempts, error or "exhausted level 4 attempts", ref_id),
                    )
                else:
                    gap = max(0, 1.0 - new_completeness)
                    prio = max(0.005, min(0.05, gap / max(attempts, 1) / 10))
                    conn.execute(
                        """UPDATE enrich_queue SET
                             status = 'pending', attempts = ?,
                             strategy_level = 4, difficulty = 3,
                             priority = ?, last_error = ?
                           WHERE ref_id = ?""",
                        (attempts, round(prio, 3), error or "deferred: exhausted current strategies", ref_id),
                    )
            elif error and attempts >= 5:
                # Too many hard failures — park it
                conn.execute(
                    "UPDATE enrich_queue SET status = 'failed', attempts = ?, last_error = ? WHERE ref_id = ?",
                    (attempts, error, ref_id),
                )
            else:
                # No improvement — escalate strategy, reduce priority, re-queue
                new_level = min(level + 1, 4)
                new_diff  = min(new_level, 3)
                gap       = max(0, 1.0 - new_completeness)
                ease      = max(0.1, 1.0 / (1 + attempts))
                prio      = gap * ease
                conn.execute(
                    """UPDATE enrich_queue SET
                         status = 'pending', attempts = ?, strategy_level = ?,
                         difficulty = ?, priority = ?, last_error = ?
                       WHERE ref_id = ?""",
                    (attempts, new_level, new_diff, round(prio, 3), error, ref_id),
                )

    def enrich_queue_stats(self) -> Dict[str, Any]:
        """Return aggregate stats about the enrichment queue."""
        import time
        with self._db() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM enrich_queue GROUP BY status"
            ).fetchall()
            stats = {r["status"]: r["cnt"] for r in rows}
            total = sum(stats.values())
            active_rows = conn.execute(
                """SELECT eq.ref_id, r.title, eq.strategy_level, eq.attempts
                   FROM enrich_queue eq JOIN refs r ON r.id = eq.ref_id
                   WHERE eq.status = 'active'
                   ORDER BY eq.priority DESC LIMIT 20""",
            ).fetchall()
            # Total enrichment attempts across all items (shows work even when items cycle)
            attempted_row = conn.execute(
                "SELECT COALESCE(SUM(attempts), 0) as total_attempts FROM enrich_queue"
            ).fetchone()

            # LIVE momentum (NOT cached — must be fresh to answer "is it working
            # right now?"). touched_10min = rows the daemon dequeued in the last
            # 10 min; enriched_1h = rows finished in the last hour whose
            # completeness actually rose. These are the real "is it moving" signal.
            try:
                touched_10min = conn.execute(
                    "SELECT COUNT(*) FROM enrich_queue "
                    "WHERE last_attempt > datetime('now','-10 minutes')"
                ).fetchone()[0]
                enriched_1h = conn.execute(
                    "SELECT COUNT(*) FROM enrich_queue eq JOIN refs r ON r.id = eq.ref_id "
                    "WHERE eq.status='done' AND eq.last_attempt > datetime('now','-60 minutes') "
                    "AND r.completeness > COALESCE(eq.completeness_before, 0) + 0.05"
                ).fetchone()[0]
            except Exception:
                touched_10min = enriched_1h = 0
            
            # Average completeness of done items vs pending (cached for 5 minutes)
            now = time.time()
            with _stats_cache_lock:
                if now - _stats_completeness_cache["last_update"] > 300.0:
                    try:
                        avg_row = conn.execute("""
                            SELECT
                                COALESCE(AVG(CASE WHEN eq.status='done' THEN r.completeness END), 0) as avg_done,
                                COALESCE(AVG(CASE WHEN eq.status='pending' THEN r.completeness END), 0) as avg_pending,
                                -- "Successfully enriched": done refs whose completeness
                                -- actually rose by >5% vs when they were queued. This
                                -- EXCLUDES no-match give-ups and already-complete refs,
                                -- so it reflects real enrichment work (unlike raw 'done').
                                COALESCE(SUM(CASE WHEN eq.status='done'
                                    AND r.completeness > COALESCE(eq.completeness_before, 0) + 0.05
                                    THEN 1 ELSE 0 END), 0) as enriched_success
                            FROM enrich_queue eq JOIN refs r ON r.id = eq.ref_id
                        """).fetchone()
                        _stats_completeness_cache["avg_done"] = avg_row["avg_done"]
                        _stats_completeness_cache["avg_pending"] = avg_row["avg_pending"]
                        _stats_completeness_cache["enriched_success"] = avg_row["enriched_success"]
                        _stats_completeness_cache["last_update"] = now
                    except Exception as e:
                        _dbg(f"Error calculating stats: {e}")
                avg_done = _stats_completeness_cache["avg_done"]
                avg_pending = _stats_completeness_cache["avg_pending"]
                enriched_success = _stats_completeness_cache["enriched_success"]

            return {
                "pending": stats.get("pending", 0),
                "active": stats.get("active", 0),
                "done": stats.get("done", 0),
                "failed": stats.get("failed", 0),
                "total": total,
                "total_attempts": attempted_row["total_attempts"],
                "enriched_success": enriched_success,
                "touched_10min": touched_10min,
                "enriched_1h": enriched_1h,
                "avg_done_completeness": round(avg_done, 2),
                "avg_pending_completeness": round(avg_pending, 2),
                "active_items": [
                    {"ref_id": r["ref_id"], "title": r["title"],
                     "level": r["strategy_level"], "attempts": r["attempts"]}
                    for r in active_rows
                ],
            }

    # Tier classification SQL — single source of truth
    TIER_CASE_SQL = """(CASE WHEN COALESCE(doi,'')!='' OR COALESCE(pmid,'')!='' OR COALESCE(arxiv_id,'')!='' OR COALESCE(isbn,'')!='' THEN 1 WHEN COALESCE(url,'')!='' OR COALESCE(oa_url,'')!='' THEN 2 WHEN COALESCE(title,'')!='' AND (COALESCE(year,'')!='' OR COALESCE(journal,'')!='' OR COALESCE(container_title,'')!='') THEN 3 WHEN COALESCE(title,'')!='' THEN 4 ELSE 5 END)"""

    def tier_breakdown(self) -> dict:
        """Per-tier counts for the library AND the enrichment queue."""
        tc = self.TIER_CASE_SQL
        with self._db() as conn:
            lib_rows = conn.execute(f"""
                SELECT {tc} as tier, COUNT(*) as cnt
                FROM refs GROUP BY tier
            """.format(tc=tc)).fetchall()
            q_rows = conn.execute(f"""
                SELECT {tc} as tier, COUNT(*) as cnt
                FROM enrich_queue eq
                JOIN refs r ON r.id = eq.ref_id
                WHERE eq.status = 'pending'
                GROUP BY tier
            """.format(tc=tc)).fetchall()
        lib   = {row["tier"]: row["cnt"] for row in lib_rows}
        queue = {row["tier"]: row["cnt"] for row in q_rows}
        return {"library": lib, "queue_pending": queue}


    def clear_queue(self) -> int:
        """Delete ALL entries from enrich_queue (pending, active, done, failed).
        Use this to completely reset the enrichment queue.
        Returns number of rows deleted.
        """
        with self._db() as conn:
            cur = conn.execute("DELETE FROM enrich_queue")
            return cur.rowcount

    def skip_pending_below_level(self, max_strategy_level: int) -> int:
        """Mark all 'pending' queue entries with strategy_level < max_strategy_level
        as 'done' with a note, so the daemon skips them.

        Use this to fast-forward past already-processed low tiers (L0-L2) and
        focus the daemon on the harder entries (L3+).

        Returns the number of rows updated.
        """
        with self._db() as conn:
            cur = conn.execute(
                """UPDATE enrich_queue
                   SET status = 'done', last_error = 'skipped: below focus level'
                   WHERE status = 'pending'
                   AND strategy_level < ?""",
                (max_strategy_level,),
            )
            return cur.rowcount

    def reset_stale_active(self, max_age_minutes: int = 10) -> int:
        """Reset 'active' items older than max_age_minutes back to 'pending' (crash recovery)."""
        with self._db() as conn:
            cur = conn.execute(
                """UPDATE enrich_queue SET status = 'pending'
                   WHERE status = 'active'
                   AND last_attempt < datetime('now', ?)""",
                (f"-{max_age_minutes} minutes",),
            )
            return cur.rowcount


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
