"""python -m mouseion.kb {stats|build-fulltext|ocr-empty|build-citations|frontier|search|serve|daemon|backfill-notes|eval}
[--library refs.db] [--kb kb.db]

  backfill-notes --bib references.bib --project synesism     (BibTeX `note` fields)
  backfill-notes --refs-table REFERENCES.md --project semplex (Key | Reference | Level | Used in | Used for)
  eval questions.jsonl                                        (see mouseion.kb.evaluate)
  build-statements | statements "univalent" --kind definition
  expand "question" [--evidence 12,40]                        (multi-hop by bridge terms)
  bridges "A" [--with "C"]                                    (literature-based discovery)
"""

import argparse
import json
import sys

from mouseion.kb import citations, fulltext, search
from mouseion.kb.store import open_kb, open_library


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mouseion.kb")
    ap.add_argument(
        "cmd",
        choices=[
            "stats",
            "build-fulltext",
            "ocr-empty",
            "build-citations",
            "frontier",
            "search",
            "serve",
            "daemon",
            "backfill-notes",
            "eval",
            "build-statements",
            "statements",
            "expand",
            "bridges",
        ],
    )
    ap.add_argument("query", nargs="?")
    ap.add_argument("--library")
    ap.add_argument("--kb")
    ap.add_argument("--pdf-root")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--rate", type=float, default=8.0)
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument(
        "--interval", type=float, default=6.0, help="daemon: hours between cycles"
    )
    ap.add_argument("--bib", help="backfill-notes: BibTeX file")
    ap.add_argument("--refs-table", help="backfill-notes: Markdown reference table")
    ap.add_argument("--project", help="backfill-notes: project name for the notes")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--kind", help="statements: definition | theorem | lemma | ...")
    ap.add_argument("--evidence", help="expand: comma-separated chunk ids judged to be evidence")
    ap.add_argument("--with", dest="with_", help="bridges: the C side (closed discovery)")
    a = ap.parse_args(argv)
    if a.cmd == "daemon":
        from mouseion.kb.daemon import run_forever

        return run_forever(
            a.interval, lib_p=a.library, kb_p=a.kb, pdf_root=a.pdf_root, rate=a.rate
        )
    if a.cmd == "serve":
        from mouseion.kb.mcp_server import serve

        return serve(a.library, a.kb)
    lib, kb = open_library(a.library), open_kb(a.kb)

    def prog(i, n, s):
        if i % 200 == 0 or i == n:
            print(f"  {i}/{n} ({s})", file=sys.stderr, flush=True)

    if a.cmd == "build-fulltext":
        out = fulltext.build(lib, kb, a.pdf_root, a.limit, prog)
    elif a.cmd == "ocr-empty":
        out = fulltext.ocr_empty(lib, kb, a.pdf_root, a.limit, progress=prog)
    elif a.cmd == "backfill-notes":
        from pathlib import Path

        from mouseion.kb import backfill

        if not a.project or not (a.bib or a.refs_table):
            ap.error("backfill-notes needs --project and --bib or --refs-table")
        if a.bib:
            recs = backfill.parse_bibtex(Path(a.bib).read_text(encoding="utf-8"))
        else:
            recs = backfill.parse_reference_table(Path(a.refs_table).read_text(encoding="utf-8"))
        out = backfill.backfill(lib, kb, a.project, recs, a.dry_run)
    elif a.cmd == "eval":
        from mouseion.kb import evaluate

        if not a.query:
            ap.error("eval needs a questions file")
        out = evaluate.run(lib, kb, evaluate.load(a.query), a.n)
    elif a.cmd == "build-statements":
        from mouseion.kb import statements as St

        out = St.build(kb, a.limit)
    elif a.cmd == "statements":
        from mouseion.kb import statements as St

        out = St.search(lib, kb, a.query or "", a.kind, a.n)
    elif a.cmd == "expand":
        from mouseion.kb import expand as X

        ev = [int(x) for x in a.evidence.split(",")] if a.evidence else None
        out = X.expand(kb, a.query or "", ev, n=a.n)
    elif a.cmd == "bridges":
        from mouseion.kb import expand as X

        out = X.bridges(kb, a.query or "", a.with_, a.n)
    elif a.cmd == "build-citations":
        out = citations.build(lib, kb, a.limit, a.rate, progress=prog)
    elif a.cmd == "frontier":
        out = citations.frontier(kb, a.n)
    elif a.cmd == "search":
        out = {
            "refs": search.search_refs(lib, a.query or "", a.n),
            "passages": search.search_passages(lib, kb, a.query or "", a.n),
            "notes": search.search_notes(lib, kb, a.query or "", a.n),
        }
    else:
        out = search.stats(lib, kb)
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
