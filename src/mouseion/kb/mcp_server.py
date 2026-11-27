"""Mouseion knowledge base as an MCP server (stdio, JSON-RPC 2.0), dependency-free.

Register with Claude Code (any project):
    claude mcp add mouseion -- python -m mouseion.kb serve
or in .mcp.json:
    {"mcpServers": {"mouseion": {"command": "python", "args": ["-m", "mouseion.kb", "serve"]}}}

Read tools never modify the library; the only write tool (`add_note`) writes to kb.db and requires provenance.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict

from mouseion.kb import citations as C
from mouseion.kb import search as S
from mouseion.kb.store import open_kb, open_library

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _schema(props: Dict[str, Any], required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


STR, INT = {"type": "string"}, {"type": "integer"}


def tools(lib, kb) -> Dict[str, tuple]:
    """name -> (description, input schema, handler)"""
    T: Dict[str, tuple] = {}

    def tool(name, desc, schema):
        def deco(fn: Callable):
            T[name] = (desc, schema, fn)
            return fn

        return deco

    @tool(
        "search_refs",
        "Search the library's references (title/abstract, BM25). Returns ref_ids with citations.",
        _schema({"query": STR, "n": INT, "year_from": INT, "year_to": INT}, ["query"]),
    )
    def _(a):
        return S.search_refs(
            lib, a["query"], a.get("n", 10), a.get("year_from"), a.get("year_to")
        )

    @tool(
        "search_fulltext",
        "Search inside the full texts of the library's PDFs. Returns passages with ref_id, page "
        "and a citation; restrict to some references with ref_ids.",
        _schema(
            {"query": STR, "n": INT, "ref_ids": {"type": "array", "items": STR}},
            ["query"],
        ),
    )
    def _(a):
        return S.search_passages(lib, kb, a["query"], a.get("n", 8), a.get("ref_ids"))

    @tool(
        "get_ref",
        "Full metadata and abstract of one reference, plus its citation-graph counts.",
        _schema({"ref_id": STR}, ["ref_id"]),
    )
    def _(a):
        d = S.get_ref(lib, a["ref_id"])
        if d:
            d["n_references"] = len(C.references(kb, a["ref_id"]))
            d["n_cited_by_in_library"] = len(C.cited_by(kb, a["ref_id"]))
        return d or {"error": "not found"}

    @tool(
        "citations",
        "Citation graph around a reference: mode = references | cited_by | coupling (shares cited works) "
        "| cocitation (cited together with it).",
        _schema(
            {
                "ref_id": STR,
                "mode": {
                    "type": "string",
                    "enum": ["references", "cited_by", "coupling", "cocitation"],
                },
                "n": INT,
            },
            ["ref_id", "mode"],
        ),
    )
    def _(a):
        m, n, r = a["mode"], a.get("n", 20), a["ref_id"]
        if m == "references":
            return [
                x | {"citation": S.cite(lib, x["ref_id"])} if x["ref_id"] else x
                for x in C.references(kb, r)
            ][:n]
        if m == "cited_by":
            return [
                {"ref_id": x, "citation": S.cite(lib, x)} for x in C.cited_by(kb, r)
            ][:n]
        f = C.coupling if m == "coupling" else C.cocitation
        return [
            {
                "id": x,
                "weight": k,
                "citation": S.cite(lib, x) if not x.startswith("W") else None,
            }
            for x, k in f(kb, r, n)
        ]

    @tool(
        "reading_frontier",
        "Works the library cites most often but does not contain (OpenAlex ids): what to acquire next.",
        _schema({"n": INT}),
    )
    def _(a):
        return [
            {"openalex_id": w, "cited_by_library": k}
            for w, k in C.frontier(kb, a.get("n", 50))
        ]

    @tool(
        "search_notes",
        "Search the knowledge base's notes (claims with provenance) by text, optionally per project.",
        _schema(
            {
                "query": STR,
                "project": STR,
                "n": INT,
                "include_inactive": {"type": "boolean"},
            },
            ["query"],
        ),
    )
    def _(a):
        return S.search_notes(
            lib,
            kb,
            a["query"],
            a.get("n", 10),
            a.get("project"),
            a.get("include_inactive", False),
        )

    @tool(
        "add_note",
        "Record a claim in the knowledge base. Requires ref_id; give the supporting passage (chunk_id) and/or "
        "a verbatim quote, which is checked against the text. Use supersedes to replace an older note.",
        _schema(
            {
                "project": STR,
                "claim": STR,
                "ref_id": STR,
                "chunk_id": INT,
                "quote": STR,
                "kind": {
                    "type": "string",
                    "enum": [
                        "finding",
                        "method",
                        "definition",
                        "critique",
                        "open-question",
                    ],
                },
                "author": STR,
                "supersedes": INT,
            },
            ["project", "claim", "ref_id"],
        ),
    )
    def _(a):
        return S.add_note(
            lib,
            kb,
            a["project"],
            a["claim"],
            a["ref_id"],
            a.get("chunk_id"),
            a.get("quote"),
            a.get("kind", "finding"),
            a.get("author"),
            a.get("supersedes"),
        )

    @tool(
        "expand",
        "Multi-hop search by bridge terms (SAG2-style). Run search_fulltext first, judge which passages are real "
        "evidence, and pass their chunk_ids as evidence: rare terms from them retrieve passages that share no "
        "word with the question (the next hop). Repeat with the new evidence for further hops. Without evidence, "
        "the top BM25 passages are used as seeds. Returns passages (each with the bridge term that reached it) "
        "and the activated terms. Always pass evidence: without a judge, expansion ranked below plain BM25 once the "
        "corpus had distractors (docs/KB_METHOD.md), so the no-evidence mode is for exploration only.",
        _schema(
            {"query": STR, "evidence": {"type": "array", "items": INT}, "rounds": INT, "n": INT},
            ["query"],
        ),
    )
    def _(a):
        from mouseion.kb import expand as X

        out = X.expand(kb, a["query"], a.get("evidence"), rounds=a.get("rounds", 2), n=a.get("n", 10))
        for p in out["passages"]:
            p["citation"] = S.cite(lib, p["ref_id"]) + f", p. {p['page']}"
        return out

    @tool(
        "bridges",
        "Literature-based discovery (Swanson ABC). With a and c: the intermediate terms that link the two "
        "literatures, one example passage per side, and how many passages already discuss both (0 = an "
        "unexplored connection). With only a: terms reached from A through rare intermediates that never "
        "co-occur with A, each with the linking terms and passages. Candidates to check, not findings.",
        _schema({"a": STR, "c": STR, "n": INT}, ["a"]),
    )
    def _(a):
        from mouseion.kb import expand as X

        return X.bridges(kb, a["a"], a.get("c"), a.get("n", 15))

    @tool(
        "statements",
        "Search typed mathematical statements (definition, theorem, lemma, proposition, corollary, conjecture, "
        "axiom, hypothesis, remark, example, construction, notation, problem, question) by text, a match in "
        "the statement's name weighted higher. Use kind='definition' for 'where is X defined?'.",
        _schema({"query": STR, "kind": STR, "n": INT}, ["query"]),
    )
    def _(a):
        from mouseion.kb import statements as St

        return St.search(lib, kb, a["query"], a.get("kind"), a.get("n", 10))

    @tool(
        "kb_stats",
        "Coverage of the knowledge base: references, indexed PDFs by status, passages, citation edges, notes.",
        _schema({}),
    )
    def _(a):
        return S.stats(lib, kb)

    return T


def handle(msg: dict, T: Dict[str, tuple]) -> dict | None:
    mid, method, p = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None  # notification

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        v = p.get("protocolVersion")
        v = v if v in PROTOCOLS else PROTOCOLS[0]
        return ok(
            {
                "protocolVersion": v,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mouseion-kb", "version": "0.1.0"},
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok(
            {
                "tools": [
                    {"name": n, "description": d, "inputSchema": s}
                    for n, (d, s, _) in T.items()
                ]
            }
        )
    if method == "tools/call":
        name, args = p.get("name"), p.get("arguments") or {}
        if name not in T:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32602, "message": f"unknown tool {name}"},
            }
        try:
            res = T[name][2](args)
            return ok(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                res, ensure_ascii=False, indent=1, default=str
                            ),
                        }
                    ],
                    "isError": False,
                }
            )
        except Exception as e:
            return ok(
                {
                    "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                    "isError": True,
                }
            )
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve(lib_path=None, kb_path=None, stdin=sys.stdin, stdout=sys.stdout) -> None:
    lib, kb = open_library(lib_path), open_kb(kb_path)
    T = tools(lib, kb)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }
        else:
            out = handle(msg, T)
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()
