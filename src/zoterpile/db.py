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
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from .models import Author, RefType, Reference


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


# lazy import re for isbn normalisation
import re


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
        "keywords":       json.dumps(ref.keywords),
        "language":       ref.language,
        "open_access":    int(ref.open_access) if ref.open_access is not None else None,
        "citation_count": ref.citation_count,
        "sources":        json.dumps(ref.sources),
        "completeness":   ref.completeness,
        "cite_key":       ref.cite_key or ref.auto_cite_key(),
        "updated_at":     _now(),
    }


def _row_to_ref(row: sqlite3.Row) -> Reference:
    ref = Reference(
        doi            = row["doi"],
        pmid           = row["pmid"],
        pmcid          = row["pmcid"],
        arxiv_id       = row["arxiv_id"],
        isbn           = row["isbn"],
        issn           = row["issn"],
        url            = row["url"],
        oa_url         = row["oa_url"],
        title          = row["title"] or None,
        authors        = _authors_from_json(row["authors"] or ""),
        year           = row["year"],
        month          = row["month"],
        abstract       = row["abstract"],
        ref_type       = RefType(row["ref_type"]) if row["ref_type"] else RefType.UNKNOWN,
        journal        = row["journal"],
        journal_abbrev = row["journal_abbrev"],
        volume         = row["volume"],
        issue          = row["issue"],
        pages          = row["pages"],
        publisher      = row["publisher"],
        place          = row["place"],
        edition        = row["edition"],
        series         = row["series"],
        keywords       = json.loads(row["keywords"] or "[]"),
        language       = row["language"],
        open_access    = bool(row["open_access"]) if row["open_access"] is not None else None,
        citation_count = row["citation_count"],
        sources        = json.loads(row["sources"] or "{}"),
        cite_key       = row["cite_key"],
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

    CREATE INDEX IF NOT EXISTS idx_doi      ON refs (doi)      WHERE doi IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_arxiv    ON refs (arxiv_id) WHERE arxiv_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_pmid     ON refs (pmid)     WHERE pmid IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_year     ON refs (year);
    CREATE INDEX IF NOT EXISTS idx_type     ON refs (ref_type);
    CREATE INDEX IF NOT EXISTS idx_complete ON refs (completeness);

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
    """

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

    def __exit__(self, *_) -> None:
        self.close()

    def open(self) -> None:
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

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
        On conflict (same DOI), updates all fields *except* created_at.
        """
        row = _ref_to_row(ref)
        ref_id = row["id"]

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
            ON CONFLICT(id)  DO UPDATE SET {updates}
            ON CONFLICT(doi) DO UPDATE SET {updates}
        """

        with self._db() as conn:
            # Ensure created_at is set on first insert
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
    ) -> List[str]:
        """Bulk upsert. Returns list of IDs in the same order."""
        ids = []
        with self._db() as conn:
            for i, ref in enumerate(refs):
                row = _ref_to_row(ref)
                ref_id = row["id"]
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
                    ON CONFLICT(id)  DO UPDATE SET {updates}
                    ON CONFLICT(doi) DO UPDATE SET {updates}
                """
                if "created_at" not in row:
                    row["created_at"] = _now()
                conn.execute(sql, row)

                if tags_per_ref and i < len(tags_per_ref) and tags_per_ref[i]:
                    self._apply_tags(conn, ref_id, tags_per_ref[i])

                ids.append(ref_id)
        return ids

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

    # -----------------------------------------------------------------------
    # Read operations
    # -----------------------------------------------------------------------

    def get(self, ref_id: str) -> Optional[Reference]:
        with self._db() as conn:
            cur = conn.execute("SELECT * FROM refs WHERE id = ?", (ref_id,))
            row = cur.fetchone()
            return _row_to_ref(row) if row else None

    def get_by_doi(self, doi: str) -> Optional[Reference]:
        with self._db() as conn:
            cur = conn.execute("SELECT * FROM refs WHERE doi = ?", (doi.strip(),))
            row = cur.fetchone()
            return _row_to_ref(row) if row else None

    def get_extra(self, ref_id: str) -> Dict[str, Any]:
        """Return integration/PDF fields not in the Reference model."""
        with self._db() as conn:
            cur = conn.execute(
                "SELECT pdf_local, pdf_drive_id, notion_page_id, zotero_item_key, "
                "created_at, updated_at FROM refs WHERE id = ?",
                (ref_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else {}

    def search(
        self,
        query: str,
        tags: Optional[List[str]] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        ref_type: Optional[str] = None,
        open_access_only: bool = False,
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
        if open_access_only:
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

        with self._db() as conn:
            try:
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                # FTS query might be malformed; fall back to LIKE
                return self._search_fallback(query, limit, offset)

        results = []
        for row in rows:
            ref   = _row_to_ref(row)
            score = abs(row["fts_rank"]) if "fts_rank" in row.keys() and row["fts_rank"] else 0.5
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
        limit: int = 1000,
        offset: int = 0,
    ) -> List[Reference]:
        return [r for r, _ in self.search(
            "", tags=tags, year_from=year_from, year_to=year_to,
            ref_type=ref_type, limit=limit, offset=offset,
        )]

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
