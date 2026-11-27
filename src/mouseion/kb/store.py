"""Knowledge-base store: a separate SQLite file (kb.db) next to the library (refs.db).

refs.db is only ever opened read-only from here, so nothing in the knowledge base can damage the library.
kb.db holds three layers, each exact or provenance-bound (no LLM-extracted triples):

  chunks     page-level full-text passages of local PDFs, with an FTS5 (BM25) index      -> citable passages
  works/cites  OpenAlex work ids per reference and the reference list of each work       -> exact citation graph
  notes      claims written by people or agents, each REQUIRED to cite a ref (and ideally a passage), with
             project, status and supersession                                            -> the growing, auditable KB
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

DEFAULT_DIR = Path.home() / ".local" / "share" / "mouseion"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_files (
    ref_id   TEXT PRIMARY KEY,
    path     TEXT NOT NULL,
    size     INTEGER, mtime REAL,
    n_pages  INTEGER, n_chunks INTEGER,
    status   TEXT,                       -- ok | missing | empty | error:<msg>
    indexed_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_id  TEXT NOT NULL,
    page    INTEGER NOT NULL,            -- 1-based page of the PDF
    ord     INTEGER NOT NULL,            -- chunk order within the document
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_ref ON chunks (ref_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5 (
    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS works (
    ref_id      TEXT PRIMARY KEY,
    openalex_id TEXT,                    -- e.g. W2741809807
    status      TEXT,                    -- ok | not_found | no_doi | error:<msg>
    fetched_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_works_oa ON works (openalex_id);
CREATE TABLE IF NOT EXISTS cites (
    src_ref    TEXT NOT NULL,            -- local reference that cites
    dst_work   TEXT NOT NULL,            -- OpenAlex id of the cited work (may be outside the library)
    PRIMARY KEY (src_ref, dst_work)
);
CREATE INDEX IF NOT EXISTS idx_cites_dst ON cites (dst_work);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,           -- e.g. semplex, synesism
    claim       TEXT NOT NULL,
    ref_id      TEXT NOT NULL,           -- provenance is mandatory
    chunk_id    INTEGER,                 -- the passage that supports the claim, if any
    quote       TEXT,                    -- verbatim supporting text (checked against the chunk when given)
    kind        TEXT DEFAULT 'finding',  -- finding | method | definition | critique | open-question
    status      TEXT DEFAULT 'active',   -- active | superseded | retracted
    superseded_by INTEGER,
    author      TEXT,                    -- who wrote the note (person or agent session)
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notes_ref ON notes (ref_id);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5 (
    claim, quote, content='notes', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, claim, quote) VALUES (new.id, new.claim, coalesce(new.quote, ''));
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE OF claim, quote ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, claim, quote) VALUES ('delete', old.id, old.claim, coalesce(old.quote, ''));
    INSERT INTO notes_fts(rowid, claim, quote) VALUES (new.id, new.claim, coalesce(new.quote, ''));
END;
"""


def library_path(path: Optional[str] = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get("MOUSEION_DB_PATH")
    if env:
        return Path(env)
    try:
        from mouseion.config import get_config

        return Path(get_config().db_path)
    except Exception:
        return DEFAULT_DIR / "refs.db"


def kb_path(path: Optional[str] = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get("MOUSEION_KB_PATH")
    return Path(env) if env else library_path().with_name("kb.db")


def open_library(path: Optional[str] = None) -> sqlite3.Connection:
    """read-only connection to refs.db"""
    p = library_path(path)
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def open_kb(path: Optional[str] = None) -> sqlite3.Connection:
    p = kb_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")  # the citation builder and the full-text builder take turns
    # WAL + NORMAL: no corruption on power loss, at most the last commits roll back (kb.db is derived and rebuilds
    # incrementally); without it the per-file commits of build-fulltext cost ~40% of the run in fsyncs
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def fts_query(text: str) -> str:
    """turn free text into a safe FTS5 query: quoted terms joined by OR (BM25 ranks documents matching more terms)"""
    import re

    terms = [t for t in re.findall(r"[\w\-]+", text.lower()) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in terms[:32]) or '""'
