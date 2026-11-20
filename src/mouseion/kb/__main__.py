"""python -m mouseion.kb {stats|build-fulltext|build-citations|frontier|search|serve} [--library refs.db] [--kb kb.db]"""

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
            "build-citations",
            "frontier",
            "search",
            "serve",
            "daemon",
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
