"""Always-on knowledge-base maintenance: `python -m mouseion.kb daemon` (or `pythonw` for no console window).

Every cycle (default 6 h): incremental full-text pass (new or changed PDFs only), incremental citation pass (DOIs never
fetched), then write `kb_status.json` next to refs.db. That file feeds the health report:
coverage, last cycle, errors. A cycle that fails is recorded, not fatal; the loop continues.
"""

from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mouseion.kb import citations, fulltext, search
from mouseion.kb.store import kb_path, library_path, open_kb, open_library


def status_path(lib: Optional[str] = None) -> Path:
    return library_path(lib).with_name("kb_status.json")


def cycle(
    lib_p: Optional[str] = None,
    kb_p: Optional[str] = None,
    pdf_root: Optional[str] = None,
    max_pdfs: Optional[int] = None,
    max_dois: Optional[int] = 5000,
    rate: float = 8.0,
    fetcher=None,
) -> dict:
    t0 = time.time()
    rep = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kb": str(kb_path(kb_p)),
        "errors": [],
    }
    lib, kb = open_library(lib_p), open_kb(kb_p)
    try:
        rep["fulltext"] = fulltext.build(lib, kb, pdf_root, max_pdfs)
    except Exception as e:
        rep["errors"].append(f"fulltext: {type(e).__name__}: {e}")
    try:
        kw = {"fetcher": fetcher} if fetcher else {}
        rep["citations"] = citations.build(lib, kb, max_dois, rate, **kw)
    except Exception as e:
        rep["errors"].append(f"citations: {type(e).__name__}: {e}")
    try:
        rep["stats"] = search.stats(lib, kb)
    except Exception as e:
        rep["errors"].append(f"stats: {type(e).__name__}: {e}")
    rep["seconds"] = round(time.time() - t0, 1)
    try:
        status_path(lib_p).write_text(
            json.dumps(rep, indent=1, default=str), encoding="utf-8"
        )
    except OSError:
        pass
    lib.close()
    kb.close()
    return rep


def run_forever(interval_h: float = 6.0, **kw) -> None:
    while True:
        try:
            cycle(**kw)
        except Exception:
            traceback.print_exc()
        time.sleep(max(60.0, interval_h * 3600))


def read_status(lib: Optional[str] = None) -> Optional[dict]:
    try:
        return json.loads(status_path(lib).read_text(encoding="utf-8"))
    except Exception:
        return None
