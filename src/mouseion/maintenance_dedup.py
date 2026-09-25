"""Bulk deduplication maintenance routines.

The UI merge path is intentionally careful and row-oriented. This module is
for large maintenance passes: set-based safe merges with reports.

Nothing is destroyed (2026-09-25): a merged-away row moves to the
``refs_duplicates`` archive (with ``duplicate_of`` = the row it merged into)
and every keeper's pre-merge row is copied to ``refs_dedup_keep_bak``, so any
merge can be undone in SQL -- no 1 GB database copy per run. Every identifier
merge must also pass a title-agreement veto: book chapters share their book's
ISBN and URL ("Scientific Realism" + Boyd's and McMullin's chapters), and a
wrongly assigned DOI must not fuse two works.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


FILL_COLS = [
    "doi", "pmid", "pmcid", "arxiv_id", "isbn", "issn", "oa_url", "authors",
    "month", "abstract", "ref_type", "journal", "journal_abbrev", "volume",
    "issue", "pages", "publisher", "place", "edition", "series", "keywords",
    "language", "sources", "cite_key", "pdf_local", "pdf_drive_id",
    "notion_page_id", "zotero_item_key", "notes", "status", "eissn",
    "container_title", "article_number", "event_name", "editors", "num_pages",
    "license", "pdf_path", "extras",
]


def create_restore_point(label: str, include_program: bool = False) -> Path:
    from .config import get_config

    db_path = Path(get_config().db_path).expanduser()
    root = db_path.parent / "restore-points"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rp = root / f"{label}-{stamp}"
    (rp / "database").mkdir(parents=True, exist_ok=True)

    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.exists():
            shutil.copy2(src, rp / "database" / src.name)

    if include_program:
        (rp / "program").mkdir(exist_ok=True)
        try:
            src_root = Path(__file__).resolve().parents[2]
            for rel in (
                "src/mouseion/db.py",
                "src/mouseion/web.py",
                "src/mouseion/enrich_daemon.py",
                "src/mouseion/maintenance_dedup.py",
            ):
                p = src_root / rel
                if p.exists():
                    shutil.copy2(p, rp / "program" / p.name)
        except Exception:
            pass

    (rp / "RESTORE_POINT.txt").write_text(
        f"Mouseion restore point '{label}' created {stamp}\nDatabase: {db_path}\n",
        encoding="utf-8",
    )
    return rp


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0] or 0)


def _recreate_fts_triggers(conn: sqlite3.Connection) -> None:
    """Recreate FTS triggers without executescript's implicit transaction behavior."""
    from .db import _FTS_TRIGGER_DDL

    for stmt in re.findall(r"CREATE\s+TRIGGER\b.*?END;", _FTS_TRIGGER_DDL, flags=re.I | re.S):
        conn.execute(stmt)


_TAG = re.compile(r"<[^>]+>")


def _norm_title(t: Any) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", _TAG.sub(" ", str(t or "")))
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _type_family(t: Any) -> str:
    t = str(t or "").lower()
    if t in ("book", "monograph", "edited-book", "reference-book"):
        return "book"
    if t in ("book-chapter", "chapter", "book-section", "reference-entry"):
        return "chapter"
    return "other"


def _veto_pairs(conn: sqlite3.Connection, strict: bool) -> int:
    """Drop pairs whose titles disagree. Lenient (DOI/PMID/arXiv): shared words
    must dominate -- subtitles and casing differ between sources. Strict
    (ISBN/URL, which chapters share with their book): near-identical titles and
    the same kind of item."""
    try:
        from rapidfuzz import fuzz
        set_ratio, sort_ratio = fuzz.token_set_ratio, fuzz.token_sort_ratio
    except Exception:                                  # pragma: no cover - rapidfuzz ships with Mouseion
        from difflib import SequenceMatcher
        set_ratio = sort_ratio = lambda a, b: 100 * SequenceMatcher(None, a, b).ratio()  # noqa: E731
    rows = conn.execute(
        "SELECT p.keep_id, p.drop_id, k.title, d.title, k.ref_type, d.ref_type "
        "FROM pairs p JOIN refs k ON k.id = p.keep_id JOIN refs d ON d.id = p.drop_id").fetchall()
    bad = []
    for keep, drop, kt, dt, ktype, dtype in rows:
        a, b = _norm_title(kt), _norm_title(dt)
        if strict:
            ok = bool(a and b) and sort_ratio(a, b) >= 92 and _type_family(ktype) == _type_family(dtype)
        else:
            ok = not a or not b or set_ratio(a, b) >= 60 and sort_ratio(a, b) >= 45
        if not ok:
            bad.append((keep, drop))
    conn.executemany("DELETE FROM pairs WHERE keep_id = ? AND drop_id = ?", bad)
    return len(bad)


def _ensure_archive(conn: sqlite3.Connection) -> list[str]:
    """refs_duplicates mirrors refs (+ duplicate_of, archived_at, archive_rule);
    refs_dedup_keep_bak holds keepers' pre-merge rows. Returns refs' columns."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(refs)")]
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='refs_duplicates'").fetchone():
        conn.execute("CREATE TABLE refs_duplicates AS SELECT * FROM refs WHERE 0")
        for extra in ("duplicate_of", "archived_at", "archive_rule"):
            conn.execute(f"ALTER TABLE refs_duplicates ADD COLUMN {extra} TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS refs_duplicates_id ON refs_duplicates(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS refs_duplicates_of ON refs_duplicates(duplicate_of)")
    have = {r[1] for r in conn.execute("PRAGMA table_info(refs_duplicates)")}
    for c in cols:                                     # refs gained a column since: follow it
        if c not in have:
            conn.execute(f"ALTER TABLE refs_duplicates ADD COLUMN {c}")
    conn.execute("CREATE TABLE IF NOT EXISTS refs_dedup_keep_bak "
                 "(id TEXT, row_json TEXT, backed_up_at TEXT DEFAULT (datetime('now')))")
    return cols


def _archive_marked(conn: sqlite3.Connection) -> int:
    """Rows already flagged status='duplicate' (earlier repairs) join the archive."""
    cols = _ensure_archive(conn)
    n = _scalar(conn, "SELECT COUNT(*) FROM refs WHERE status = 'duplicate'")
    if n:
        cl = ", ".join(cols)
        conn.execute(f"INSERT INTO refs_duplicates ({cl}, duplicate_of, archived_at, archive_rule) "
                     f"SELECT {cl}, NULL, datetime('now'), 'marked-duplicate' FROM refs WHERE status = 'duplicate'")
        conn.execute("DELETE FROM enrich_queue WHERE ref_id IN (SELECT id FROM refs WHERE status = 'duplicate')")
        conn.execute("DELETE FROM refs_fts WHERE ref_id IN (SELECT id FROM refs WHERE status = 'duplicate')")
        conn.execute("DELETE FROM refs WHERE status = 'duplicate'")
    return n


def _run_url_title_year_batch(conn: sqlite3.Connection, max_merges: int) -> Dict[str, int]:
    """Merge exact URL + exact title + same year groups in one set-based batch."""
    conn.execute("DROP TABLE IF EXISTS temp.work")
    conn.execute("DROP TABLE IF EXISTS temp.pairs")
    conn.execute(
        """
        CREATE TEMP TABLE work AS
        SELECT
            id, lower(trim(url)) url_key, lower(trim(title)) title_key, year, created_at,
            (
                CASE WHEN doi IS NOT NULL AND trim(doi)<>'' THEN 60 ELSE 0 END +
                CASE WHEN title IS NOT NULL AND trim(title)<>'' THEN 20 ELSE 0 END +
                CASE WHEN authors IS NOT NULL AND trim(authors)<>'' AND authors<>'[]' THEN 15 ELSE 0 END +
                CASE WHEN year IS NOT NULL THEN 10 ELSE 0 END +
                COALESCE(completeness,0)*10
            ) quality
        FROM refs
        WHERE url IS NOT NULL AND trim(url)<>''
          AND title IS NOT NULL AND trim(title)<>''
          AND lower(trim(title)) NOT IN ('[no title]','no title','untitled')
          AND lower(title) NOT LIKE '%just a moment%'
          AND lower(title) NOT LIKE '%download limit exceeded%'
          AND year IS NOT NULL
        """
    )
    conn.execute(
        f"""
        CREATE TEMP TABLE pairs AS
        WITH ranked AS (
            SELECT
                id,
                first_value(id) OVER (
                    PARTITION BY url_key,title_key,year
                    ORDER BY quality DESC, created_at ASC, id ASC
                ) keep_id,
                row_number() OVER (
                    PARTITION BY url_key,title_key,year
                    ORDER BY quality DESC, created_at ASC, id ASC
                ) rn
            FROM work
        )
        SELECT keep_id, id AS drop_id FROM ranked WHERE rn > 1 LIMIT {int(max_merges)}
        """
    )
    return _apply_pairs(conn, rule="same_url_title_year")


def _run_url_batch(conn: sqlite3.Connection, max_merges: int) -> Dict[str, int]:
    """Merge exact normalized URL groups, guarded by strong-ID conflict checks."""
    conn.execute("DROP TABLE IF EXISTS temp.work")
    conn.execute("DROP TABLE IF EXISTS temp.pairs")
    conn.execute(
        """
        CREATE TEMP TABLE work AS
        SELECT
            id, lower(trim(url)) url_key, created_at,
            (
                CASE WHEN doi IS NOT NULL AND trim(doi)<>'' THEN 60 ELSE 0 END +
                CASE WHEN title IS NOT NULL AND trim(title)<>'' THEN 20 ELSE 0 END +
                CASE WHEN authors IS NOT NULL AND trim(authors)<>'' AND authors<>'[]' THEN 15 ELSE 0 END +
                CASE WHEN year IS NOT NULL THEN 10 ELSE 0 END +
                CASE WHEN journal IS NOT NULL AND trim(journal)<>'' THEN 5 ELSE 0 END +
                COALESCE(completeness,0)*10
            ) quality
        FROM refs
        WHERE url IS NOT NULL AND trim(url)<>''
          AND length(trim(url)) >= 16
          AND lower(trim(url)) NOT IN ('http://','https://')
          AND lower(trim(url)) NOT LIKE '%/search%'
          AND lower(trim(url)) NOT LIKE '%google.com/search%'
          AND lower(trim(url)) NOT LIKE '%scholar.google.%'
          AND lower(trim(url)) NOT LIKE '%zotero.org/%'
          AND lower(trim(url)) NOT LIKE '%localhost%'
        """
    )
    conn.execute(
        f"""
        CREATE TEMP TABLE pairs AS
        WITH ranked AS (
            SELECT
                id,
                first_value(id) OVER (
                    PARTITION BY url_key
                    ORDER BY quality DESC, created_at ASC, id ASC
                ) keep_id,
                row_number() OVER (
                    PARTITION BY url_key
                    ORDER BY quality DESC, created_at ASC, id ASC
                ) rn
            FROM work
        )
        SELECT keep_id, id AS drop_id FROM ranked WHERE rn > 1 LIMIT {int(max_merges)}
        """
    )
    vetoed = _veto_pairs(conn, strict=True)
    stats = _apply_pairs(conn, rule="same_url")
    stats["vetoed_title_mismatch"] = vetoed
    return stats


def _run_title_year_author_batch(conn: sqlite3.Connection, max_merges: int) -> Dict[str, int]:
    """Merge exact title + year + first-family groups in all mode."""
    conn.execute("DROP TABLE IF EXISTS temp.work")
    conn.execute("DROP TABLE IF EXISTS temp.pairs")
    conn.execute(
        """
        CREATE TEMP TABLE work AS
        SELECT
            id,
            lower(trim(title)) title_key,
            year,
            lower(trim(json_extract(authors, '$[0].family'))) first_family,
            created_at,
            (
                CASE WHEN doi IS NOT NULL AND trim(doi)<>'' THEN 60 ELSE 0 END +
                CASE WHEN title IS NOT NULL AND trim(title)<>'' THEN 20 ELSE 0 END +
                CASE WHEN authors IS NOT NULL AND trim(authors)<>'' AND authors<>'[]' THEN 15 ELSE 0 END +
                CASE WHEN year IS NOT NULL THEN 10 ELSE 0 END +
                CASE WHEN journal IS NOT NULL AND trim(journal)<>'' THEN 5 ELSE 0 END +
                COALESCE(completeness,0)*10
            ) quality
        FROM refs
        WHERE title IS NOT NULL AND trim(title)<>''
          AND lower(trim(title)) NOT IN ('[no title]','no title','untitled')
          AND lower(title) NOT LIKE '%just a moment%'
          AND lower(title) NOT LIKE '%download limit exceeded%'
          AND year IS NOT NULL
          AND authors IS NOT NULL AND trim(authors)<>'' AND authors<>'[]'
          AND json_extract(authors, '$[0].family') IS NOT NULL
          AND trim(json_extract(authors, '$[0].family')) <> ''
        """
    )
    conn.execute(
        f"""
        CREATE TEMP TABLE pairs AS
        WITH ranked AS (
            SELECT
                id,
                first_value(id) OVER (
                    PARTITION BY title_key,year,first_family
                    ORDER BY quality DESC, created_at ASC, id ASC
                ) keep_id,
                row_number() OVER (
                    PARTITION BY title_key,year,first_family
                    ORDER BY quality DESC, created_at ASC, id ASC
                ) rn
            FROM work
        )
        SELECT keep_id, id AS drop_id FROM ranked WHERE rn > 1 LIMIT {int(max_merges)}
        """
    )
    return _apply_pairs(conn, rule="same_title_year_first_author")


def _run_identifier_batch(conn: sqlite3.Connection, identifier: str, max_merges: int) -> Dict[str, int]:
    conn.execute("DROP TABLE IF EXISTS temp.work")
    conn.execute("DROP TABLE IF EXISTS temp.pairs")
    if identifier == "isbn":
        key_expr = "replace(replace(lower(trim(isbn)),'-',''),' ','')"
    else:
        key_expr = f"lower(trim({identifier}))"
    conn.execute(
        f"""
        CREATE TEMP TABLE work AS
        SELECT
            id, {key_expr} key, created_at,
            (
                CASE WHEN doi IS NOT NULL AND trim(doi)<>'' THEN 60 ELSE 0 END +
                CASE WHEN title IS NOT NULL AND trim(title)<>'' THEN 20 ELSE 0 END +
                CASE WHEN authors IS NOT NULL AND trim(authors)<>'' AND authors<>'[]' THEN 15 ELSE 0 END +
                CASE WHEN year IS NOT NULL THEN 10 ELSE 0 END +
                COALESCE(completeness,0)*10
            ) quality
        FROM refs
        WHERE {identifier} IS NOT NULL AND trim({identifier})<>''
        """
    )
    conn.execute(
        f"""
        CREATE TEMP TABLE pairs AS
        WITH ranked AS (
            SELECT
                id,
                first_value(id) OVER (PARTITION BY key ORDER BY quality DESC, created_at ASC, id ASC) keep_id,
                row_number() OVER (PARTITION BY key ORDER BY quality DESC, created_at ASC, id ASC) rn
            FROM work
        )
        SELECT keep_id, id AS drop_id FROM ranked WHERE rn > 1 LIMIT {int(max_merges)}
        """
    )
    vetoed = _veto_pairs(conn, strict=(identifier == "isbn"))
    stats = _apply_pairs(conn, rule=f"same_{identifier}")
    stats["vetoed_title_mismatch"] = vetoed
    return stats


def _json_dict(value: Any) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _nonnull_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _status_winner(values: list[Any]) -> Optional[str]:
    rank = {"unread": 0, "reading": 1, "read": 2}
    best = None
    best_rank = -1
    for value in values:
        value = _nonnull_text(value) or "unread"
        if rank.get(value, -1) > best_rank:
            best = value
            best_rank = rank.get(value, -1)
    return best


def _quality_from_row(row: sqlite3.Row) -> float:
    score = 0.45
    if _nonnull_text(row["doi"]):
        score += 0.20
    if _nonnull_text(row["title"]):
        score += 0.08
    if _nonnull_text(row["authors"]) and row["authors"] != "[]":
        score += 0.08
    if row["year"] is not None:
        score += 0.06
    if _nonnull_text(row["journal"]) or _nonnull_text(row["publisher"]) or _nonnull_text(row["container_title"]):
        score += 0.06
    score += min(float(row["completeness"] or 0.0), 1.0) * 0.20
    return min(score, 0.98)


def _build_consolidated_rows(conn: sqlite3.Connection) -> list[dict]:
    """Build net-positive keeper rows using Mouseion's normal merge engine."""
    from .db import _ref_to_row, _row_to_ref
    from .merge import merge as merge_refs

    keep_ids = [r[0] for r in conn.execute("SELECT DISTINCT keep_id FROM pairs").fetchall()]
    consolidated: list[dict] = []
    for keep_id in keep_ids:
        keep_row = conn.execute("SELECT * FROM refs WHERE id = ?", (keep_id,)).fetchone()
        if keep_row is None:
            continue
        drop_rows = conn.execute(
            "SELECT r.* FROM pairs p JOIN refs r ON r.id = p.drop_id WHERE p.keep_id = ?",
            (keep_id,),
        ).fetchall()
        if not drop_rows:
            continue

        keep_ref = _row_to_ref(keep_row)
        candidates = [(keep_ref, _quality_from_row(keep_row))]
        candidates.extend((_row_to_ref(row), _quality_from_row(row)) for row in drop_rows)
        merged = merge_refs(keep_ref, candidates)

        # Merge dynamic extras explicitly; the bibliography merge engine keeps
        # provenance but intentionally does not know every app-local extra key.
        extras = _json_dict(keep_row["extras"])
        for row in drop_rows:
            for key, value in _json_dict(row["extras"]).items():
                extras.setdefault(key, value)
        merged_from = extras.setdefault("dedup_merged_from", [])
        if isinstance(merged_from, list):
            for row in drop_rows:
                if row["id"] not in merged_from:
                    merged_from.append(row["id"])
        merged.extras = extras

        row = _ref_to_row(merged)
        row["id"] = keep_id
        row["created_at"] = min(
            [r["created_at"] for r in [keep_row, *drop_rows] if _nonnull_text(r["created_at"])],
            default=keep_row["created_at"],
        )
        row["updated_at"] = datetime.now().isoformat()

        # Preserve/upgrade app-local fields that the bibliographic merge does
        # not rank on its own.
        all_rows = [keep_row, *drop_rows]
        for col in ("notes", "pdf_local", "pdf_drive_id", "pdf_path",
                    "notion_page_id", "zotero_item_key", "cite_key"):
            values = [_nonnull_text(r[col]) for r in all_rows if col in r.keys()]
            nonempty = [v for v in values if v]
            if col == "notes" and nonempty:
                seen = set()
                notes = []
                for value in nonempty:
                    key = re.sub(r"\s+", " ", value).strip()
                    if key not in seen:
                        notes.append(value)
                        seen.add(key)
                row[col] = "\n\n--- merged duplicate note ---\n\n".join(notes)
            elif nonempty:
                row[col] = nonempty[0]
        if "status" in keep_row.keys():
            row["status"] = _status_winner([r["status"] for r in all_rows])

        consolidated.append(row)
    return consolidated


def _write_consolidated_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    written = 0
    for row in rows:
        cols = list(row.keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("id", "created_at"))
        conn.execute(
            f"""
            INSERT INTO refs ({', '.join(cols)})
            VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET {updates}
            """,
            row,
        )
        written += 1
    return written


def _apply_pairs(conn: sqlite3.Connection, rule: str = "") -> Dict[str, int]:
    selected = _scalar(conn, "SELECT COUNT(*) FROM pairs")
    if selected <= 0:
        return {"selected": 0, "merged": 0, "skipped_conflicts": 0}

    conn.execute("CREATE INDEX IF NOT EXISTS pairs_keep ON pairs(keep_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS pairs_drop ON pairs(drop_id)")
    for col in ("doi", "pmid", "pmcid", "arxiv_id", "isbn"):
        conn.execute(
            f"""
            DELETE FROM pairs
            WHERE EXISTS (
                SELECT 1
                FROM refs k
                JOIN refs d ON d.id = pairs.drop_id
                WHERE k.id = pairs.keep_id
                  AND k.{col} IS NOT NULL AND trim(k.{col}) <> ''
                  AND d.{col} IS NOT NULL AND trim(d.{col}) <> ''
                  AND lower(trim(k.{col})) <> lower(trim(d.{col}))
            )
            """
        )
    after_conflicts = _scalar(conn, "SELECT COUNT(*) FROM pairs")
    if after_conflicts <= 0:
        return {"selected": selected, "merged": 0, "skipped_conflicts": selected}
    cols = _ensure_archive(conn)
    conn.execute(
        "INSERT INTO refs_dedup_keep_bak (id, row_json) SELECT r.id, json_object("
        + ", ".join(f"'{c}', r.{c}" for c in cols)
        + ") FROM refs r WHERE r.id IN (SELECT DISTINCT keep_id FROM pairs)")
    consolidated_rows = _build_consolidated_rows(conn)

    for col in FILL_COLS:
        conn.execute(
            f"""
            UPDATE refs
            SET {col}=COALESCE((
                SELECT d.{col}
                FROM pairs p JOIN refs d ON d.id=p.drop_id
                WHERE p.keep_id=refs.id
                  AND d.{col} IS NOT NULL
                  AND trim(CAST(d.{col} AS TEXT))<>''
                LIMIT 1
            ), {col})
            WHERE id IN (SELECT keep_id FROM pairs)
              AND ({col} IS NULL OR trim(CAST({col} AS TEXT))='')
            """
        )
    conn.execute(
        """
        UPDATE refs
        SET citation_count=max(COALESCE(citation_count,0), COALESCE((
            SELECT MAX(d.citation_count) FROM pairs p JOIN refs d ON d.id=p.drop_id
            WHERE p.keep_id=refs.id),0))
        WHERE id IN (SELECT keep_id FROM pairs)
        """
    )
    conn.execute(
        """
        UPDATE refs
        SET completeness=max(COALESCE(completeness,0), COALESCE((
            SELECT MAX(d.completeness) FROM pairs p JOIN refs d ON d.id=p.drop_id
            WHERE p.keep_id=refs.id),0))
        WHERE id IN (SELECT keep_id FROM pairs)
        """
    )
    conn.execute(
        """
        UPDATE refs SET open_access=1
        WHERE id IN (SELECT keep_id FROM pairs)
          AND EXISTS (SELECT 1 FROM pairs p JOIN refs d ON d.id=p.drop_id
                      WHERE p.keep_id=refs.id AND d.open_access=1)
        """
    )
    conn.execute("UPDATE refs SET updated_at=datetime('now') WHERE id IN (SELECT keep_id FROM pairs)")
    conn.execute("INSERT OR IGNORE INTO ref_tags(ref_id,tag_id) SELECT p.keep_id, rt.tag_id FROM pairs p JOIN ref_tags rt ON rt.ref_id=p.drop_id")
    conn.execute("INSERT OR IGNORE INTO ref_collections(ref_id,collection_id) SELECT p.keep_id, rc.collection_id FROM pairs p JOIN ref_collections rc ON rc.ref_id=p.drop_id")
    conn.execute(
        """
        INSERT INTO enrich_queue
            (ref_id, priority, difficulty, strategy_level, attempts, last_attempt,
             status, last_error, completeness_before, created_at)
        SELECT
            p.keep_id,
            MAX(eq.priority),
            MAX(eq.difficulty),
            MIN(eq.strategy_level),
            MAX(eq.attempts),
            MAX(eq.last_attempt),
            CASE
              WHEN SUM(eq.status='pending') > 0 THEN 'pending'
              WHEN SUM(eq.status='active') > 0 THEN 'pending'
              WHEN SUM(eq.status='failed') > 0 THEN 'failed'
              ELSE 'done'
            END,
            MAX(eq.last_error),
            MIN(eq.completeness_before),
            MIN(eq.created_at)
        FROM pairs p
        JOIN enrich_queue eq ON eq.ref_id = p.drop_id
        GROUP BY p.keep_id
        ON CONFLICT(ref_id) DO UPDATE SET
            priority = MAX(priority, excluded.priority),
            difficulty = MAX(difficulty, excluded.difficulty),
            strategy_level = MIN(strategy_level, excluded.strategy_level),
            attempts = MAX(attempts, excluded.attempts),
            status = CASE
                WHEN enrich_queue.status IN ('pending','active') OR excluded.status IN ('pending','active') THEN 'pending'
                WHEN enrich_queue.status = 'failed' OR excluded.status = 'failed' THEN 'failed'
                ELSE enrich_queue.status
            END,
            last_error = COALESCE(enrich_queue.last_error, excluded.last_error)
        """
    )
    conn.execute("DELETE FROM enrich_queue WHERE ref_id IN (SELECT drop_id FROM pairs)")
    conn.execute("DELETE FROM ref_tags WHERE ref_id IN (SELECT drop_id FROM pairs)")
    conn.execute("DELETE FROM ref_collections WHERE ref_id IN (SELECT drop_id FROM pairs)")
    conn.execute("DELETE FROM refs_fts WHERE ref_id IN (SELECT drop_id FROM pairs) OR ref_id IN (SELECT keep_id FROM pairs)")
    cl = ", ".join(cols)
    conn.execute(
        f"INSERT INTO refs_duplicates ({cl}, duplicate_of, archived_at, archive_rule) "
        f"SELECT {', '.join('r.' + c for c in cols)}, p.keep_id, datetime('now'), ? "
        f"FROM refs r JOIN pairs p ON p.drop_id = r.id", (rule,))
    conn.execute("DELETE FROM refs WHERE id IN (SELECT drop_id FROM pairs)")   # archived above
    consolidated = _write_consolidated_rows(conn, consolidated_rows)
    conn.execute("DELETE FROM refs_fts WHERE ref_id IN (SELECT keep_id FROM pairs)")
    conn.execute(
        """
        INSERT INTO refs_fts (ref_id,title,abstract,authors_text,keywords_text,journal,identifiers)
        SELECT id,title,abstract,
               (SELECT group_concat(json_extract(value, '$.family') || ' ' ||
                       IFNULL(json_extract(value, '$.given'), ''), ' ')
                FROM json_each(IFNULL(refs.authors, '[]'))),
               keywords,journal,
               IFNULL(doi,'') || ' ' || IFNULL(url,'') || ' ' || IFNULL(isbn,'') || ' ' ||
               IFNULL(pmid,'') || ' ' || IFNULL(arxiv_id,'') || ' ' || IFNULL(publisher,'')
        FROM refs WHERE id IN (SELECT keep_id FROM pairs)
        """
    )
    return {
        "selected": selected,
        "merged": after_conflicts,
        "skipped_conflicts": selected - after_conflicts,
        "consolidated_keepers": consolidated,
    }


def run_dedup_all(
    db_path: Optional[Path | str] = None,
    max_merges: int = 50_000,
    mode: str = "safe",
    report_path: Optional[Path | str] = None,
    restore_point: bool = True,
) -> Dict[str, Any]:
    """Run all safe set-based dedup passes.

    ``mode='safe'`` merges exact identifier groups and exact URL/title/year
    groups only. ``mode='all'`` additionally merges exact URL groups and exact
    title/year/first-author groups, still skipping rows with conflicting strong
    identifiers.
    """
    from .config import get_config

    start = time.time()
    db_path = Path(db_path or get_config().db_path).expanduser()
    rp = create_restore_point("before-dedup-all", include_program=True) if restore_point else None
    report_path = Path(report_path) if report_path else ((rp or db_path.parent) / "dedup-all-report.json")

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=OFF")
    before = _scalar(conn, "SELECT COUNT(*) FROM refs")
    passes: list[dict] = []
    remaining = int(max_merges)

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DROP TRIGGER IF EXISTS refs_ad")
    conn.execute("DROP TRIGGER IF EXISTS refs_au")
    try:
        passes.append({"rule": "archive_marked_duplicates", "merged": _archive_marked(conn)})
        for ident in ("doi", "pmid", "pmcid", "arxiv_id", "isbn"):
            if remaining <= 0:
                break
            stats = _run_identifier_batch(conn, ident, remaining)
            stats["rule"] = f"same_{ident}"
            passes.append(stats)
            remaining -= stats["merged"]

        while remaining > 0:
            stats = _run_url_title_year_batch(conn, min(remaining, 10_000))
            stats["rule"] = "same_url_title_year"
            passes.append(stats)
            remaining -= stats["merged"]
            if stats["merged"] == 0:
                break

        if mode == "all":
            while remaining > 0:
                stats = _run_url_batch(conn, min(remaining, 10_000))
                stats["rule"] = "same_url"
                passes.append(stats)
                remaining -= stats["merged"]
                if stats["merged"] == 0:
                    break

            while remaining > 0:
                stats = _run_title_year_author_batch(conn, min(remaining, 10_000))
                stats["rule"] = "same_title_year_first_author"
                passes.append(stats)
                remaining -= stats["merged"]
                if stats["merged"] == 0:
                    break

        # A keeper merged away by a later pass leaves chains: point every
        # archived row at the record that survived.
        for _ in range(8):
            changed = conn.execute(
                """UPDATE refs_duplicates SET duplicate_of = (
                       SELECT d2.duplicate_of FROM refs_duplicates d2
                       WHERE d2.id = refs_duplicates.duplicate_of LIMIT 1)
                   WHERE duplicate_of IS NOT NULL
                     AND duplicate_of NOT IN (SELECT id FROM refs)
                     AND duplicate_of IN (SELECT id FROM refs_duplicates)""").rowcount
            if not changed:
                break
        # Recreate triggers using the canonical DB DDL.
        _recreate_fts_triggers(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    after = _scalar(conn, "SELECT COUNT(*) FROM refs")
    report = {
        "mode": mode,
        "restore_point": str(rp) if rp else None,
        "before_total_refs": before,
        "after_total_refs": after,
        "merged": before - after,
        "max_merges": max_merges,
        "passes": passes,
        "elapsed_seconds": round(time.time() - start, 2),
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    conn.close()
    return report
