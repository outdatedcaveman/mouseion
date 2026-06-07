"""Local-library candidate resolver.

Before spending provider quota, try to enrich a sparse reference from already
known rows in the user's own Mouseion database. This is especially useful
after imports/re-enrichments have created partial rows that duplicate richer
records without sharing the same content-addressed id.
"""

from __future__ import annotations

import re
import sqlite3
from typing import List, Tuple

from .models import Reference


def _norm_title(title: str | None) -> str:
    text = re.sub(r"[^\w\s]", " ", (title or "").strip().lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _author_families(ref: Reference) -> set[str]:
    return {
        re.sub(r"\W+", "", (a.family or "").lower())
        for a in (ref.authors or [])
        if getattr(a, "family", "")
    }


def _fts_query(title: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]{3,}", title.lower()) if len(w) >= 3]
    return " ".join(words[:8])


def _compatible(seed: Reference, candidate: Reference) -> float:
    """Return confidence that candidate describes seed, or 0 for no match."""
    if seed.doi and candidate.doi and seed.doi.lower() == candidate.doi.lower():
        return 0.99
    if seed.pmid and candidate.pmid and seed.pmid == candidate.pmid:
        return 0.98
    if seed.arxiv_id and candidate.arxiv_id and seed.arxiv_id.lower() == candidate.arxiv_id.lower():
        return 0.98
    if seed.isbn and candidate.isbn and re.sub(r"[-\s]", "", seed.isbn) == re.sub(r"[-\s]", "", candidate.isbn):
        return 0.95

    st = _norm_title(seed.title)
    ct = _norm_title(candidate.title)
    if not st or not ct:
        return 0.0
    if st == ct:
        if seed.year and candidate.year and seed.year != candidate.year:
            return 0.0
        sa = _author_families(seed)
        ca = _author_families(candidate)
        if sa and ca and not (sa & ca):
            return 0.0
        if seed.year and candidate.year:
            return 0.90
        if sa and ca:
            return 0.84
        return 0.74

    # Conservative fuzzy acceptance only when year and at least one author match.
    if seed.year and candidate.year and seed.year == candidate.year:
        sa = _author_families(seed)
        ca = _author_families(candidate)
        if sa and ca and (sa & ca):
            s_words = set(re.findall(r"[a-z0-9]{4,}", st))
            c_words = set(re.findall(r"[a-z0-9]{4,}", ct))
            if s_words and len(s_words & c_words) / max(1, len(s_words | c_words)) >= 0.72:
                return 0.78
    return 0.0


def find_local_candidates(seed: Reference, limit: int = 6) -> List[Tuple[Reference, float]]:
    """Return locally known candidates with confidence scores."""
    try:
        from .config import get_config
        from .db import _row_to_ref
        db_path = str(get_config().db_path)
    except Exception:
        return []

    rows = []
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        if seed.doi:
            rows.extend(conn.execute(
                "SELECT * FROM refs WHERE doi IS NOT NULL AND lower(doi)=lower(?) LIMIT ?",
                (seed.doi, limit),
            ).fetchall())
        if seed.pmid:
            rows.extend(conn.execute("SELECT * FROM refs WHERE pmid=? LIMIT ?", (seed.pmid, limit)).fetchall())
        if seed.arxiv_id:
            rows.extend(conn.execute(
                "SELECT * FROM refs WHERE arxiv_id IS NOT NULL AND lower(arxiv_id)=lower(?) LIMIT ?",
                (seed.arxiv_id, limit),
            ).fetchall())
        if seed.isbn:
            compact = re.sub(r"[-\s]", "", seed.isbn)
            rows.extend(conn.execute(
                "SELECT * FROM refs WHERE isbn IS NOT NULL AND replace(replace(isbn,'-',''),' ','')=? LIMIT ?",
                (compact, limit),
            ).fetchall())
        if seed.title:
            q = _fts_query(seed.title)
            if q:
                rows.extend(conn.execute(
                    """
                    SELECT r.* FROM refs_fts f
                    JOIN refs r ON r.id=f.ref_id
                    WHERE refs_fts MATCH ?
                    ORDER BY bm25(refs_fts)
                    LIMIT ?
                    """,
                    (q, max(limit * 5, 20)),
                ).fetchall())
        conn.close()
    except Exception:
        return []

    seen = set()
    scored: List[Tuple[Reference, float]] = []
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        ref = _row_to_ref(row)
        conf = _compatible(seed, ref)
        if conf > 0:
            ref.sources["local_library"] = max(ref.sources.get("local_library", 0.0), conf)
            ref.extras.setdefault("local_resolver_note", "matched against existing Mouseion row")
            scored.append((ref, conf))
    scored.sort(key=lambda item: (item[1], item[0].completeness), reverse=True)
    return scored[:limit]
