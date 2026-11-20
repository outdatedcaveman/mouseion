"""Retrieval over the library and the knowledge base, always returning citable units.

- refs:     the library's own FTS5 index over title/abstract (refs_fts), BM25.
- passages: kb.chunks FTS5 (BM25); optionally fused with a dense ranking (reciprocal-rank fusion) when the
            semantic stack (sentence-transformers + chromadb) is installed and the chunk collection is built.
- notes:    kb.notes FTS5, active notes first.
Every hit carries ref_id (+ page for passages) and a short citation string.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from mouseion.kb.store import fts_query


def cite(lib, ref_id: str) -> str:
    r = lib.execute(
        "SELECT title, authors, year, doi FROM refs WHERE id=?", (ref_id,)
    ).fetchone()
    if not r:
        return ref_id
    try:
        au = json.loads(r["authors"] or "[]")
        first = (au[0].get("family") or au[0].get("name") or "") if au else ""
        who = (
            f"{first} et al."
            if len(au) > 2
            else " & ".join(a.get("family") or a.get("name") or "" for a in au)
        )
    except Exception:
        who = ""
    return f"{who} ({r['year'] or 'n.d.'}). {r['title']}" + (
        f". doi:{r['doi']}" if r["doi"] else ""
    )


def get_ref(lib, ref_id: str) -> Optional[dict]:
    r = lib.execute("SELECT * FROM refs WHERE id=?", (ref_id,)).fetchone()
    if not r:
        return None
    keep = (
        "id",
        "doi",
        "arxiv_id",
        "pmid",
        "title",
        "authors",
        "year",
        "journal",
        "abstract",
        "keywords",
        "ref_type",
        "citation_count",
        "url",
        "oa_url",
        "pdf_local",
        "pdf_path",
    )
    d = {k: r[k] for k in r.keys() if k in keep and r[k] not in (None, "")}
    for k in ("authors", "keywords"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    d["citation"] = cite(lib, ref_id)
    return d


def search_refs(
    lib,
    query: str,
    n: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> List[dict]:
    wh, params = ["refs_fts MATCH ?"], [fts_query(query)]
    if year_from:
        wh.append("refs.year >= ?")
        params.append(year_from)
    if year_to:
        wh.append("refs.year <= ?")
        params.append(year_to)
    rows = lib.execute(
        f"""SELECT refs.id, bm25(refs_fts) AS s FROM refs_fts JOIN refs ON refs.id = refs_fts.ref_id
                           WHERE {" AND ".join(wh)} ORDER BY s LIMIT ?""",
        params + [n],
    ).fetchall()
    return [
        {"ref_id": r["id"], "score": -r["s"], "citation": cite(lib, r["id"])}
        for r in rows
    ]


def _bm25_passages(
    kb, query: str, n: int, ref_ids: Optional[List[str]] = None
) -> List[dict]:
    extra, params = "", [fts_query(query)]
    if ref_ids:
        extra = f" AND chunks.ref_id IN ({','.join('?' * len(ref_ids))})"
        params += list(ref_ids)
    rows = kb.execute(
        f"""SELECT chunks.id, chunks.ref_id, chunks.page, chunks.text, bm25(chunks_fts) AS s
                          FROM chunks_fts JOIN chunks ON chunks.id = chunks_fts.rowid
                          WHERE chunks_fts MATCH ?{extra} ORDER BY s LIMIT ?""",
        params + [n],
    ).fetchall()
    return [
        {
            "chunk_id": r["id"],
            "ref_id": r["ref_id"],
            "page": r["page"],
            "text": r["text"],
            "bm25": -r["s"],
        }
        for r in rows
    ]


def rrf(rankings: List[List[int]], k: int = 60) -> Dict[int, float]:
    s: Dict[int, float] = {}
    for rank in rankings:
        for i, x in enumerate(rank):
            s[x] = s.get(x, 0.0) + 1.0 / (k + i + 1)
    return s


def search_passages(
    lib,
    kb,
    query: str,
    n: int = 8,
    ref_ids: Optional[List[str]] = None,
    dense=None,
    per_ref: int = 2,
) -> List[dict]:
    """BM25 passages (optionally fused with a dense ranking), at most `per_ref` passages per reference"""
    cand = _bm25_passages(kb, query, max(50, 5 * n), ref_ids)
    by_id = {c["chunk_id"]: c for c in cand}
    if dense is not None:
        try:
            dids = dense(query, max(50, 5 * n))
            for cid in dids:
                if cid not in by_id:
                    r = kb.execute(
                        "SELECT id, ref_id, page, text FROM chunks WHERE id=?", (cid,)
                    ).fetchone()
                    if r and (not ref_ids or r["ref_id"] in ref_ids):
                        by_id[cid] = {
                            "chunk_id": r["id"],
                            "ref_id": r["ref_id"],
                            "page": r["page"],
                            "text": r["text"],
                            "bm25": None,
                        }
            fused = rrf(
                [[c["chunk_id"] for c in cand], [d for d in dids if d in by_id]]
            )
        except Exception:
            fused = rrf([[c["chunk_id"] for c in cand]])
    else:
        fused = rrf([[c["chunk_id"] for c in cand]])
    out, seen = [], {}
    for cid in sorted(fused, key=fused.get, reverse=True):
        c = by_id[cid]
        if seen.get(c["ref_id"], 0) >= per_ref:
            continue
        seen[c["ref_id"]] = seen.get(c["ref_id"], 0) + 1
        out.append(
            {
                **c,
                "score": round(fused[cid], 5),
                "citation": cite(lib, c["ref_id"]) + f", p. {c['page']}",
            }
        )
        if len(out) >= n:
            break
    return out


def search_notes(
    lib,
    kb,
    query: str,
    n: int = 10,
    project: Optional[str] = None,
    include_inactive: bool = False,
) -> List[dict]:
    wh, params = ["notes_fts MATCH ?"], [fts_query(query)]
    if project:
        wh.append("notes.project = ?")
        params.append(project)
    if not include_inactive:
        wh.append("notes.status = 'active'")
    rows = kb.execute(
        f"""SELECT notes.*, bm25(notes_fts) AS s FROM notes_fts JOIN notes ON notes.id = notes_fts.rowid
                          WHERE {" AND ".join(wh)} ORDER BY s LIMIT ?""",
        params + [n],
    ).fetchall()
    return [
        {k: r[k] for k in r.keys() if k != "s"} | {"citation": cite(lib, r["ref_id"])}
        for r in rows
    ]


def _norm(s: str) -> str:
    import re

    return re.sub(r"\s+", " ", s or "").strip().lower()


def add_note(
    lib,
    kb,
    project: str,
    claim: str,
    ref_id: str,
    chunk_id: Optional[int] = None,
    quote: Optional[str] = None,
    kind: str = "finding",
    author: Optional[str] = None,
    supersedes: Optional[int] = None,
) -> dict:
    """add a provenance-bound claim. Rejected unless the reference exists; a quote must occur verbatim (up to
    whitespace and case) in the given passage, so a note can never cite text that is not there."""
    if not claim.strip():
        raise ValueError("empty claim")
    if not lib.execute("SELECT 1 FROM refs WHERE id=?", (ref_id,)).fetchone():
        raise ValueError(f"unknown ref_id {ref_id}")
    if chunk_id is not None:
        ch = kb.execute(
            "SELECT ref_id, text FROM chunks WHERE id=?", (chunk_id,)
        ).fetchone()
        if not ch or ch["ref_id"] != ref_id:
            raise ValueError("chunk_id does not belong to ref_id")
        if quote and _norm(quote) not in _norm(ch["text"]):
            raise ValueError("quote not found verbatim in the passage")
    elif quote:
        hit = [
            r
            for r in kb.execute("SELECT id, text FROM chunks WHERE ref_id=?", (ref_id,))
            if _norm(quote) in _norm(r["text"])
        ]
        if not hit:
            ab = lib.execute(
                "SELECT abstract FROM refs WHERE id=?", (ref_id,)
            ).fetchone()
            if not (ab and ab[0] and _norm(quote) in _norm(ab[0])):
                raise ValueError("quote not found in the reference's text or abstract")
        else:
            chunk_id = hit[0]["id"]
    cur = kb.execute(
        "INSERT INTO notes(project, claim, ref_id, chunk_id, quote, kind, author) VALUES (?,?,?,?,?,?,?)",
        (project, claim.strip(), ref_id, chunk_id, quote, kind, author),
    )
    nid = cur.lastrowid
    if supersedes:
        kb.execute(
            "UPDATE notes SET status='superseded', superseded_by=? WHERE id=?",
            (nid, supersedes),
        )
    kb.commit()
    return {"id": nid, "ref_id": ref_id, "chunk_id": chunk_id}


def stats(lib, kb) -> dict:
    def q(c, sql):
        return c.execute(sql).fetchone()[0]

    return {
        "refs": q(lib, "SELECT COUNT(*) FROM refs"),
        "pdf_files": dict(
            kb.execute(
                "SELECT status, COUNT(*) FROM pdf_files GROUP BY status"
            ).fetchall()
        ),
        "chunks": q(kb, "SELECT COUNT(*) FROM chunks"),
        "works": dict(
            kb.execute("SELECT status, COUNT(*) FROM works GROUP BY status").fetchall()
        ),
        "citation_edges": q(kb, "SELECT COUNT(*) FROM cites"),
        "edges_within_library": q(
            kb,
            "SELECT COUNT(*) FROM cites c JOIN works w ON w.openalex_id = c.dst_work",
        ),
        "notes": dict(
            kb.execute(
                "SELECT project || ':' || status, COUNT(*) FROM notes GROUP BY project, status"
            ).fetchall()
        ),
    }
