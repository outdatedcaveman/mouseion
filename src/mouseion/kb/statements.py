"""Typed mathematical statements: definitions, theorems, lemmas, ... found in the indexed passages.

Mathematical papers carry their own structure: "Definition 2.3 (Univalent category). A precategory is ...".
Indexing those units by kind makes "where is X defined?" and "which results are stated about X?" direct queries,
which plain passage search answers poorly. Detection is by pattern, so it costs nothing to build and is redone
incrementally (a reference is reprocessed only when its passages change).

Stored in kb.db: statements (+ statements_fts) and statements_done. Nothing else is touched.
"""

from __future__ import annotations

import re
from typing import List, Optional

KINDS = ("definition", "theorem", "lemma", "proposition", "corollary", "conjecture", "axiom", "hypothesis",
         "remark", "example", "construction", "notation", "problem", "question")

SCHEMA = """
CREATE TABLE IF NOT EXISTS statements (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_id   TEXT NOT NULL,
    chunk_id INTEGER NOT NULL,
    page     INTEGER,
    kind     TEXT NOT NULL,
    label    TEXT,                -- e.g. 2.3
    name     TEXT,                -- e.g. Univalent category
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_statements_ref ON statements (ref_id);
CREATE VIRTUAL TABLE IF NOT EXISTS statements_fts USING fts5 (
    name, text, content='statements', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS statements_ai AFTER INSERT ON statements BEGIN
    INSERT INTO statements_fts(rowid, name, text) VALUES (new.id, coalesce(new.name, ''), new.text);
END;
CREATE TRIGGER IF NOT EXISTS statements_ad AFTER DELETE ON statements BEGIN
    INSERT INTO statements_fts(statements_fts, rowid, name, text) VALUES ('delete', old.id, coalesce(old.name, ''), old.text);
END;
CREATE TABLE IF NOT EXISTS statements_done (
    ref_id   TEXT PRIMARY KEY,
    n_chunks INTEGER,
    max_chunk INTEGER
);
"""

_HEAD = re.compile(
    r"(?:(?<=\s)|^)(?P<kind>" + "|".join(k.capitalize() for k in KINDS) + r"|"
    + "|".join(k.upper() for k in KINDS) + r")"
    r"\s+(?P<label>(?:[A-Z]\.)?\d+(?:\.\d+){0,3}[a-z]?)?"
    r"\s*(?:\((?P<name>[^()]{2,80})\))?\s*[.:]\s+(?P<body>[A-Z(\\$a-z])",
)


def ensure(kb) -> None:
    kb.executescript(SCHEMA)


def extract(text: str, max_len: int = 700) -> List[dict]:
    """statement heads in a passage; a statement's text runs to the next head or `max_len` characters.
    A head must carry a number or a parenthesised name, so ordinary prose ("this theorem. It ...") is skipped."""
    heads = [m for m in _HEAD.finditer(text) if m.group("label") or m.group("name")]
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.start("body"): min(end, m.start("body") + max_len)].strip()
        if len(body) < 20:
            continue
        out.append({"kind": m.group("kind").lower(), "label": m.group("label"),
                    "name": (m.group("name") or "").strip() or None, "text": body})
    return out


def build(kb, limit: Optional[int] = None) -> dict:
    """index statements for references whose passages are new or changed since the last run."""
    ensure(kb)
    refs = kb.execute(
        """SELECT c.ref_id, count(*) AS n, max(c.id) AS mx FROM chunks c GROUP BY c.ref_id"""
    ).fetchall()
    done = {r["ref_id"]: (r["n_chunks"], r["max_chunk"]) for r in kb.execute("SELECT * FROM statements_done")}
    todo = [r for r in refs if done.get(r["ref_id"]) != (r["n"], r["mx"])]
    if limit:
        todo = todo[:limit]
    counts = {"references": 0, "statements": 0}
    for r in todo:
        kb.execute("DELETE FROM statements WHERE ref_id=?", (r["ref_id"],))
        for ch in kb.execute("SELECT id, page, text FROM chunks WHERE ref_id=? ORDER BY ord", (r["ref_id"],)):
            for s in extract(ch["text"]):
                kb.execute(
                    "INSERT INTO statements(ref_id, chunk_id, page, kind, label, name, text) VALUES (?,?,?,?,?,?,?)",
                    (r["ref_id"], ch["id"], ch["page"], s["kind"], s["label"], s["name"], s["text"]),
                )
                counts["statements"] += 1
        kb.execute("INSERT OR REPLACE INTO statements_done VALUES (?,?,?)", (r["ref_id"], r["n"], r["mx"]))
        counts["references"] += 1
        if counts["references"] % 100 == 0:
            kb.commit()
    kb.commit()
    return counts


def search(lib, kb, query: str, kind: Optional[str] = None, n: int = 10) -> List[dict]:
    """statements matching `query`, a match in the statement's name weighted 5x; optionally one kind only."""
    from mouseion.kb.search import cite
    from mouseion.kb.store import fts_query

    ensure(kb)
    extra, params = "", [fts_query(query)]
    if kind:
        extra, params = " AND statements.kind = ?", params + [kind.lower()]
    rows = kb.execute(
        f"""SELECT statements.*, bm25(statements_fts, 5.0, 1.0) AS s FROM statements_fts
            JOIN statements ON statements.id = statements_fts.rowid
            WHERE statements_fts MATCH ?{extra} ORDER BY s LIMIT ?""",
        params + [n],
    ).fetchall()
    return [{"kind": r["kind"], "label": r["label"], "name": r["name"], "text": r["text"],
             "ref_id": r["ref_id"], "chunk_id": r["chunk_id"], "page": r["page"],
             "citation": cite(lib, r["ref_id"]) + f", p. {r['page']}"} for r in rows]
