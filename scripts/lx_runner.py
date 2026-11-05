"""langextract worker for grounded_meta.py -- runs under its OWN venv, never Mouseion's.

langextract pulls pandas, aiohttp, google-cloud-storage and a newer google-auth; the
app's venv stays untouched, so this file runs as a subprocess under the venv that
`grounded_meta.py --setup` creates (~/.local/share/mouseion/langextract-venv).

stdin : one JSON object per line {"id": ..., "text": ...}
stdout: one JSON object per line {"id", "fields": {class: [values]}, "ungrounded": [...], "err", "s"}
Only extractions langextract could align to an exact span of the text are returned in
`fields`; the rest are reported as ungrounded and never used.
Key: MOUSEION_LX_GEMINI_KEY (a FREE-tier Gemini API key; set by Egon). Model:
MOUSEION_LX_MODEL (default gemini-3.1-flash-lite).
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

import langextract as lx
from langextract import factory

PROMPT = ("Extract the bibliographic metadata of THIS document (not of works it cites): its title, each author, "
          "the publication year, DOI, arXiv id, ISBN, venue (journal / book series / proceedings), volume, pages, "
          "and publisher. Use exact text from the document; do not paraphrase; skip anything not printed.")
EXAMPLES = [lx.data.ExampleData(
    text=("Journal of Symbolic Logic Volume 52, Number 3, Sept. 1987, pp. 641-668\n"
          "ON THE STRENGTH OF RAMSEY'S THEOREM\nDAVID SEETAPUN AND THEODORE A. SLAMAN\n"
          "Received March 2, 1986. doi:10.2307/2274359 © 1987, Association for Symbolic Logic"),
    extractions=[
        lx.data.Extraction("venue", "Journal of Symbolic Logic"),
        lx.data.Extraction("volume", "52"),
        lx.data.Extraction("year", "1987"),
        lx.data.Extraction("pages", "641-668"),
        lx.data.Extraction("title", "ON THE STRENGTH OF RAMSEY'S THEOREM"),
        lx.data.Extraction("author", "DAVID SEETAPUN"),
        lx.data.Extraction("author", "THEODORE A. SLAMAN"),
        lx.data.Extraction("doi", "10.2307/2274359"),
        lx.data.Extraction("publisher", "Association for Symbolic Logic"),
    ])]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    key = os.environ.get("MOUSEION_LX_GEMINI_KEY", "").strip()
    if not key:
        print(json.dumps({"id": None, "err": "MOUSEION_LX_GEMINI_KEY not set"}), flush=True)
        return
    # Free lite models in order; a 503 "high demand" on one moves to the next (measured 2026-11-04:
    # 9 of 17 calls to 3.1-flash-lite answered 503). Each model has its own free quota.
    models = [m.strip() for m in os.environ.get("MOUSEION_LX_MODEL",
                                                "gemini-3.1-flash-lite,gemini-3.5-flash-lite").split(",") if m.strip()]
    cfgs = [factory.ModelConfig(model_id=m, provider="GeminiLanguageModel", provider_kwargs={"api_key": key})
            for m in models]
    # a Windows pipe decodes as cp1252 by default: read the bytes as UTF-8
    for line in io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"):
        if not line.strip():
            continue
        job = json.loads(line)
        out = {"id": job["id"], "fields": {}, "ungrounded": [], "err": "", "model": ""}
        t0 = time.perf_counter()
        for model, cfg in zip(models, cfgs):
            out.update(fields={}, ungrounded=[], err="", model=model)
            try:
                res = lx.extract(text_or_documents=job["text"][:6000], prompt_description=PROMPT, examples=EXAMPLES,
                                 config=cfg, use_schema_constraints=False, fence_output=True, max_char_buffer=7000,
                                 extraction_passes=1, max_workers=1, show_progress=False)
                for e in res.extractions or []:
                    if e.char_interval is None or e.char_interval.start_pos is None:
                        out["ungrounded"].append([e.extraction_class, e.extraction_text])
                    else:
                        out["fields"].setdefault(e.extraction_class, []).append(e.extraction_text)
                break
            except Exception as ex:
                out["err"] = f"{type(ex).__name__}: {str(ex)[:300]}"
                if not ("503" in out["err"] or "UNAVAILABLE" in out["err"]):
                    break                       # quota or a real error: the caller decides
        out["s"] = round(time.perf_counter() - t0, 1)
        print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
