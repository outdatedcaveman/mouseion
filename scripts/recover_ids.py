"""Identifier recovery for no-identifier, sub-0.80 references (v2 of recover_hardtail).

Bibliographic search (Crossref + OpenAlex) by title + first author, a STRICT
precision-first match, then Mouseion's own net-positive merge (never clobbers
good data). A recovered DOI raises completeness by itself (+0.15) and usually
brings year, venue, authors and a PDF target along.

What v2 fixes (2026-09-24), measured on recover_hardtail's June run (77,196
checked, 3,559 recovered, 34,630 candidates rejected by score):
  * the accept rules and the cutoff disagreed: "containment >= 0.75 + author
    + year" scored 0.86 and was then discarded by a 0.90 cutoff, so it could
    never accept; "containment >= 0.85 + author" only passed from 0.88.
  * authors were compared by FAMILY name only; many records store given and
    family swapped ({"family": "M.", "given": "Bianchi"}) -> never confirmed.
    Now any shared name token (>= 3 letters) on either side confirms.
  * accents were compared raw ("Gödel" vs "Godel"); now folded.
  * every write run DROPPED and recreated the same backup table; now each run
    writes its own dated backup table.
  * the contact e-mail was hard-coded; now from config (crossref_email /
    openalex_email) or MOUSEION_CONTACT_EMAIL.

Usage:
  python scripts/recover_ids.py <limit> <dry|write> [concurrency] [offset] [--rejected-only]
  --show N   print N accepted matches with seed vs candidate (for manual review)
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import httpx  # noqa: E402

from mouseion.db import RefDatabase  # noqa: E402
from mouseion.merge import merge  # noqa: E402
from mouseion.providers import CrossRefProvider, OpenAlexProvider  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LIMIT = int(args[0]) if len(args) > 0 else 400
WRITE = len(args) > 1 and args[1] == "write"
CONC = int(args[2]) if len(args) > 2 else 12
OFFSET = int(args[3]) if len(args) > 3 else 0
SHOW = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 0
ACCEPT = 0.86

_LATEX = re.compile(r"\$[^$]*\$|\\[a-zA-Z]+|[{}]")
_STOP = {"the", "a", "an", "of", "in", "on", "and", "or", "to", "for", "with", "by", "from", "as", "at",
         "is", "are", "be", "its", "their", "this", "that", "these", "those", "into", "via", "using",
         "uber", "sur", "de", "la", "le", "el", "und", "der", "die", "das", "ein", "eine", "do", "da", "dos",
         "das", "em", "e", "o", "os", "as", "um", "uma", "del", "les", "des", "du"}


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", _LATEX.sub(" ", s or ""))
    return "".join(c for c in s if not unicodedata.combining(c))


def _norm(s: str) -> str:
    return " ".join("".join(c.lower() if (c.isalnum() or c == " ") else " " for c in _fold(s)).split())


def _toks(s: str) -> set:
    return {w for w in _norm(s).split() if len(w) > 2 and w not in _STOP}


def _name_tokens(ref) -> set:
    """Every name token (given AND family, >= 3 letters) of every author."""
    out = set()
    for a in (getattr(ref, "authors", None) or []):
        if hasattr(a, "family"):
            parts = [getattr(a, "family", "") or "", getattr(a, "given", "") or ""]
        elif isinstance(a, dict):
            parts = [a.get("family") or a.get("last") or "", a.get("given") or a.get("first") or ""]
        else:
            parts = [str(a)]
        for p in parts:
            out |= {w for w in _norm(p).split() if len(w) >= 3}
    return out


def _first_author(ref) -> str | None:
    for a in (getattr(ref, "authors", None) or []):
        fam = getattr(a, "family", None) if hasattr(a, "family") else (a.get("family") if isinstance(a, dict) else str(a))
        giv = getattr(a, "given", None) if hasattr(a, "given") else (a.get("given") if isinstance(a, dict) else None)
        # a swapped record stores an initial as "family": query with the longer part
        name = fam if len(_norm(fam or "")) >= len(_norm(giv or "")) else giv
        if name and _norm(name):
            return _norm(name)
    return None


def _score(seed_title, seed_year, seed_names, cand) -> float:
    """Precision-first. 0 = reject; otherwise a confidence >= ACCEPT."""
    st, ctn = _norm(seed_title), _norm(cand.title or "")
    if not st or not ctn:
        return 0.0
    seq = difflib.SequenceMatcher(None, st, ctn).ratio()
    s_tok, c_tok = _toks(seed_title), _toks(cand.title or "")
    if not s_tok or not c_tok:
        return 0.0
    containment = len(s_tok & c_tok) / min(len(s_tok), len(c_tok))
    cy = getattr(cand, "year", None)
    dy = abs(int(cy) - int(seed_year)) if (cy and seed_year) else None
    if dy is not None and dy > 5:
        return 0.0
    year_ok = dy is not None and dy <= 2
    author_ok = bool(seed_names & _name_tokens(cand))
    if seq >= 0.93 and (author_ok or year_ok or not seed_names):
        return min(0.99, 0.93 + 0.06 * min(1.0, containment))
    if containment >= 0.85 and author_ok:
        return 0.90 + 0.08 * (containment - 0.85) / 0.15
    if containment >= 0.75 and author_ok and year_ok:
        return ACCEPT
    return 0.0


def _email() -> str:
    env = os.environ.get("MOUSEION_CONTACT_EMAIL")
    if env:
        return env
    try:
        from mouseion.config import get_config
        cfg = get_config()
        for attr in ("crossref_email", "openalex_email"):
            for holder in (cfg, getattr(cfg, "providers", None), getattr(cfg, "enrichment", None)):
                v = getattr(holder, attr, None) if holder is not None else None
                if v:
                    return v
    except Exception:
        pass
    return ""


CR, OA, DB = CrossRefProvider(), OpenAlexProvider(), RefDatabase()
EMAIL = _email()
_CR_SELECT = ("DOI,type,title,subtitle,author,editor,published,published-print,published-online,issued,"
              "abstract,container-title,short-container-title,volume,issue,page,article-number,publisher,"
              "ISSN,ISBN,URL,subject,is-referenced-by-count")
STATS = {"checked": 0, "recovered": 0, "got_doi": 0, "no_cand": 0, "rejected": 0, "err": 0, "comp_gain": 0.0}
SHOWN: list[str] = []


async def _get(client, url, params):
    for attempt in range(4):
        try:
            r = await client.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503):
                await asyncio.sleep(2 ** attempt * 2)
                continue
            return None
        except Exception:
            await asyncio.sleep(1 + attempt)
    return None


async def _recover(client, ref_id, sem):
    async with sem:
        try:
            seed = DB.get(ref_id)
            if seed is None or (seed.doi or "").strip() or len(seed.title or "") < 12:
                return
            names = _name_tokens(seed)
            a0 = _first_author(seed)
            p = {"query.bibliographic": seed.title[:250], "rows": 5, "select": _CR_SELECT}
            if a0:
                p["query.author"] = a0
            if EMAIL:
                p["mailto"] = EMAIL
            cr = await _get(client, "https://api.crossref.org/works", p)
            oa = await _get(client, "https://api.openalex.org/works",
                            {"search": seed.title[:250], "per_page": 5, **({"mailto": EMAIL} if EMAIL else {})})
            items = [(CR, i) for i in ((cr or {}).get("message", {}).get("items") or [])] + \
                    [(OA, i) for i in ((oa or {}).get("results") or [])]
            cands = []
            for prov, item in items:
                try:
                    cand = prov._parse_work(item)
                except Exception:
                    continue
                s = _score(seed.title, seed.year, names, cand)
                if s >= ACCEPT:
                    cands.append((cand, s))
            STATS["checked"] += 1
            if not cands:
                STATS["no_cand" if not items else "rejected"] += 1
                return
            best, conf = max(cands, key=lambda x: x[1])
            before = seed.completeness
            merged = merge(seed, [(best, conf)])
            gained = bool(merged.doi and not (seed.doi or "").strip())
            if merged.completeness > before + 0.01 or gained:
                STATS["recovered"] += 1
                STATS["comp_gain"] += merged.completeness - before
                STATS["got_doi"] += int(gained)
                if len(SHOWN) < SHOW:
                    SHOWN.append(json.dumps({
                        "conf": round(conf, 3), "seed": [seed.title, seed.year, sorted(names)[:4]],
                        "cand": [best.title, best.year, sorted(_name_tokens(best))[:4], best.doi]},
                        ensure_ascii=False))
                if WRITE:
                    DB.replace_ref(ref_id, merged)
        except Exception:
            STATS["err"] += 1


async def main():
    with DB._db() as conn:
        ids = [r[0] for r in conn.execute(
            """SELECT id FROM refs WHERE completeness < 0.8 AND COALESCE(status,'') != 'duplicate'
                 AND COALESCE(doi,'') = '' AND COALESCE(isbn,'') = '' AND COALESCE(arxiv_id,'') = ''
                 AND title IS NOT NULL AND LENGTH(title) > 12
                 AND ref_type NOT IN ('book', 'book-chapter', 'website')
               ORDER BY completeness DESC LIMIT ? OFFSET ?""", (LIMIT, OFFSET))]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    print(f"[recover_ids] {len(ids):,} refs | {'WRITE' if WRITE else 'DRY-RUN'} | conc={CONC} | "
          f"contact={'set' if EMAIL else 'none'}", flush=True)
    if WRITE:
        with DB._db() as conn:
            conn.execute(f"CREATE TABLE recover_ids_bak_{stamp} AS SELECT id, doi, completeness, title, year, "
                         f"journal, authors, publisher, volume, issue, pages FROM refs WHERE id IN "
                         f"({','.join('?' * len(ids))})", ids) if ids else None
        print(f"[backup] recover_ids_bak_{stamp} ({len(ids):,} rows; reversible)", flush=True)
    sem = asyncio.Semaphore(CONC)
    t0 = time.time()
    headers = {"User-Agent": f"mouseion/0.2 ({'mailto:' + EMAIL if EMAIL else 'library enrichment'})"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), headers=headers,
                                 follow_redirects=True) as client:
        for j in range(0, len(ids), 500):
            await asyncio.gather(*[_recover(client, i, sem) for i in ids[j:j + 500]])
            el = time.time() - t0
            print(f"  ... {STATS['checked']:,}/{len(ids):,} | recovered {STATS['recovered']:,} "
                  f"(DOIs {STATS['got_doi']:,}) | {STATS['checked'] / max(el, 1e-6):.1f}/s", flush=True)
    for s in SHOWN:
        print("MATCH", s)
    print(json.dumps({**STATS, "seconds": int(time.time() - t0), "mode": "write" if WRITE else "dry"}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
