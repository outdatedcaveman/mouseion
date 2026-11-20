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
        and row["status"] == "ok"
    ):
        return "unchanged"
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
        for ch in split_page(text):
            kb.execute(
                "INSERT INTO chunks(ref_id,page,ord,text) VALUES (?,?,?,?)",
                (ref_id, page, n, ch),
            )
            n += 1
    status = (
        "ok" if n else "empty"
    )  # 'empty' = scanned PDF without a text layer (needs OCR)
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
