"""Records for references the regex passes could not identify, read from their OWN PDF
with source-grounded extraction (langextract); and published-version DOIs for arXiv refs.

Why (trial 2026-10-16, 20 refs with a PDF and no DOI/year): langextract (Google) on the
first two pages, gemini flash-lite on the FREE tier, returns title / authors / year /
venue / pages / publisher, each tied to an exact span of the page. 0 fabricated values
(no year where none is printed), 3 wrong stored records exposed. Fed to Crossref, those
fields found 9/20 DOIs vs 5/20 from the stored record, all 9 verified -- 4 of them on refs
that recover_ids_from_pdfs / resolve_lossy had already failed. Grounded is not the same as
right (a book review gave the reviewed book's author; an arXiv v2 date was taken as the
year), so nothing extracted is written directly: it only drives the lookup, and the record
written is the Crossref one, accepted by resolve_lossy's sorter AND by _apply's check
against the stored reference (title / year / author), backed up, merged net-positively.

Modes
  lx     refs with a PDF that earlier passes failed on, in this order:
           A. authorless (fail the completeness definition) and complete_from_pdfs found
              nothing on the page  -> a verified record completes them;
           B. no DOI, and pdf_id_scan / lossy_scan ended in no_record / title_mismatch /
              no_id / no_keywords.
         One Gemini call per ref, capped per day (--cap, default 18; usage in
         langextract_budget.json); stops for the day at the first quota error.
  arxiv  refs with an arXiv id and no DOI: the DOI of the PUBLISHED version, from Semantic
         Scholar's batch endpoint (externalIds.DOI, 500 ids a call) and, failing that, the
         DOI the authors gave arXiv (approach of bibtex-updater / the zbMATH Open matcher;
         rebiber's DBLP route only covers conferences). arXiv's own DataCite DOIs
         (10.48550/...) are not written: `doi` is kept for the published version. Every DOI
         is confirmed by its Crossref record (title via _apply, plus an author check).

A DOI that another live row already holds is never written; the pair goes to
doi_collisions (the same table db.replace_ref now fills) for the duplicate review.
Ledger grounded_scan (ref_id, mode) -- resumable; backups pdf_id_bak_<date>.

langextract runs in its own venv (it would upgrade google-auth in the app's venv):
  python scripts/grounded_meta.py --setup          # creates ~/.local/share/mouseion/langextract-venv
Key: MOUSEION_LX_GEMINI_KEY = a FREE-tier Gemini key (Egon passes its own). Never the
billed key in config.toml. Model: MOUSEION_LX_MODEL (default gemini-3.1-flash-lite).

Usage: python scripts/grounded_meta.py <lx|arxiv> <limit> <dry|write> [--cap N] [--ids a,b] [--show N]
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

DATA = Path.home() / ".local" / "share" / "mouseion"
LX_VENV = DATA / "langextract-venv"
BUDGET = DATA / "langextract_budget.json"
CACHE = DATA / "langextract_cache.jsonl"     # every extraction kept: a dry run then a write run pays once


def _setup() -> None:
    import shutil
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv not found: install it, or create the venv by hand and `pip install langextract==1.7.1`")
    subprocess.run([uv, "venv", str(LX_VENV), "--python", "3.12"], check=True)
    subprocess.run([uv, "pip", "install", "--python", str(LX_VENV / "Scripts" / "python.exe"), "langextract==1.7.1"],
                   check=True)
    print(f"langextract venv ready: {LX_VENV}")


if "--setup" in sys.argv:
    _setup()
    sys.exit(0)

_argv = list(sys.argv)
sys.argv = [sys.argv[0], "1", "dry"]          # the imported modules parse argv at import time
import recover_ids as R                        # noqa: E402  name tokens
import recover_ids_from_pdfs as P              # noqa: E402  PDF bytes/text, Crossref/arXiv records, _apply
import resolve_lossy as L                      # noqa: E402  Crossref search, sorter, S2 key + rate
sys.argv = _argv

import httpx  # noqa: E402

from mouseion.db import RefDatabase  # noqa: E402
from mouseion.models import Author, Reference  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
_opt = lambda name, default: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default  # noqa: E731
MODE = args[0] if args else "lx"
LIMIT = int(args[1]) if len(args) > 1 else 20
WRITE = len(args) > 2 and args[2] == "write"
CAP = int(_opt("--cap", "18"))
SHOW = int(_opt("--show", "0"))
MAX_MIN = float(_opt("--max-minutes", "90"))   # Egon's rotation never kills a job: the job stops itself
CALL_TIMEOUT_S = 300                           # one langextract call (its own 503 retries included)
import socket  # noqa: E402
socket.setdefaulttimeout(120)                  # Drive downloads (httplib2) and every other socket
IDS = [i for i in _opt("--ids", "").split(",") if i]
P.WRITE = WRITE                                 # _apply writes only when this is True
STATS = {"mode": MODE, "checked": 0, "updated": 0, "rejected": 0, "no_record": 0, "collision": 0,
         "no_text": 0, "no_pdf": 0, "lx_calls": 0, "lx_err": 0, "lx_quota": False, "found_doi": 0,
         "via_lx_doi": 0, "via_lx_search": 0, "via_s2": 0, "via_arxiv": 0, "author_mismatch": 0}
SAMPLES: list = []

LIVE = "COALESCE(status,'') != 'duplicate'"
HAS_PDF = "(COALESCE(pdf_local,'') != '' OR COALESCE(pdf_drive_id,'') != '')"
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)


# ---------------------------------------------------------------- shared

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(P.DB.path if hasattr(P.DB, "path") else P.DB._path), timeout=60, isolation_level=None)   # autocommit, like the others
    conn.row_factory = None
    conn.execute("CREATE TABLE IF NOT EXISTS grounded_scan (ref_id TEXT, mode TEXT, result TEXT, found TEXT, "
                 "fields TEXT, scanned_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (ref_id, mode))")
    conn.execute("CREATE TABLE IF NOT EXISTS doi_collisions (ref_id TEXT, doi TEXT, holder_id TEXT, source TEXT, "
                 "seen_at TEXT DEFAULT (datetime('now')), PRIMARY KEY (ref_id, doi))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS pdf_id_bak_{time.strftime('%Y%m%d')} "
                 "(ref_id TEXT PRIMARY KEY, row_json TEXT)")
    return conn


def _log(conn, rid, result, found=None, fields=None) -> None:
    if SHOW:
        print("RESULT", rid[:10], result, found or "",
              json.dumps({k: v[:2] for k, v in (fields or {}).items() if k in ("title", "author", "year", "doi")},
                         ensure_ascii=False)[:200], flush=True)
    if WRITE:
        conn.execute("INSERT OR REPLACE INTO grounded_scan (ref_id, mode, result, found, fields) VALUES (?,?,?,?,?)",
                     (rid, MODE, result, found, json.dumps(fields, ensure_ascii=False) if fields else None))


def _holder(conn, doi: str, rid: str) -> str | None:
    """Another LIVE row already carrying this DOI (exact or lower-case: both hit idx_doi)."""
    r = conn.execute(f"SELECT id FROM refs WHERE doi IN (?, ?) AND id != ? AND {LIVE} LIMIT 1",
                     (doi, doi.lower(), rid)).fetchone()
    return r[0] if r else None


_TAG = re.compile(r"<[^>]+>")
_WORDS = re.compile(r"[^\W_]{3,}")


def _keep_better_title(seed_title: str | None, cand) -> None:
    """Crossref titles can be worse than the stored one: MathML / <i> markup, or the math
    dropped ('SOME CONSEQUENCES OF AND' for 'Some consequences of TD and sTD'): measured on
    the first 82 arXiv writes, 4 titles degraded. Strip markup; when the candidate then has
    fewer words than the stored title and does not extend a truncated one, keep the stored."""
    import html as _html
    if cand is None or not cand.title:
        return
    t = " ".join(_html.unescape(_TAG.sub(" ", cand.title)).split())
    s = seed_title or ""
    sw, tw = _WORDS.findall(s.lower()), _WORDS.findall(t.lower())
    extends = bool(sw) and tw[:len(sw)] == sw
    cand.title = s if (s and len(tw) < len(sw) and not extends) else t


def _write_doi(conn, stamp, rid, kind, doi, cand, via, exact=False) -> str:
    """Collision check -> author check -> _apply. Returns the verdict. exact: the DOI is tied
    to the ref by an identifier (its arXiv id), so a truncated stored title gives way."""
    holder = _holder(conn, doi, rid)
    if holder:
        STATS["collision"] += 1
        if WRITE:
            conn.execute("INSERT OR IGNORE INTO doi_collisions (ref_id, doi, holder_id, source) VALUES (?,?,?,?)",
                         (rid, doi, holder, f"grounded_meta:{MODE}"))
        return "collision"
    seed = P.DB.get(rid)
    if seed is not None:
        _keep_better_title(seed.title, cand)
        # a stored title cut off mid-way ("Deformations of quasi") makes Mouseion's merge drop
        # the very record that completes it; when its words are a prefix of the accepted
        # record's title, let it give way (resolve_lossy's verified_title, same reason)
        sw = _WORDS.findall((seed.title or "").lower())
        cw = _WORDS.findall(((cand.title if cand else "") or "").lower())
        if sw and len(cw) > len(sw) and cw[:len(sw)] == sw:
            exact = True
    if seed is not None and cand is not None:
        names = R._name_tokens(seed)
        if names and not (names & R._name_tokens(cand)):
            STATS["author_mismatch"] += 1
            return "author_mismatch"
    res = P._apply(conn, stamp, rid, kind, doi, cand, verified_title=exact, log_scan=False)
    if res == "improved":
        STATS["updated"] += 1
        STATS[via] = STATS.get(via, 0) + 1
    elif res == "title_mismatch":
        STATS["rejected"] += 1
    elif res == "no_record":
        STATS["no_record"] += 1
    if len(SAMPLES) < SHOW:
        SAMPLES.append([res, via, rid[:10], (seed.title if seed else "")[:60], ((cand.title if cand else "") or "")[:60],
                        cand.year if cand else None, doi])
    return res or "err"


# ---------------------------------------------------------------- arXiv -> published DOI

def _s2_batch(arxiv_ids: list[str]) -> dict:
    """arXiv id -> published DOI (Semantic Scholar externalIds), 500 ids per call."""
    out: dict = {}
    if not L.S2_KEY:
        return out
    for i in range(0, len(arxiv_ids), 500):
        chunk = arxiv_ids[i:i + 500]
        for attempt in range(4):
            L._S2_RATE.wait()
            try:
                r = httpx.post("https://api.semanticscholar.org/graph/v1/paper/batch",
                               params={"fields": "externalIds"}, json={"ids": [f"arXiv:{a}" for a in chunk]},
                               headers={"x-api-key": L.S2_KEY}, timeout=60)
            except Exception:
                time.sleep(3)
                continue
            if r.status_code == 200:
                for aid, d in zip(chunk, r.json()):
                    doi = ((d or {}).get("externalIds") or {}).get("DOI")
                    if doi and not doi.lower().startswith("10.48550/"):
                        out[aid] = doi
                break
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            break
    return out


def run_arxiv(conn, stamp) -> None:
    if IDS:
        rows = conn.execute(f"SELECT id, arxiv_id FROM refs WHERE COALESCE(arxiv_id,'') != '' "
                            f"AND id IN ({','.join('?' * len(IDS))})", IDS).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT id, arxiv_id FROM refs WHERE {LIVE} AND COALESCE(arxiv_id,'') != '' AND COALESCE(doi,'') = ''
                AND id NOT IN (SELECT ref_id FROM grounded_scan WHERE mode = 'arxiv')
                ORDER BY RANDOM() LIMIT ?""", (LIMIT,)).fetchall()
    print(f"[grounded arxiv] {len(rows):,} refs | {'WRITE' if WRITE else 'DRY-RUN'} | S2 key "
          f"{'yes' if L.S2_KEY else 'NO'}", flush=True)
    norm = {rid: re.sub(r"v\d+$", "", (aid or "").strip().removeprefix("arXiv:")) for rid, aid in rows}
    s2 = _s2_batch(sorted(set(norm.values())))
    missing = sorted({a for a in norm.values() if a not in s2})
    ax = P._arxiv_records(missing) if missing else {}
    for rid, aid in norm.items():
        STATS["checked"] += 1
        doi, via = s2.get(aid), "via_s2"
        if not doi:
            rec = ax.get(aid)
            d = (rec.doi or "") if rec is not None else ""
            doi, via = (d, "via_arxiv") if d and not d.lower().startswith("10.48550/") else (None, None)
        if not doi:
            _log(conn, rid, "no_published_doi")
            continue
        STATS["found_doi"] += 1
        cand = P._crossref_record(doi)
        res = _write_doi(conn, stamp, rid, "doi", doi, cand, via, exact=True)
        _log(conn, rid, res, doi)


# ---------------------------------------------------------------- langextract on the PDF

def _cache() -> dict:
    out = {}
    try:
        for line in CACHE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                if not d.get("err"):
                    out[d["id"]] = d
    except Exception:
        pass
    return out


def _cache_add(d: dict) -> None:
    try:
        with CACHE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _budget() -> dict:
    try:
        b = json.loads(BUDGET.read_text(encoding="utf-8"))
    except Exception:
        b = {}
    if b.get("date") != date.today().isoformat():
        b = {"date": date.today().isoformat(), "calls": 0, "quota_hit": False}
    return b


def _save_budget(b: dict) -> None:
    try:
        BUDGET.write_text(json.dumps(b), encoding="utf-8")
    except Exception:
        pass


def _runner() -> subprocess.Popen:
    """The langextract worker under its own venv, through the BASE interpreter with the
    venv's site-packages on PYTHONPATH: a venv's python.exe is a launcher that can open
    a console window when the parent has none."""
    cfg = (LX_VENV / "pyvenv.cfg").read_text(encoding="utf-8")
    home = next(l.split("=", 1)[1].strip() for l in cfg.splitlines() if l.lower().replace(" ", "").startswith("home="))
    env = dict(os.environ, PYTHONPATH=str(LX_VENV / "Lib" / "site-packages"), PYTHONIOENCODING="utf-8")
    return subprocess.Popen([str(Path(home) / "python.exe"), "-X", "utf8", str(REPO / "scripts" / "lx_runner.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                            creationflags=0x08000000)


def _pool(conn, n: int) -> list:
    if IDS:
        return conn.execute(f"SELECT id, pdf_drive_id, pdf_local FROM refs WHERE id IN ({','.join('?' * len(IDS))})",
                            IDS).fetchall()
    done = "id NOT IN (SELECT ref_id FROM grounded_scan WHERE mode = 'lx')"
    a = conn.execute(
        f"""SELECT id, pdf_drive_id, pdf_local FROM refs WHERE {LIVE} AND {HAS_PDF} AND {done}
            AND COALESCE(authors,'') IN ('','[]') AND COALESCE(title,'') != ''
            AND id IN (SELECT ref_id FROM own_pdf_scan WHERE result IN ('nothing','error'))
            ORDER BY RANDOM() LIMIT ?""", (n,)).fetchall()
    if len(a) >= n:
        return a
    b = conn.execute(
        f"""SELECT id, pdf_drive_id, pdf_local FROM refs WHERE {LIVE} AND {HAS_PDF} AND {done}
            AND COALESCE(doi,'') = ''
            AND (id IN (SELECT ref_id FROM pdf_id_scan
                        WHERE result IN ('pdf_title:no_record','pdf_title:title_mismatch','no_id'))
                 OR id IN (SELECT ref_id FROM lossy_scan WHERE result IN ('no_record','no_keywords')))
            ORDER BY RANDOM() LIMIT ?""", (n - len(a),)).fetchall()
    return a + b


def _text(row):
    rid, drive_id, local = row
    try:
        data = P._pdf_bytes(drive_id, local)
        if not data:
            return rid, None, "no_pdf"
        t = P._first_pages_text(data)
        return rid, t, ("ok" if len(t.strip()) >= 40 else "no_text")
    except Exception as e:
        return rid, None, f"err:{type(e).__name__}"


def _year(vals) -> int | None:
    for v in vals or []:
        m = re.search(r"\b(1[4-9]\d\d|20\d\d)\b", v)
        if m and int(m.group(1)) <= date.today().year + 1:
            return int(m.group(1))
    return None


_SPACING = {"¨": "̈", "´": "́", "`": "̀", "ˆ": "̂", "˜": "̃",
            "¸": "̧", "ˇ": "̌"}
_SPACING_RE = re.compile("([" + "".join(_SPACING) + "])\\s?([A-Za-zı])")   # ı: TeX's dotless i


def _fix_accents(s: str) -> str:
    """TeX-made PDFs put a SPACING accent before its letter ('J¨orn S¨uß'); fold it onto
    the letter ('Jörn Süß') or Crossref's author search misses the surname."""
    import unicodedata
    return unicodedata.normalize("NFC", _SPACING_RE.sub(lambda m: m.group(2).replace("ı", "i") + _SPACING[m.group(1)], s))


def _author(name: str) -> Author:
    parts = " ".join(name.replace(",", " ").split()).split(" ")
    return Author(family=parts[-1].title() if parts[-1].isupper() else parts[-1],
                  given=" ".join(parts[:-1]).title() if " ".join(parts[:-1]).isupper() else " ".join(parts[:-1]))


def _resolve(conn, stamp, rid, fields: dict) -> tuple[str, str | None]:
    """Grounded fields -> an identifier or a Crossref record -> the safe write path."""
    fields = {k: [_fix_accents(v) for v in vs] for k, vs in fields.items()}
    for d in fields.get("doi") or []:
        m = DOI_RE.search(d)
        if m:
            doi = m.group(1).rstrip(".,;")
            STATS["found_doi"] += 1
            return _write_doi(conn, stamp, rid, "doi", doi, P._crossref_record(doi), "via_lx_doi"), doi
    titles = [t.strip() for t in fields.get("title") or [] if len(t.strip()) >= 6]
    if not titles:
        return "no_title", None
    title = re.sub(r"\s+", " ", ": ".join(titles[:2]) if len(titles) > 1 and len(titles[0]) < 40 else titles[0])
    authors = [_author(a) for a in (fields.get("author") or [])[:6] if len(a.split()) >= 2]
    year = _year(fields.get("year"))
    seed = Reference(title=title, authors=authors, year=year)
    info = L.parse(title)
    info["author"] = authors[0].family if authors else None
    info["year"] = year
    if len(info["keywords"]) < 2:
        return "no_keywords", None
    venue = (fields.get("venue") or [""])[0]
    q = " ".join(info["keywords"] + ([str(year)] if year else []) + ([venue] if venue else []))
    best = L._best(seed, info, L._crossref(q, info["author"]), None, "crossref")
    if not best or best[0] < L.ACCEPT_P or not best[1].doi:
        STATS["no_record"] += 1
        return "no_record", (best[1].doi if best else None)
    return _write_doi(conn, stamp, rid, "pdf_title", best[1].doi, best[1], "via_lx_search"), best[1].doi


def run_lx(conn, stamp) -> None:
    key = os.environ.get("MOUSEION_LX_GEMINI_KEY", "").strip()
    if not key:
        print("[grounded lx] MOUSEION_LX_GEMINI_KEY not set (a FREE-tier key): nothing to do", flush=True)
        return
    if not (LX_VENV / "pyvenv.cfg").exists():
        print(f"[grounded lx] {LX_VENV} missing: run `python scripts/grounded_meta.py --setup`", flush=True)
        return
    b = _budget()
    room = 0 if b.get("quota_hit") else max(0, CAP - b["calls"])
    rows = _pool(conn, min(LIMIT, room)) if room else []
    print(f"[grounded lx] {len(rows):,} refs | {'WRITE' if WRITE else 'DRY-RUN'} | today {b['calls']}/{CAP} calls"
          f"{' (quota hit)' if b.get('quota_hit') else ''}", flush=True)
    if not rows:
        return
    cached = _cache()
    overloaded = 0
    proc = _runner()
    import queue
    import threading
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in iter(proc.stdout.readline, b"")], daemon=True).start()
    t_end = time.time() + MAX_MIN * 60
    try:
        with ThreadPoolExecutor(4) as pool:                      # PDFs stream from Drive in parallel
            for rid, text, state in pool.map(_text, rows):
                if time.time() > t_end:
                    print(f"  {MAX_MIN:.0f}-minute limit reached: stopping (the rest stays in the pool)", flush=True)
                    break
                STATS["checked"] += 1
                if state != "ok":
                    STATS["no_pdf" if state == "no_pdf" else "no_text"] += 1
                    if state in ("no_pdf", "no_text"):
                        _log(conn, rid, state)     # permanent; a Drive error is retried next run
                    continue
                out = cached.get(rid)
                if out is None:
                    proc.stdin.write((json.dumps({"id": rid, "text": text}, ensure_ascii=False) + "\n").encode("utf-8"))
                    proc.stdin.flush()
                    try:
                        line = lines.get(timeout=CALL_TIMEOUT_S).decode("utf-8", "replace")
                    except queue.Empty:
                        print(f"  langextract gave no answer in {CALL_TIMEOUT_S}s: stopping", flush=True)
                        break
                    out = json.loads(line) if line.strip() else {"err": "runner died"}
                    STATS["lx_calls"] += 1
                    if not re.search(r"503|UNAVAILABLE", out.get("err") or ""):   # an overload is not a used call
                        b["calls"] += 1
                        _save_budget(b)
                        _cache_add(out)
                else:
                    STATS["lx_cached"] = STATS.get("lx_cached", 0) + 1
                err = out.get("err") or ""
                if err:
                    STATS["lx_err"] += 1
                    if re.search(r"429|RESOURCE_EXHAUSTED|quota", err, re.I):
                        b["quota_hit"] = True
                        STATS["lx_quota"] = True
                        _save_budget(b)
                        print(f"  quota reached after {b['calls']} calls today: stopping", flush=True)
                        break
                    if "runner died" in err or "not set" in err:
                        break
                    if re.search(r"503|UNAVAILABLE", err):
                        overloaded += 1
                        if overloaded >= 3:
                            print("  every free model overloaded 3 times running: stopping until the next run", flush=True)
                            break
                        continue                    # not logged: stays in the pool
                    _log(conn, rid, "lx_err", None, {"err": err[:200]})
                    continue
                overloaded = 0
                if b["calls"] >= CAP:
                    room = 0
                res, doi = _resolve(conn, stamp, rid, out.get("fields") or {})
                _log(conn, rid, res, doi, out.get("fields"))
                if room == 0:
                    print(f"  daily cap {CAP} reached", flush=True)
                    break
    finally:
        try:
            proc.stdin.close()
            proc.wait(30)
        except Exception:
            proc.kill()


def main() -> None:
    stamp = time.strftime("%Y%m%d")
    conn = _conn()
    t0 = time.time()
    if MODE == "arxiv":
        run_arxiv(conn, stamp)
    elif MODE == "lx":
        run_lx(conn, stamp)
    else:
        sys.exit(f"unknown mode {MODE!r} (lx | arxiv)")
    for s in SAMPLES:
        print("SAMPLE", json.dumps(s, ensure_ascii=False))
    STATS["seconds"] = int(time.time() - t0)
    print(json.dumps(STATS, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
