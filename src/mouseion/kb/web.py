"""Browsable knowledge base inside the Mouseion web app: /kb (page) and /api/kb/* (JSON).

Every hit is shown with its citation and page, and links to the reference; notes show their supporting quote,
so anything the knowledge base asserts can be checked against the source in one click.
"""
from __future__ import annotations

import html

from flask import Blueprint, jsonify, request

from mouseion.kb import citations as C
from mouseion.kb import search as S
from mouseion.kb.daemon import read_status
from mouseion.kb.store import open_kb, open_library

bp = Blueprint("kb", __name__)


def _conns():
    return open_library(), open_kb()


@bp.route("/api/kb/search")
def api_search():
    q = request.args.get("q", "").strip(); n = min(int(request.args.get("n", 10)), 50)
    lib, kb = _conns()
    try:
        return jsonify({"refs": S.search_refs(lib, q, n) if q else [], "passages": S.search_passages(lib, kb, q, n) if q else [],
                        "notes": S.search_notes(lib, kb, q, n, request.args.get("project") or None) if q else []})
    finally:
        lib.close(); kb.close()


@bp.route("/api/kb/stats")
def api_stats():
    lib, kb = _conns()
    try:
        return jsonify({"stats": S.stats(lib, kb), "last_cycle": read_status()})
    finally:
        lib.close(); kb.close()


@bp.route("/api/kb/frontier")
def api_frontier():
    kb = open_kb()
    try:
        return jsonify([{"openalex_id": w, "cited_by_library": k, "url": f"https://openalex.org/{w}"} for w, k in C.frontier(kb, 100)])
    finally:
        kb.close()


@bp.route("/api/kb/notes", methods=["POST"])
def api_add_note():
    d = request.json or {}
    lib, kb = _conns()
    try:
        return jsonify(S.add_note(lib, kb, d.get("project", ""), d.get("claim", ""), d.get("ref_id", ""), d.get("chunk_id"),
                                  d.get("quote"), d.get("kind", "finding"), d.get("author"), d.get("supersedes")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        lib.close(); kb.close()


PAGE = """<!doctype html><meta charset=utf-8><title>Mouseion knowledge base</title>
<style>body{font:15px system-ui;max-width:980px;margin:2em auto;padding:0 1em;color:#1d2433}
input{font-size:16px;padding:.5em;width:70%}button{font-size:16px;padding:.5em 1em}
.hit{border-left:3px solid #6366f1;padding:.4em .8em;margin:.8em 0}.cite{color:#555;font-size:13px}
.q{background:#f3f4ff}.stats{font-size:13px;color:#444}h2{margin-top:1.6em}</style>
<h1>Knowledge base</h1><div class=stats>{stats}</div>
<form><input name=q value="{q}" placeholder="search full texts, references and notes" autofocus> <button>Search</button></form>
{body}"""


@bp.route("/kb")
def page():
    q = request.args.get("q", "").strip()
    lib, kb = _conns()
    try:
        st = S.stats(lib, kb); last = read_status() or {}
        stats = (f"{st['refs']:,} references · {st['chunks']:,} passages from {st['pdf_files'].get('ok', 0):,} PDFs · "
                 f"{st['citation_edges']:,} citation edges · {sum(st['notes'].values()) if st['notes'] else 0} notes · "
                 f"last update {html.escape(str(last.get('at', 'never')))}")
        body = ""
        if q:
            e = html.escape
            notes = S.search_notes(lib, kb, q, 10)
            body += "<h2>Notes</h2>" + ("".join(f"<div class='hit q'><b>{e(x['claim'])}</b><div class=cite>{e(x['citation'])}"
                     f" · {e(x['project'])} · note {x['id']}</div>{('<i>“' + e(x['quote']) + '”</i>') if x.get('quote') else ''}</div>"
                     for x in notes) or "<p>none</p>")
            ps = S.search_passages(lib, kb, q, 10)
            body += "<h2>Passages</h2>" + ("".join(f"<div class=hit>{e(x['text'])}<div class=cite>{e(x['citation'])} · "
                    f"ref {e(x['ref_id'])}</div></div>" for x in ps) or "<p>none</p>")
            rs = S.search_refs(lib, q, 10)
            body += "<h2>References</h2>" + ("".join(f"<div class=hit>{e(x['citation'])}<div class=cite>ref {e(x['ref_id'])}</div></div>"
                    for x in rs) or "<p>none</p>")
        return PAGE.replace("{stats}", stats).replace("{q}", html.escape(q)).replace("{body}", body)
    finally:
        lib.close(); kb.close()
