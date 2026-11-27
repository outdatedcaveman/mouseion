"""Full-text layer: local PDFs -> page-anchored passages in kb.db (FTS5/BM25), incremental.

A file is re-indexed only when its size or mtime changed. Passages are ~1,200 characters, split on paragraph
boundaries within a page, so every hit can be cited as (reference, page).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

CHUNK_CHARS = 1200
# 2: text is NFKC-normalised, so the ligatures LaTeX PDFs use ("ﬁ" in "deﬁnition", "ﬀ" in "diﬀerential") become
#    plain letters and match ordinary queries. Measured on 127 arXiv papers: 50% of passages had ligatures, and
#    a search for "definition" missed 2,745 passages while finding 1,638.
EXTRACT_VERSION = 2

_VERSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_versions (ref_id TEXT PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS chunks_archive (
    id INTEGER PRIMARY KEY, ref_id TEXT NOT NULL, page INTEGER, ord INTEGER, text TEXT NOT NULL,
    archived_at TEXT DEFAULT (datetime('now')), reason TEXT
);
"""


def normalize(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKC", text or "")


def _version(kb, ref_id: str) -> int:
    kb.executescript(_VERSION_SCHEMA)
    r = kb.execute("SELECT version FROM pdf_versions WHERE ref_id=?", (ref_id,)).fetchone()
    return r[0] if r else 1


def _archive_cited_chunks(kb, ref_id: str, reason: str) -> list:
    """before a reference's passages are replaced, copy every passage a note points to into chunks_archive, so
    nothing a note cites is ever lost; returns (note id, quote, old chunk id) for re-anchoring."""
    kb.executescript(_VERSION_SCHEMA)
    rows = kb.execute(
        "SELECT n.id, n.quote, n.chunk_id FROM notes n JOIN chunks c ON c.id = n.chunk_id WHERE c.ref_id=?",
        (ref_id,),
    ).fetchall()
    for _, _, cid in rows:
        kb.execute(
            "INSERT OR IGNORE INTO chunks_archive(id, ref_id, page, ord, text, reason) "
            "SELECT id, ref_id, page, ord, text, ? FROM chunks WHERE id=?",
            (reason, cid),
        )
    return [tuple(r) for r in rows]


def _reanchor(kb, ref_id: str, cited: list) -> None:
    """point notes at the new passage that contains their quote; a note whose quote is not found keeps its old
    chunk_id, which still resolves through chunks_archive."""
    import re as _re

    norm = lambda t: _re.sub(r"\s+", " ", normalize(t)).strip().lower()  # noqa: E731
    for nid, quote, _old in cited:
        if not quote:
            continue
        for r in kb.execute("SELECT id, text FROM chunks WHERE ref_id=?", (ref_id,)):
            if norm(quote) in norm(r["text"]):
                kb.execute("UPDATE notes SET chunk_id=? WHERE id=?", (r["id"], nid))
                break


def pdf_column(lib) -> Optional[str]:
    cols = {r[1] for r in lib.execute("PRAGMA table_info(refs)")}
    for c in ("pdf_local", "pdf_path"):
        if c in cols:
            return c
    return None


def resolve(path: str, pdf_root: Optional[str]) -> Path:
    p = Path(os.path.expanduser(path))
    if not p.is_absolute() and pdf_root:
        p = Path(pdf_root) / p
    return p


def split_page(text: str, size: int = CHUNK_CHARS) -> List[str]:
    text = re.sub(r"-\n(\w)", r"\1", text)  # de-hyphenate line breaks
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    out, cur = [], ""
    for p in paras:
        if not p:
            continue
        if len(cur) + len(p) + 1 <= size:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                out.append(cur)
            while len(p) > size:  # very long paragraph: cut at a sentence end
                cut = p.rfind(". ", 0, size)
                cut = cut + 1 if cut > size // 2 else size
                out.append(p[:cut].strip())
                p = p[cut:].strip()
            cur = p
    if cur:
        out.append(cur)
    return [c for c in out if len(c) >= 40]


def extract(path: Path) -> List[Tuple[int, str]]:
    import fitz  # pymupdf

    pages = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc):
            pages.append((i + 1, page.get_text("text") or ""))
    return pages


def index_ref(kb, ref_id: str, path: Path) -> str:
    st = path.stat()
    row = kb.execute(
        "SELECT size, mtime, status FROM pdf_files WHERE ref_id=?", (ref_id,)
    ).fetchone()
    if (
        row
        and row["size"] == st.st_size
        and abs((row["mtime"] or 0) - st.st_mtime) < 1e-6
        and row["status"] in ("ok", "ocr", "empty")
        and (row["status"] != "ok" or _version(kb, ref_id) >= EXTRACT_VERSION)
    ):  # 'empty' stays until ocr_empty() runs; re-extracting a file without a text layer gains nothing
        return "unchanged"
    cited = _archive_cited_chunks(kb, ref_id, "re-extracted")
    kb.execute("DELETE FROM chunks WHERE ref_id=?", (ref_id,))
    try:
        pages = extract(path)
    except Exception as e:  # broken or encrypted PDF: record, do not crash
        kb.execute(
            "INSERT OR REPLACE INTO pdf_files(ref_id,path,size,mtime,n_pages,n_chunks,status) VALUES (?,?,?,?,?,?,?)",
            (
                ref_id,
                str(path),
                st.st_size,
                st.st_mtime,
                0,
                0,
                f"error:{type(e).__name__}",
            ),
        )
        return "error"
    n = 0
    for page, text in pages:
        for ch in split_page(normalize(text)):
            kb.execute(
                "INSERT INTO chunks(ref_id,page,ord,text) VALUES (?,?,?,?)",
                (ref_id, page, n, ch),
            )
            n += 1
    status = (
        "ok" if n else "empty"
    )  # 'empty' = scanned PDF without a text layer (needs OCR)
    _reanchor(kb, ref_id, cited)
    kb.execute("INSERT OR REPLACE INTO pdf_versions VALUES (?,?)", (ref_id, EXTRACT_VERSION))
    kb.execute(
        "INSERT OR REPLACE INTO pdf_files(ref_id,path,size,mtime,n_pages,n_chunks,status) VALUES (?,?,?,?,?,?,?)",
        (ref_id, str(path), st.st_size, st.st_mtime, len(pages), n, status),
    )
    return status


def build(
    lib,
    kb,
    pdf_root: Optional[str] = None,
    limit: Optional[int] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    col = pdf_column(lib)
    if not col:
        return {"error": "library has no PDF path column"}
    rows = lib.execute(
        f"SELECT id, {col} AS p FROM refs WHERE {col} IS NOT NULL AND {col} != ''"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    counts: dict = {}
    for i, r in enumerate(rows):
        path = resolve(r["p"], pdf_root)
        if not path.exists():
            kb.execute(
                "INSERT OR REPLACE INTO pdf_files(ref_id,path,status) VALUES (?,?,?)",
                (r["id"], str(path), "missing"),
            )
            s = "missing"
        else:
            s = index_ref(kb, r["id"], path)
        counts[s] = counts.get(s, 0) + 1
        if i % 50 == 0:
            kb.commit()
        if progress:
            progress(i + 1, len(rows), s)
    kb.commit()
    return counts


def ocr_empty(
    lib,
    kb,
    pdf_root: Optional[str] = None,
    limit: Optional[int] = None,
    ocr: Optional[Callable[[str], List[str]]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """OCR the PDFs that build() found without a text layer (status 'empty') and index their pages.

    `ocr(path)` returns one string per page; the default is Windows' built-in engine (mouseion.ocr), which
    yields nothing elsewhere, so on other systems this reports 'unavailable' and changes nothing. A file that
    OCR turns into text gets status 'ocr' (its passages are then searchable like any other); one that stays
    blank keeps 'empty'."""
    if ocr is None:
        from mouseion import ocr as _ocr

        if not _ocr.available():
            n = kb.execute("SELECT count(*) FROM pdf_files WHERE status='empty'").fetchone()[0]
            return {"unavailable": n} if n else {}
        ocr = _ocr.ocr_pdf_page_texts
    rows = kb.execute(
        "SELECT ref_id, path FROM pdf_files WHERE status='empty' ORDER BY ref_id"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    counts: dict = {}
    for i, r in enumerate(rows):
        path = Path(r["path"])
        if not path.exists():
            s = "missing"
        else:
            try:
                texts = ocr(str(path))
            except Exception:
                texts = []
            cited = _archive_cited_chunks(kb, r["ref_id"], "ocr")
            kb.execute("DELETE FROM chunks WHERE ref_id=?", (r["ref_id"],))
            n = 0
            for page, text in enumerate(texts, 1):
                for ch in split_page(normalize(text or "")):
                    kb.execute(
                        "INSERT INTO chunks(ref_id,page,ord,text) VALUES (?,?,?,?)",
                        (r["ref_id"], page, n, ch),
                    )
                    n += 1
            s = "ocr" if n else "empty"
            _reanchor(kb, r["ref_id"], cited)
            kb.execute(
                "UPDATE pdf_files SET n_chunks=?, status=?, indexed_at=datetime('now') WHERE ref_id=?",
                (n, s, r["ref_id"]),
            )
        counts[s] = counts.get(s, 0) + 1
        kb.commit()
        if progress:
            progress(i + 1, len(rows), s)
    return counts
