"""Recover identifiers from the PDFs themselves (arXiv stamp / printed DOI).

Why (measured 2026-09-24): 27,174 references with a PDF have no DOI and no
arXiv id; title search already failed on most of them (June: 5% recovered).
But the PDF IS the paper: in a random sample 19 of 40 carried the arXiv
margin stamp ("arXiv:2110.01468v2 [math.LO] ..."). An identifier found in the
file is exact -- no fuzzy matching -- and arXiv/Crossref then return the full
record (title, authors, year, abstract), which Mouseion's own merge applies
net-positively (never clobbers). Many seed titles are truncated at a hyphen
("Entanglement and non", "Simplicial 2"); the record repairs them.

Safety:
  * precision: an id is accepted only if its record's title agrees with the
    reference's (token containment >= 0.6, or the truncated seed is a prefix);
  * reversible: the old row (as JSON) goes to a dated backup table first;
  * resumable: every scanned ref lands in `pdf_id_scan`; reruns skip them;
  * PDFs stream from the Drive API into memory and are never written to disk.

Drive access: an authorized-user token file, path in MOUSEION_GDRIVE_TOKEN.

Usage:
  python scripts/recover_ids_from_pdfs.py <limit> <dry|write> [workers]
"""
from __future__ import annotations

import difflib
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import httpx  # noqa: E402
from pypdf import PdfReader  # noqa: E402

from mouseion.db import RefDatabase  # noqa: E402
from mouseion.merge import merge  # noqa: E402
from mouseion.providers.arxiv import ArXivProvider  # noqa: E402
from mouseion.providers.crossref import CrossRefProvider  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LIMIT = int(args[0]) if args else 200
WRITE = len(args) > 1 and args[1] == "write"
WORKERS = int(args[2]) if len(args) > 2 else 6

ARXIV_RE = re.compile(r"arXiv:\s?(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>\]\)]+)", re.I)
DB = RefDatabase()
STATS = {"scanned": 0, "no_pdf": 0, "no_id": 0, "arxiv": 0, "doi": 0, "pdf_title": 0, "verified": 0,
         "title_mismatch": 0, "no_record": 0, "updated": 0, "err": 0}
_lock = threading.Lock()
_local = threading.local()


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split())


def _title_agrees(seed: str, cand: str) -> bool:
    s, c = _norm(seed), _norm(cand)
    if not s or not c:
        return False
    st = [w for w in s.split() if len(w) > 2]
    ct = set(c.split())
    if st and sum(w in ct for w in st) / len(st) >= 0.6:
        return True
    return c.startswith(s) or difflib.SequenceMatcher(None, s, c).ratio() >= 0.6


def _drive():
    svc = getattr(_local, "svc", None)
    if svc is None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        token = os.environ.get("MOUSEION_GDRIVE_TOKEN")
        if not token or not Path(token).exists():
            raise RuntimeError("set MOUSEION_GDRIVE_TOKEN to an authorized-user Drive token file")
        svc = build("drive", "v3", credentials=Credentials.from_authorized_user_file(token), cache_discovery=False)
        _local.svc = svc
    return svc


_FOLDERS: dict[str, str] = {}


def _drive_id_for_path(p: str) -> str | None:
    """'G:\\My Drive\\A\\B\\x.pdf' -> Drive file id, resolving folder names from root."""
    parts = Path(p).parts
    try:
        i = [x.lower() for x in parts].index("my drive")
    except ValueError:
        return None
    parent, key = "root", ""
    svc = _drive()
    for name in parts[i + 1:-1]:
        key += "/" + name
        if key not in _FOLDERS:
            q = (f"name = '{name.replace(chr(39), chr(92) + chr(39))}' and '{parent}' in parents and trashed = false "
                 "and mimeType = 'application/vnd.google-apps.folder'")
            r = svc.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
            if not r:
                return None
            _FOLDERS[key] = r[0]["id"]
        parent = _FOLDERS[key]
    fname = parts[-1].replace("'", "\\'")
    r = svc.files().list(q=f"name = '{fname}' and '{parent}' in parents and trashed = false",
                         fields="files(id)", pageSize=1).execute().get("files", [])
    return r[0]["id"] if r else None


def _pdf_bytes(drive_id: str | None, local: str | None) -> bytes | None:
    if local and Path(local).exists() and not local.upper().startswith("G:"):
        return Path(local).read_bytes()
    fid = drive_id
    if not fid and local:
        if "\\" not in local and "/" not in local:
            # a bare file name (Mendeley-merge rows): find it on Drive by name
            q = "name = '" + local.replace("'", "\\'") + "' and trashed = false"
            hit = _drive().files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
            fid = hit[0]["id"] if hit else None
        else:
            fid = _drive_id_for_path(local)
    if not fid:
        return None
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, _drive().files().get_media(fileId=fid), chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def _first_pages_text(data: bytes) -> str:
    # PyMuPDF reads the arXiv stamp, which is rotated 90 degrees in the margin;
    # pypdf usually drops it (0 of 120 found with pypdf, 19 of 40 with PyMuPDF).
    try:
        import pymupdf as fitz
        doc = fitz.open(stream=data, filetype="pdf")
        return "\n".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
    except ImportError:
        pass
    reader = PdfReader(io.BytesIO(data))
    out = []
    for page in reader.pages[:2]:
        try:
            out.append(page.extract_text() or "")
        except Exception:
            pass
    return "\n".join(out)


_HEADER = re.compile(r"^(arxiv|doi|http|www\.|vol\.?|pp\.|page|issn|isbn|copyright|©|journal of|proceedings|"
                     r"lecture notes|working paper|nber|preprint|draft|submitted|accepted|received|"
                     r"united states patent|sage|springer|elsevier|chapter \d|\d+$)", re.I)


def _pdf_title(data: bytes) -> str:
    """The largest-font text on page 1, skipping running heads and publisher lines."""
    try:
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        if not doc.page_count:
            return ""
        spans = []
        for b in doc[0].get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                for sp in line["spans"]:
                    t = " ".join(sp["text"].split())
                    if len(t) > 2 and not _HEADER.match(t):
                        spans.append((round(sp["size"], 1), sp["bbox"][1], t))
        if not spans:
            return ""
        sizes = sorted({sz for sz, _, _ in spans}, reverse=True)
        for top in sizes[:3]:              # the biggest line can be a logo word: try the next size down
            title = " ".join(t for sz, y, t in sorted(spans, key=lambda x: x[1]) if abs(sz - top) <= 0.5)
            if len([w for w in title.split() if len(w) > 2]) >= 2:
                return title[:300]
        return ""
    except Exception:
        return ""


_FILENAME_RE = re.compile(r"^(microsoft word|untitled|document\b)|\.(indd|docx?|pdf|tex|dvi|ps)\b|"
                          r"^[A-Z][\w'\-]+ (19|20)\d\d\b", re.I)


class _Junk:
    """Stored titles that say nothing about the work, so they cannot veto a match:
    file names ("ATMP-15-2-A3-KUMAR.dvi", "Brooks 1994 Unity Mind"), GUID pieces
    from UUID-named PDFs ("93eb-049a-c6a481850d91"), and titles that lost their
    spaces ("PhysicsasanOptimizationProblemoverAll")."""

    @staticmethod
    def search(t: str) -> bool:
        t = (t or "").strip()
        if not t or _FILENAME_RE.search(t):
            return True
        if re.fullmatch(r"[0-9a-f\-]{8,}", t, re.I):
            return True
        return " " not in t and len(t) > 15


_FILENAMEISH = _Junk


def _search_by_title(pdf_title: str):
    """Crossref + OpenAlex bibliographic search; candidates whose title matches the PDF's."""
    import sys as _s
    _s.path.insert(0, str(REPO / "scripts"))
    import recover_ids as R          # shared parsers + contact e-mail
    cands = []
    p = {"query.bibliographic": pdf_title[:250], "rows": 4, "select": R._CR_SELECT}
    if R.EMAIL:
        p["mailto"] = R.EMAIL
    for url, params, prov, key in (
            ("https://api.crossref.org/works", p, R.CR, ("message", "items")),
            ("https://api.openalex.org/works", {"search": pdf_title[:250], "per_page": 4,
                                                **({"mailto": R.EMAIL} if R.EMAIL else {})}, R.OA, ("results",))):
        try:
            j = httpx.get(url, params=params, timeout=30,
                          headers={"User-Agent": f"mouseion/0.2 (mailto:{R.EMAIL})"}).json()
            items = j.get(key[0], {}).get(key[1], []) if len(key) == 2 else j.get(key[0], [])
        except Exception:
            items = []
        for it in items or []:
            try:
                cand = prov._parse_work(it)
            except Exception:
                continue
            seq = difflib.SequenceMatcher(None, _norm(pdf_title), _norm(cand.title or "")).ratio()
            if seq >= 0.9:
                cands.append((seq, cand))
    return max(cands, key=lambda x: x[0])[1] if cands else None


def _agrees_with_ref(seed, cand) -> bool:
    """The PDF can be the WRONG paper for the reference (seen: an Iranzo PDF under
    another Iranzo title). The record must also fit the reference itself: its
    title (unless the stored title is a file name), and year/author if known."""
    st = seed.title or ""
    if not _FILENAMEISH.search(st) and not _title_agrees(st, cand.title or ""):
        return False
    if seed.year and cand.year and abs(int(seed.year) - int(cand.year)) > 2:
        return False
    import recover_ids as R
    names = R._name_tokens(seed)
    if names and not (names & R._name_tokens(cand)):
        return False
    return True


def _clean_seed(seed):
    """Clear ONLY demonstrably garbage fields so a verified record can fill them.
    Mouseion's merge (rightly) drops a candidate whose title does not resemble
    the seed's -- so a seed titled "01.dvi" could never be repaired. Seen on
    the 2026-08-27 Mendeley merge rows: file-name titles, GUID fragments as
    author names ("4008b141"), GUID digits as years (6615, 2320). The full
    original row is in the dated backup table before any write."""
    if _Junk.search(seed.title or ""):
        seed.title = None
    authors = []
    for a in (seed.authors or []):
        fam = getattr(a, "family", None) if not isinstance(a, dict) else a.get("family")
        if fam and re.fullmatch(r"[0-9a-f]{6,}", fam.strip(), re.I):
            continue
        authors.append(a)
    seed.authors = authors
    if seed.year and not (1400 <= int(seed.year) <= time.localtime().tm_year + 1):
        seed.year = None
    return seed


def _scan(ref_id: str, drive_id: str | None, local: str | None) -> tuple[str, str, str | None]:
    """-> (ref_id, kind, identifier) with kind in no_pdf/no_id/arxiv/doi/err."""
    try:
        data = _pdf_bytes(drive_id, local)
        if not data:
            return ref_id, "no_pdf", None
        text = _first_pages_text(data)
        m = ARXIV_RE.search(text)
        if m:
            return ref_id, "arxiv", m.group(1)
        m = DOI_RE.search(text)
        if m:
            return ref_id, "doi", m.group(1).rstrip(".,;")
        title = _pdf_title(data)
        return (ref_id, "pdf_title", title) if len(title) >= 12 else (ref_id, "no_id", None)
    except Exception as e:
        return ref_id, "err", f"{type(e).__name__}: {str(e)[:80]}"


def _arxiv_records(ids: list[str]) -> dict:
    """Batch arXiv lookup. Two bugs cost the 2026-09-24 run all 2,600 arXiv ids:
    httpx percent-encodes the comma in id_list and arXiv answers 406; and
    Mouseion's parser leaves arxiv_id empty for old-style ids (math/0404258),
    so records could not be mapped back. urllib sends the URL as written, and
    the id is read from each entry's own <id> element."""
    import subprocess
    import xml.etree.ElementTree as ET
    ns = "{http://www.w3.org/2005/Atom}"
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        url = f"https://export.arxiv.org/api/query?id_list={','.join(chunk)}&max_results={len(chunk)}"
        for attempt in range(3):
            try:
                # curl, not Python's HTTP stack: arXiv's front end answered 406 to
                # httpx (percent-encoded commas) and to urllib for batches > ~5 ids,
                # while curl got 200 on every batch size (measured 2026-09-24)
                r = subprocess.run(["curl", "-s", "-f", "-m", "90", "-A", "mouseion/0.2 (library enrichment)", url],
                                   capture_output=True, timeout=120, creationflags=0x08000000)
                if r.returncode != 0:
                    raise RuntimeError(f"curl exit {r.returncode}")
                root = ET.fromstring(r.stdout)
                for entry in root.findall(f"{ns}entry"):
                    raw = (entry.findtext(f"{ns}id") or "").split("/abs/")[-1]
                    aid = re.sub(r"v\d+$", "", raw.strip())
                    if not aid:
                        continue
                    rec = ArXivProvider._parse_entry(entry)
                    rec.arxiv_id = rec.arxiv_id or aid
                    out[aid] = rec
                break
            except Exception:
                time.sleep(5 * (attempt + 1))
        time.sleep(3.1)                     # arXiv API etiquette: one request per 3 s
    return out


def _csl_record(doi: str):
    """doi.org content negotiation (CSL-JSON): Crossref AND DataCite DOIs
    (arXiv's 10.48550, Zenodo, figshare...), which Crossref's API does not hold."""
    from mouseion.models import Author, Reference
    try:
        r = httpx.get(f"https://doi.org/{doi}", timeout=30, follow_redirects=True,
                      headers={"Accept": "application/vnd.citationstyles.csl+json",
                               "User-Agent": "mouseion/0.2 (library enrichment)"})
        if r.status_code != 200:
            return None
        j = r.json()
    except Exception:
        return None
    parts = ((j.get("issued") or {}).get("date-parts") or [[None]])[0]
    title = j.get("title")
    title = title[0] if isinstance(title, list) and title else title
    cont = j.get("container-title")
    cont = cont[0] if isinstance(cont, list) and cont else cont
    ref = Reference(
        doi=(j.get("DOI") or doi).lower(), title=title, year=parts[0] if parts else None,
        journal=cont or None, publisher=j.get("publisher"), volume=j.get("volume"), issue=j.get("issue"),
        pages=j.get("page"), abstract=j.get("abstract"),
        authors=[Author(family=a.get("family") or a.get("literal") or "", given=a.get("given") or "")
                 for a in (j.get("author") or [])[:20]])
    ref.sources = {"doi_org": 0.9}
    return ref


def _crossref_record(doi: str):
    try:
        r = httpx.get(f"https://api.crossref.org/works/{doi}", timeout=20,
                      headers={"User-Agent": "mouseion/0.2 (library enrichment)"})
        if r.status_code == 200:
            return CrossRefProvider()._parse_work(r.json()["message"])
    except Exception:
        pass
    return _csl_record(doi)


def _scan_and_resolve(ref_id, drive_id, local):
    """Worker: fetch + read the PDF, then (network, in parallel) look the record up.
    arXiv ids are returned unresolved: the arXiv API wants batched, spaced calls."""
    rid, kind, ident = _scan(ref_id, drive_id, local)
    cand = None
    if kind == "doi":
        cand = _crossref_record(ident)
    elif kind == "pdf_title":
        cand = _search_by_title(ident)
    return rid, kind, ident, cand


def _apply(conn, stamp, rid, kind, ident, cand, verified_title=False):
    """Main thread only (one SQLite connection): verify, merge, back up, write, log.
    verified_title: the caller has already matched a DAMAGED title against the
    candidate (resolve_lossy); the damaged title must then give way, or Mouseion's
    merge drops the candidate as a title mismatch ("Mechanics in Six" vs
    "Mechanics in Six-Dimensional Spacetime": 11 of 53 verified would be written)."""
    seed = DB.get(rid)
    seed = _clean_seed(seed) if seed is not None else None
    original_title = seed.title if seed is not None else None
    result = "no_record"
    if cand is None:
        STATS["no_record"] += 1
    if cand is not None and seed is not None:
        ok = (_agrees_with_ref(seed, cand) if kind == "pdf_title"
              else _title_agrees(seed.title or "", cand.title or "") or _FILENAMEISH.search(seed.title or ""))
        if not ok:
            STATS["title_mismatch"] += 1
            result = "title_mismatch"
            if len(STATS.setdefault("_mismatch_samples", [])) < 8:
                STATS["_mismatch_samples"].append([seed.title, cand.title, ident])
        else:
            STATS["verified"] += 1
            if len(STATS.setdefault("_verified_samples", [])) < 25:
                STATS["_verified_samples"].append(
                    [kind, (seed.title or "")[:60], seed.year, (cand.title or "")[:60], cand.year, cand.doi or ident])
            if verified_title and cand.title:
                seed.title = None
            merged = merge(seed, [(cand, 0.97)])
            if verified_title and not merged.title:
                merged.title = original_title
            if kind == "arxiv" and not merged.arxiv_id:
                merged.arxiv_id = ident
            before = seed.completeness or 0.0
            gained = (merged.completeness > before + 0.005 or
                      (merged.arxiv_id and not seed.arxiv_id) or (merged.doi and not seed.doi) or
                      (merged.is_complete and not seed.is_complete) or (merged.authors and not seed.authors))
            result = "improved" if gained else "no_gain"
            if gained:
                STATS["updated"] += 1
                STATS["comp_gain"] = STATS.get("comp_gain", 0.0) + merged.completeness - before
                if WRITE:
                    row = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
                    cols = [d[0] for d in conn.execute("SELECT * FROM refs LIMIT 0").description]
                    conn.execute(f"INSERT OR IGNORE INTO pdf_id_bak_{stamp} VALUES (?,?)",
                                 (rid, json.dumps(dict(zip(cols, row)), default=str)))
                    conn.commit()
                    DB.replace_ref(rid, merged)
    if WRITE:
        conn.execute("INSERT OR REPLACE INTO pdf_id_scan (ref_id, result, found_id) VALUES (?,?,?)",
                     (rid, f"{kind}:{result}", ident))


def retry_logged():
    """--retry: rows logged as arxiv:no_record / doi:no_record get re-resolved from
    the identifier already found (no re-download)."""
    stamp = time.strftime("%Y%m%d")
    conn = sqlite3.connect(str(DB.path if hasattr(DB, "path") else DB._path), timeout=60,
                           isolation_level=None)   # autocommit: never hold the write lock across network calls
    conn.execute(f"CREATE TABLE IF NOT EXISTS pdf_id_bak_{stamp} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    rows = conn.execute("SELECT ref_id, result, found_id FROM pdf_id_scan "
                        "WHERE result IN ('arxiv:no_record', 'doi:no_record') AND found_id IS NOT NULL").fetchall()
    print(f"[pdf-ids retry] {len(rows):,} logged ids | {'WRITE' if WRITE else 'DRY-RUN'}", flush=True)
    t0 = time.time()
    ax = _arxiv_records(sorted({i for _, r, i in rows if r.startswith("arxiv")}))
    print(f"  arXiv records fetched: {len(ax):,}", flush=True)
    for n, (rid, res, ident) in enumerate(rows, 1):
        kind = res.split(":")[0]
        cand = ax.get(ident) if kind == "arxiv" else _crossref_record(ident)
        _apply(conn, stamp, rid, kind, ident, cand)
        if n % 50 == 0 and WRITE:
            conn.commit()
        if n % 200 == 0:
            print(f"  ... {n:,}/{len(rows):,} | improved {STATS['updated']:,}", flush=True)
    if WRITE:
        conn.commit()
    STATS["seconds"] = int(time.time() - t0)
    print(json.dumps(STATS, ensure_ascii=False, default=str), flush=True)


def main():
    if "--retry" in sys.argv:
        return retry_logged()
    stamp = time.strftime("%Y%m%d")
    conn = sqlite3.connect(str(DB.path if hasattr(DB, "path") else DB._path), timeout=60,
                           isolation_level=None)   # autocommit: never hold the write lock across network calls
    conn.execute("CREATE TABLE IF NOT EXISTS pdf_id_scan (ref_id TEXT PRIMARY KEY, result TEXT, found_id TEXT, "
                 "scanned_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS pdf_id_bak_{stamp} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    conn.commit()
    rows = conn.execute(
        """SELECT r.id, r.pdf_drive_id, r.pdf_local FROM refs r
           WHERE COALESCE(r.doi,'') = '' AND COALESCE(r.arxiv_id,'') = '' AND COALESCE(r.status,'') != 'duplicate'
             AND (COALESCE(r.pdf_drive_id,'') != '' OR COALESCE(r.pdf_local,'') != '')
             AND r.id NOT IN (SELECT ref_id FROM pdf_id_scan)
           ORDER BY (COALESCE(r.completeness, 0) >= 0.8), RANDOM() LIMIT ?""", (LIMIT,)).fetchall()
    print(f"[pdf-ids] {len(rows):,} refs | {'WRITE' if WRITE else 'DRY-RUN'} | workers={WORKERS} | pipelined",
          flush=True)
    t0 = time.time()
    arxiv_found: list[str] = []
    ax_rows: list[tuple[str, str]] = []
    with ThreadPoolExecutor(WORKERS) as pool:
        futs = [pool.submit(_scan_and_resolve, *r) for r in rows]
        for n, f in enumerate(as_completed(futs), 1):
            rid, kind, ident, cand = f.result()
            STATS["scanned"] += 1
            STATS[kind if kind in STATS else "err"] += 1
            if kind == "arxiv":
                ax_rows.append((rid, ident))
            elif kind in ("doi", "pdf_title"):
                _apply(conn, stamp, rid, kind, ident, cand)
            elif WRITE:
                conn.execute("INSERT OR REPLACE INTO pdf_id_scan (ref_id, result, found_id) VALUES (?,?,?)",
                             (rid, kind, ident))
            if n % 50 == 0 and WRITE:
                conn.commit()
            if n % 200 == 0:
                print(f"  ... {n:,}/{len(rows):,} | improved {STATS['updated']:,} (+{STATS.get('comp_gain', 0):.0f} "
                      f"completeness) | rejected {STATS['title_mismatch']:,} | {n / (time.time() - t0):.2f}/s",
                      flush=True)
    if WRITE:
        conn.commit()
    ax = _arxiv_records(sorted({i for _, i in ax_rows}))
    for rid, ident in ax_rows:
        _apply(conn, stamp, rid, "arxiv", ident, ax.get(ident))
    if WRITE:
        conn.commit()
    STATS["seconds"] = int(time.time() - t0)
    print(json.dumps(STATS, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
