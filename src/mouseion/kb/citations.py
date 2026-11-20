"""Citation layer: exact reference lists from OpenAlex, stored as edges in kb.db.

For every library reference with a DOI: one OpenAlex call (`/works/doi:<doi>?select=id,referenced_works`), cached in
`works` so it is never fetched twice. Edges point to OpenAlex ids; a cited work is "in the library" when some local
reference has that id. Queries: references, cited-by, co-citation, bibliographic coupling.

No contact details are sent to OpenAlex (no mailto); requests are paced (default 8/s, below the documented limit).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Callable, Dict, List, Optional

API = "https://api.openalex.org/works/doi:{doi}?select=id,referenced_works"
UA = "mouseion-kb (reference manager)"


def short_id(url: str) -> str:
    return url.rsplit("/", 1)[-1] if url else url


def fetch(doi: str, timeout: float = 20.0) -> Optional[dict]:
    url = API.format(doi=urllib.parse.quote(doi.strip().lower(), safe="/:().;-_"))
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def build(
    lib,
    kb,
    limit: Optional[int] = None,
    rate: float = 8.0,
    fetcher: Callable = fetch,
    refresh: bool = False,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, int]:
    done = (
        set()
        if refresh
        else {
            r[0]
            for r in kb.execute(
                "SELECT ref_id FROM works WHERE status IN ('ok','not_found','no_doi')"
            )
        }
    )
    rows = [r for r in lib.execute("SELECT id, doi FROM refs") if r["id"] not in done]
    if limit:
        rows = rows[:limit]
    counts: Dict[str, int] = {}
    last = 0.0
    for i, r in enumerate(rows):
        if not r["doi"]:
            kb.execute(
                "INSERT OR REPLACE INTO works(ref_id, openalex_id, status) VALUES (?,?,?)",
                (r["id"], None, "no_doi"),
            )
            s = "no_doi"
        else:
            wait = 1.0 / rate - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.time()
            try:
                data = fetcher(r["doi"])
            except Exception as e:
                kb.execute(
                    "INSERT OR REPLACE INTO works(ref_id, openalex_id, status) VALUES (?,?,?)",
                    (r["id"], None, f"error:{type(e).__name__}"),
                )
                s = "error"
            else:
                if not data:
                    kb.execute(
                        "INSERT OR REPLACE INTO works(ref_id, openalex_id, status) VALUES (?,?,?)",
                        (r["id"], None, "not_found"),
                    )
                    s = "not_found"
                else:
                    wid = short_id(data.get("id", ""))
                    kb.execute(
                        "INSERT OR REPLACE INTO works(ref_id, openalex_id, status) VALUES (?,?,?)",
                        (r["id"], wid, "ok"),
                    )
                    kb.execute("DELETE FROM cites WHERE src_ref=?", (r["id"],))
                    kb.executemany(
                        "INSERT OR IGNORE INTO cites(src_ref, dst_work) VALUES (?,?)",
                        [
                            (r["id"], short_id(w))
                            for w in data.get("referenced_works") or []
                        ],
                    )
                    s = "ok"
        counts[s] = counts.get(s, 0) + 1
        if i % 50 == 0:
            kb.commit()
        if progress:
            progress(i + 1, len(rows), s)
    kb.commit()
    return counts


def _local(kb, works: List[str]) -> Dict[str, str]:
    if not works:
        return {}
    q = ",".join("?" * len(works))
    return {
        r["openalex_id"]: r["ref_id"]
        for r in kb.execute(
            f"SELECT ref_id, openalex_id FROM works WHERE openalex_id IN ({q})", works
        )
    }


def references(kb, ref_id: str) -> List[dict]:
    ws = [
        r[0]
        for r in kb.execute("SELECT dst_work FROM cites WHERE src_ref=?", (ref_id,))
    ]
    loc = _local(kb, ws)
    return [{"openalex_id": w, "ref_id": loc.get(w)} for w in ws]


def cited_by(kb, ref_id: str) -> List[str]:
    row = kb.execute(
        "SELECT openalex_id FROM works WHERE ref_id=?", (ref_id,)
    ).fetchone()
    if not row or not row[0]:
        return []
    return [
        r[0]
        for r in kb.execute("SELECT src_ref FROM cites WHERE dst_work=?", (row[0],))
    ]


def coupling(kb, ref_id: str, n: int = 20) -> List[tuple]:
    """bibliographic coupling: local references sharing the most cited works with ref_id"""
    rows = kb.execute(
        """SELECT c2.src_ref, COUNT(*) AS k FROM cites c1 JOIN cites c2 ON c1.dst_work = c2.dst_work
                         WHERE c1.src_ref = ? AND c2.src_ref != ? GROUP BY c2.src_ref ORDER BY k DESC LIMIT ?""",
        (ref_id, ref_id, n),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def cocitation(kb, ref_id: str, n: int = 20) -> List[tuple]:
    """co-citation: works most often cited together with ref_id by the library (local ids where known)"""
    row = kb.execute(
        "SELECT openalex_id FROM works WHERE ref_id=?", (ref_id,)
    ).fetchone()
    if not row or not row[0]:
        return []
    rows = kb.execute(
        """SELECT c2.dst_work, COUNT(*) AS k FROM cites c1 JOIN cites c2 ON c1.src_ref = c2.src_ref
                         WHERE c1.dst_work = ? AND c2.dst_work != ? GROUP BY c2.dst_work ORDER BY k DESC LIMIT ?""",
        (row[0], row[0], n),
    ).fetchall()
    loc = _local(kb, [r[0] for r in rows])
    return [(loc.get(r[0], r[0]), r[1]) for r in rows]


def frontier(kb, n: int = 50) -> List[tuple]:
    """works cited most often by the library but NOT in it: the reading list the library itself implies"""
    rows = kb.execute(
        """SELECT c.dst_work, COUNT(*) AS k FROM cites c LEFT JOIN works w ON w.openalex_id = c.dst_work
                         WHERE w.ref_id IS NULL GROUP BY c.dst_work ORDER BY k DESC LIMIT ?""",
        (n,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]
