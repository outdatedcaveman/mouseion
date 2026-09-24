"""Resolve references whose only data is a damaged title (lossy imports).

2026-09-24: 73,917 incomplete references have no identifier. Their titles are
not titles: file names ("Dunn 2010 Contradictory Information Too Much" --
author, year, stopwords stripped, end cut off), citation strings, identifiers
("math 0209084 K", "arXiv:math/9905006v1 [math.AG] 3 May 1999"), GUIDs. Exact
or whole-string matching cannot work on them (June: 5% recovered). But search
engines handle keyword bags fine; what failed was the MATCHER. So:

  1. parse: embedded arXiv id / DOI (exact, resolved directly); else an
     author hint, a year hint and the keyword set of the remainder;
  2. retrieve: Crossref bibliographic search with the author as its own field
     (books: OpenLibrary too);
  3. fuzzy sort: keyword coverage of the candidate's title (the last keyword
     may be truncated: prefix match), author agreement, year agreement.
     Accept only clear wins (see _decide); the ambiguous band is logged as
     `judge` for a second-stage judge instead of being guessed.
Writes through Mouseion's merge after clearing junk fields (a junk title can
never veto its own repair). Reversible (lossy_bak_<date>), resumable
(lossy_scan).

Usage: python scripts/resolve_lossy.py <limit> <dry|write> [workers] [--show N]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

_argv = list(sys.argv)
sys.argv = [sys.argv[0], "1", "dry"]          # the imported modules parse argv at import time
import recover_ids as R                        # noqa: E402  Crossref parser, contact e-mail
import recover_ids_from_pdfs as P              # noqa: E402  _clean_seed, _Junk, arXiv/DOI lookups, _apply
sys.argv = _argv

import httpx  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LIMIT = int(args[0]) if args else 200
WRITE = len(args) > 1 and args[1] == "write"
WORKERS = int(args[2]) if len(args) > 2 else 8
SHOW = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 0
P.WRITE = WRITE                                 # _apply writes only when this is True
STATS = P.STATS
STATS.update({"embedded_id": 0, "no_keywords": 0, "judge": 0})
DECISIONS: list = []
# v2 (2026-09-24): the library's definition -- complete = title + author + ANY identifier or
# delivery (DOI/ISBN/arXiv/PMID/URL/PDF). --incomplete targets exactly the refs
# that fail it; Crossref no-record rows go on to OpenAlex (key, when configured)
# and Semantic Scholar (key configured), whose work URL also counts.
INCOMPLETE = "--incomplete" in sys.argv
import threading  # noqa: E402

from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase  # noqa: E402
from mouseion.providers.openalex import OpenAlexProvider as _OAP  # noqa: E402
from mouseion.providers.semantic_scholar import SemanticScholarProvider as _S2P  # noqa: E402

_CFG = get_config()
S2_KEY = (_CFG.semantic_scholar_api_key or "").strip()
OA_KEY = (_CFG.openalex_api_key or "").strip()
OA_EMAIL = (_CFG.openalex_email or "").strip()
STATS.update({"via_crossref": 0, "via_openalex": 0, "via_s2": 0, "s2_429": 0, "oa_err": 0})


class _Rate:
    """Process-wide minimum spacing between calls to one API."""
    def __init__(self, interval: float):
        self.interval, self._next, self._lock = interval, 0.0, threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next)
            self._next = t + self.interval
        if t > now:
            time.sleep(t - now)


_S2_RATE = _Rate(1.2)           # standard S2 key: 1 request/s (1.05 still drew 429s)
_OA_RATE = _Rate(0.11)          # OpenAlex with a key: 10 requests/s


def _s2(query: str) -> list:
    if not S2_KEY:
        return []
    for attempt in range(4):
        _S2_RATE.wait()
        try:
            r = httpx.get("https://api.semanticscholar.org/graph/v1/paper/search",
                          params={"query": query[:250], "limit": 5,
                                  "fields": "title,authors,year,externalIds,url,venue,journal,publicationTypes,"
                                            "publicationDate,openAccessPdf"},
                          headers={"x-api-key": S2_KEY}, timeout=30)
        except Exception:
            time.sleep(2)
            continue
        if r.status_code == 200:
            out = []
            for d in r.json().get("data") or []:
                try:
                    ref = _S2P._parse_paper(d)
                    ref.url = ref.url or d.get("url")
                    out.append(ref)
                except Exception:
                    pass
            return out
        if r.status_code == 429:
            STATS["s2_429"] += 1
            time.sleep(2 * (attempt + 1))
            continue
        return []
    return []


def _openalex(query: str) -> list:
    if not OA_KEY:
        return []
    params = {"search": query[:250], "per-page": 5, "api_key": OA_KEY}
    if OA_EMAIL:
        params["mailto"] = OA_EMAIL
    for attempt in range(3):
        _OA_RATE.wait()
        try:
            r = httpx.get("https://api.openalex.org/works", params=params, timeout=30)
        except Exception:
            time.sleep(2)
            continue
        if r.status_code == 200:
            out = []
            for d in r.json().get("results") or []:
                try:
                    ref = _OAP._parse_work(d)
                    ref.url = ref.url or (d.get("primary_location") or {}).get("landing_page_url") or d.get("id")
                    out.append(ref)
                except Exception:
                    pass
            return out
        if r.status_code in (429, 503):
            time.sleep(3 * (attempt + 1))
            continue
        STATS["oa_err"] += 1
        return []
    return []

_STOP = {"the", "a", "an", "of", "in", "on", "and", "or", "to", "for", "with", "by", "from", "as", "at", "is",
         "are", "its", "into", "via", "de", "la", "le", "el", "der", "die", "das", "und", "do", "da", "dos", "em",
         "e", "o", "os", "um", "uma", "del", "les", "des", "du", "y", "en", "et", "il", "sur", "zur", "zum", "von"}
_OLD_ARXIV = re.compile(r"\b(math|hep-th|hep-ph|hep-lat|hep-ex|gr-qc|quant-ph|cond-mat|astro-ph|cs|physics|nlin|"
                        r"q-bio|q-fin|math-ph|nucl-th|nucl-ex|stat|alg-geom|dg-ga|funct-an|q-alg)"
                        r"(?:\.[A-Z]{2})?[\s/_]?(\d{7})(?:v\d+)?\b", re.I)
_NEW_ARXIV = re.compile(r"(?:arxiv[:\s_]*)?\b((?:0[7-9]|1\d|2[0-6])(?:0[1-9]|1[0-2])\.\d{4,5})(?:v\d+)?\b", re.I)
_FILENAME = re.compile(r"^(?P<author>(?:[A-Z][\w'\-]+|[A-Z])(?:\s(?:and|&|et al\.?)\s[A-Z][\w'\-]+)?"
                       r"(?:\s[A-Z][\w'\-]+)?)\s+(?P<year>(?:1[6-9]|20)\d\d)[a-z]?\s+(?P<rest>.{6,})$")
_YEARS = re.compile(r"^(?P<year>(?:1[6-9]|20)\d\d)(?:\s+(?:1[6-9]|20)\d\d)?\s+(?P<rest>.{6,})$")
_NOISE = re.compile(r"^(microsoft word|untitled)\s*-\s*|\.(pdf|docx?|tex|dvi|ps|indd)$|\(\d\)$|_", re.I)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _kw(s: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", _fold(s)).split() if len(w) > 2 and w not in _STOP]


def parse(title: str) -> dict:
    """Title -> {arxiv, doi, author, year, keywords, query}."""
    t = " ".join((title or "").split())
    out = {"arxiv": None, "doi": None, "author": None, "year": None}
    m = _OLD_ARXIV.search(t)
    if m:
        out["arxiv"] = f"{m.group(1).lower()}/{m.group(2)}"
    else:
        m = _NEW_ARXIV.search(t)
        if m and ("arxiv" in t.lower() or re.fullmatch(r"[\s\w.:]*", t) and len(_kw(t)) <= 3):
            out["arxiv"] = m.group(1)
    m = P.DOI_RE.search(t)
    if m:
        out["doi"] = m.group(1).rstrip(".,;")
    clean = _NOISE.sub(" ", t).strip()
    m = _FILENAME.match(clean)
    if m and not _kw(m.group("author")) == []:
        out["author"], out["year"], clean = m.group("author"), int(m.group("year")), m.group("rest")
    else:
        m = _YEARS.match(clean)
        if m:
            # "1943 1975 Infinitary Logic..." is a LIFESPAN (Carol Karp), not a
            # publication year: a range gives no year hint at all
            two = re.match(r"^(?:1[6-9]|20)\d\d\s+(?:1[6-9]|20)\d\d\b", clean)
            out["year"], clean = (None if two else int(m.group("year"))), m.group("rest")
    out["keywords"] = _kw(clean)
    out["query"] = clean
    return out


def _cand_title_words(cand) -> set[str]:
    return set(_kw((cand.title or "") + " " + (getattr(cand, "subtitle", None) or "")))


def _coverage(kws: list[str], cand) -> float:
    """Share of the reference's keywords in the candidate's title; the LAST
    keyword may be truncated ("metr" -> "metricity"), so it matches as prefix."""
    words = _cand_title_words(cand)
    if not kws or not words:
        return 0.0
    hit = 0
    for i, k in enumerate(kws):
        if k in words or (i == len(kws) - 1 and len(k) >= 3 and any(w.startswith(k) for w in words)):
            hit += 1
    return hit / len(kws)


_MARKER = re.compile(r"\b(review|reviews|precis|reply|replies|response|comment|comments|correction|erratum|"
                     r"corrigendum|discussion|rejoinder|critical notice|book notes?|abstracts?)\b", re.I)


def _names(people: list[str]) -> set[str]:
    return {t for p in people for t in re.sub(r"[^a-z]", " ", _fold(p)).split() if len(t) >= 3}


_TAIL_STOP = {"and", "of", "the", "a", "an", "for", "to", "in", "on", "with", "from", "by", "at", "or", "as",
              "de", "la", "le", "et", "und", "der", "die", "das", "do", "da", "e", "o"}
_NUMS = re.compile(r"\b(?:\d{1,3}|i{1,3}|iv|v|vi{1,3}|ix|x)\b")   # part/volume numbers; 4-digit years excluded


def features(seed_title: str, seed_authors: list[str], seed_year, hint_author, hint_year,
             cand_title: str, cand_authors: list[str], cand_year) -> list[float]:
    """Plain-value features of one (damaged reference, candidate) pair. Shared by
    training (labeled synthetic damage, 2026-09-24) and the running job. Each of
    the later features comes from a real error in the labeled evaluation."""
    import difflib
    raw_seed = _NOISE.sub(" ", seed_title or "").strip()
    info_kw = _kw(raw_seed)
    is_filename = float(bool(hint_author))
    if hint_author:
        drop = set(_kw(hint_author)) | {str(hint_year)}
        info_kw = [k for k in info_kw if k not in drop]
    ckw = _kw(cand_title or "")
    cset = set(ckw)
    hits = [k in cset or (i == len(info_kw) - 1 and len(k) >= 3 and any(w.startswith(k) for w in cset))
            for i, k in enumerate(info_kw)]
    n = max(1, len(info_kw))
    cov = sum(hits) / n
    miss_mid = sum(1 for i, h in enumerate(hits) if not h and i != len(hits) - 1) / n
    rev = sum(1 for w in ckw if w in set(info_kw)) / max(1, len(ckw))
    first_pos = next((i for i, w in enumerate(ckw) if info_kw and w == info_kw[0]), len(ckw))
    lead = min(first_pos, 6) / 6
    # matched keywords in the same ORDER ("Radiation Reaction of a" vs "Dual charges and radiation reaction")
    pos, ordered = -1, 0
    for k in info_kw:
        nxt = next((i for i in range(pos + 1, len(ckw)) if ckw[i] == k or ckw[i].startswith(k)), None)
        if nxt is not None:
            ordered, pos = ordered + 1, nxt
    order = ordered / n
    # words the candidate has AFTER the last matched keyword
    tail = min(max(0, len(ckw) - 1 - pos), 6) / 6 if pos >= 0 else 1.0
    # the reference was cut mid-phrase ("Arrow Logic and", "nd machine learning"): the true title is longer
    words = re.sub(r"[^\w ]", " ", _fold(raw_seed)).split()
    trunc_end = float(bool(words) and words[-1] in _TAIL_STOP)
    trunc_front = float(bool(raw_seed) and raw_seed[0].islower() and not hint_author)
    short_cand_on_trunc = float((trunc_end and tail == 0) or (trunc_front and lead == 0))
    # part/volume numbers that disagree ("Part I" vs "Part II")
    sn_nums = set(_NUMS.findall(_fold(raw_seed)))
    cn_nums = set(_NUMS.findall(_fold(cand_title or "")))
    # ... or a number the reference has and the candidate lacks ("Geometric Complexity Theory V")
    num_conflict = float(bool(sn_nums and not (sn_nums & cn_nums)))
    # JSL-style reviews are titled "A. J. Kempner. Remarks on ..." -- a review OF the work
    review_style = bool(re.match(r"^(?:[A-Z]\.\s?){1,3}[A-Z][\w'\-]+(?:\s[A-Z][\w'\-]+)?\.\s", cand_title or ""))
    marker = float((bool(_MARKER.search(_fold(cand_title or ""))) or review_style)
                   and not _MARKER.search(_fold(seed_title or "")))
    sn, cn = _names(seed_authors) | _names([hint_author] if hint_author else []), _names(cand_authors)
    # CONFLICT, not agreement: retrieval filters by author, so in training nearly every
    # wrong candidate shared the author too and "author_ok" learned a NEGATIVE weight.
    # A conflict (both known, no name in common) can only count against a match
    # (and is a hard veto in _accept).
    author_conflict = float(bool(sn and cn and not (sn & cn)))
    author_unknown = float(not sn)
    y = hint_year or seed_year
    ydiff = min(abs(int(y) - int(cand_year)), 5) / 5 if (y and cand_year) else 0.0
    year_unknown = float(not (y and cand_year))
    seq = difflib.SequenceMatcher(None, " ".join(info_kw), " ".join(ckw)).ratio()
    return [cov, miss_mid, rev, lead, marker, author_conflict, author_unknown, ydiff, year_unknown, seq,
            min(len(info_kw), 12) / 12, order, tail, short_cand_on_trunc, num_conflict, is_filename,
            lead * is_filename]


FEATURE_NAMES = ["cov", "miss_mid", "rev", "lead", "marker", "author_conflict", "author_unknown", "ydiff",
                 "year_unknown", "seq", "n_kw", "order", "tail", "short_cand_on_trunc", "num_conflict",
                 "is_filename", "lead_x_filename"]


def _decide(seed, info: dict, cand) -> tuple[str, float]:
    """-> ('accept'|'judge'|'reject', coverage)."""
    kws = info["keywords"]
    cov = _coverage(kws, cand)
    names = R._name_tokens(seed) | set(_kw(info.get("author") or ""))
    author_ok = bool(names & R._name_tokens(cand))
    y = info.get("year") or (seed.year if seed.year and 1400 <= int(seed.year) <= 2027 else None)
    year_ok = bool(y and cand.year and abs(int(y) - int(cand.year)) <= 1)
    year_bad = bool(y and cand.year and abs(int(y) - int(cand.year)) > 3)
    if year_bad or cov < 0.5:
        return "reject", cov
    n = len(kws)
    if cov >= 0.85 and n >= 3 and (author_ok or year_ok):
        return "accept", cov
    if cov == 1.0 and n >= 2 and author_ok and year_ok:
        return "accept", cov
    if cov >= 0.7 and author_ok and year_ok and n >= 3:
        return "accept", cov
    return "judge", cov


def _crossref(query: str, author: str | None) -> list:
    p = {"query.bibliographic": query[:250], "rows": 5, "select": R._CR_SELECT}
    if author:
        p["query.author"] = author
    if R.EMAIL:
        p["mailto"] = R.EMAIL
    for attempt in range(3):
        try:
            r = httpx.get("https://api.crossref.org/works", params=p, timeout=30,
                          headers={"User-Agent": f"mouseion/0.2 (mailto:{R.EMAIL})"})
            if r.status_code == 200:
                out = []
                for it in r.json().get("message", {}).get("items", []) or []:
                    try:
                        out.append(R.CR._parse_work(it))
                    except Exception:
                        pass
                return out
            if r.status_code in (429, 503):
                time.sleep(3 * (attempt + 1))
                continue
            return []
        except Exception:
            time.sleep(2)
    return []


# Logistic sorter fitted 2026-09-24 on 1,893 references with known DOIs whose titles
# were damaged the way the imports damaged them (truncated either end, "Author Year
# keywords" without stopwords, subtitle dropped), 9,152 candidate pairs, labels =
# same work (DOI, or same title/year). Cross-validated by reference: at p >= 0.85,
# 99.1% precision and 73% of findable references resolved (0.90: 99.4% / 64%).
SORTER = [2.3205, -2.8156, 2.2498, 0.3169, -0.7249, -0.2223, -1.1344, -5.2013, -1.7042, 2.6991, 0.7586, 3.3179, 0.9456, -2.0172, -0.7131, -0.5397, -1.2072, -5.9827]
ACCEPT_P = 0.85
JUDGE_P = 0.30


def score(seed, info: dict, cand) -> tuple[float, bool]:
    """-> (probability the candidate is the same work, hard veto)."""
    import math
    seed_auth = [f"{a.given or ''} {a.family or ''}".strip() for a in (seed.authors or [])][:4]
    cand_auth = [f"{a.given or ''} {a.family or ''}".strip() for a in (cand.authors or [])][:4]
    y = seed.year if seed.year and 1400 <= int(seed.year) <= 2027 else None
    x = features(seed.title or "", seed_auth, y, info.get("author"), info.get("year"),
                 cand.title or "", cand_auth, cand.year)
    z = sum(w * v for w, v in zip(SORTER, x + [1.0]))
    p = 1 / (1 + math.exp(-z))
    f = dict(zip(FEATURE_NAMES, x))
    yy = info.get("year") or y
    veto = bool(f["author_conflict"]) or bool(yy and cand.year and abs(int(yy) - int(cand.year)) > 3)
    # Real data (not in the synthetic set): a BOOK reference matched to a journal
    # item is a review of the book ("History of Science as Explanation" -> the
    # Philosophy of Science review "M. A. Finocchiaro <i>History of ...</i>").
    st = str(getattr(seed.ref_type, "value", seed.ref_type) or "")         # RefType enum -> "book"
    ct = str(getattr(getattr(cand, "ref_type", None), "value", getattr(cand, "ref_type", None)) or "")
    if st in ("book", "monograph") and ct in ("journal-article", "article"):
        veto = True
    raw = cand.title or ""
    looks_review = ("<i>" in raw or re.match(r"^(?:[A-Z]\.\s?){1,3}[A-Z][\w'\-]+\s*(?:<i>|[.:])", raw)
                    or re.match(r"^\s*(?:review|book review)\s*[:.]", raw, re.I))   # S2: "Review: A. G. Lunc, ..."
    if looks_review and "<i>" not in (seed.title or "") and not _MARKER.search(_fold(seed.title or "")):
        p = min(p, JUDGE_P + 0.2)          # looks like a review of the work: the judge decides
    return p, veto


def _best(seed, info, cands, best, src):
    for cand in cands:
        p, veto = score(seed, info, cand)
        if not veto and (best is None or p > best[0]):
            best = (p, cand, src)
    return best


def _ident(cand) -> str | None:
    return cand.doi or cand.arxiv_id or cand.pmid or cand.url or cand.oa_url


def _work(rid: str, skip_crossref: bool = False):
    seed = P.DB.get(rid)
    if seed is None:
        return rid, "err", None, None, None
    seed = P._clean_seed(seed)
    info = parse(seed.title or "")
    if info["arxiv"]:
        return rid, "arxiv", info["arxiv"], None, info
    if info["doi"]:
        return rid, "doi", info["doi"], P._crossref_record(info["doi"]), info
    if INCOMPLETE and not seed.authors:
        # Authorless but identified: exact lookup, title-checked by _apply.
        url = seed.url or ""
        m = re.search(r"doi\.org/(10\.\d{4,9}/\S+)", url)
        doi = seed.doi or (m.group(1).rstrip(".,;)") if m else None)
        m = re.search(r"arxiv\.org/(?:abs|pdf)/([\w.\-/]+?\d)(?:v\d+)?(?:\.pdf)?$", url)
        arx = seed.arxiv_id or (m.group(1) if m else None)
        if arx:
            return rid, "arxiv", arx, None, info
        if doi:
            return rid, "doi", doi, P._crossref_record(doi) or P._csl_record(doi), info
        # The library's PDF names are "Surname_Year_words.pdf": an author hint.
        m = re.match(r"([A-Z][A-Za-z'\-]{1,30})_((?:1[5-9]|20)\d\d)_",
                     Path(str((seed.extras or {}).get("pdf_local") or "")).name)
        if m and not info.get("author"):
            info["author"] = m.group(1)
            info["year"] = info.get("year") or int(m.group(2))
    if len(info["keywords"]) < 2:
        return rid, "no_keywords", None, None, info
    author = info.get("author") or R._first_author(seed)
    q = " ".join(info["keywords"]) + (f" {info['year']}" if info.get("year") else "")
    best = None
    if not skip_crossref:
        best = _best(seed, info, _crossref(q, author), best, "crossref")
    if INCOMPLETE and (best is None or best[0] < ACCEPT_P):
        wq = " ".join(filter(None, [author, " ".join(info["keywords"])]))
        best = _best(seed, info, _openalex(wq), best, "openalex")
        if best is None or best[0] < ACCEPT_P:
            best = _best(seed, info, _s2(wq), best, "s2")
    if not best or best[0] < JUDGE_P:
        return rid, "no_record", None, None, info
    info["p"] = round(best[0], 3)
    info["src"] = best[2]
    # two-keyword titles ("Quantum information") are too generic to auto-accept
    # ...unless the whole title is identical ("O przedmiocie matematycznym": stopword-light languages)
    fw = lambda t: re.sub(r"[^a-z0-9]+", " ", _fold(t or "")).split()          # noqa: E731
    same = fw(seed.title) == fw(best[1].title) and len(fw(seed.title)) >= 3
    ok = best[0] >= ACCEPT_P and (len(info["keywords"]) >= 3 or same) and bool(_ident(best[1]))
    return rid, ("accept" if ok else "judge"), _ident(best[1]), best[1], info


def main():
    stamp = time.strftime("%Y%m%d")
    conn = sqlite3.connect(str(P.DB._path), timeout=60,
                           isolation_level=None)   # autocommit: never hold the write lock across network calls
    conn.execute("CREATE TABLE IF NOT EXISTS lossy_scan (ref_id TEXT PRIMARY KEY, result TEXT, found TEXT, "
                 "scanned_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS pdf_id_bak_{stamp} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    conn.commit()
    ledger = "lossy_scan2" if INCOMPLETE else "lossy_scan"
    conn.execute("CREATE TABLE IF NOT EXISTS lossy_scan2 (ref_id TEXT PRIMARY KEY, result TEXT, found TEXT, "
                 "source TEXT, scanned_at TEXT DEFAULT (datetime('now')))")
    crossref_done: set = set()
    if INCOMPLETE:
        # Fails the definition and has a title. Rows the v1 pass still has queued
        # are left to it (no two writers on one ref); rows it logged no_record
        # skip Crossref (already asked).
        ids = [r[0] for r in conn.execute(
            f"""SELECT id FROM refs WHERE COALESCE(status,'') != 'duplicate' AND COALESCE(title,'') != ''
                 AND NOT {RefDatabase.COMPLETE_SQL}
                 AND id NOT IN (SELECT ref_id FROM lossy_scan2)
                 AND NOT (COALESCE(completeness, 0) < 0.8 AND COALESCE(doi,'') = '' AND COALESCE(isbn,'') = ''
                          AND COALESCE(arxiv_id,'') = '' AND id NOT IN (SELECT ref_id FROM lossy_scan))
                 ORDER BY RANDOM() LIMIT ?""", (LIMIT,))]
        crossref_done = {r[0] for r in conn.execute("SELECT ref_id FROM lossy_scan WHERE result='no_record'")}
    else:
        ids = [r[0] for r in conn.execute(
            """SELECT id FROM refs WHERE COALESCE(status,'') != 'duplicate' AND COALESCE(completeness, 0) < 0.8
                 AND COALESCE(doi,'') = '' AND COALESCE(isbn,'') = '' AND COALESCE(arxiv_id,'') = ''
                 AND id NOT IN (SELECT ref_id FROM lossy_scan) ORDER BY RANDOM() LIMIT ?""", (LIMIT,))]
    print(f"[lossy] sources: crossref{' + openalex' if INCOMPLETE and OA_KEY else ''}"
          f"{' + semantic scholar' if INCOMPLETE and S2_KEY else ''} | ledger {ledger}", flush=True)
    print(f"[lossy] {len(ids):,} refs | {'WRITE' if WRITE else 'DRY-RUN'} | workers={WORKERS}", flush=True)
    t0 = time.time()
    arx: list = []
    with ThreadPoolExecutor(WORKERS) as pool:
        futs = [pool.submit(_work, rid, rid in crossref_done) for rid in ids]
        for n, f in enumerate(as_completed(futs), 1):
            rid, kind, ident, cand, info = f.result()
            if kind == "arxiv":
                STATS["embedded_id"] += 1
                arx.append((rid, ident))
                continue
            if kind == "doi":
                STATS["embedded_id"] += 1
                P._apply(conn, stamp, rid, "doi", ident, cand)
            elif kind == "accept":
                # our sorter matched the damaged title; _apply still checks year/authors
                STATS["via_" + info.get("src", "crossref")] += 1
                P._apply(conn, stamp, rid, "pdf_title", ident, cand, verified_title=True)
            elif kind == "judge":
                STATS["judge"] += 1
            else:
                STATS[kind if kind in STATS else "err"] = STATS.get(kind, 0) + 1
            if len(DECISIONS) < SHOW and kind in ("accept", "judge"):
                seed_t = (P.DB.get(rid).title or "")[:50]
                DECISIONS.append([kind, info.get("p"), info.get("src"), seed_t, info.get("author"), info.get("year"),
                                  (cand.title or "")[:55], cand.year, ident])
            if WRITE:
                found = json.dumps({"doi": ident, "p": info.get("p")}) if kind == "judge" and info else ident
                if INCOMPLETE:
                    conn.execute("INSERT OR REPLACE INTO lossy_scan2 (ref_id, result, found, source) VALUES (?,?,?,?)",
                                 (rid, kind, found, (info or {}).get("src")))
                else:
                    conn.execute("INSERT OR REPLACE INTO lossy_scan (ref_id, result, found) VALUES (?,?,?)",
                                 (rid, kind, found))
                if n % 50 == 0:
                    conn.commit()
            if n % 200 == 0:
                print(f"  ... {n:,}/{len(ids):,} | improved {STATS['updated']:,} | judge {STATS['judge']:,} "
                      f"| {n / (time.time() - t0):.2f}/s", flush=True)
    if arx:
        recs = P._arxiv_records(sorted({i for _, i in arx}))
        for rid, ident in arx:
            P._apply(conn, stamp, rid, "arxiv", ident, recs.get(ident))
            if WRITE:
                conn.execute(f"INSERT OR REPLACE INTO {ledger} (ref_id, result, found) VALUES (?,?,?)",
                             (rid, "arxiv", ident))
    if WRITE:
        conn.commit()
    for d in DECISIONS:
        print("DECISION", json.dumps(d, ensure_ascii=False))
    STATS["seconds"] = int(time.time() - t0)
    print(json.dumps({k: v for k, v in STATS.items() if not k.startswith("_")}, default=str), flush=True)


if __name__ == "__main__":
    main()
