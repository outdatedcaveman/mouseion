"""Measure retrieval on real project questions before adding dense retrieval.

A question file is JSON Lines, one object per line:
    {"q": "...", "project": "semplex", "expect": [{"doi": "..."}, {"arxiv": "2210.01117"}, {"title": "..."}]}
`expect` lists the sources a good answer must surface (any one counts as a hit).

For every question the report says where the first expected source ranks among passages and among references,
and, for a miss, why: the source is not in the library, it is in the library but has no indexed text, or it is
indexed and retrieval missed it. Only the last kind is evidence for dense retrieval (paraphrase misses); the
first two are fixed by adding or indexing PDFs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from mouseion.kb import search
from mouseion.kb.backfill import Matcher, norm_doi


def load(path: str) -> List[dict]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


def _rank(ids: List[str], want: set) -> Optional[int]:
    for i, r in enumerate(ids):
        if r in want:
            return i + 1
    return None


def run(lib, kb, questions: List[dict], k: int = 10) -> dict:
    m = Matcher(lib)
    rows, ks = [], (1, 5, k)
    for qd in questions:
        want, missing = set(), []
        for e in qd.get("expect", []):
            rec = {"doi": norm_doi(e.get("doi")), "arxiv": e.get("arxiv"), "title": e.get("title", "")}
            rid, _ = m.match(rec)
            (want.add(rid) if rid else missing.append(e))
        p = [h["ref_id"] for h in search.search_passages(lib, kb, qd["q"], k)]
        r = [h["ref_id"] for h in search.search_refs(lib, qd["q"], k)]
        rp, rr = _rank(p, want), _rank(r, want)
        indexed = {
            x[0] for x in kb.execute(
                f"SELECT DISTINCT ref_id FROM chunks WHERE ref_id IN ({','.join('?' * len(want)) or 'NULL'})",
                list(want),
            )
        }
        if rp:
            why = "hit"
        elif not want:
            why = "not_in_library"
        elif not indexed:
            why = "not_indexed"
        else:
            why = "retrieval_miss"
        rows.append({"q": qd["q"], "project": qd.get("project"), "passage_rank": rp, "ref_rank": rr,
                     "why": why, "not_in_library": missing})
    n = len(rows) or 1
    summary = {
        "questions": len(rows),
        "passages": {f"hit@{x}": round(sum(1 for r in rows if r["passage_rank"] and r["passage_rank"] <= x) / n, 3)
                     for x in ks},
        "refs": {f"hit@{x}": round(sum(1 for r in rows if r["ref_rank"] and r["ref_rank"] <= x) / n, 3)
                 for x in ks},
        "passages_mrr": round(sum(1 / r["passage_rank"] for r in rows if r["passage_rank"]) / n, 3),
        "misses": {w: sum(1 for r in rows if r["why"] == w)
                   for w in ("retrieval_miss", "not_indexed", "not_in_library")},
    }
    rm, total_miss = summary["misses"]["retrieval_miss"], n - sum(1 for r in rows if r["why"] == "hit")
    summary["verdict"] = (
        "add dense retrieval: retrieval misses on indexed text are the main failure"
        if total_miss and rm / total_miss > 0.5
        else "do not add dense retrieval yet: most misses are sources missing from the library or not indexed"
        if total_miss
        else "no misses"
    )
    return {"summary": summary, "rows": rows}
