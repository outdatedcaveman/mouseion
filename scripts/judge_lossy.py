"""Second-stage judge for the title-fixer's leftovers (resolve_lossy's `judge` band, then
`no_record`): entries with a title and authors but no identifier.

What the first stage missed, measured on samples (2026-09-30):
  * titles cut at the front or back ("and Power in Nietzsche", "Transitional Justice and the [Re")
  * LaTeX debris from BibTeX imports ("Equivalence relations and textbraceleft rm S textbraceright 5")
  * series prefixes ("Ideas in context: The shaping of deduction ...")
  * works OpenAlex knows and Crossref's ranking buried.
Rule (all three must hold): title similarity >= 0.85 (or one title contains the other,
3+ words), an author surname in common, publication years within one year when both
are known. Sample: 9/40 of the judge band accepted, 9/9 correct by hand.

Writes only empty fields (mouseion.pdf_ingest.fill_plan), after a backup row
(judge_bak_<date>); every entry is logged in lossy_judge (resumable).

Usage: python scripts/judge_lossy.py <limit> <dry|write> [workers] [--band judge|no_record] [--reviews] [--show N]
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from mouseion import pdf_ingest as PI  # noqa: E402
from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase  # noqa: E402
from mouseion.providers.crossref import CrossRefProvider  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LIMIT = int(args[0]) if args else 100
WRITE = len(args) > 1 and args[1] == "write"
WORKERS = int(args[2]) if len(args) > 2 else 4
BAND = sys.argv[sys.argv.index("--band") + 1] if "--band" in sys.argv else "judge"
SHOW = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else (40 if not WRITE else 0)
STAMP = date.today().strftime("%Y%m%d")
CFG = get_config()
MAILTO = CFG.crossref_email or CFG.openalex_email or ""
OA_KEY = getattr(CFG, "openalex_api_key", "") or ""
_UA = {"User-Agent": "mouseion/0.3 (https://github.com/outdatedcaveman/mouseion; library repair)"}

SERIES = re.compile(r"^(?:(?:cambridge|oxford|lecture notes|studies|library|ideas in context|texts|graduate texts|"
                    r"synthese library|boston studies|the [\w ]+ series|[\w ]{3,60} series|"
                    r"[\w ]{0,40}companions? to [\w ]+|[\w ]{0,40}tracts in [\w ]+|[\w ]{0,40}handbooks? of [\w ]+|"
                    r"london mathematical society[\w ]*)[^:]{0,40}:\s+)", re.I)
LATEX = re.compile(r"\b(textbraceleft|textbraceright|textbackslash|mathbf|mathrm|mathcal|mathbb|rm|bf|it|"
                   r"textit|textbf|emph|textasciicircum|textunderscore|textendash|textemdash)\b|[{}$\\^_]")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def clean_title(t: str) -> str:
    t = SERIES.sub("", t or "").strip()
    t = LATEX.sub(" ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(t.split())


def title_match(stored: str, found: str) -> float:
    """Similarity; a STORED title that is a cut-off piece of the found one counts as 0.9
    (never the reverse: a stored 'Symposium honoring X' is not one paper inside it)."""
    ns, nf = norm(clean_title(stored)), norm(clean_title(found))
    if not ns or not nf:
        return 0.0
    sim = difflib.SequenceMatcher(None, ns, nf).ratio()
    if len(ns.split()) >= 3 and len(ns) < len(nf) and ns in nf:
        sim = max(sim, 0.9)
    return sim


_local = threading.local()


def client() -> httpx.Client:
    if not hasattr(_local, "c"):
        _local.c = httpx.Client(timeout=30, headers=_UA, follow_redirects=True)
    return _local.c


def candidates(title: str, fams: list, journal: str) -> list:
    """[(title, surnames, year, doi, crossref item or None)]"""
    out = []
    q = " ".join([title, " ".join(fams[:2]), journal or ""])
    try:
        r = client().get("https://api.crossref.org/works",
                         params={"query.bibliographic": q, "rows": 5, **({"mailto": MAILTO} if MAILTO else {})})
        for it in r.json()["message"]["items"]:
            people = it.get("author", []) + it.get("editor", [])
            out.append(((it.get("title") or [""])[0], {norm(x.get("family", "")) for x in people},
                        (it.get("issued", {}).get("date-parts") or [[None]])[0][0], it.get("DOI"), it))
    except Exception:
        pass
    try:
        params = {"search": title[:200], "per_page": 5}
        if OA_KEY:
            params["api_key"] = OA_KEY
        for it in client().get("https://api.openalex.org/works", params=params).json().get("results", []):
            fam = {norm(((a.get("author") or {}).get("display_name") or "").split()[-1])
                   for a in it.get("authorships", []) if (a.get("author") or {}).get("display_name")}
            doi = (it.get("doi") or "").replace("https://doi.org/", "")
            if doi:
                out.append((it.get("title") or "", fam, it.get("publication_year"), doi, None))
    except Exception:
        pass
    return out


REVIEW = re.compile(r"^(?:review(?: of)?:?\s+)?(?P<book>.{6,}?)\s+by\s+(?P<author>[A-Z][\w.'’\- ]{2,60}?)"
                    r"(?:\s*[;:(]|\s*$)")


def judge_review(seed, title: str) -> tuple:
    """'Kultur der Urzeit by Hoernes, Moritz' -- a book review, as JSTOR/Isis title them.
    Crossref often lacks the reviewer, so the stored author can't be checked; instead the
    found title must name the reviewed book AND its author, in the same year."""
    m = REVIEW.match(title)
    if not m or not seed.year:
        return None, ""
    book, bauthor = m.group("book"), norm(m.group("author").split(",")[0]).split()
    if not bauthor:
        return None, ""
    for ht, hf, hy, doi, item in candidates(title, [a.family for a in seed.authors if a.family], seed.journal or ""):
        nh = norm(ht)
        if hy and int(hy) == int(seed.year) and title_match(book, ht) >= 0.85 and \
                (bauthor[-1] in nh.split() or bauthor[0] in nh.split()):
            rec = CrossRefProvider()._parse_work(item) if item is not None else PI._crossref_doi(client(), doi, MAILTO)
            if rec is not None:
                rec.doi = rec.doi or doi
                rec.title = " ".join(re.sub(r"<[^>]+>", "", rec.title or "").split())
                rec.authors = rec.authors or list(seed.authors)     # the reviewer stays
                return rec, "review"
    return None, ""


def judge(seed) -> tuple:
    """-> (record or None, reason)"""
    fams = [a.family for a in seed.authors if a.family]
    want = {norm(f).split()[-1] for f in fams if norm(f)}
    title = clean_title(seed.title or "")
    rec, why = judge_review(seed, title)
    if rec is not None:
        return rec, why
    if len(norm(title).split()) < 2 or not want:
        return None, "too_little"
    best = None
    for ht, hf, hy, doi, item in candidates(title, fams, seed.journal or ""):
        sim = title_match(title, ht)
        if sim < 0.85 or not (want & {x.split()[-1] for x in hf if x}):
            continue
        if seed.year and hy and abs(int(hy) - int(seed.year)) > 1:
            continue
        if best is None or sim > best[0]:
            best = (sim, doi, item)
    if best is None:
        return None, "no_match"
    sim, doi, item = best
    try:
        if item is not None:
            rec = CrossRefProvider()._parse_work(item)
        else:
            rec = PI._crossref_doi(client(), doi, MAILTO)
    except Exception:
        rec = None
    if rec is None:
        return None, "no_record"
    rec.doi = rec.doi or doi
    return rec, f"match {sim:.2f}"


def main() -> None:
    dbp = str(Path(CFG.db_path).expanduser())
    conn = sqlite3.connect(dbp, timeout=120, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS lossy_judge (ref_id TEXT PRIMARY KEY, result TEXT, doi TEXT, "
                 "scanned_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS judge_bak_{STAMP} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    if "--reviews" in sys.argv:      # 'Book by Author' titles the first judge pass turned down
        ids = [r[0] for r in conn.execute(f"""SELECT r.id FROM refs r JOIN lossy_judge j ON j.ref_id = r.id
            WHERE j.result != 'accept' AND r.title LIKE '% by %' AND NOT ({RefDatabase.COMPLETE_SQL})
            AND COALESCE(r.doi,'') = '' LIMIT ?""", (LIMIT,))]
    else:
        ids = [r[0] for r in conn.execute(f"""SELECT r.id FROM refs r JOIN lossy_scan2 l ON l.ref_id = r.id
            WHERE l.result = ? AND NOT ({RefDatabase.COMPLETE_SQL}) AND COALESCE(r.doi,'') = ''
            AND r.id NOT IN (SELECT ref_id FROM lossy_judge) ORDER BY RANDOM() LIMIT ?""", (BAND, LIMIT))]
    print(f"[judge] band={BAND} | {len(ids):,} entries | {'WRITE' if WRITE else 'DRY-RUN'} | workers={WORKERS}",
          flush=True)
    db = RefDatabase()
    cols = [d[0] for d in conn.execute("SELECT * FROM refs LIMIT 0").description]
    stats: Counter = Counter()
    t0 = time.time()
    seeds = {rid: db.get(rid) for rid in ids}
    shown = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for n, (rid, (rec, why)) in enumerate(zip(ids, pool.map(lambda i: judge(seeds[i]) if seeds[i] else (None, "gone"), ids)), 1):
            seed = seeds[rid]
            up = PI.fill_plan(seed, rec) if (rec is not None and seed is not None) else {}
            result = "accept" if up else why.split()[0]
            stats[result] += 1
            if up and WRITE:
                row = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
                conn.execute(f"INSERT OR IGNORE INTO judge_bak_{STAMP} VALUES (?,?)",
                             (rid, json.dumps(dict(zip(cols, row)), default=str)))
                PI.apply_fill(db, rid, up)
            if WRITE:
                conn.execute("INSERT OR REPLACE INTO lossy_judge (ref_id, result, doi) VALUES (?,?,?)",
                             (rid, result, (rec.doi if rec is not None else "") or ""))
            if up and shown < SHOW:
                shown += 1
                print(f"  {why:10s} {(seed.title or '')[:50]:50s} -> {(rec.title or '')[:50]:50s} | "
                      f"{seed.year}->{rec.year} | {rec.doi}", flush=True)
            if n % 100 == 0:
                print(f"  ... {n:,}/{len(ids):,} | {dict(stats)} | {n / (time.time() - t0):.2f}/s", flush=True)
    print(f"done: {dict(stats)} in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
