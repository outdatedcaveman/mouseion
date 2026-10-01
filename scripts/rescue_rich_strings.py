"""Rescue entries whose "title" is a rich string, not junk (owner, 2026-09-30: "LNCS 3796 is
CLEARLY a rich string ... be more thorough").

Recognised and acted on (each change backed up in rescue_bak_<date>, logged in rescue_scan):
  * JSTOR stable id as title/file name ('2042657', '2042657.pdf')  -> 10.2307/<id>; accepted
    when the record's journal or year agrees with the archive folder ('.../Proceedings of the
    American Mathematical Society/pdf/1960_v011_n03/') or its title words are in the PDF
  * NBER working paper ('w31779')                                   -> 10.3386/w31779
  * Elsevier PII ('PII 0168 0072 89 90009')                          -> 10.1016/0168-0072(89)90009-<check>
  * a URL as the title                                               -> the URL becomes the entry's
    link; a DOI or arXiv id inside it is resolved
  * a whole journal issue as file name ('JMP1984V25N12')             -> described: 'Journal of
    Mathematical Physics, vol. 25, no. 12 (1984)', journal/volume/issue/year filled
  * encoded/joined file-name titles ('motizuki 2020 11 20Classical 20roots 20of 20IUT',
    'teoremaBell_Peres_AJP1978', 'Causalworlds_2024_Book_of_abstracts')
                                                                     -> readable title restored
                                                                        (then searched by web_trail)
Records must agree with what the entry already says; nothing is guessed.

Usage: python scripts/rescue_rich_strings.py <dry|write> [--show N]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import unquote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
import httpx  # noqa: E402

import junk_candidates as J  # noqa: E402
from mouseion import pdf_ingest as PI  # noqa: E402
from mouseion import web_trail as W  # noqa: E402
from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase  # noqa: E402

WRITE = len(sys.argv) > 1 and sys.argv[1] == "write"
SHOW = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 60
STAMP = date.today().strftime("%Y%m%d")
CFG = get_config()
MAILTO = CFG.crossref_email or CFG.openalex_email or ""


def readable(title: str) -> str:
    """Undo file-name encodings: %20 / '20' runs, underscores, camelCase and letter-digit joins."""
    t = unquote(title or "")
    t = re.sub(r"(?<=[\s\d])20(?=[A-Za-z(])", " ", t)                          # leftover '%20': '20Classical', ' 20roots'
    t = re.sub(r"\b28(?=[a-z])", "(", t)                                          # '%28' -> '28marked'
    t = re.sub(r"\.(pdf|djvu|docx?)\b", " ", t, flags=re.I)
    t = t.replace("_", " ")
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t)                                    # teoremaBell
    t = re.sub(r"(?<=[A-Za-z]{3})(?=\d{4}\b)", " ", t)                            # AJP1978
    t = SHADOW_PREFIX.sub(" ", t)
    out = []
    for tok in t.split():                       # 'Diamondsintherough' -> 'Diamonds in the rough'
        if len(tok) >= 16 and tok[1:].islower() and _split is not None:     # long glue only: 'Schrodingers' stays
            parts = _split(tok)
            if len(parts) > 1 and all(len(x) >= 2 or x.lower() in ("a", "i") for x in parts):
                out.extend(parts)
                continue
        out.append(tok)
    return " ".join(out)


SHADOW_PREFIX = re.compile(r"\b(ebooksclub\s*org|z-?lib(\s*org)?|libgen(\s*\w+)?|www\s+[\w-]+\s+(com|org|net)|"
                           r"booksee|1lib|epdf\s*pub)\b", re.I)
try:                                            # word splitter for glued file names (offline, English)
    from wordninja import split as _split
except Exception:
    _split = None


def folder_hints(pdf: str):
    f = PI.PdfFacts(path=pdf or "x")
    year = journal = None
    for part in reversed(Path(pdf or "").parts[:-1]):
        m = re.search(r"(1[6-9]\d\d|20[0-4]\d)", part)
        if m and not year:
            year = int(m.group(1))
        if not journal and re.search(r"(journal|proceedings|annals|transactions|mathematica|review|bulletin|"
                                     r"computation|letters|acta|quarterly|society)", part, re.I):
            journal = part
    return year, journal


def main() -> None:
    dbp = Path(CFG.db_path).expanduser()
    conn = sqlite3.connect(str(dbp), timeout=120, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS rescue_scan (ref_id TEXT PRIMARY KEY, result TEXT, detail TEXT, "
                 "scanned_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS rescue_bak_{STAMP} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    done = {r[0] for r in conn.execute("SELECT ref_id FROM rescue_scan")}
    db = RefDatabase()
    cl = httpx.Client(timeout=30, headers={"User-Agent": "mouseion/0.3 (library repair)"}, follow_redirects=True)
    cols = [d[0] for d in conn.execute("SELECT * FROM refs LIMIT 0").description]
    stats: Counter = Counter()
    shown = 0
    rows = conn.execute(f"""SELECT id, title, pdf_local, doi, url FROM refs WHERE NOT ({RefDatabase.COMPLETE_SQL})""").fetchall()
    for rid, title, pdf, doi, url in rows:
        if rid in done or doi:
            continue
        t = (title or "").strip()
        stem = re.sub(r"\.(pdf|djvu)$", "", (pdf or "").split("\\")[-1], flags=re.I)
        up, how = {}, ""
        seed = None
        # --- URL as title
        if re.match(r"https?://", t):
            seed = db.get(rid)
            how = "url-title"
            if not url:
                up["url"] = t
            m = W.DOI_IN_URL.search(unquote(t))
            if m:
                rec = PI._crossref_doi(cl, m.group(1).rstrip(".,;)"), MAILTO)
                if rec:
                    up.update({k: v for k, v in PI.fill_plan(seed, rec).items()})
                    up["doi"] = rec.doi or m.group(1)
                    how = "url-title+doi"
        # --- NBER working paper
        elif re.fullmatch(r"w\d{4,5}", t) or re.fullmatch(r"w\d{4,5}", stem):
            num = t if re.fullmatch(r"w\d{4,5}", t) else stem
            rec = PI._crossref_doi(cl, f"10.3386/{num}", MAILTO)
            if rec and rec.title:
                seed = db.get(rid)
                seed.title = None
                up, how = PI.fill_plan(seed, rec), "nber"
                up["doi"] = rec.doi or f"10.3386/{num}"
        # --- JSTOR stable id
        elif re.fullmatch(r"\d{6,9}", t) or re.fullmatch(r"\d{6,9}", stem or "-"):
            num = t if re.fullmatch(r"\d{6,9}", t) else stem
            rec = PI._crossref_doi(cl, f"10.2307/{num}", MAILTO)
            if rec and rec.title:
                fy, fj = folder_hints(pdf or "")
                agree = (fy and rec.year and abs(int(rec.year) - fy) <= 1) or \
                        (fj and rec.journal and W.coverage(rec.journal, fj) >= 0.6)
                if not agree and pdf and Path(pdf).exists():
                    f = PI.extract(pdf)
                    agree = PI._title_ok(f, rec)
                if agree:
                    seed = db.get(rid)
                    seed.title = None
                    up, how = PI.fill_plan(seed, rec), "jstor"
                    up["doi"] = rec.doi or f"10.2307/{num}"
                else:
                    how = "jstor-unconfirmed"
        # --- Elsevier PII
        elif J.PII_TITLE.match(t):
            m = J.PII_TITLE.match(t)
            for chk in "0123456789X":
                cand = f"10.1016/{m.group(1)}-{m.group(2)}({m.group(3)}){m.group(4)}-{chk}".lower()
                rec = PI._crossref_doi(cl, cand, MAILTO)
                if rec and rec.title:
                    seed = db.get(rid)
                    seed.title = None
                    up, how = PI.fill_plan(seed, rec), "pii"
                    up["doi"] = rec.doi or cand
                    break
        else:
            # --- whole journal issue
            for x in (t, stem):
                m = J.JOURNAL_FILE.match(x or "")
                if m:
                    j = J.JOURNAL_CODES.get(m.group("j"), m.group("j"))
                    up = {"title": f"{j}, vol. {int(m.group('v'))}, no. {int(m.group('n'))} ({m.group('y')})",
                          "journal": j, "volume": str(int(m.group("v"))), "issue": str(int(m.group("n"))),
                          "year": int(m.group("y"))}
                    how = "journal-issue"
                    break
            # --- encoded / joined file-name title -> readable
            if not up:
                r = readable(t)
                if r and r != t and J.reason(t, set(), Counter()).startswith("unreadable") and \
                        not J.reason(r, set(), Counter()):
                    up, how = {"title": r[:300]}, "readable-title"
        if not how:
            continue
        stats[how] += 1
        if up and WRITE:
            row = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
            conn.execute(f"INSERT OR IGNORE INTO rescue_bak_{STAMP} VALUES (?,?)",
                         (rid, json.dumps(dict(zip(cols, row)), default=str)))
            PI.apply_fill(db, rid, up)
            if how in ("readable-title",):
                conn.execute("DELETE FROM web_trail_scan WHERE ref_id=?", (rid,))      # search it again, now readable
        if WRITE:
            conn.execute("INSERT OR REPLACE INTO rescue_scan (ref_id, result, detail) VALUES (?,?,?)",
                         (rid, how, json.dumps({k: str(v)[:120] for k, v in up.items()}, ensure_ascii=False)))
        if shown < SHOW and up:
            shown += 1
            print(f"  {how:15s} {t[:55]:55s} -> {str(up.get('title') or up.get('url') or '')[:60]:60s} {up.get('doi', '')}",
                  flush=True)
    print(f"{'written' if WRITE else 'dry run'}: {dict(stats)}", flush=True)


if __name__ == "__main__":
    main()
