"""Multi-hop retrieval and discovery over the lexical index, with no embeddings and no LLM at indexing time.

Why this design (measured, not assumed):
- Wang et al. 2026, "BM25 Wins at Scale" (arXiv:2607.26497): with questions held fixed and the corpus grown
  450-fold, BM25 overtakes dense retrieval, file-system agents and every LLM-built graph (GraphRAG, LightRAG,
  HippoRAG 2) above ~10M corpus tokens. LLM graph construction hits walls (LightRAG ~4 instance-years at 500k docs).
  An agent working *after* BM25 ranking beats BM25 by ~15 points.
- Wu et al. 2026, "SAG2" (arXiv:2610.22588): the FTS5 index already holds a passage-term hypergraph implicitly.
  A judge marks which retrieved passages are evidence; rare terms from those passages ("bridge terms") retrieve
  passages that share nothing with the question; two rounds reach later hops. Highest R@10 on MuSiQue/2Wiki/Hotpot.

Here the judge is whoever calls the tool: Claude (via the kb MCP tools) passes the chunk ids it judged to be
evidence, so the decision model costs nothing extra. Without a judge, seeds are the top BM25 passages
(pseudo-relevance feedback; weaker, per SAG2's ablations, but still useful).

Everything reads kb.db. The only object created is the fts5vocab view `chunks_vocab` (no stored data).
"""

from __future__ import annotations

import math
import re
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from mouseion.kb.store import fts_query

STOP = set(
    """a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each either else etc few for from further had has
have having he her here hers him his how however i if in into is it its itself just let may me might more most
much must my no nor not now of off on once one only or other our ours out over own per same she should since so
some such than that the their theirs them then there these they this those through thus to too two under until
up upon us use used using very via was we were what when where whether which while who whom why will with within
without would yet you your fig figure section page pp vol eq eqs equation theorem lemma proof definition
proposition corollary remark example see cf ie eg et al let thus hence now note case first second given show
shown follows following called new paper work result results""".split()
)

_WORD = re.compile(r"[A-Za-z][A-Za-z\-']{2,}")
# multiword names: capitalised runs ("Grothendieck topos", "Barratt-Priddy-Quillen theorem") are kept as phrases
_NAME = re.compile(r"\b([A-Z][a-z]+(?:[\-–][A-Z][a-z]+)+|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def _common() -> Dict[str, int]:
    from pathlib import Path

    global _COMMON
    if _COMMON is None:
        try:
            words = [w.strip() for w in (Path(__file__).with_name("common_en.txt")).read_text(encoding="utf-8")
                     .splitlines() if w.strip() and not w.startswith("#")]
        except OSError:
            words = []
        _COMMON = {w: i for i, w in enumerate(words)}
    return _COMMON


_COMMON: Optional[Dict[str, int]] = None


def is_technical(term: str) -> bool:
    """ordinary English is not a discovery bridge. A single word must be outside the 10,000 most frequent English
    words; a phrase must contain no word from the top 1,000 and no -ly adverb. Phrases of common words that are
    technical together ("finite sets", "complex numbers") pass."""
    c = _common()
    words = term.replace("-", " ").split()
    if any(w.endswith("ly") and len(w) > 4 for w in words):
        return False
    if len(words) == 1:
        return words[0] not in c
    return not any(c.get(w, 10**9) < 1000 for w in words)


_CITE = re.compile(r"\[\d{1,3}\]|\b(?:19|20)\d\d[a-z]?\b|\barXiv\b|\bdoi\b|\bpp\.|\bVol\.|\bProc\.")


def is_boilerplate(text: str) -> bool:
    """acknowledgements and reference lists: their terms (funders, colleagues, journal names) are not bridges."""
    low = text.lower()
    if "acknowledg" in low or "grateful" in low or "we thank" in low or "funded by" in low:
        return True
    return len(_CITE.findall(text)) >= max(6, len(text) // 150)


def _ensure_vocab(kb) -> None:
    kb.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vocab USING fts5vocab(chunks_fts, 'row')")


class Lexicon:
    """document frequencies of indexed (stemmed) terms, via fts5vocab; cached per instance."""

    def __init__(self, kb):
        _ensure_vocab(kb)
        self.kb = kb
        self.N = kb.execute("SELECT count(*) FROM chunks").fetchone()[0] or 1
        self._df: Dict[str, int] = {}
        self._refs: Dict[str, int] = {}

    def df(self, phrase: str) -> int:
        """number of chunks containing the phrase (a single word or a multiword phrase)."""
        key = phrase.lower()
        if key not in self._df:
            try:
                self._df[key] = self.kb.execute(
                    "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (f'"{key}"',)
                ).fetchone()[0]
            except Exception:
                self._df[key] = 0
        return self._df[key]

    def refs(self, phrase: str) -> int:
        """number of distinct references whose passages contain the phrase."""
        key = phrase.lower()
        if key not in self._refs:
            try:
                self._refs[key] = self.kb.execute(
                    "SELECT count(DISTINCT chunks.ref_id) FROM chunks_fts JOIN chunks ON chunks.id = chunks_fts.rowid "
                    "WHERE chunks_fts MATCH ?", (f'"{key}"',)).fetchone()[0]
            except Exception:
                self._refs[key] = 0
        return self._refs[key]

    def burst(self, phrase: str) -> float:
        """passages per containing reference: technical terms recur inside the papers that use them, generic
        words ("standard", "together") are spread thinly over many papers."""
        r = self.refs(phrase)
        return self.df(phrase) / r if r else 0.0

    def is_term(self, phrase: str, min_refs: int = 2, min_burst: float = 1.6) -> bool:
        """a usable discovery bridge: a phrase, or a word of 5+ letters, found in at least `min_refs` references
        (one-off extraction fragments fail this) and bursty enough to be technical vocabulary."""
        if " " not in phrase and "-" not in phrase and len(phrase) < 5:
            return False
        if not is_technical(phrase):
            return False
        return self.refs(phrase) >= min_refs and self.burst(phrase) >= min_burst

    def idf(self, phrase: str) -> float:
        d = self.df(phrase)
        return math.log(1 + (self.N - d + 0.5) / (d + 0.5)) if d else 0.0


def _tokens(text: str) -> List[str]:
    return [w.lower().strip("-'") for w in _WORD.findall(text) if w.lower() not in STOP]


def candidate_terms(text: str, bigrams: bool = False) -> Dict[str, int]:
    """terms of a passage with local frequency: content words plus capitalised multiword names (a name counts
    once; its words are not counted again for that occurrence)."""
    tf: Dict[str, int] = {}
    covered = set()
    for m in _NAME.finditer(text):
        words = m.group(1).split()
        while words and words[0].lower() in STOP:  # sentence-initial "The Barratt-Priddy-Quillen"
            words = words[1:]
        name = " ".join(words)
        if len(words) >= 2 or "-" in name or "–" in name:
            k = re.sub(r"\s+", " ", name.lower().replace("–", "-"))
            if not all(w in STOP for w in k.replace("-", " ").split()):
                tf[k] = tf.get(k, 0) + 1
                covered.update(range(m.end() - len(name), m.end()))
    prev = None
    for m in _WORD.finditer(text):
        if m.start() in covered:
            prev = None
            continue
        w = m.group(0).lower().strip("-'")
        if w not in STOP and len(w) > 2:
            tf[w] = tf.get(w, 0) + 1
            if bigrams and prev and text[prev[1]:m.start()].strip() == "":
                bg = prev[0] + " " + w
                tf[bg] = tf.get(bg, 0) + 1
            prev = (w, m.end())
        else:
            prev = None
    return tf


def _passages(kb, match: str, n: int, exclude: Iterable[int] = ()) -> List[dict]:
    ex = list(exclude)
    extra = f" AND chunks.id NOT IN ({','.join('?' * len(ex))})" if ex else ""
    try:
        rows = kb.execute(
            f"""SELECT chunks.id, chunks.ref_id, chunks.page, chunks.text, bm25(chunks_fts) AS s
                FROM chunks_fts JOIN chunks ON chunks.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?{extra} ORDER BY s LIMIT ?""",
            [match] + ex + [n],
        ).fetchall()
    except Exception:
        return []
    return [{"chunk_id": r["id"], "ref_id": r["ref_id"], "page": r["page"], "text": r["text"],
             "bm25": -r["s"]} for r in rows]


def _get(kb, ids: Sequence[int]) -> List[dict]:
    if not ids:
        return []
    rows = kb.execute(
        f"SELECT id, ref_id, page, text FROM chunks WHERE id IN ({','.join('?' * len(ids))})", list(ids)
    ).fetchall()
    by = {r["id"]: r for r in rows}
    return [{"chunk_id": i, "ref_id": by[i]["ref_id"], "page": by[i]["page"], "text": by[i]["text"]}
            for i in ids if i in by]


def activate(
    lex: Lexicon,
    query: str,
    seeds: List[dict],
    weights: Optional[Dict[int, float]] = None,
    L: int = 20,
    max_df_frac: float = 0.05,
) -> List[tuple]:
    """SAG2 term activation (Eqs. 2-3): a(t) = max_d w(d) (1 + log tf) idf(t) (1 + g_q(t, d)), where g_q is the
    largest summed idf of question tokens in a sentence of d containing t. Question tokens, stopwords, terms
    absent from the index and terms in more than `max_df_frac` of chunks are ineligible."""
    qtok = set(_tokens(query))
    qidf = {t: lex.idf(t) for t in qtok}
    best: Dict[str, float] = {}
    wmax = max((weights or {}).values(), default=0) or 1.0
    for d in seeds:
        if is_boilerplate(d["text"]):
            continue
        w = 0.5 + 0.5 * ((weights or {}).get(d["chunk_id"], wmax) / wmax)
        sents = _SENT.split(d["text"])
        tf = candidate_terms(d["text"])
        for t, f in tf.items():
            if t in qtok or all(p in qtok for p in t.replace("-", " ").split()):
                continue
            df = lex.df(t)
            if df == 0 or df > max(max_df_frac * lex.N, 20):
                continue
            tl = t.lower()
            g = max(
                (sum(qidf[v] for v in set(_tokens(s)) & qtok) for s in sents if tl in s.lower()),
                default=0.0,
            )
            a = w * (1 + math.log(f)) * lex.idf(t) * (1 + g)
            if a > best.get(t, 0):
                best[t] = a
    return sorted(best.items(), key=lambda x: -x[1])[:L]


def expand(
    kb,
    query: str,
    evidence: Optional[Sequence[int]] = None,
    judge: Optional[Callable[[str, List[dict], List[dict]], Dict[int, float]]] = None,
    rounds: int = 2,
    B: int = 100,
    K: int = 10,
    L: int = 20,
    n: int = 10,
) -> dict:
    """Multi-hop retrieval by bridge terms.

    evidence: chunk ids the caller has judged to be evidence (the agent is the decision model). If omitted and
              no `judge` is given, the top-K BM25 passages are the seeds (pseudo-relevance feedback).
    judge(query, candidates, context) -> {chunk_id: score in [0,1]}: optional model-based decision function.
    Returns the final ranking (BM25 hits first, then expansion hits interleaved by term priority, or the judge's
    scores), the activated bridge terms with the passages each one reached, and the visited count."""
    lex = Lexicon(kb)
    qterms = " OR ".join(f'"{t}"' for t in sorted(set(_tokens(query)))[:24])
    pool = _passages(kb, qterms or fts_query(query), B)  # content words only: question words add noise
    visited = {p["chunk_id"] for p in pool}
    ranking_lists: List[List[int]] = [[p["chunk_id"] for p in pool]]
    by_id = {p["chunk_id"]: p for p in pool}
    trace = []
    scores: Dict[int, float] = {}
    if evidence:
        seeds = _get(kb, list(evidence))
        for s in seeds:
            by_id.setdefault(s["chunk_id"], s)
            visited.add(s["chunk_id"])
        scores = {s["chunk_id"]: 1.0 for s in seeds}
    elif judge:
        scores = judge(query, pool, [])
        seeds = sorted(pool, key=lambda p: -scores.get(p["chunk_id"], 0))[:K]
    else:
        seeds = pool[:K]
    expanded = set()
    for r in range(rounds):
        seeds = [s for s in seeds if s["chunk_id"] not in expanded][:K]
        if not seeds:
            break
        expanded.update(s["chunk_id"] for s in seeds)
        terms = activate(lex, query, seeds, scores or None, L)
        if not terms:
            break
        xs = " OR ".join(f'"{t}"' for t, _ in terms)
        per_term = []
        for t, a in terms:
            match = f'"{t}" AND ({xs}{" OR " + qterms if qterms else ""})'
            hits = _passages(kb, match, B, visited)
            per_term.append((t, a, hits))
        new: List[dict] = []
        seen = set()
        for i in range(B):
            for t, a, hits in per_term:
                if i < len(hits) and hits[i]["chunk_id"] not in seen and len(new) < B:
                    seen.add(hits[i]["chunk_id"])
                    hits[i]["via"] = t
                    new.append(hits[i])
            if len(new) >= B:
                break
        if not new:
            break
        for p in new:
            by_id[p["chunk_id"]] = p
            visited.add(p["chunk_id"])
        ranking_lists.append([p["chunk_id"] for p in new])
        trace.append({"round": r + 1, "terms": [{"term": t, "priority": round(a, 3), "reached": len(h)}
                                                for t, a, h in per_term]})
        if judge:
            ctx = sorted(by_id.values(), key=lambda p: -scores.get(p["chunk_id"], 0))[:K]
            scores.update(judge(query, new + seeds, ctx))
            seeds = sorted(new, key=lambda p: -scores.get(p["chunk_id"], 0))[:K]
        else:
            seeds = new[:K]
    if judge:
        order = sorted(by_id, key=lambda c: -scores.get(c, 0))
    else:
        from mouseion.kb.search import rrf

        # weight expansion lists below the original ranking so that pure BM25 hits keep their lead
        fused: Dict[int, float] = {}
        for li, lst in enumerate(ranking_lists):
            wgt = 1.0 if li == 0 else 0.5 ** li
            for i, c in enumerate(lst):
                fused[c] = fused.get(c, 0.0) + wgt / (60 + i + 1)
        order = sorted(fused, key=fused.get, reverse=True)
    if evidence:
        order = [c for c in order if c not in set(evidence)]
    out = [{k: v for k, v in by_id[c].items() if k != "bm25"} for c in order[:n]]
    return {"passages": out, "trace": trace, "visited": len(visited)}


def bridges(
    kb,
    a: str,
    c: Optional[str] = None,
    n: int = 15,
    pool: int = 60,
) -> dict:
    """Literature-based discovery (Swanson's ABC model) on the lexical index.

    Closed discovery (a and c given): terms B that occur in passages about A and in passages about C, ranked by
    rarity and by how strongly they occur on both sides, with one supporting passage per side, and the number of
    passages where A and C already co-occur (0 = an unexplored connection).
    Open discovery (only a): terms C reached from A through rare intermediate terms B, which never co-occur with
    A in any passage; each C lists the B terms that link it."""
    lex = Lexicon(kb)
    A = _passages(kb, fts_query(a), pool)
    a_q = " OR ".join(f'"{t}"' for t in _tokens(a)) or '""'
    a_ids = {p["ref_id"] for p in A}

    def side_terms(ps):
        agg: Dict[str, tuple] = {}
        for p in ps:
            if is_boilerplate(p["text"]):
                continue
            for t, f in candidate_terms(p["text"], bigrams=True).items():
                cur = agg.get(t, (0, None))
                agg[t] = (cur[0] + 1, cur[1] or p)
        return agg

    ta = side_terms(A)
    if c:
        C = _passages(kb, fts_query(c), pool)
        tc = side_terms(C)
        excl = set(_tokens(a)) | set(_tokens(c))
        rows = []
        for t in set(ta) & set(tc):
            if t in excl:
                continue
            df = lex.df(t)
            if df == 0 or df > max(0.05 * lex.N, 20) or not lex.is_term(t):
                continue
            na, nc = ta[t][0], tc[t][0]
            rows.append((lex.idf(t) * math.log(1 + min(na, nc)), t, na, nc, ta[t][1], tc[t][1]))
        rows.sort(key=lambda r: -r[0])
        c_q = " OR ".join(f'"{t}"' for t in _tokens(c)) or '""'
        try:
            co = kb.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                            (f"({a_q}) AND ({c_q})",)).fetchone()[0]
        except Exception:
            co = None
        return {
            "a": a, "c": c, "cooccurring_passages": co,
            "bridges": [{"term": t, "score": round(s, 3), "a_passages": na, "c_passages": nc,
                         "a_example": {"chunk_id": pa["chunk_id"], "ref_id": pa["ref_id"], "page": pa["page"]},
                         "c_example": {"chunk_id": pc["chunk_id"], "ref_id": pc["ref_id"], "page": pc["page"]}}
                        for s, t, na, nc, pa, pc in rows[:n]],
        }
    # open discovery
    bs = [(b, pb) for b, pb in activate(lex, a, A[:20], None, L=80)
          if (" " not in b or "-" in b) and lex.is_term(b)][:25]
    top = max((pb for _, pb in bs), default=1.0) or 1.0
    bs = [(b, pb / top) for b, pb in bs]
    reach: Dict[str, dict] = {}
    for b, pb in bs:
        hits = _passages(kb, f'"{b}" NOT ({a_q})', 30)
        hits = [h for h in hits if h["ref_id"] not in a_ids and not is_boilerplate(h["text"])]
        for h in hits:
            for t, f in candidate_terms(h["text"], bigrams=True).items():
                if t == b or t in ta:
                    continue
                df = lex.df(t)
                if df == 0 or df > max(0.05 * lex.N, 20) or not lex.is_term(t):
                    continue
                e = reach.setdefault(t, {"via": {}, "score": 0.0})
                if b not in e["via"]:
                    e["via"][b] = {"chunk_id": h["chunk_id"], "ref_id": h["ref_id"], "page": h["page"]}
                    e["score"] += pb * lex.idf(t)
    out = []
    for t, e in sorted(reach.items(), key=lambda x: -x[1]["score"]):
        if len(e["via"]) < 2:  # a single path is too weak to report
            continue
        try:
            co = kb.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                            (f'({a_q}) AND "{t}"',)).fetchone()[0]
        except Exception:
            co = 0
        if co:
            continue
        out.append({"term": t, "score": round(e["score"], 3), "via": e["via"]})
        if len(out) >= n:
            break
    return {"a": a, "intermediates": [b for b, _ in bs], "candidates": out}
