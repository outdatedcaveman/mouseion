"""
zoterpile Web UI — Flask-based browser interface.

Launch with:
    zoterpile web           # after pip install
    python -m zoterpile.web # dev / no-install

Opens at http://localhost:7274
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import threading
import uuid
from typing import Any, Dict, List, Optional

from flask import Flask, Response, abort, jsonify, redirect, request, send_file


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.json.sort_keys = False

# In-memory job store for background enrichment
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()

# Cached API key (loaded from DB on first use)
_api_key_cache: Optional[str] = None
_api_key_lock  = threading.Lock()


def _get_or_create_api_key() -> str:
    """Return the API key, generating and persisting one if absent."""
    global _api_key_cache
    with _api_key_lock:
        if _api_key_cache:
            return _api_key_cache
        try:
            from .db import RefDatabase
            with RefDatabase() as db:
                stored = db.get_setting("api_key")
                if stored:
                    _api_key_cache = stored
                    return _api_key_cache
                new_key = uuid.uuid4().hex + uuid.uuid4().hex  # 64-char hex
                db.set_setting("api_key", new_key)
                _api_key_cache = new_key
                return _api_key_cache
        except Exception:
            # DB not available yet — generate ephemeral key
            if not _api_key_cache:
                _api_key_cache = uuid.uuid4().hex + uuid.uuid4().hex
            return _api_key_cache


@app.before_request
def _require_api_key() -> Optional[Response]:
    """Enforce API key authentication for all /api/* routes."""
    if not request.path.startswith("/api/"):
        return None  # public routes: /, /manifest.json, /sw.js, /webhooks/*
    key = _get_or_create_api_key()
    # Check Authorization: Bearer <key>, X-API-Key: <key>, or ?api_key= query param
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        provided = auth_header[7:].strip()
    else:
        provided = (request.headers.get("X-API-Key", "")
                    or request.args.get("api_key", "")).strip()
    if not hmac.compare_digest(provided, key):
        return jsonify({"error": "Unauthorized — invalid or missing API key"}), 401
    return None


@app.after_request
def _add_cors(response: Response) -> Response:
    """Add CORS headers to all responses (needed for browser extension and remote clients)."""
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, X-API-Key"
    )
    return response


@app.route("/api/auth/check")
def auth_check():
    """Lightweight endpoint to verify the API key is valid."""
    return jsonify({"ok": True})

# Zotero streaming state — managed by the streaming background thread
_stream_thread: Optional[threading.Thread] = None
_stream_status: Dict[str, Any] = {
    "active":           False,
    "last_event_at":    None,
    "library_version":  None,
    "last_synced_count": 0,
    "error":            None,
}
_stream_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ref_id(ref) -> str:
    if ref.doi:
        key = "doi:" + ref.doi.lower().strip()
    elif ref.arxiv_id:
        key = "arxiv:" + ref.arxiv_id.lower().strip()
    elif ref.pmid:
        key = "pmid:" + ref.pmid.strip()
    elif ref.isbn:
        key = "isbn:" + re.sub(r"[-\s]", "", ref.isbn)
    else:
        title = (ref.title or "").lower().strip()
        year  = str(ref.year or "")
        auth  = ref.authors[0].family.lower() if ref.authors else ""
        key   = f"title:{title}:{year}:{auth}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _ref_to_dict(
    ref,
    tags: List[str],
    ref_id: str,
    has_pdf: bool = False,
    notes: Optional[str] = None,
    status: Optional[str] = None,
    cite_key: Optional[str] = None,
    collections: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    return {
        "id":             ref_id,
        "title":          ref.title or "(untitled)",
        "authors":        [{"family": a.family, "given": a.given} for a in ref.authors],
        "year":           ref.year,
        "journal":        ref.journal or ref.container_title or "",
        "doi":            ref.doi,
        "arxiv_id":       ref.arxiv_id,
        "pmid":           ref.pmid,
        "isbn":           ref.isbn,
        "url":            ref.url,
        "oa_url":         ref.oa_url,
        "open_access":    ref.open_access,
        "abstract":       ref.abstract,
        "ref_type":       ref.ref_type.value if ref.ref_type else "unknown",
        "completeness":   ref.completeness,
        "citation_count": ref.citation_count,
        "tags":           tags,
        "has_pdf":        has_pdf,
        "notes":          notes or "",
        "status":         status or "unread",
        "cite_key":       cite_key or ref.auto_cite_key(),
        "collections":    collections or [],
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/refs")
def list_refs():
    from .db import RefDatabase
    q             = request.args.get("q", "").strip()
    ref_type      = request.args.get("type") or None
    oa_only       = request.args.get("oa", "").lower() == "true"
    limit         = min(int(request.args.get("limit", 500)), 2000)
    collection_id = request.args.get("collection_id")
    try:
        with RefDatabase() as db:
            if collection_id:
                coll_refs = db.list_collection_refs(int(collection_id), limit=limit)
                # Apply additional filters
                if q:
                    q_low = q.lower()
                    coll_refs = [
                        r for r in coll_refs
                        if q_low in (r.title or "").lower()
                        or q_low in " ".join(a.family for a in r.authors).lower()
                    ]
                raw = [(r, 0.5) for r in coll_refs]
            else:
                raw = db.search(q or "", ref_type=ref_type, oa_only=oa_only, limit=limit)
            ref_ids    = [_ref_id(ref) for ref, _ in raw]
            tags_map   = db.get_tags_batch(ref_ids)
            extras_map = db.get_extras_bulk(ref_ids)
            result = [
                _ref_to_dict(
                    ref,
                    tags_map.get(_ref_id(ref), []),
                    _ref_id(ref),
                    has_pdf=bool(
                        extras_map.get(_ref_id(ref), {}).get("pdf_drive_id")
                        or extras_map.get(_ref_id(ref), {}).get("pdf_local")
                    ),
                    notes=extras_map.get(_ref_id(ref), {}).get("notes"),
                    status=extras_map.get(_ref_id(ref), {}).get("status"),
                    cite_key=extras_map.get(_ref_id(ref), {}).get("cite_key"),
                )
                for ref, _ in raw
            ]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refs/<ref_id>")
def get_ref(ref_id: str):
    from .db import RefDatabase
    try:
        with RefDatabase() as db:
            ref = db.get(ref_id)
            if ref is None:
                abort(404)
            tags        = db.get_tags(ref_id)
            extra       = db.get_extra(ref_id)
            collections = db.get_ref_collections(ref_id)
        has_pdf = bool(extra.get("pdf_drive_id") or extra.get("pdf_local"))
        return jsonify(_ref_to_dict(
            ref, tags, ref_id,
            has_pdf=has_pdf,
            notes=extra.get("notes"),
            status=extra.get("status"),
            cite_key=extra.get("cite_key"),
            collections=collections,
        ))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refs", methods=["POST"])
def add_ref():
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "No input provided"}), 400
    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "message": "Looking up…", "count": 0}
    threading.Thread(target=_enrich_worker, args=(text, job_id), daemon=True).start()
    return jsonify({"job_id": job_id}), 202


@app.route("/api/jobs/<job_id>")
def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        abort(404)
    return jsonify(job)


@app.route("/api/refs/<ref_id>/tags", methods=["POST"])
def add_tag(ref_id: str):
    tag = (request.json or {}).get("tag", "").strip().lower()
    if not tag:
        return jsonify({"error": "No tag provided"}), 400
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            db.add_tags(ref_id, [tag])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refs/<ref_id>/tags/<tag>", methods=["DELETE"])
def remove_tag(ref_id: str, tag: str):
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            db.remove_tag(ref_id, tag)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refs/<ref_id>", methods=["DELETE"])
def delete_ref(ref_id: str):
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            db.delete(ref_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refs/<ref_id>", methods=["PATCH"])
def patch_ref(ref_id: str):
    """Update editable fields: notes, status, or bibliographic metadata."""
    import json as _json
    body = request.json or {}

    # User-facing metadata
    notes    = body.get("notes")
    status   = body.get("status")
    title    = body.get("title")
    journal  = body.get("journal")
    volume   = body.get("volume")
    issue    = body.get("issue")
    pages    = body.get("pages")
    abstract = body.get("abstract")
    cite_key = body.get("cite_key")

    year = body.get("year")
    if year is not None:
        try:
            year = int(year) if year != "" else None
        except (TypeError, ValueError):
            return jsonify({"error": "year must be an integer"}), 400

    # Authors: accept either a list of {family, given} dicts or None
    authors_json = None
    if "authors" in body:
        raw = body["authors"]
        if isinstance(raw, list):
            authors_json = _json.dumps(raw)
        elif isinstance(raw, str):
            authors_json = raw  # pre-serialized

    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            if db.get(ref_id) is None:
                abort(404)
            db.update_ref_fields(
                ref_id,
                notes=notes, status=status,
                title=title, year=year,
                journal=journal, volume=volume, issue=issue,
                pages=pages, abstract=abstract,
                cite_key=cite_key, authors_json=authors_json,
            )
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

@app.route("/api/collections")
def list_collections():
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            return jsonify(db.get_collections())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/collections", methods=["POST"])
def create_collection():
    name      = (request.json or {}).get("name", "").strip()
    parent_id = (request.json or {}).get("parent_id")
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            cid = db.create_collection(name, parent_id)
        return jsonify({"id": cid, "name": name}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/collections/<int:collection_id>", methods=["DELETE"])
def delete_collection(collection_id: int):
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            db.delete_collection(collection_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/collections/<int:collection_id>", methods=["PATCH"])
def rename_collection(collection_id: int):
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            db.rename_collection(collection_id, name)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refs/<ref_id>/collections", methods=["POST"])
def add_ref_to_collection(ref_id: str):
    collection_id = (request.json or {}).get("collection_id")
    if collection_id is None:
        return jsonify({"error": "collection_id is required"}), 400
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            db.add_to_collection(ref_id, int(collection_id))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refs/<ref_id>/collections/<int:collection_id>", methods=["DELETE"])
def remove_ref_from_collection(ref_id: str, collection_id: int):
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            db.remove_from_collection(ref_id, collection_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Semantic similarity
# ---------------------------------------------------------------------------

@app.route("/api/refs/<ref_id>/similar")
def get_similar(ref_id: str):
    """Return semantically similar references using the embedding index."""
    n = min(int(request.args.get("n", 8)), 20)
    try:
        from .semantic import SemanticIndex
        from .db import RefDatabase
        idx = SemanticIndex()
        pairs = idx.find_similar(ref_id, n=n)
        if not pairs:
            return jsonify([])
        with RefDatabase() as db:
            results = []
            for sid, score in pairs:
                ref = db.get(sid)
                if ref:
                    tags = db.get_tags(sid)
                    d = _ref_to_dict(ref, tags, sid)
                    d["similarity"] = round(score, 3)
                    results.append(d)
        return jsonify(results)
    except RuntimeError as e:
        return jsonify({"error": str(e), "not_indexed": True}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------

@app.route("/api/batch", methods=["POST"])
def batch_op():
    """
    Apply a single action to multiple references at once.

    Body:
        ref_ids       list[str]  — reference IDs to act on
        action        str        — one of: delete, tag, untag, set_status,
                                           add_to_collection, remove_from_collection
        tag           str        — required for tag/untag
        status        str        — required for set_status
        collection_id int        — required for collection ops
    """
    body    = request.json or {}
    ref_ids = body.get("ref_ids", [])
    action  = body.get("action", "")
    if not ref_ids or not action:
        return jsonify({"error": "ref_ids and action are required"}), 400
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            if action == "delete":
                for rid in ref_ids:
                    db.delete(rid)
            elif action == "tag":
                tag = body.get("tag", "").strip().lower()
                if not tag:
                    return jsonify({"error": "tag is required"}), 400
                for rid in ref_ids:
                    db.add_tags(rid, [tag])
            elif action == "untag":
                tag = body.get("tag", "").strip().lower()
                if not tag:
                    return jsonify({"error": "tag is required"}), 400
                for rid in ref_ids:
                    db.remove_tag(rid, tag)
            elif action == "set_status":
                status = body.get("status", "")
                for rid in ref_ids:
                    db.update_ref_fields(rid, status=status)
            elif action == "add_to_collection":
                cid = body.get("collection_id")
                if cid is None:
                    return jsonify({"error": "collection_id is required"}), 400
                for rid in ref_ids:
                    db.add_to_collection(rid, int(cid))
            elif action == "remove_from_collection":
                cid = body.get("collection_id")
                if cid is None:
                    return jsonify({"error": "collection_id is required"}), 400
                for rid in ref_ids:
                    db.remove_from_collection(rid, int(cid))
            else:
                return jsonify({"error": f"Unknown action: {action}"}), 400
        return jsonify({"ok": True, "count": len(ref_ids)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tags")
def list_tags():
    """Return all tags with counts, sorted by usage (for autocomplete)."""
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            tags = db.all_tags()
        return jsonify([{"name": t["name"], "count": t["count"]} for t in tags])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/import", methods=["POST"])
def import_file():
    """
    Import references from an uploaded .bib or .ris file.

    Accepts multipart/form-data with:
        file   — the .bib or .ris file
        enrich — 'true' (default) to enrich via CrossRef; 'false' for fast import
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f      = request.files["file"]
    enrich = request.form.get("enrich", "true").lower() == "true"
    fname  = (f.filename or "").lower()
    raw    = f.read().decode("utf-8", errors="replace")
    try:
        if fname.endswith(".ris"):
            from .parsers.ris import parse_ris_string
            refs = parse_ris_string(raw)
        else:
            from .parsers.bibtex import parse_bibtex_string
            refs = parse_bibtex_string(raw)
    except Exception as e:
        return jsonify({"error": f"Parse error: {e}"}), 422

    if not refs:
        return jsonify({"error": "No references found in file"}), 422

    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "message": f"Importing {len(refs)} references…", "count": 0}

    def _import_worker():
        try:
            from .tagger  import auto_tag, tag_from_keywords
            from .config  import get_config
            from .db      import RefDatabase

            if enrich:
                import anyio
                from .lookup import enrich_batch
                async def _run():
                    return await enrich_batch(refs)
                enriched = anyio.run(_run)
            else:
                enriched = refs

            cfg = get_config()
            with RefDatabase() as db:
                for ref in enriched:
                    tags = list(set(auto_tag(ref, cfg) + tag_from_keywords(ref)))
                    db.upsert(ref, tags=tags)
            n = len(enriched)
            with _jobs_lock:
                _jobs[job_id] = {"status": "done", "count": n,
                                 "message": f"Imported {n} reference{'s' if n != 1 else ''}"}
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id] = {"status": "error", "message": str(e)}

    threading.Thread(target=_import_worker, daemon=True).start()
    return jsonify({"job_id": job_id, "count": len(refs)}), 202


@app.route("/api/duplicates")
def find_duplicates():
    """
    Return groups of references that are likely duplicates.
    Groups by: (1) same DOI, (2) same normalised title + year.
    """
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            all_refs = db.list_all(limit=10_000)

        groups: list = []

        # Group by DOI
        doi_map: dict = {}
        for ref in all_refs:
            if ref.doi:
                doi_map.setdefault(ref.doi.lower().strip(), []).append(ref)
        for doi, grp in doi_map.items():
            if len(grp) > 1:
                groups.append({
                    "reason": f"Same DOI: {doi}",
                    "refs": [_ref_to_dict(r, [], _ref_id(r)) for r in grp],
                })

        # Group by normalised title+year (no-DOI refs only)
        def _norm(t: str) -> str:
            return re.sub(r"[^a-z0-9]", "", (t or "").lower())

        title_map: dict = {}
        for ref in all_refs:
            if not ref.doi:
                key = (_norm(ref.title), ref.year)
                if key[0]:
                    title_map.setdefault(key, []).append(ref)
        for (title, year), grp in title_map.items():
            if len(grp) > 1:
                groups.append({
                    "reason": f"Same title+year ({year or '?'})",
                    "refs": [_ref_to_dict(r, [], _ref_id(r)) for r in grp],
                })

        return jsonify(groups)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refs/<ref_id>/pdf")
def get_ref_pdf(ref_id: str):
    """Serve or redirect to the PDF for a reference.

    Priority:
      1. Google Drive file ID → redirect to Drive viewer
      2. Local file path (still on disk) → stream to browser
      3. 404
    """
    try:
        from pathlib import Path as _P
        from .db import RefDatabase
        with RefDatabase() as db:
            extra = db.get_extra(ref_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    drive_id = extra.get("pdf_drive_id")
    if drive_id:
        from .integrations.google_drive import get_view_url
        return redirect(get_view_url(drive_id))

    local = extra.get("pdf_local")
    if local:
        path = _P(local)
        if path.exists():
            return send_file(path, mimetype="application/pdf")

    return jsonify({"error": "PDF not available"}), 404


@app.route("/api/export")
def export_refs():
    """Export references.  Supports ?fmt=bibtex|ris|markdown and optional
    ?collection_id=<int> or ?ref_ids=id1,id2,… to narrow the set."""
    fmt           = request.args.get("fmt", "bibtex")
    collection_id = request.args.get("collection_id")
    ref_ids_param = request.args.get("ref_ids", "")
    try:
        from .db import RefDatabase
        from .exporters.bibtex   import to_bibtex_string
        from .exporters.ris      import to_ris_string
        from .exporters.markdown import to_markdown_string
        with RefDatabase() as db:
            if ref_ids_param:
                ids  = [i.strip() for i in ref_ids_param.split(",") if i.strip()]
                refs = [r for r in (db.get(i) for i in ids) if r is not None]
                fname_base = "selection"
            elif collection_id:
                refs       = db.list_collection_refs(int(collection_id), limit=10_000)
                coll_name  = next(
                    (c["name"] for c in db.get_collections() if c["id"] == int(collection_id)),
                    "collection",
                )
                fname_base = re.sub(r"[^a-z0-9_-]", "_", coll_name.lower())[:32]
            else:
                refs       = db.list_all(limit=10_000)
                fname_base = "refs"
        if fmt == "ris":
            content, mime = to_ris_string(refs), "application/x-research-info-systems"
            fname = fname_base + ".ris"
        elif fmt == "markdown":
            content, mime = to_markdown_string(refs), "text/markdown"
            fname = fname_base + ".md"
        else:
            content, mime = to_bibtex_string(refs), "application/x-bibtex"
            fname = fname_base + ".bib"
        return Response(content, mimetype=mime,
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats")
def stats():
    """Rich library analytics for the stats dashboard."""
    try:
        from collections import Counter, defaultdict
        from .db import RefDatabase
        with RefDatabase() as db:
            all_refs = db.list_all(limit=10_000)
            all_tags = db.all_tags()
            # Read counts from extras
            conn = db._conn
            status_rows = conn.execute(
                "SELECT COALESCE(status,'unread') as s, COUNT(*) as c FROM refs GROUP BY s"
            ).fetchall()

        n   = len(all_refs)
        avg = sum(r.completeness for r in all_refs) / n if n else 0.0

        # Papers by year (last 30 years)
        year_counts: dict = Counter()
        for r in all_refs:
            if r.year and r.year >= 1990:
                year_counts[r.year] += 1
        by_year = [{"year": y, "count": c} for y, c in sorted(year_counts.items())]

        # Papers by type
        type_counts: dict = Counter(r.ref_type.value for r in all_refs)
        by_type = [{"type": t, "count": c} for t, c in type_counts.most_common(8)]

        # Open access
        oa_count = sum(1 for r in all_refs if r.open_access)

        # Top journals
        journal_counts: dict = Counter(r.journal for r in all_refs if r.journal)
        top_journals = [{"name": j, "count": c} for j, c in journal_counts.most_common(10)]

        # Top authors
        author_counts: dict = Counter()
        for r in all_refs:
            for a in r.authors:
                if a.family:
                    author_counts[a.family + (f", {a.given[0]}." if a.given else "")] += 1
        top_authors = [{"name": a, "count": c} for a, c in author_counts.most_common(10)]

        # Citation count stats
        cited = [r.citation_count for r in all_refs if r.citation_count is not None]
        citation_stats = {
            "total":  sum(cited),
            "max":    max(cited) if cited else 0,
            "median": sorted(cited)[len(cited)//2] if cited else 0,
        }

        # Reading status
        status_map = {row["s"]: row["c"] for row in status_rows}

        return jsonify({
            "count":            n,
            "avg_completeness": round(avg, 3),
            "oa_count":         oa_count,
            "oa_pct":           round(oa_count / n * 100, 1) if n else 0,
            "by_year":          by_year,
            "by_type":          by_type,
            "top_tags":         [{"name": t["name"], "count": t["count"]} for t in all_tags[:15]],
            "top_journals":     top_journals,
            "top_authors":      top_authors,
            "citation_stats":   citation_stats,
            "status":           status_map,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tags/<tag_name>", methods=["PATCH"])
def update_tag(tag_name: str):
    """Update tag metadata (currently: color)."""
    body  = request.json or {}
    color = body.get("color", "").strip()
    if color and not re.match(r"^#[0-9a-fA-F]{3,6}$", color):
        return jsonify({"error": "Invalid color — use hex e.g. #ff6b6b"}), 400
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            db._conn.execute(
                "UPDATE tags SET color = ? WHERE name = ?",
                (color or "#6366f1", tag_name),
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/csv")
def export_csv():
    """Export references as CSV — useful for spreadsheet import."""
    import csv
    import io
    collection_id = request.args.get("collection_id")
    ref_ids_param = request.args.get("ref_ids", "")
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            if ref_ids_param:
                ids  = [i.strip() for i in ref_ids_param.split(",") if i.strip()]
                refs = [r for r in (db.get(i) for i in ids) if r is not None]
            elif collection_id:
                refs = db.list_collection_refs(int(collection_id), limit=10_000)
            else:
                refs = db.list_all(limit=10_000)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "cite_key", "title", "authors", "year", "journal", "volume", "issue",
            "pages", "doi", "arxiv_id", "pmid", "url", "abstract",
            "type", "open_access", "citation_count", "keywords",
        ])
        for ref in refs:
            writer.writerow([
                ref.cite_key or ref.auto_cite_key(),
                ref.title or "",
                "; ".join(a.full_name for a in ref.authors),
                ref.year or "",
                ref.journal or ref.container_title or "",
                ref.volume or "",
                ref.issue or "",
                ref.pages or "",
                ref.doi or "",
                ref.arxiv_id or "",
                ref.pmid or "",
                ref.url or ref.oa_url or "",
                (ref.abstract or "").replace("\n", " "),
                ref.ref_type.value,
                "yes" if ref.open_access else "",
                ref.citation_count or "",
                "; ".join(ref.keywords),
            ])
        content = buf.getvalue()
        return Response(content, mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="refs.csv"'})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/enrich-incomplete", methods=["POST"])
def enrich_incomplete():
    """
    Background job: re-enrich all refs with completeness below a threshold.
    Body: { "threshold": 0.5, "limit": 100 }
    """
    body      = request.json or {}
    threshold = float(body.get("threshold", 0.5))
    limit     = min(int(body.get("limit", 100)), 500)
    job_id    = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "message": "Finding incomplete refs…", "count": 0}

    def _worker():
        try:
            import anyio
            from .db     import RefDatabase
            from .lookup import enrich_batch
            from .tagger import auto_tag, tag_from_keywords
            from .config import get_config

            with RefDatabase() as db:
                all_refs = db.list_all(limit=10_000)
                targets  = [r for r in all_refs if r.completeness < threshold][:limit]

            if not targets:
                with _jobs_lock:
                    _jobs[job_id] = {"status": "done", "count": 0,
                                     "message": "All refs already complete!"}
                return

            with _jobs_lock:
                _jobs[job_id]["message"] = f"Enriching {len(targets)} references…"

            async def _run():
                return await enrich_batch(targets)

            enriched = anyio.run(_run)
            cfg      = get_config()
            with RefDatabase() as db:
                for ref in enriched:
                    tags = list(set(auto_tag(ref, cfg) + tag_from_keywords(ref)))
                    db.upsert(ref, tags=tags)

            n = len(enriched)
            with _jobs_lock:
                _jobs[job_id] = {"status": "done", "count": n,
                                 "message": f"Enriched {n} reference{'s' if n != 1 else ''}"}
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id] = {"status": "error", "message": str(e)}

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"job_id": job_id}), 202


@app.route("/api/refs/search/similar", methods=["POST"])
def find_similar_to_text():
    """
    Find library refs semantically similar to an arbitrary text passage.
    Body: { "text": "...", "n": 8 }
    """
    body = request.json or {}
    text = body.get("text", "").strip()
    n    = min(int(body.get("n", 8)), 20)
    if not text:
        return jsonify({"error": "text is required"}), 400
    try:
        from .semantic import SemanticIndex
        from .db import RefDatabase
        idx   = SemanticIndex()
        pairs = idx.search(text, n=n)
        if not pairs:
            return jsonify([])
        with RefDatabase() as db:
            results = []
            for sid, score in pairs:
                ref = db.get(sid)
                if ref:
                    tags = db.get_tags(sid)
                    d = _ref_to_dict(ref, tags, sid)
                    d["similarity"] = round(score, 3)
                    results.append(d)
        return jsonify(results)
    except RuntimeError as e:
        return jsonify({"error": str(e), "not_indexed": True}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Streaming API — Zotero real-time sync background thread
# ---------------------------------------------------------------------------

@app.route("/api/stream/status")
def stream_status():
    """Return the current status of the Zotero streaming listener."""
    return jsonify({
        "active":            bool(_stream_thread and _stream_thread.is_alive()),
        "last_event_at":     _stream_status["last_event_at"],
        "library_version":   _stream_status["library_version"],
        "last_synced_count": _stream_status["last_synced_count"],
        "error":             _stream_status["error"],
    })


@app.route("/api/stream/zotero/start", methods=["POST"])
def start_zotero_stream():
    """Start the Zotero streaming listener in a background thread."""
    global _stream_thread
    if _stream_thread and _stream_thread.is_alive():
        return jsonify({"ok": True, "message": "Already running"})
    _stream_stop_event.clear()
    _stream_thread = threading.Thread(
        target=_zotero_stream_worker, daemon=True, name="zotero-stream"
    )
    _stream_thread.start()
    return jsonify({"ok": True, "message": "Streaming started"})


@app.route("/api/stream/zotero/stop", methods=["POST"])
def stop_zotero_stream():
    """Signal the streaming thread to stop."""
    _stream_stop_event.set()
    return jsonify({"ok": True, "message": "Stop signal sent"})


def _zotero_stream_worker() -> None:
    """
    Background thread: connects to the Zotero streaming API (websocket) and
    pulls changed items into the local database whenever the library changes.
    Requires the ``websockets`` package.
    """
    import datetime

    _stream_status.update({"active": True, "error": None})

    async def _run() -> None:
        from .integrations.zotero import ZoteroIntegration
        from .db import RefDatabase
        from .tagger import auto_tag, tag_from_keywords
        from .config import get_config

        try:
            async with ZoteroIntegration() as intg:
                if not await intg.is_configured():
                    _stream_status.update({
                        "active": False,
                        "error": "Zotero not configured",
                    })
                    return

                async for _topic, version in intg.stream_changes():
                    if _stream_stop_event.is_set():
                        break

                    _stream_status["last_event_at"] = (
                        datetime.datetime.utcnow().isoformat() + "Z"
                    )

                    with RefDatabase() as db:
                        stored = db.get_setting("zotero_library_version")
                    since = int(stored) if stored else None

                    refs, keys, new_version = await intg.pull(since=since)

                    if refs:
                        cfg = get_config()
                        with RefDatabase() as db:
                            tags_per_ref = [
                                list(set(auto_tag(r, cfg) + tag_from_keywords(r)))
                                for r in refs
                            ]
                            ids = db.upsert_many(refs, tags_per_ref=tags_per_ref)
                            for ref_id, key in zip(ids, keys):
                                if key and not db.get_extra(ref_id).get("zotero_item_key"):
                                    db.update_integration_ids(ref_id, zotero_item_key=key)
                            if new_version:
                                db.set_setting("zotero_library_version", str(new_version))

                    _stream_status.update({
                        "library_version":   new_version,
                        "last_synced_count": len(refs),
                    })

        except Exception as exc:
            _stream_status.update({"active": False, "error": str(exc)})
            raise

    try:
        asyncio.run(_run())
    finally:
        _stream_status["active"] = False


# ---------------------------------------------------------------------------
# Webhooks — receive push notifications from external tools
# ---------------------------------------------------------------------------

@app.route("/webhooks/notion", methods=["POST"])
def notion_webhook():
    """
    Receive a Notion webhook event.

    Notion posts to this URL when a page in the configured database is created
    or updated.  We verify the HMAC-SHA256 signature (if a secret is
    configured), extract the page ID, fetch the updated page, convert it to a
    Reference, and upsert it into the local database.

    Configure a webhook secret via the ``NOTION_WEBHOOK_SECRET`` environment
    variable or ``notion_webhook_secret`` in the config file.
    """
    # --- Signature verification ---
    secret = _notion_webhook_secret()
    if secret:
        raw_body  = request.get_data()
        sig_header = request.headers.get("X-Notion-Signature", "")
        expected  = "sha256=" + hmac.new(
            secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            abort(403, "Invalid signature")

    payload    = request.json or {}
    event_type = (payload.get("type") or "").lower()

    # Notion sends page.created, page.updated, page.property_value_updated, etc.
    if "page" not in event_type:
        return jsonify({"ok": True, "skipped": True})

    # Extract page ID from various payload shapes Notion may send
    entity  = payload.get("entity") or {}
    data    = payload.get("data") or {}
    page_id = entity.get("id") or data.get("id") or data.get("page_id") or ""

    if page_id:
        threading.Thread(
            target=_handle_notion_page_update,
            args=(page_id,),
            daemon=True,
        ).start()

    return jsonify({"ok": True, "page_id": page_id})


def _handle_notion_page_update(page_id: str) -> None:
    """Background: fetch the updated Notion page and upsert into local DB."""
    async def _run() -> None:
        from .integrations.notion import NotionIntegration, _notion_page_to_ref
        from .db import RefDatabase

        try:
            async with NotionIntegration() as intg:
                if not await intg.is_configured():
                    return
                resp = await intg._client.get(
                    f"https://api.notion.com/v1/pages/{page_id}"
                )
                if resp.status_code != 200:
                    return
                ref = _notion_page_to_ref(resp.json())
                if ref is None:
                    return
                with RefDatabase() as db:
                    ref_id = db.upsert(ref)
                    if not db.get_extra(ref_id).get("notion_page_id"):
                        db.update_integration_ids(ref_id, notion_page_id=page_id)
        except Exception:
            pass

    try:
        asyncio.run(_run())
    except Exception:
        pass


def _notion_webhook_secret() -> Optional[str]:
    import os
    try:
        from .config import get_config
        cfg = get_config()
        if hasattr(cfg, "notion_webhook_secret") and cfg.notion_webhook_secret:
            return cfg.notion_webhook_secret
    except Exception:
        pass
    return os.environ.get("NOTION_WEBHOOK_SECRET") or None


# ---------------------------------------------------------------------------
# PWA support — manifest, service worker, icon
# ---------------------------------------------------------------------------

@app.route("/api/saved-searches", methods=["GET"])
def list_saved_searches():
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            return jsonify(db.list_saved_searches())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/saved-searches", methods=["POST"])
def create_saved_search():
    import json as _json
    body = request.json or {}
    name = body.get("name", "").strip()
    query = body.get("query", "")
    filters = body.get("filters", {})
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            sid = db.create_saved_search(name, query, _json.dumps(filters))
        return jsonify({"ok": True, "id": sid}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/saved-searches/<int:search_id>", methods=["DELETE"])
def delete_saved_search(search_id: int):
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            db.delete_saved_search(search_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/manifest.json")
def pwa_manifest():
    return jsonify({
        "name": "zoterpile",
        "short_name": "zoterpile",
        "description": "Reference manager — search, enrich, sync",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d0d12",
        "theme_color": "#5b8af5",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"},
        ],
    })


@app.route("/icon.svg")
def pwa_icon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="12" fill="#0d0d12"/>'
        '<text x="32" y="46" font-size="38" text-anchor="middle" '
        'font-family="system-ui" fill="#5b8af5">&#128194;</text>'
        '</svg>'
    )
    return Response(svg, mimetype="image/svg+xml")


@app.route("/sw.js")
def service_worker():
    """Minimal service worker for PWA installability and offline shell caching."""
    js = r"""
const CACHE = 'zoterpile-v1';
const SHELL = ['/'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Network-first for API; cache-first for shell.
self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/')) {
    e.respondWith(fetch(e.request));
  } else {
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request))
    );
  }
});
"""
    return Response(js, mimetype="application/javascript")


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return _HTML


# ---------------------------------------------------------------------------
# Background enrichment worker
# ---------------------------------------------------------------------------

def _enrich_worker(text: str, job_id: str) -> None:
    import anyio

    try:
        from .input  import parse_input
        from .lookup import enrich_batch
        from .tagger import auto_tag, tag_from_keywords
        from .config import get_config
        from .db     import RefDatabase

        refs = parse_input(text)
        if not refs:
            with _jobs_lock:
                _jobs[job_id] = {"status": "error",
                                 "message": "Could not parse — try a DOI, arXiv ID, or URL"}
            return

        with _jobs_lock:
            _jobs[job_id]["message"] = f"Enriching {len(refs)} reference(s)…"

        async def _run():
            return await enrich_batch(refs)

        enriched = anyio.run(_run)
        cfg = get_config()
        with RefDatabase() as db:
            for ref in enriched:
                tags = list(set(auto_tag(ref, cfg) + tag_from_keywords(ref)))
                db.upsert(ref, tags=tags)

        n = len(enriched)
        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "count": n,
                             "message": f"Added {n} reference{'s' if n != 1 else ''}"}
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id] = {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Embedded single-page app
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#5b8af5">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.svg">
<title>zoterpile</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:          #0d0d12;
  --surface:     #14141c;
  --panel:       #1b1b26;
  --border:      #26263a;
  --border-hi:   #3a3a55;
  --primary:     #5b8af5;
  --primary-dim: #2a3f78;
  --success:     #3ecf8e;
  --warning:     #f0b429;
  --error:       #ef4444;
  --text:        #dde1f0;
  --muted:       #6c6c8a;
  --tag-bg:      #22223a;
  --tag-fg:      #8888cc;
  --r:           7px;
  --font:        system-ui,-apple-system,'Segoe UI',sans-serif;
  --mono:        'JetBrains Mono','Fira Code','Courier New',monospace;
}
[data-theme="light"] {
  --bg:          #f8f9fb;
  --surface:     #ffffff;
  --panel:       #f0f1f5;
  --border:      #e2e4eb;
  --border-hi:   #c8ccd8;
  --primary:     #3b6ff0;
  --primary-dim: #dce6ff;
  --success:     #10a56a;
  --warning:     #d4870e;
  --error:       #dc2626;
  --text:        #1a1d2e;
  --muted:       #7a7f99;
  --tag-bg:      #ededf8;
  --tag-fg:      #4b4f99;
}
html, body { height: 100%; overflow: hidden; }
body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  display: flex;
  flex-direction: column;
  font-size: 14px;
}

/* ── Header ── */
header {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 0 18px;
  height: 54px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.logo {
  font-size: 17px;
  font-weight: 700;
  color: var(--primary);
  letter-spacing: -.4px;
  white-space: nowrap;
}
.logo span { color: var(--muted); font-weight: 400; font-size: 13px; margin-left: 6px; }
.search-area { display: flex; align-items: center; gap: 8px; flex: 1; }
#search {
  flex: 1; max-width: 380px;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--r); padding: 7px 12px;
  color: var(--text); font-size: 13px; outline: none;
  transition: border-color .15s;
}
#search:focus { border-color: var(--primary); }
#search::placeholder { color: var(--muted); }
.filters { display: flex; gap: 3px; }
.f-btn {
  padding: 5px 11px; border: 1px solid var(--border);
  border-radius: var(--r); background: transparent;
  color: var(--muted); font-size: 12px; cursor: pointer;
  transition: all .15s; font-family: var(--font);
}
.f-btn:hover { background: var(--panel); color: var(--text); }
.f-btn.active { background: var(--primary-dim); border-color: var(--primary); color: var(--text); font-weight: 600; }
.h-actions { display: flex; gap: 8px; flex-shrink: 0; }
.btn {
  padding: 7px 14px; border: none; border-radius: var(--r);
  font-size: 13px; cursor: pointer; font-weight: 500;
  font-family: var(--font); transition: opacity .15s;
}
.btn:hover { opacity: .85; }
.btn:disabled { opacity: .4; cursor: not-allowed; }
.btn-primary { background: var(--primary); color: #fff; }
.btn-ghost { background: var(--panel); border: 1px solid var(--border); color: var(--text); }

/* ── Main split ── */
main { display: flex; flex: 1; overflow: hidden; }

/* ── Ref list ── */
.ref-list {
  width: 340px; flex-shrink: 0;
  overflow-y: auto; overflow-x: hidden;
  border-right: 1px solid var(--border);
  background: var(--surface);
}
.ref-card {
  padding: 12px 14px; border-bottom: 1px solid var(--border);
  cursor: pointer; display: flex; gap: 10px; align-items: flex-start;
  transition: background .1s; position: relative;
}
.ref-card:hover { background: var(--panel); }
.ref-card.active {
  background: var(--panel);
  border-left: 3px solid var(--primary);
  padding-left: 11px;
}
.dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 5px; flex-shrink: 0; }
.dot-g { background: var(--success); }
.dot-y { background: var(--warning); }
.dot-r { background: var(--error); }
.rc-body { flex: 1; min-width: 0; }
.rc-title {
  font-size: 13px; font-weight: 500; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  margin-bottom: 2px; line-height: 1.4;
}
.rc-meta { font-size: 11px; color: var(--muted); margin-bottom: 5px; }
.rc-tags { display: flex; flex-wrap: wrap; gap: 3px; }
.tag {
  padding: 1px 7px; background: var(--tag-bg);
  color: var(--tag-fg); border-radius: 4px; font-size: 11px;
}

/* ── Empty / loading states ── */
.empty {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; height: 100%;
  gap: 10px; color: var(--muted); padding: 32px; text-align: center;
}
.empty-icon { font-size: 36px; }
.empty h3 { color: var(--text); font-size: 15px; }
.empty p { font-size: 13px; line-height: 1.5; }

/* ── Detail panel ── */
.detail { flex: 1; overflow-y: auto; padding: 28px 32px; background: var(--bg); }
.detail-ph { display: flex; align-items: center; justify-content: center; height: 100%; color: var(--muted); font-size: 15px; }
.d-title { font-size: 21px; font-weight: 700; line-height: 1.3; margin-bottom: 8px; }
.d-authors { font-size: 13px; color: var(--muted); margin-bottom: 3px; }
.d-venue { font-size: 13px; color: var(--muted); font-style: italic; margin-bottom: 14px; }
.d-badges { display: flex; gap: 7px; flex-wrap: wrap; margin-bottom: 16px; }
.badge {
  padding: 3px 9px; border-radius: 999px;
  font-size: 11px; font-weight: 500;
}
.b-doi { background: #152040; color: #7ab2ff; font-family: var(--mono); }
.b-arxiv { background: #301a0a; color: #ffaa70; font-family: var(--mono); }
.b-oa { background: #0d3326; color: #3ecf8e; }
.b-type { background: var(--tag-bg); color: var(--tag-fg); }
hr.div { border: none; border-top: 1px solid var(--border); margin: 18px 0; }

/* Tags editable */
.section-label {
  font-size: 11px; color: var(--muted); text-transform: uppercase;
  letter-spacing: .6px; margin-bottom: 8px; font-weight: 600;
}
.tags-row { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 8px; }
.tag-edit {
  display: inline-flex; align-items: center; gap: 3px;
  padding: 2px 6px 2px 9px; background: var(--tag-bg);
  color: var(--tag-fg); border-radius: 4px; font-size: 12px;
}
.tag-x {
  background: none; border: none; color: var(--muted);
  cursor: pointer; font-size: 15px; line-height: 1;
  display: flex; align-items: center; padding: 0;
}
.tag-x:hover { color: var(--error); }
.tag-add-row { display: flex; gap: 6px; align-items: center; margin-top: 4px; }
.tag-inp {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--r); padding: 5px 10px;
  color: var(--text); font-size: 12px; outline: none; width: 150px;
  transition: border-color .15s;
}
.tag-inp:focus { border-color: var(--primary); }

/* Abstract */
.abstract {
  font-size: 13px; line-height: 1.7; color: var(--text);
  max-height: 180px; overflow-y: auto;
}

/* Completeness bar */
.comp-row { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--muted); }
.bar { height: 5px; border-radius: 3px; background: var(--border); width: 110px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 3px; transition: width .3s; }
.bar-g { background: var(--success); }
.bar-y { background: var(--warning); }
.bar-r { background: var(--error); }

/* Detail actions */
.d-actions { display: flex; gap: 8px; flex-wrap: wrap; padding-top: 4px; }
.btn-sm { padding: 5px 11px; font-size: 12px; }
a.btn { text-decoration: none; display: inline-flex; align-items: center; }

/* ── Status bar ── */
.statusbar {
  height: 24px; background: var(--surface);
  border-top: 1px solid var(--border);
  padding: 0 18px; display: flex; align-items: center;
  font-size: 11px; color: var(--muted); flex-shrink: 0;
}

/* ── Spinner ── */
.spin {
  display: inline-block; width: 12px; height: 12px;
  border: 2px solid var(--border); border-top-color: var(--primary);
  border-radius: 50%; animation: spin .7s linear infinite; margin-right: 6px;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Modal ── */
.overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.65);
  display: flex; align-items: center; justify-content: center;
  z-index: 200; opacity: 0; pointer-events: none; transition: opacity .15s;
}
.overlay.open { opacity: 1; pointer-events: all; }
.modal-box {
  background: var(--panel); border: 1px solid var(--border-hi);
  border-radius: 12px; padding: 26px 28px;
  width: 500px; max-width: 92vw;
  box-shadow: 0 24px 64px rgba(0,0,0,.6);
}
.modal-box h2 { font-size: 17px; font-weight: 700; margin-bottom: 6px; }
.modal-hint { font-size: 12px; color: var(--muted); margin-bottom: 14px; line-height: 1.5; }
.modal-ta {
  width: 100%; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r); padding: 10px 12px;
  color: var(--text); font-size: 13px; font-family: var(--mono);
  resize: vertical; min-height: 72px; outline: none;
  transition: border-color .15s;
}
.modal-ta:focus { border-color: var(--primary); }
.modal-status {
  height: 22px; font-size: 12px; margin-top: 6px;
  display: flex; align-items: center;
}
.s-run { color: var(--warning); }
.s-ok  { color: var(--success); }
.s-err { color: var(--error); }
.modal-foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }

/* ── Dropdown ── */
.dd-wrap { position: relative; }
.dd-menu {
  display: none; position: absolute; top: calc(100% + 6px); right: 0;
  background: var(--panel); border: 1px solid var(--border-hi);
  border-radius: var(--r); box-shadow: 0 8px 28px rgba(0,0,0,.5);
  min-width: 150px; z-index: 100; overflow: hidden;
}
.dd-menu.open { display: block; }
.dd-item {
  display: block; padding: 9px 15px;
  color: var(--text); font-size: 13px; cursor: pointer;
  transition: background .1s;
}
.dd-item:hover { background: var(--border); }

/* ── Collections sidebar ── */
.coll-sidebar {
  width: 190px; flex-shrink: 0;
  border-right: 1px solid var(--border);
  background: var(--surface);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.coll-header {
  padding: 10px 12px 8px;
  font-size: 11px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .6px; font-weight: 600;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
}
.coll-new-btn {
  background: none; border: none; color: var(--muted);
  cursor: pointer; font-size: 18px; line-height: 1; padding: 0;
  display: flex; align-items: center;
}
.coll-new-btn:hover { color: var(--primary); }
.coll-list { flex: 1; overflow-y: auto; padding: 4px 0; }
.coll-item {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 12px; cursor: pointer;
  font-size: 13px; color: var(--text);
  transition: background .1s; position: relative;
}
.coll-item:hover { background: var(--panel); }
.coll-item.active { background: var(--panel); color: var(--primary); font-weight: 600; }
.coll-item-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.coll-count { font-size: 11px; color: var(--muted); flex-shrink: 0; }
.coll-del {
  display: none; background: none; border: none; color: var(--muted);
  cursor: pointer; font-size: 14px; line-height: 1; padding: 0; margin-left: 2px;
}
.coll-item:hover .coll-del { display: flex; align-items: center; }
.coll-del:hover { color: var(--error); }

/* ── Status toggle ── */
.status-row { display: flex; gap: 4px; margin-bottom: 12px; }
.status-btn {
  padding: 4px 12px; border-radius: 999px; font-size: 12px;
  cursor: pointer; border: 1px solid var(--border);
  background: transparent; color: var(--muted);
  font-family: var(--font); transition: all .15s;
}
.status-btn:hover { border-color: var(--border-hi); color: var(--text); }
.status-btn.active-unread  { background: #301a2a; border-color: #cc6699; color: #ee88bb; }
.status-btn.active-reading { background: #1a2a40; border-color: #5588cc; color: #88aaee; }
.status-btn.active-read    { background: #0d3326; border-color: #3ecf8e; color: #3ecf8e; }

/* ── Notes editor ── */
.notes-ta {
  width: 100%; background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--r); padding: 8px 10px;
  color: var(--text); font-size: 13px; font-family: var(--font);
  resize: vertical; min-height: 70px; outline: none; line-height: 1.6;
  transition: border-color .15s;
}
.notes-ta:focus { border-color: var(--primary); }
.notes-ta::placeholder { color: var(--muted); }
.notes-saved { font-size: 11px; color: var(--muted); margin-top: 4px; height: 14px; }

/* ── Collection chip in detail ── */
.coll-chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px 2px 10px; background: #1a2040;
  color: #7799ee; border-radius: 4px; font-size: 12px; margin: 2px;
}
.coll-chip-x {
  background: none; border: none; color: var(--muted);
  cursor: pointer; font-size: 14px; line-height: 1; padding: 0;
}
.coll-chip-x:hover { color: var(--error); }
.coll-add-row { display: flex; gap: 6px; align-items: center; margin-top: 6px; }
.coll-select {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--r); padding: 5px 8px;
  color: var(--text); font-size: 12px; outline: none;
  flex: 1; max-width: 200px;
}
.coll-select:focus { border-color: var(--primary); }

/* ── Cite key copy badge ── */
.b-citekey {
  background: #1a2a1a; color: #7acc7a;
  font-family: var(--mono); cursor: pointer;
  transition: background .15s;
}
.b-citekey:hover { background: #243824; }
.b-citekey.copied { background: #0d3326; color: #3ecf8e; }

/* ── Batch selection ── */
.ref-card-cb {
  position: absolute; top: 12px; left: 7px;
  width: 14px; height: 14px; cursor: pointer; z-index: 1;
  opacity: 0; transition: opacity .1s; accent-color: var(--primary);
}
.ref-card:hover .ref-card-cb,
.ref-card.selected .ref-card-cb { opacity: 1; }
.ref-card.selected { background: #181828; border-left: 3px solid var(--primary); padding-left: 11px; }
.sel-toolbar {
  display: none; align-items: center; gap: 6px; flex-shrink: 0;
  padding: 6px 10px; background: var(--panel);
  border-bottom: 1px solid var(--border); flex-wrap: wrap;
}
.sel-toolbar.visible { display: flex; }
.sel-count { font-size: 12px; color: var(--muted); margin-right: 4px; }
.sel-btn {
  padding: 4px 10px; border-radius: var(--r); font-size: 12px;
  cursor: pointer; border: 1px solid var(--border);
  background: var(--surface); color: var(--text);
  font-family: var(--font); transition: background .1s;
}
.sel-btn:hover { background: var(--panel); }
.sel-btn-del { border-color: #552222; color: var(--error); }
.sel-btn-del:hover { background: #2a0a0a; }

/* ── Similar papers modal ── */
.similar-list { display: flex; flex-direction: column; gap: 10px; max-height: 420px; overflow-y: auto; }
.sim-row {
  display: flex; gap: 12px; align-items: flex-start;
  padding: 10px 12px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--r);
  cursor: pointer; transition: background .1s;
}
.sim-row:hover { background: var(--panel); }
.sim-score {
  flex-shrink: 0; width: 38px; text-align: center;
  font-size: 11px; font-weight: 700; color: var(--primary);
  padding-top: 2px;
}
.sim-body { flex: 1; min-width: 0; }
.sim-title {
  font-size: 13px; font-weight: 500; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  margin-bottom: 3px;
}
.sim-meta { font-size: 11px; color: var(--muted); }

/* ── Sort bar ── */
.sort-bar {
  display: flex; align-items: center; gap: 4px; flex-shrink: 0;
  padding: 4px 10px; background: var(--surface);
  border-bottom: 1px solid var(--border);
  font-size: 11px; color: var(--muted);
}
.sort-bar label { white-space: nowrap; }
.sort-sel {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--r); padding: 2px 6px;
  color: var(--text); font-size: 11px; outline: none;
  cursor: pointer;
}
.sort-sel:focus { border-color: var(--primary); }

/* ── Tag autocomplete ── */
.tag-ac-wrap { position: relative; }
.tag-ac-list {
  display: none; position: absolute; top: 100%; left: 0;
  background: var(--panel); border: 1px solid var(--border-hi);
  border-radius: var(--r); box-shadow: 0 8px 24px rgba(0,0,0,.5);
  z-index: 50; min-width: 150px; max-height: 180px; overflow-y: auto;
}
.tag-ac-list.open { display: block; }
.tag-ac-item {
  padding: 6px 12px; cursor: pointer; font-size: 12px;
  color: var(--text); transition: background .1s;
  display: flex; justify-content: space-between; align-items: center;
}
.tag-ac-item:hover, .tag-ac-item.highlighted { background: var(--border); }
.tag-ac-item span { font-size: 10px; color: var(--muted); }

/* ── Citation copy panel ── */
.cite-panel {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r); padding: 10px 12px; margin-top: 6px;
}
.cite-format-row { display: flex; gap: 4px; margin-bottom: 8px; }
.cite-fmt-btn {
  padding: 3px 10px; border-radius: var(--r); font-size: 11px;
  cursor: pointer; border: 1px solid var(--border);
  background: transparent; color: var(--muted);
  font-family: var(--font); transition: all .15s;
}
.cite-fmt-btn.active { background: var(--primary-dim); border-color: var(--primary); color: var(--text); font-weight: 600; }
.cite-text {
  font-size: 12px; line-height: 1.6; color: var(--text);
  user-select: all; word-break: break-word;
  background: var(--panel); border-radius: 4px; padding: 8px 10px;
  min-height: 36px; cursor: text;
}
.cite-copy-row { display: flex; justify-content: flex-end; margin-top: 6px; }

/* ── Edit-in-place ── */
.edit-field-wrap { display: flex; align-items: flex-start; gap: 5px; }
.edit-pencil {
  background: none; border: none; color: var(--muted); cursor: pointer;
  font-size: 12px; padding: 1px 3px; border-radius: 3px; flex-shrink: 0;
  opacity: 0; transition: opacity .1s;
}
.edit-field-wrap:hover .edit-pencil { opacity: 1; }
.edit-pencil:hover { color: var(--primary); background: var(--panel); }
.edit-inp {
  background: var(--panel); border: 1px solid var(--primary);
  border-radius: 4px; padding: 3px 7px;
  color: var(--text); font-size: inherit; font-family: inherit;
  outline: none; flex: 1;
}
.edit-inp-area {
  width: 100%; background: var(--panel); border: 1px solid var(--primary);
  border-radius: 4px; padding: 5px 8px;
  color: var(--text); font-size: 13px; font-family: var(--font);
  resize: vertical; outline: none; line-height: 1.6; min-height: 60px;
}
.edit-save-row { display: flex; gap: 5px; margin-top: 4px; }
.edit-save-btn {
  padding: 2px 10px; border-radius: 4px; font-size: 11px;
  cursor: pointer; border: 1px solid var(--primary);
  background: var(--primary-dim); color: var(--text);
  font-family: var(--font);
}
.edit-cancel-btn {
  padding: 2px 8px; border-radius: 4px; font-size: 11px;
  cursor: pointer; border: 1px solid var(--border);
  background: transparent; color: var(--muted);
  font-family: var(--font);
}

/* ── Keyboard help modal ── */
.kbd-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; }
.kbd-row { display: flex; align-items: center; gap: 8px; font-size: 12px; }
kbd {
  background: var(--panel); border: 1px solid var(--border-hi);
  border-radius: 4px; padding: 1px 6px; font-size: 11px;
  font-family: var(--mono); color: var(--text); white-space: nowrap;
}
.kbd-desc { color: var(--muted); }

/* ── Advanced filter panel ── */
.adv-filter-panel {
  display: none; flex-direction: column; gap: 12px;
  padding: 10px 12px; background: var(--surface);
  border-bottom: 1px solid var(--border); font-size: 12px;
}
.adv-filter-panel.open { display: flex; }
.af-section { display: flex; flex-direction: column; gap: 4px; }
.af-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; font-weight: 600; }
.af-row { display: flex; gap: 4px; flex-wrap: wrap; }
.af-pill {
  padding: 3px 9px; border-radius: 999px; font-size: 11px;
  cursor: pointer; border: 1px solid var(--border);
  background: transparent; color: var(--muted);
  font-family: var(--font); transition: all .15s;
}
.af-pill:hover { border-color: var(--border-hi); color: var(--text); }
.af-pill.on { background: var(--primary-dim); border-color: var(--primary); color: var(--text); font-weight: 600; }
.af-year-row { display: flex; gap: 6px; align-items: center; }
.af-year-inp {
  width: 62px; background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--r); padding: 3px 6px;
  color: var(--text); font-size: 11px; outline: none;
}
.af-year-inp:focus { border-color: var(--primary); }
.af-tag-list { display: flex; flex-wrap: wrap; gap: 3px; max-height: 70px; overflow-y: auto; }
.af-clear { margin-left: auto; background: none; border: none; color: var(--muted);
  cursor: pointer; font-size: 11px; }
.af-clear:hover { color: var(--error); }
.filter-toggle-btn {
  padding: 2px 8px; border-radius: var(--r); font-size: 11px;
  cursor: pointer; border: 1px solid var(--border);
  background: transparent; color: var(--muted);
  font-family: var(--font); transition: all .15s; flex-shrink: 0;
}
.filter-toggle-btn.active { border-color: var(--primary); color: var(--primary); }

/* ── Stats modal ── */
.stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.stats-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r); padding: 12px 14px;
}
.stats-card h4 { font-size: 11px; color: var(--muted); text-transform: uppercase;
  letter-spacing: .5px; margin-bottom: 10px; }
.stats-num { font-size: 28px; font-weight: 700; color: var(--text); line-height: 1; }
.stats-sub { font-size: 11px; color: var(--muted); margin-top: 3px; }
.bar-chart { display: flex; flex-direction: column; gap: 4px; }
.bc-row { display: flex; align-items: center; gap: 6px; }
.bc-label { width: 100px; font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex-shrink: 0; text-align: right; }
.bc-bar-wrap { flex: 1; height: 12px; background: var(--border); border-radius: 2px; overflow: hidden; }
.bc-bar { height: 100%; border-radius: 2px; background: var(--primary); transition: width .3s; }
.bc-val { font-size: 10px; color: var(--muted); width: 28px; text-align: right; flex-shrink: 0; }
.year-chart { display: flex; align-items: flex-end; gap: 2px; height: 60px; }
.yc-col { flex: 1; background: var(--primary); border-radius: 2px 2px 0 0;
  min-width: 3px; transition: background .15s; cursor: default; position: relative; }
.yc-col:hover { background: var(--success); }
.yc-col[title]:hover::after {
  content: attr(title); position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%);
  background: var(--panel); border: 1px solid var(--border-hi); padding: 2px 6px;
  font-size: 10px; color: var(--text); border-radius: 3px; white-space: nowrap; pointer-events: none; z-index: 10;
}
.status-donut { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.sd-item { display: flex; align-items: center; gap: 4px; font-size: 11px; }
.sd-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.stats-modal-box { max-height: 82vh; overflow-y: auto; }

/* ── Duplicates modal ── */
.dup-group {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r); padding: 10px 12px; margin-bottom: 10px;
}
.dup-reason { font-size: 11px; color: var(--warning); margin-bottom: 8px; font-weight: 600; }
.dup-ref-row {
  display: flex; gap: 8px; align-items: center;
  padding: 6px 0; border-top: 1px solid var(--border); font-size: 12px;
}
.dup-ref-row:first-of-type { border-top: none; padding-top: 0; }
.dup-ref-info { flex: 1; min-width: 0; }
.dup-ref-title { color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dup-ref-meta  { color: var(--muted); font-size: 11px; }
/* ── Fullscreen detail ── */
.detail.fullscreen {
  position: fixed !important; inset: 0; z-index: 7000;
  width: 100% !important; max-width: 100% !important;
  border-radius: 0; overflow-y: auto;
}
.detail.fullscreen #btn-detail-fullscreen { display: inline-block !important; }

/* ── Toast animation ── */
@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(12px); }
  to   { opacity: 1; transform: translateY(0); }
}

.dup-list { max-height: 60vh; overflow-y: auto; }

/* ── Saved searches / smart sidebar ── */
.coll-section-header {
  padding: 8px 12px 4px; font-size: 10px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .6px; font-weight: 600;
  border-top: 1px solid var(--border); margin-top: 4px;
}
.coll-save-search {
  padding: 6px 12px; cursor: pointer; border-top: 1px solid var(--border);
}
.coll-save-search:hover { background: var(--panel); }
.coll-item-inline-edit {
  flex: 1; background: var(--panel); border: 1px solid var(--primary);
  border-radius: 4px; padding: 1px 5px; font-size: 13px; color: var(--text);
  outline: none; font-family: var(--font); min-width: 0;
}

/* ── Kanban board ── */
.kanban-col {
  flex: 1; min-width: 260px; max-width: 340px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r); display: flex; flex-direction: column; overflow: hidden;
}
.kanban-col-header {
  padding: 10px 12px; font-size: 12px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .5px;
  display: flex; justify-content: space-between; align-items: center;
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.kanban-col-items {
  flex: 1; overflow-y: auto; padding: 8px; display: flex; flex-direction: column; gap: 6px;
}
.kanban-card {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 8px 10px; cursor: pointer;
  transition: border-color .15s, box-shadow .15s;
}
.kanban-card:hover { border-color: var(--border-hi); box-shadow: 0 1px 4px rgba(0,0,0,.12); }
.kanban-card-title { font-size: 12px; color: var(--text); line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.kanban-card-meta { font-size: 10px; color: var(--muted); margin-top: 4px; }
.kanban-move-btn {
  font-size: 11px; background: var(--panel); border: 1px solid var(--border);
  border-radius: 4px; padding: 2px 6px; cursor: pointer; color: var(--muted);
  font-family: var(--font); transition: border-color .1s, color .1s;
}
.kanban-move-btn:hover { border-color: var(--primary); color: var(--primary); }
</style>
</head>
<body>

<!-- ── Toast notification ── -->
<div id="toast-container" style="position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none"></div>

<!-- ── Hover preview tooltip ── -->
<div id="hover-preview" style="display:none;position:fixed;z-index:8000;max-width:320px;
  background:var(--surface);border:1px solid var(--border-hi);border-radius:8px;
  padding:12px 14px;font-size:12px;box-shadow:0 8px 24px rgba(0,0,0,.3);pointer-events:none">
  <div id="hp-title" style="font-weight:600;color:var(--text);margin-bottom:4px;line-height:1.3"></div>
  <div id="hp-meta" style="color:var(--muted);font-size:11px;margin-bottom:6px"></div>
  <div id="hp-abstract" style="color:var(--muted);font-size:11px;line-height:1.5;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden"></div>
</div>

<!-- ── Tag Management Modal ── -->
<div class="overlay" id="tags-modal">
  <div class="modal-box" style="width:560px;max-height:80vh;overflow:hidden;display:flex;flex-direction:column">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <h2 style="margin:0">🏷 Tag Manager</h2>
      <button class="close-btn" onclick="document.getElementById('tags-modal').classList.remove('open')">✕</button>
    </div>
    <div id="tags-manager-list" style="flex:1;overflow-y:auto"></div>
  </div>
</div>

<!-- ── Search History Dropdown ── -->
<div id="search-history-dd" style="display:none;position:absolute;z-index:5000;
  background:var(--surface);border:1px solid var(--border-hi);border-radius:var(--r);
  box-shadow:0 4px 16px rgba(0,0,0,.2);min-width:300px;max-width:500px;overflow:hidden"></div>

<!-- ── Header ── -->
<header>
  <div class="logo">🗂 zoterpile <span>Reference Manager</span></div>
  <div class="search-area">
    <input type="search" id="search" placeholder="🔍  Search by title, author, DOI…" autocomplete="off">
    <div class="filters">
      <button class="f-btn active" data-type="" data-oa="0">All</button>
      <button class="f-btn" data-type="journal-article" data-oa="0">Article</button>
      <button class="f-btn" data-type="preprint" data-oa="0">Preprint</button>
      <button class="f-btn" data-type="book" data-oa="0">Book</button>
      <button class="f-btn" data-type="" data-oa="1">OA</button>
    </div>
  </div>
  <div class="h-actions">
    <button class="btn btn-primary" id="btn-open-add">＋ Add</button>
    <button class="btn btn-ghost" id="btn-open-import" title="Import .bib or .ris file">⬆ Import</button>
    <div class="dd-wrap">
      <button class="btn btn-ghost" id="btn-export-toggle">Export ▾</button>
      <div class="dd-menu" id="dd-export">
        <div class="dd-item" data-fmt="bibtex">BibTeX (.bib)</div>
        <div class="dd-item" data-fmt="ris">RIS (.ris)</div>
        <div class="dd-item" data-fmt="markdown">Markdown (.md)</div>
        <div class="dd-item" data-fmt="csv">CSV (.csv)</div>
        <div class="dd-item" id="dd-export-coll" data-fmt="bibtex" style="display:none">📁 Export Collection (.bib)</div>
      </div>
    </div>
    <button class="btn btn-ghost" id="btn-duplicates" title="Find duplicate references">⚡ Dupes</button>
    <button class="btn btn-ghost" id="btn-stats" title="Library analytics">📊 Stats</button>
    <button class="btn btn-ghost" id="btn-tags-mgr" title="Manage tags">🏷 Tags</button>
    <button class="btn btn-ghost" id="btn-enrich-all" title="Re-enrich incomplete references">🔧 Enrich</button>
    <button class="btn btn-ghost" id="btn-theme-toggle" title="Toggle light/dark mode">☀</button>
    <button class="btn btn-ghost" id="btn-kbd-help" title="Keyboard shortcuts">?</button>
    <button class="btn btn-ghost" id="btn-settings" title="Settings">⚙</button>
  </div>
</header>

<!-- ── Main ── -->
<main>
  <!-- Collections sidebar -->
  <div class="coll-sidebar">
    <div class="coll-header">
      Collections
      <div style="display:flex;gap:4px;align-items:center">
        <button class="coll-new-btn" id="btn-kanban-toggle" title="Toggle kanban reading board" style="font-size:14px">⊞</button>
        <button class="coll-new-btn" id="btn-coll-new" title="New collection">＋</button>
      </div>
    </div>
    <div class="coll-list" id="coll-list">
      <div class="coll-item active" data-id="" onclick="selectCollection(null)">
        <span class="coll-item-name">📚 All References</span>
        <span class="coll-count" id="all-count"></span>
      </div>
    </div>
    <div class="coll-section-header">Smart Searches</div>
    <div class="coll-list" id="saved-search-list" style="max-height:180px"></div>
    <div class="coll-item coll-save-search" id="btn-save-search" onclick="saveCurrentSearch()">
      <span style="color:var(--muted);font-size:12px">＋ Save current search</span>
    </div>
  </div>

  <div style="display:flex;flex-direction:column;width:340px;flex-shrink:0;border-right:1px solid var(--border);">
    <div class="sort-bar">
      <label for="sort-sel">Sort:</label>
      <select class="sort-sel" id="sort-sel">
        <option value="date-desc">Recently Added</option>
        <option value="year-desc">Year (newest)</option>
        <option value="year-asc">Year (oldest)</option>
        <option value="title-asc">Title A–Z</option>
        <option value="citations-desc">Most Cited</option>
        <option value="completeness-desc">Completeness</option>
      </select>
      <button class="filter-toggle-btn" id="btn-adv-filter" title="Advanced filters">⚗ Filter</button>
    </div>
    <div class="adv-filter-panel" id="adv-filter-panel">
      <div class="af-section">
        <div class="af-label">Year range</div>
        <div class="af-year-row">
          <input class="af-year-inp" id="af-year-from" placeholder="From" type="number" min="1000" max="2099">
          <span style="color:var(--muted)">–</span>
          <input class="af-year-inp" id="af-year-to" placeholder="To" type="number" min="1000" max="2099">
        </div>
      </div>
      <div class="af-section">
        <div class="af-label">Reading status</div>
        <div class="af-row">
          <button class="af-pill" data-status="unread">○ Unread</button>
          <button class="af-pill" data-status="reading">◑ Reading</button>
          <button class="af-pill" data-status="read">● Read</button>
        </div>
      </div>
      <div class="af-section">
        <div class="af-label">Has PDF</div>
        <div class="af-row">
          <button class="af-pill" id="af-pdf">📄 PDF only</button>
        </div>
      </div>
      <div class="af-section" id="af-tags-section">
        <div class="af-label">Tags</div>
        <div class="af-tag-list" id="af-tag-list"></div>
      </div>
      <div style="display:flex;justify-content:flex-end">
        <button class="af-clear" id="af-clear-btn">✕ Clear filters</button>
      </div>
    </div>
    <div class="sel-toolbar" id="sel-toolbar">
      <span class="sel-count" id="sel-count"></span>
      <button class="sel-btn" onclick="batchTagPrompt()">🏷 Tag</button>
      <button class="sel-btn" onclick="batchStatusPrompt()">◑ Status</button>
      <button class="sel-btn" onclick="batchCollPrompt()">📁 Collection</button>
      <button class="sel-btn sel-btn-del" onclick="batchDelete()">🗑 Delete</button>
      <button class="sel-btn" onclick="exportSelected()">⬇ Export</button>
      <button class="sel-btn" style="margin-left:auto" onclick="clearSel()">✕ Clear</button>
    </div>
    <div class="ref-list" id="ref-list" style="flex:1;border-right:none">
      <div class="empty"><div class="spin"></div></div>
    </div>
  </div>
  <div class="detail" id="detail">
    <button id="btn-detail-fullscreen" title="Fullscreen detail view"
      onclick="toggleDetailFullscreen()"
      style="position:sticky;top:0;float:right;margin:0 0 -28px 8px;z-index:10;
             background:var(--panel);border:1px solid var(--border);border-radius:4px;
             padding:2px 7px;font-size:12px;color:var(--muted);cursor:pointer;
             display:none">⤢</button>
    <div class="detail-ph">Select a reference to see details</div>
  </div>
</main>

<!-- ── Status bar ── -->
<div class="statusbar" id="statusbar">Loading…</div>

<!-- ── Add Modal ── -->
<div class="overlay" id="add-modal">
  <div class="modal-box">
    <h2>Add Reference</h2>
    <p class="modal-hint">
      Paste a DOI, arXiv ID, URL, PMID, title, or raw BibTeX / RIS.<br>
      Separate multiple entries with newlines or semicolons. Press Ctrl+↵ to submit.
    </p>
    <textarea class="modal-ta" id="add-ta"
      placeholder="10.1038/nature12373&#10;arXiv:1706.03762&#10;https://…"></textarea>
    <div class="modal-status" id="add-st"></div>
    <div class="modal-foot">
      <button class="btn btn-ghost" id="btn-paste-clipboard" title="Paste from clipboard" style="margin-right:auto">📋 Paste</button>
      <button class="btn btn-ghost" id="btn-add-cancel">Cancel</button>
      <button class="btn btn-primary" id="btn-add-submit">Add &amp; Enrich</button>
    </div>
  </div>
</div>

<!-- ── Settings Modal ── -->
<div class="overlay" id="settings-modal">
  <div class="modal-box">
    <h2>⚙ Settings</h2>
    <p class="modal-hint">
      Connect to a remote zoterpile server. Leave blank to use the local server.<br>
      The API key is shown in the server console on startup.
    </p>
    <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Server URL</label>
    <input type="url" class="modal-ta" id="cfg-url" placeholder="http://localhost:7274"
           style="min-height:0;padding:8px 12px;resize:none;font-family:var(--mono);margin-bottom:12px">
    <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">API Key</label>
    <input type="password" class="modal-ta" id="cfg-key" placeholder="64-character hex key"
           style="min-height:0;padding:8px 12px;resize:none;font-family:var(--mono)">
    <div class="modal-status" id="cfg-st"></div>
    <div class="modal-foot">
      <button class="btn btn-ghost" id="btn-cfg-cancel">Cancel</button>
      <button class="btn btn-ghost" id="btn-cfg-test">Test connection</button>
      <button class="btn btn-primary" id="btn-cfg-save">Save</button>
    </div>
  </div>
</div>

<!-- ── Kanban Reading Board ── -->
<div class="overlay" id="kanban-modal">
  <div class="modal-box" style="width:min(95vw,1100px);max-height:80vh;overflow:hidden;display:flex;flex-direction:column">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <h2 style="margin:0">⊞ Reading Board</h2>
      <button class="close-btn" onclick="document.getElementById('kanban-modal').classList.remove('open')">✕</button>
    </div>
    <div id="kanban-board" style="display:flex;gap:12px;overflow-x:auto;flex:1;padding-bottom:8px"></div>
  </div>
</div>

<!-- ── Similar Papers Modal ── -->
<div class="overlay" id="similar-modal">
  <div class="modal-box" style="width:620px">
    <h2>🔮 Similar Papers</h2>
    <p class="modal-hint" id="similar-hint">Finding semantically similar papers from your library…</p>
    <div class="similar-list" id="similar-list"></div>
    <div class="modal-foot">
      <button class="btn btn-ghost" onclick="document.getElementById('similar-modal').classList.remove('open')">Close</button>
    </div>
  </div>
</div>

<!-- ── Keyboard Help Modal ── -->
<div class="overlay" id="kbd-modal">
  <div class="modal-box" style="width:520px">
    <h2>⌨ Keyboard Shortcuts</h2>
    <br>
    <div class="kbd-grid">
      <div class="kbd-row"><kbd>/</kbd><span class="kbd-desc">Focus search</span></div>
      <div class="kbd-row"><kbd>a</kbd><span class="kbd-desc">Add reference</span></div>
      <div class="kbd-row"><kbd>i</kbd><span class="kbd-desc">Import file</span></div>
      <div class="kbd-row"><kbd>s</kbd><span class="kbd-desc">Library stats</span></div>
      <div class="kbd-row"><kbd>r</kbd><span class="kbd-desc">Refresh list</span></div>
      <div class="kbd-row"><kbd>b</kbd><span class="kbd-desc">Reading board (kanban)</span></div>
      <div class="kbd-row"><kbd>f</kbd><span class="kbd-desc">Toggle advanced filter</span></div>
      <div class="kbd-row"><kbd>?</kbd><span class="kbd-desc">This help</span></div>
      <div class="kbd-row"><kbd>j</kbd> / <kbd>↓</kbd><span class="kbd-desc">Next reference</span></div>
      <div class="kbd-row"><kbd>k</kbd> / <kbd>↑</kbd><span class="kbd-desc">Previous reference</span></div>
      <div class="kbd-row"><kbd>Del</kbd><span class="kbd-desc">Delete selected</span></div>
      <div class="kbd-row"><kbd>Ctrl</kbd>+<kbd>↵</kbd><span class="kbd-desc">Submit Add modal</span></div>
      <div class="kbd-row"><kbd>Esc</kbd><span class="kbd-desc">Close modals / cancel edit</span></div>
      <div class="kbd-row"><kbd>dblclick</kbd><span class="kbd-desc">Rename collection</span></div>
    </div>
    <div class="modal-foot" style="margin-top:18px">
      <button class="btn btn-ghost" onclick="document.getElementById('kbd-modal').classList.remove('open')">Close</button>
    </div>
  </div>
</div>

<!-- ── Stats Modal ── -->
<div class="overlay" id="stats-modal">
  <div class="modal-box stats-modal-box" style="width:700px">
    <h2>📊 Library Analytics</h2>
    <p class="modal-hint" id="stats-hint">Loading…</p>
    <div id="stats-content"></div>
    <div class="modal-foot">
      <button class="btn btn-ghost" onclick="document.getElementById('stats-modal').classList.remove('open')">Close</button>
    </div>
  </div>
</div>

<!-- ── Import Modal ── -->
<div class="overlay" id="import-modal">
  <div class="modal-box">
    <h2>⬆ Import References</h2>
    <p class="modal-hint">
      Upload a BibTeX (.bib) or RIS (.ris) file exported from Zotero, Mendeley, Paperpile, or any reference manager.
      Metadata will be automatically enriched from CrossRef and Semantic Scholar.
    </p>
    <input type="file" id="import-file" accept=".bib,.ris" style="display:none">
    <div id="import-drop" style="
        border: 2px dashed var(--border-hi); border-radius: var(--r);
        padding: 28px; text-align: center; cursor: pointer;
        color: var(--muted); font-size: 13px; transition: border-color .15s;
      " onclick="document.getElementById('import-file').click()"
         ondragover="event.preventDefault();this.style.borderColor='var(--primary)'"
         ondragleave="this.style.borderColor=''"
         ondrop="handleImportDrop(event)">
      <div style="font-size:28px;margin-bottom:8px">📂</div>
      Click to choose a file, or drag and drop here<br>
      <span id="import-filename" style="color:var(--primary);font-weight:500"></span>
    </div>
    <label style="display:flex;align-items:center;gap:7px;margin-top:10px;font-size:12px;color:var(--muted);cursor:pointer">
      <input type="checkbox" id="import-enrich" checked style="accent-color:var(--primary)">
      Enrich metadata from CrossRef / Semantic Scholar (recommended)
    </label>
    <div class="modal-status" id="import-st"></div>
    <div class="modal-foot">
      <button class="btn btn-ghost" id="btn-import-cancel">Cancel</button>
      <button class="btn btn-primary" id="btn-import-submit" disabled>Import</button>
    </div>
  </div>
</div>

<!-- ── Duplicates Modal ── -->
<div class="overlay" id="dupes-modal">
  <div class="modal-box" style="width:640px">
    <h2>⚡ Duplicate References</h2>
    <p class="modal-hint" id="dupes-hint">Scanning library for duplicates…</p>
    <div class="dup-list" id="dup-list"><div class="empty"><div class="spin"></div></div></div>
    <div class="modal-foot">
      <button class="btn btn-ghost" onclick="document.getElementById('dupes-modal').classList.remove('open')">Close</button>
    </div>
  </div>
</div>

<script>
'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let refs        = [];
let selId       = null;
let filter      = { type: '', oa: false };
let addTimer    = null;
let collections = [];
let activeColl  = null;   // null = all refs, int = collection id
let selectedIds = new Set(); // batch-selected ref IDs
let sortMode    = 'date-desc';  // current sort order
let allTags     = [];           // fetched from /api/tags for autocomplete

// ── DOM refs ───────────────────────────────────────────────────────────────
const $search   = document.getElementById('search');
const $list     = document.getElementById('ref-list');
const $detail   = document.getElementById('detail');
const $statusbar= document.getElementById('statusbar');
const $addModal = document.getElementById('add-modal');
const $addTa    = document.getElementById('add-ta');
const $addSt    = document.getElementById('add-st');
const $addBtn   = document.getElementById('btn-add-submit');
const $ddExport = document.getElementById('dd-export');
const $cfgModal = document.getElementById('settings-modal');
const $cfgUrl   = document.getElementById('cfg-url');
const $cfgKey   = document.getElementById('cfg-key');
const $cfgSt    = document.getElementById('cfg-st');

// ── Config / API key ───────────────────────────────────────────────────────
function getCfg() {
  return {
    url: localStorage.getItem('zp_url') || '',
    key: localStorage.getItem('zp_key') || '',
  };
}
function apiBase() {
  const u = getCfg().url.replace(/\/$/, '');
  return u || '';
}
function apiHeaders(extra) {
  const h = { 'Content-Type': 'application/json' };
  const k = getCfg().key;
  if (k) h['X-API-Key'] = k;
  return Object.assign(h, extra || {});
}

// Wrapper for fetch that handles 401 by opening settings modal
async function apiFetch(path, opts) {
  const url = apiBase() + path;
  const res = await fetch(url, Object.assign({}, opts, {
    headers: apiHeaders((opts || {}).headers),
  }));
  if (res.status === 401) {
    openSettings('⚠ Authentication failed — check your API key');
    throw new Error('Unauthorized');
  }
  return res;
}

// ── Init ───────────────────────────────────────────────────────────────────
(async () => {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
  loadCollections();
  loadRefs();
  loadAllTags();
})();

// ── Search (debounced) ─────────────────────────────────────────────────────
let searchT = null;
$search.addEventListener('input', () => {
  clearTimeout(searchT);
  searchT = setTimeout(loadRefs, 350);
});

// ── Filters ────────────────────────────────────────────────────────────────
document.querySelectorAll('.f-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.f-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    filter = { type: btn.dataset.type, oa: btn.dataset.oa === '1' };
    loadRefs();
  });
});

// ── Add modal ──────────────────────────────────────────────────────────────
document.getElementById('btn-open-add').addEventListener('click', openAdd);
document.getElementById('btn-add-cancel').addEventListener('click', closeAdd);
$addModal.addEventListener('click', e => { if (e.target === $addModal) closeAdd(); });
$addBtn.addEventListener('click', submitAdd);
$addTa.addEventListener('keydown', e => { if (e.key === 'Enter' && e.ctrlKey) submitAdd(); });

// ── Settings modal ─────────────────────────────────────────────────────────
document.getElementById('btn-settings').addEventListener('click', () => openSettings());
document.getElementById('btn-cfg-cancel').addEventListener('click', closeSettings);
$cfgModal.addEventListener('click', e => { if (e.target === $cfgModal) closeSettings(); });
document.getElementById('btn-cfg-save').addEventListener('click', saveSettings);
document.getElementById('btn-cfg-test').addEventListener('click', testConnection);

function openSettings(msg) {
  const cfg = getCfg();
  $cfgUrl.value = cfg.url;
  $cfgKey.value = cfg.key;
  $cfgSt.textContent = msg || '';
  $cfgSt.className = msg ? 'modal-status s-err' : 'modal-status';
  $cfgModal.classList.add('open');
}
function closeSettings() { $cfgModal.classList.remove('open'); }
function saveSettings() {
  localStorage.setItem('zp_url', $cfgUrl.value.trim());
  localStorage.setItem('zp_key', $cfgKey.value.trim());
  closeSettings();
  loadRefs();
}
async function testConnection() {
  $cfgSt.className = 'modal-status s-run';
  $cfgSt.innerHTML = '<span class="spin"></span>Testing…';
  const base = ($cfgUrl.value.trim()).replace(/\/$/, '');
  const key  = $cfgKey.value.trim();
  try {
    const res = await fetch((base || '') + '/api/auth/check', {
      headers: key ? { 'X-API-Key': key } : {},
    });
    if (res.ok) {
      $cfgSt.className = 'modal-status s-ok';
      $cfgSt.textContent = '✓ Connected successfully';
    } else {
      $cfgSt.className = 'modal-status s-err';
      $cfgSt.textContent = `✗ Server returned ${res.status}`;
    }
  } catch(e) {
    $cfgSt.className = 'modal-status s-err';
    $cfgSt.textContent = '✗ Could not reach server';
  }
}

// ── Export dropdown ────────────────────────────────────────────────────────
document.getElementById('btn-export-toggle').addEventListener('click', e => {
  e.stopPropagation();
  $ddExport.classList.toggle('open');
  // Show/hide "Export Collection" item based on active collection
  const collItem = document.getElementById('dd-export-coll');
  if (collItem) collItem.style.display = activeColl != null ? '' : 'none';
});
document.addEventListener('click', () => $ddExport.classList.remove('open'));
$ddExport.addEventListener('click', e => e.stopPropagation());
document.querySelectorAll('.dd-item').forEach(el => {
  el.addEventListener('click', () => {
    const key = getCfg().key;
    const fmt = el.dataset.fmt;
    let url;
    if (fmt === 'csv') {
      url = apiBase() + '/api/export/csv';
      if (activeColl != null) url += `?collection_id=${activeColl}`;
    } else if (el.id === 'dd-export-coll' && activeColl != null) {
      url = apiBase() + `/api/export?fmt=${fmt}&collection_id=${activeColl}`;
    } else {
      url = apiBase() + `/api/export?fmt=${fmt}`;
    }
    const sep = url.includes('?') ? '&' : '?';
    window.location.href = key ? url + `${sep}api_key=${encodeURIComponent(key)}` : url;
    $ddExport.classList.remove('open');
  });
});

// ── Sort ────────────────────────────────────────────────────────────────────
document.getElementById('sort-sel').addEventListener('change', e => {
  sortMode = e.target.value;
  applySort();
  renderList();
});

function applySort() {
  refs.sort((a, b) => {
    switch (sortMode) {
      case 'year-desc': return (b.year||0) - (a.year||0);
      case 'year-asc':  return (a.year||0) - (b.year||0);
      case 'title-asc': return (a.title||'').localeCompare(b.title||'');
      case 'citations-desc': return (b.citation_count||0) - (a.citation_count||0);
      case 'completeness-desc': return (b.completeness||0) - (a.completeness||0);
      default: return 0; // date-desc: server order preserved
    }
  });
}

// ── Keyboard shortcuts ─────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  const escaping = e.key === 'Escape';
  if (escaping) {
    closeAdd();
    closeSettings();
    $ddExport.classList.remove('open');
    ['dupes-modal','import-modal','similar-modal','stats-modal','kbd-modal','kanban-modal','tags-modal'].forEach(id => {
      document.getElementById(id)?.classList.remove('open');
    });
    if (_detailFullscreen) toggleDetailFullscreen();
    cancelEdit(selId);  // cancel any active edit
    return;
  }
  if (isEditing()) return;
  if (e.key === 'a' || e.key === 'A') { openAdd(); return; }
  if (e.key === 'i' || e.key === 'I') { document.getElementById('btn-open-import').click(); return; }
  if (e.key === 's' || e.key === 'S') { document.getElementById('btn-stats').click(); return; }
  if (e.key === '/')  { e.preventDefault(); $search.focus(); return; }
  if (e.key === 'r' || e.key === 'R') { loadRefs(); return; }
  if (e.key === 'b' || e.key === 'B') { openKanban(); return; }
  if (e.key === 'f' || e.key === 'F') { document.getElementById('btn-adv-filter')?.click(); return; }
  // j/k navigation
  if (e.key === 'j' || e.key === 'ArrowDown') {
    e.preventDefault();
    const idx = refs.findIndex(r => r.id === selId);
    if (idx < refs.length - 1) selectRef(refs[idx + 1].id);
    else if (idx === -1 && refs.length) selectRef(refs[0].id);
    scrollSelIntoView();
    return;
  }
  if (e.key === 'k' || e.key === 'ArrowUp') {
    e.preventDefault();
    const idx = refs.findIndex(r => r.id === selId);
    if (idx > 0) selectRef(refs[idx - 1].id);
    scrollSelIntoView();
    return;
  }
  if ((e.key === 'Delete' || e.key === 'Backspace') && selId) {
    delRef(selId);
  }
});

function scrollSelIntoView() {
  requestAnimationFrame(() => {
    document.querySelector('.ref-card.active')?.scrollIntoView({ block: 'nearest' });
  });
}

function isEditing() {
  const t = document.activeElement?.tagName;
  return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT';
}

// ── Import modal ────────────────────────────────────────────────────────────
let _importFile = null;

document.getElementById('btn-open-import').addEventListener('click', () => {
  _importFile = null;
  document.getElementById('import-filename').textContent = '';
  document.getElementById('btn-import-submit').disabled = true;
  document.getElementById('import-st').textContent = '';
  document.getElementById('import-st').className = 'modal-status';
  document.getElementById('import-modal').classList.add('open');
});

document.getElementById('btn-import-cancel').addEventListener('click', () => {
  document.getElementById('import-modal').classList.remove('open');
});

document.getElementById('import-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('import-modal'))
    document.getElementById('import-modal').classList.remove('open');
});

document.getElementById('import-file').addEventListener('change', e => {
  const file = e.target.files?.[0];
  if (file) setImportFile(file);
});

function handleImportDrop(e) {
  e.preventDefault();
  document.getElementById('import-drop').style.borderColor = '';
  const file = e.dataTransfer?.files?.[0];
  if (file) setImportFile(file);
}

function setImportFile(file) {
  _importFile = file;
  document.getElementById('import-filename').textContent = file.name;
  document.getElementById('btn-import-submit').disabled = false;
}

document.getElementById('btn-import-submit').addEventListener('click', async () => {
  if (!_importFile) return;
  const btn    = document.getElementById('btn-import-submit');
  const st     = document.getElementById('import-st');
  const enrich = document.getElementById('import-enrich').checked;
  btn.disabled = true;
  st.className = 'modal-status s-run';
  st.innerHTML = '<span class="spin"></span>Uploading…';
  try {
    const fd = new FormData();
    fd.append('file', _importFile);
    fd.append('enrich', enrich ? 'true' : 'false');
    const r = await fetch(apiBase() + '/api/import', {
      method: 'POST',
      headers: getCfg().key ? { 'X-API-Key': getCfg().key } : {},
      body: fd,
    });
    if (r.status === 401) { openSettings('⚠ Authentication failed'); return; }
    const data = await r.json();
    if (data.error) { st.className = 'modal-status s-err'; st.textContent = '✗ ' + data.error; btn.disabled = false; return; }
    st.innerHTML = `<span class="spin"></span>Importing ${data.count} references${enrich ? ' (enriching…)' : ''}`;
    let importTimer = setInterval(async () => {
      const jr = await apiFetch(`/api/jobs/${data.job_id}`);
      const job = await jr.json();
      if (job.status === 'running') {
        st.innerHTML = `<span class="spin"></span>${esc(job.message)}`;
        return;
      }
      clearInterval(importTimer);
      if (job.status === 'done') {
        st.className = 'modal-status s-ok';
        st.textContent = `✓ ${job.message}`;
        setTimeout(async () => {
          document.getElementById('import-modal').classList.remove('open');
          await loadRefs();
        }, 1400);
      } else {
        st.className = 'modal-status s-err';
        st.textContent = '✗ ' + job.message;
        btn.disabled = false;
      }
    }, 800);
  } catch(e) {
    st.className = 'modal-status s-err';
    st.textContent = '✗ Network error';
    btn.disabled = false;
  }
});

// ── Duplicates modal ────────────────────────────────────────────────────────
document.getElementById('btn-duplicates').addEventListener('click', async () => {
  const modal = document.getElementById('dupes-modal');
  const hint  = document.getElementById('dupes-hint');
  const list  = document.getElementById('dup-list');
  hint.textContent = 'Scanning library for duplicates…';
  list.innerHTML   = '<div class="empty"><div class="spin"></div></div>';
  modal.classList.add('open');
  try {
    const r     = await apiFetch('/api/duplicates');
    const groups = await r.json();
    if (!groups.length) {
      hint.textContent = '✓ No duplicates found!';
      list.innerHTML = '<div class="empty"><p style="color:var(--success)">Your library is clean.</p></div>';
      return;
    }
    hint.textContent = `Found ${groups.length} duplicate group${groups.length > 1 ? 's' : ''} — click a reference to view it`;
    list.innerHTML = groups.map(g => `
      <div class="dup-group">
        <div class="dup-reason">⚠ ${esc(g.reason)}</div>
        ${g.refs.map(r => `
          <div class="dup-ref-row">
            <div class="dup-ref-info">
              <div class="dup-ref-title">${esc(r.title)}</div>
              <div class="dup-ref-meta">${esc(fmtAuth(r.authors))} · ${r.year||'?'} · completeness ${Math.round(r.completeness*100)}%</div>
            </div>
            <button class="btn btn-ghost btn-sm" onclick="simSelect('${r.id}');document.getElementById('dupes-modal').classList.remove('open')">View</button>
            <button class="btn btn-ghost btn-sm" style="color:var(--error)"
                    onclick="delRef('${r.id}').then(()=>document.getElementById('btn-duplicates').click())">Delete</button>
          </div>`).join('')}
      </div>`).join('');
  } catch(e) {
    hint.textContent = `Error: ${e.message}`;
    list.innerHTML = '';
  }
});

// ── Stats modal ──────────────────────────────────────────────────────────────
document.getElementById('btn-stats').addEventListener('click', async () => {
  const modal   = document.getElementById('stats-modal');
  const hint    = document.getElementById('stats-hint');
  const content = document.getElementById('stats-content');
  hint.textContent = 'Loading analytics…';
  content.innerHTML = '';
  modal.classList.add('open');
  try {
    const r    = await apiFetch('/api/stats');
    const data = await r.json();
    if (data.error) { hint.textContent = 'Error: ' + data.error; return; }
    hint.textContent = `${data.count} references in your library`;
    content.innerHTML = renderStats(data);
  } catch(e) { hint.textContent = 'Error: ' + e.message; }
});

function renderStats(d) {
  const maxYear = d.by_year.length ? Math.max(...d.by_year.map(x=>x.count)) : 1;
  const yearCols = d.by_year.map(x => {
    const h = Math.round(x.count / maxYear * 60);
    return `<div class="yc-col" style="height:${h}px" title="${x.year}: ${x.count}"></div>`;
  }).join('');
  const yearLabels = d.by_year.length ? (() => {
    const years = d.by_year.map(x=>x.year);
    return `<div style="display:flex;justify-content:space-between;font-size:9px;color:var(--muted);margin-top:2px">
      <span>${years[0]}</span><span>${years[Math.floor(years.length/2)]||''}</span><span>${years[years.length-1]}</span></div>`;
  })() : '';

  const topN = (arr, key, max) => {
    const mx = arr.length ? arr[0][key] : 1;
    return arr.slice(0,max).map(x =>
      `<div class="bc-row">
        <div class="bc-label" title="${esc(x.name||x.type||'')}">${esc((x.name||x.type||'').slice(0,16))}</div>
        <div class="bc-bar-wrap"><div class="bc-bar" style="width:${Math.round(x[key]/mx*100)}%"></div></div>
        <div class="bc-val">${x[key]}</div>
      </div>`
    ).join('');
  };

  const st = d.status || {};
  const unread  = st.unread  || 0;
  const reading = st.reading || 0;
  const read    = st.read    || 0;
  const statusTotal = unread + reading + read || 1;

  const typeColors = {'journal-article':'#5b8af5','preprint':'#f0b429','book':'#3ecf8e',
    'book-chapter':'#a78bfa','conference-paper':'#fb923c','thesis':'#f472b6'};

  return `
  <div class="stats-grid">
    <div class="stats-card">
      <h4>Total References</h4>
      <div class="stats-num">${d.count.toLocaleString()}</div>
      <div class="stats-sub">avg ${Math.round(d.avg_completeness*100)}% metadata completeness</div>
    </div>
    <div class="stats-card">
      <h4>Open Access</h4>
      <div class="stats-num">${d.oa_count.toLocaleString()}</div>
      <div class="stats-sub">${d.oa_pct}% of library</div>
    </div>
    <div class="stats-card" style="grid-column:1/-1">
      <h4>Publications by Year</h4>
      <div class="year-chart">${yearCols}</div>
      ${yearLabels}
    </div>
    <div class="stats-card">
      <h4>Reading Progress</h4>
      <div class="status-donut">
        <div class="sd-item"><div class="sd-dot" style="background:#ee88bb"></div>${unread} unread</div>
        <div class="sd-item"><div class="sd-dot" style="background:#88aaee"></div>${reading} reading</div>
        <div class="sd-item"><div class="sd-dot" style="background:#3ecf8e"></div>${read} read</div>
      </div>
      <div style="margin-top:8px;height:8px;border-radius:4px;overflow:hidden;background:var(--border);display:flex">
        <div style="width:${Math.round(read/statusTotal*100)}%;background:#3ecf8e"></div>
        <div style="width:${Math.round(reading/statusTotal*100)}%;background:#88aaee"></div>
        <div style="width:${Math.round(unread/statusTotal*100)}%;background:#ee88bb"></div>
      </div>
    </div>
    <div class="stats-card">
      <h4>Citations</h4>
      <div class="stats-num">${(d.citation_stats.total||0).toLocaleString()}</div>
      <div class="stats-sub">max ${(d.citation_stats.max||0).toLocaleString()} · median ${d.citation_stats.median||0}</div>
    </div>
    <div class="stats-card">
      <h4>By Type</h4>
      <div class="bar-chart">${topN(d.by_type, 'count', 6)}</div>
    </div>
    <div class="stats-card">
      <h4>Top Tags</h4>
      <div class="bar-chart">${topN(d.top_tags, 'count', 6)}</div>
    </div>
    <div class="stats-card">
      <h4>Top Journals</h4>
      <div class="bar-chart">${topN(d.top_journals, 'count', 6)}</div>
    </div>
    <div class="stats-card">
      <h4>Top Authors</h4>
      <div class="bar-chart">${topN(d.top_authors, 'count', 6)}</div>
    </div>
  </div>`;
}

// ── Keyboard help ─────────────────────────────────────────────────────────────
document.getElementById('btn-kbd-help').addEventListener('click', () => {
  document.getElementById('kbd-modal').classList.add('open');
});

// ── Edit-in-place ─────────────────────────────────────────────────────────────
const EDIT_LABELS = {
  title:    'Title',
  year:     'Year',
  journal:  'Journal / Venue',
  abstract: 'Abstract',
  pages:    'Pages',
  volume:   'Volume',
  issue:    'Issue',
};

function startEdit(refId, field) {
  const ref = refs.find(r => r.id === refId);
  if (!ref) return;

  const hostId = `ef-${field}-${refId}`;
  let host = document.getElementById(hostId);

  // For abstract/journal, the host may not exist if value is empty
  if (!host) {
    host = document.getElementById('detail');
  }

  const current = field === 'year' ? (ref.year || '')
    : field === 'title'    ? (ref.title || '')
    : field === 'journal'  ? (ref.journal || ref.container_title || '')
    : field === 'abstract' ? (ref.abstract || '')
    : '';

  const isMultiline = field === 'abstract';
  const inputHtml = isMultiline
    ? `<textarea class="edit-inp-area" id="edit-active-inp">${esc(current)}</textarea>`
    : `<input class="edit-inp" id="edit-active-inp" type="${field==='year'?'number':'text'}" value="${esc(current)}">`;

  const editHtml = `<div id="edit-active-wrap" style="width:100%;margin-bottom:8px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">${EDIT_LABELS[field] || field}</div>
    ${inputHtml}
    <div class="edit-save-row">
      <button class="edit-save-btn" onclick="commitEdit('${refId}','${field}')">✓ Save</button>
      <button class="edit-cancel-btn" onclick="cancelEdit('${refId}')">Cancel</button>
    </div>
  </div>`;

  // Replace the host element with our editor
  const wrapper = document.createElement('div');
  wrapper.innerHTML = editHtml;
  if (host && host.parentNode) {
    host.parentNode.replaceChild(wrapper.firstElementChild, host);
  }
  document.getElementById('edit-active-inp')?.focus();
}

async function commitEdit(refId, field) {
  const inp = document.getElementById('edit-active-inp');
  if (!inp) return;
  const value = inp.value.trim();
  const body  = { [field]: field === 'year' ? (value ? parseInt(value) : null) : value };
  try {
    const r = await apiFetch(`/api/refs/${refId}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json();
      alert('Save failed: ' + (err.error || r.status));
      return;
    }
    // Update local ref cache
    const ref = refs.find(r => r.id === refId);
    if (ref) {
      if (field === 'year')     ref.year     = body.year;
      if (field === 'title')    ref.title    = value;
      if (field === 'journal')  ref.journal  = value;
      if (field === 'abstract') ref.abstract = value;
      renderDetail(ref);
      renderList();
    }
  } catch(e) { alert('Save failed: ' + e.message); }
}

function cancelEdit(refId) {
  const ref = refs.find(r => r.id === refId);
  if (ref) renderDetail(ref);
}

// ── Export selected refs ───────────────────────────────────────────────────
function exportSelected() {
  if (!selectedIds.size) return;
  const key = getCfg().key;
  const ids = [...selectedIds].join(',');
  const url = apiBase() + `/api/export?fmt=bibtex&ref_ids=${ids}`;
  const sep = url.includes('?') ? '&' : '?';
  window.location.href = key ? url + `${sep}api_key=${encodeURIComponent(key)}` : url;
}

// ── Enrich incomplete ────────────────────────────────────────────────────────
document.getElementById('btn-enrich-all').addEventListener('click', async () => {
  const threshold = parseFloat(prompt('Enrich refs below completeness (0.0–1.0):', '0.5') || '0.5');
  if (isNaN(threshold)) return;
  $statusbar.innerHTML = '<span class="spin"></span>Starting enrichment job…';
  try {
    const r   = await apiFetch('/api/enrich-incomplete', {
      method: 'POST',
      body: JSON.stringify({ threshold, limit: 100 }),
    });
    const { job_id } = await r.json();
    const timer = setInterval(async () => {
      const jr  = await apiFetch(`/api/jobs/${job_id}`);
      const job = await jr.json();
      $statusbar.innerHTML = job.status === 'running'
        ? `<span class="spin"></span>${esc(job.message)}`
        : esc(job.message);
      if (job.status !== 'running') {
        clearInterval(timer);
        await loadRefs();
        setTimeout(() => renderStatus(), 2000);
      }
    }, 900);
  } catch(e) { renderStatus(); }
});

// ── Advanced filter panel ─────────────────────────────────────────────────────
const advFilter = { yearFrom: null, yearTo: null, statuses: new Set(), hasPdf: false, tags: new Set() };

document.getElementById('btn-adv-filter').addEventListener('click', () => {
  const panel = document.getElementById('adv-filter-panel');
  const btn   = document.getElementById('btn-adv-filter');
  const open  = panel.classList.toggle('open');
  btn.classList.toggle('active', open);
  if (open) renderAdvTagOptions();
});

document.getElementById('af-year-from').addEventListener('input', e => {
  advFilter.yearFrom = e.target.value ? parseInt(e.target.value) : null;
  applyAdvFilter();
});
document.getElementById('af-year-to').addEventListener('input', e => {
  advFilter.yearTo = e.target.value ? parseInt(e.target.value) : null;
  applyAdvFilter();
});

document.querySelectorAll('.af-pill[data-status]').forEach(btn => {
  btn.addEventListener('click', () => {
    const s = btn.dataset.status;
    if (advFilter.statuses.has(s)) { advFilter.statuses.delete(s); btn.classList.remove('on'); }
    else { advFilter.statuses.add(s); btn.classList.add('on'); }
    applyAdvFilter();
  });
});

document.getElementById('af-pdf').addEventListener('click', function() {
  advFilter.hasPdf = !advFilter.hasPdf;
  this.classList.toggle('on', advFilter.hasPdf);
  applyAdvFilter();
});

document.getElementById('af-clear-btn').addEventListener('click', () => {
  advFilter.yearFrom = null; advFilter.yearTo = null;
  advFilter.statuses.clear(); advFilter.hasPdf = false; advFilter.tags.clear();
  document.getElementById('af-year-from').value = '';
  document.getElementById('af-year-to').value   = '';
  document.querySelectorAll('.af-pill').forEach(b => b.classList.remove('on'));
  renderAdvTagOptions();
  applyAdvFilter();
});

function renderAdvTagOptions() {
  const container = document.getElementById('af-tag-list');
  if (!allTags.length) { container.innerHTML = '<span style="color:var(--muted);font-size:11px">No tags yet</span>'; return; }
  container.innerHTML = allTags.slice(0,30).map(t =>
    `<button class="af-pill${advFilter.tags.has(t.name) ? ' on' : ''}"
             onclick="toggleAdvTag('${esc(t.name)}')">${esc(t.name)}</button>`
  ).join('');
}

function toggleAdvTag(name) {
  if (advFilter.tags.has(name)) advFilter.tags.delete(name);
  else advFilter.tags.add(name);
  renderAdvTagOptions();
  applyAdvFilter();
}

let _fullRefs = null; // cache of unfiltered refs

function applyAdvFilter() {
  const hasActiveFilter =
    advFilter.yearFrom || advFilter.yearTo ||
    advFilter.statuses.size || advFilter.hasPdf || advFilter.tags.size;

  // Update the filter button indicator
  document.getElementById('btn-adv-filter').classList.toggle('active', hasActiveFilter);

  if (!hasActiveFilter) {
    if (_fullRefs) { refs = _fullRefs; _fullRefs = null; }
    applySort(); renderList(); renderStatus();
    return;
  }
  if (!_fullRefs) _fullRefs = [...refs];
  refs = _fullRefs.filter(r => {
    if (advFilter.yearFrom && (r.year||0) < advFilter.yearFrom) return false;
    if (advFilter.yearTo   && (r.year||0) > advFilter.yearTo)   return false;
    if (advFilter.statuses.size && !advFilter.statuses.has(r.status||'unread')) return false;
    if (advFilter.hasPdf && !r.has_pdf) return false;
    if (advFilter.tags.size) {
      const rTags = new Set(r.tags);
      for (const t of advFilter.tags) if (!rTags.has(t)) return false;
    }
    return true;
  });
  applySort();
  renderList();
  renderStatus();
}

// ── Tag color management ─────────────────────────────────────────────────────
let _tagColorMap = {};

async function loadAllTags() {
  try {
    const r = await apiFetch('/api/tags');
    allTags = await r.json();
    // Build color map
    _tagColorMap = {};
    for (const t of allTags) {
      if (t.color && t.color !== '#6366f1') _tagColorMap[t.name] = t.color;
    }
  } catch(e) {}
}

async function setTagColor(tagName, color) {
  try {
    await apiFetch(`/api/tags/${encodeURIComponent(tagName)}`, {
      method: 'PATCH',
      body: JSON.stringify({ color }),
    });
    _tagColorMap[tagName] = color;
    // Re-render current detail if it has this tag
    const ref = refs.find(r => r.id === selId);
    if (ref?.tags.includes(tagName)) renderDetail(ref);
  } catch(e) {}
}

function tagAcInput(refId) {
  const inp   = document.getElementById(`ti-${refId}`);
  const drop  = document.getElementById(`tag-ac-${refId}`);
  const val   = inp.value.trim().toLowerCase();
  if (!val) { drop.classList.remove('open'); return; }
  const ref   = refs.find(r => r.id === refId);
  const existing = new Set(ref?.tags || []);
  const matches = allTags.filter(t => t.name.includes(val) && !existing.has(t.name)).slice(0, 8);
  if (!matches.length) { drop.classList.remove('open'); return; }
  drop.innerHTML = matches.map((t, i) =>
    `<div class="tag-ac-item" data-tag="${esc(t.name)}"
          onmousedown="tagAcPick('${refId}','${esc(t.name)}')">${esc(t.name)}<span>${t.count}</span></div>`
  ).join('');
  drop.classList.add('open');
}

function tagAcHide(refId) {
  document.getElementById(`tag-ac-${refId}`)?.classList.remove('open');
}

function tagAcPick(refId, tag) {
  const inp = document.getElementById(`ti-${refId}`);
  inp.value = tag;
  tagAcHide(refId);
  addTagBtn(refId);
}

// ── Collections ────────────────────────────────────────────────────────────
async function loadCollections() {
  try {
    const r = await apiFetch('/api/collections');
    collections = await r.json();
    if (!Array.isArray(collections)) collections = [];
    renderCollections();
  } catch(e) { console.error(e); }
}

function renderCollections() {
  const $cl = document.getElementById('coll-list');
  const allCount = refs.length;
  document.getElementById('all-count').textContent = allCount || '';
  const rows = collections.map(c => {
    const act = activeColl === c.id ? ' active' : '';
    return `<div class="coll-item${act}" data-id="${c.id}" onclick="selectCollection(${c.id})"
        ondblclick="event.stopPropagation();startCollRename(${c.id},${JSON.stringify(c.name)},this)">
      <span class="coll-item-name">📁 ${esc(c.name)}</span>
      <span class="coll-count">${c.ref_count || ''}</span>
      <button class="coll-del" title="Delete collection"
        onclick="event.stopPropagation();deleteCollection(${c.id})">×</button>
    </div>`;
  }).join('');
  // Preserve "All References" item and append collections
  $cl.innerHTML = `<div class="coll-item${activeColl === null ? ' active' : ''}" data-id=""
      onclick="selectCollection(null)">
    <span class="coll-item-name">📚 All References</span>
    <span class="coll-count" id="all-count">${activeColl === null ? refs.length || '' : ''}</span>
  </div>` + rows;
}

function selectCollection(id) {
  activeColl = id;
  renderCollections();
  loadRefs();
}

document.getElementById('btn-coll-new').addEventListener('click', async () => {
  const name = prompt('New collection name:');
  if (!name?.trim()) return;
  try {
    await apiFetch('/api/collections', {
      method: 'POST',
      body: JSON.stringify({ name: name.trim() }),
    });
    await loadCollections();
  } catch(e) { console.error(e); }
});

async function deleteCollection(id) {
  const c = collections.find(x => x.id === id);
  if (!confirm(`Delete collection "${c?.name || id}"?`)) return;
  try {
    await apiFetch(`/api/collections/${id}`, { method: 'DELETE' });
    if (activeColl === id) { activeColl = null; }
    await loadCollections();
    await loadRefs();
  } catch(e) { console.error(e); }
}

// ── Load & render list ─────────────────────────────────────────────────────
async function loadRefs() {
  const q = $search.value.trim();
  const params = new URLSearchParams({ q, limit: 500 });
  if (filter.type)  params.set('type', filter.type);
  if (filter.oa)    params.set('oa', 'true');
  if (activeColl != null) params.set('collection_id', activeColl);
  try {
    const r = await apiFetch('/api/refs?' + params);
    refs = await r.json();
    if (!Array.isArray(refs)) { refs = []; }
    _fullRefs = null; // invalidate advanced filter cache
    applyAdvFilter(); // re-applies any active filters; calls applySort+renderList internally
    renderStatus();
    // Update "All References" count in sidebar
    if (activeColl === null) {
      const el = document.getElementById('all-count');
      if (el) el.textContent = refs.length || '';
    }
  } catch(e) { console.error(e); }
}

function renderList() {
  if (!refs.length) {
    $list.innerHTML = `<div class="empty">
      <div class="empty-icon">📚</div>
      <h3>No references yet</h3>
      <p>Click <strong>＋ Add</strong> to add your first reference.<br>
         Accepts DOI, arXiv, URL, title, BibTeX, RIS&hellip;</p>
    </div>`;
    return;
  }
  $list.innerHTML = refs.map(ref => {
    const d    = dotCls(ref.completeness);
    const auth = fmtAuth(ref.authors);
    const year = ref.year || '—';
    const tags = ref.tags.slice(0, 3).map(t => {
      const c = _tagColorMap[t];
      const s = c ? ` style="background:${c}22;color:${c}"` : '';
      return `<span class="tag"${s}>${esc(t)}</span>`;
    }).join('');
    const more = ref.tags.length > 3 ? `<span class="tag">+${ref.tags.length - 3}</span>` : '';
    const pdf  = ref.has_pdf ? `<span class="tag" title="PDF available" style="opacity:.7">📄</span>` : '';
    const isSel  = selectedIds.has(ref.id);
    const act  = (ref.id === selId ? ' active' : '') + (isSel ? ' selected' : '');
    return `<div class="ref-card${act}" data-id="${ref.id}" onclick="cardClick(event,'${ref.id}')">
      <input type="checkbox" class="ref-card-cb" ${isSel ? 'checked' : ''}
             onclick="event.stopPropagation();toggleSel('${ref.id}',this.checked)"
             title="Select for batch action">
      <div class="dot ${d}"></div>
      <div class="rc-body">
        <div class="rc-title">${esc(ref.title)}</div>
        <div class="rc-meta">${esc(auth)} · ${year}</div>
        <div class="rc-tags">${tags}${more}${pdf}</div>
      </div>
    </div>`;
  }).join('');
}

function cardClick(e, id) {
  // Checkbox click is handled separately; plain click selects the detail view
  if (e.target.type === 'checkbox') return;
  selectRef(id);
}

function selectRef(id) {
  selId = id;
  document.querySelectorAll('.ref-card').forEach(c =>
    c.classList.toggle('active', c.dataset.id === id));
  const ref = refs.find(r => r.id === id);
  if (ref) renderDetail(ref);
}

// ── Detail panel ───────────────────────────────────────────────────────────
function renderDetail(ref) {
  // Show fullscreen toggle button
  const $fsBtn = document.getElementById('btn-detail-fullscreen');
  if ($fsBtn) $fsBtn.style.display = 'inline-block';

  const authors = ref.authors.map(a =>
    a.given ? `${a.family}, ${a.given}` : a.family).join('; ') || '—';
  const year  = ref.year ? ` (${ref.year})` : '';
  const venue = ref.journal || '';

  const badges = [
    ref.doi      ? `<span class="badge b-doi">DOI: ${esc(ref.doi)}</span>` : '',
    ref.arxiv_id ? `<span class="badge b-arxiv">arXiv: ${esc(ref.arxiv_id)}</span>` : '',
    ref.open_access ? `<span class="badge b-oa">🔓 Open Access</span>` : '',
    ref.ref_type && ref.ref_type !== 'unknown'
      ? `<span class="badge b-type">${esc(typeLabel(ref.ref_type))}</span>` : '',
    ref.cite_key
      ? `<span class="badge b-citekey" id="ck-${ref.id}" title="Click to copy cite key"
             onclick="copyCiteKey('${ref.id}','${esc(ref.cite_key)}')">@${esc(ref.cite_key)}</span>` : '',
  ].filter(Boolean).join('');

  const tagsHtml = ref.tags.map(t => {
    const color = _tagColorMap[t];
    const style = color ? `style="background:${color}22;color:${color};border-color:${color}44"` : '';
    return `<span class="tag-edit" ${style}>
      ${esc(t)}
      <input type="color" class="tag-color-inp" title="Set tag color"
             value="${color || '#6366f1'}"
             onchange="setTagColor('${esc(t)}',this.value)"
             style="width:10px;height:10px;padding:0;border:none;background:none;cursor:pointer;opacity:.5;flex-shrink:0">
      <button class="tag-x" onclick="rmTag('${ref.id}','${esc(t)}')">×</button>
    </span>`;
  }).join('');

  const statusVal = ref.status || 'unread';
  const statuses = ['unread', 'reading', 'read'];
  const statusHtml = statuses.map(s => {
    const act = statusVal === s ? ` active-${s}` : '';
    return `<button class="status-btn${act}" onclick="setStatus('${ref.id}','${s}')">${
      s === 'unread' ? '○ Unread' : s === 'reading' ? '◑ Reading' : '● Read'
    }</button>`;
  }).join('');

  const pct = Math.round(ref.completeness * 100);
  const bc  = ref.completeness >= .8 ? 'bar-g' : ref.completeness >= .4 ? 'bar-y' : 'bar-r';

  const openUrl = ref.oa_url || ref.url ||
    (ref.doi ? `https://doi.org/${encodeURIComponent(ref.doi)}` : null);

  // Collections chips
  const collsHtml = (ref.collections || []).map(c =>
    `<span class="coll-chip">📁 ${esc(c.name)}<button class="coll-chip-x"
       title="Remove from collection"
       onclick="removeFromColl('${ref.id}',${c.id})">×</button></span>`
  ).join('');
  const collOptions = collections
    .filter(c => !(ref.collections || []).find(rc => rc.id === c.id))
    .map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');

  $detail.innerHTML = `
    <div class="edit-field-wrap">
      <div class="d-title" id="ef-title-${ref.id}">${esc(ref.title)}</div>
      <button class="edit-pencil" title="Edit title" onclick="startEdit('${ref.id}','title')">✏</button>
    </div>
    <div class="edit-field-wrap">
      <div class="d-authors" id="ef-authors-${ref.id}">${esc(authors)}${esc(year)}</div>
      <button class="edit-pencil" title="Edit year" onclick="startEdit('${ref.id}','year')">✏</button>
    </div>
    ${venue ? `<div class="edit-field-wrap">
      <div class="d-venue" id="ef-journal-${ref.id}">${esc(venue)}</div>
      <button class="edit-pencil" title="Edit journal" onclick="startEdit('${ref.id}','journal')">✏</button>
    </div>` : `<div style="margin-bottom:14px">
      <button class="btn btn-ghost btn-sm" onclick="startEdit('${ref.id}','journal')">+ Add journal/venue</button>
    </div>`}
    <div class="d-badges">${badges}</div>

    <div class="section-label">Reading Status</div>
    <div class="status-row">${statusHtml}</div>

    <div class="section-label">Tags</div>
    <div class="tags-row" id="tl-${ref.id}">${tagsHtml}</div>
    <div class="tag-add-row">
      <div class="tag-ac-wrap">
        <input class="tag-inp" id="ti-${ref.id}" placeholder="Add tag…"
               onkeydown="tagKey(event,'${ref.id}')"
               oninput="tagAcInput('${ref.id}')"
               onblur="setTimeout(()=>tagAcHide('${ref.id}'),200)"
               autocomplete="off">
        <div class="tag-ac-list" id="tag-ac-${ref.id}"></div>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="addTagBtn('${ref.id}')">Add</button>
    </div>

    <hr class="div">

    ${ref.abstract ? `
      <div class="section-label" style="display:flex;align-items:center;justify-content:space-between">
        Abstract
        <button class="edit-pencil" style="opacity:.7" title="Edit abstract" onclick="startEdit('${ref.id}','abstract')">✏</button>
      </div>
      <div class="abstract" id="ef-abstract-${ref.id}">${esc(ref.abstract)}</div>
      <hr class="div">
    ` : `<button class="btn btn-ghost btn-sm" style="margin-bottom:14px" onclick="startEdit('${ref.id}','abstract')">+ Add abstract</button><hr class="div">`}

    <div class="section-label">Notes</div>
    <textarea class="notes-ta" id="notes-${ref.id}"
      placeholder="Your notes, comments, key takeaways…"
      onblur="saveNotes('${ref.id}')">${esc(ref.notes || '')}</textarea>
    <div class="notes-saved" id="notes-st-${ref.id}"></div>

    <hr class="div">

    ${collections.length ? `
      <div class="section-label">Collections</div>
      <div id="coll-chips-${ref.id}">${collsHtml}</div>
      ${collOptions ? `<div class="coll-add-row">
        <select class="coll-select" id="coll-sel-${ref.id}">
          <option value="">Add to collection…</option>
          ${collOptions}
        </select>
        <button class="btn btn-ghost btn-sm"
          onclick="addToColl('${ref.id}')">Add</button>
      </div>` : ''}
      <hr class="div">
    ` : ''}

    <div class="section-label">Quality & Info</div>
    <div class="comp-row">
      <div class="bar"><div class="bar-fill ${bc}" style="width:${pct}%"></div></div>
      <span>${pct}% complete</span>
      ${ref.citation_count != null ? `<span>· ${ref.citation_count.toLocaleString()} citations</span>` : ''}
      ${ref.abstract ? `<span>· ~${Math.ceil(ref.abstract.split(/\s+/).length / 200)} min read</span>` : ''}
    </div>

    <hr class="div">

    <div class="section-label" style="margin-top:4px">Citation</div>
    <div class="cite-panel" id="cite-panel-${ref.id}">
      <div class="cite-format-row">
        <button class="cite-fmt-btn active" data-fmt="apa"  onclick="setCiteFmt('${ref.id}','apa',this)">APA</button>
        <button class="cite-fmt-btn"        data-fmt="mla"  onclick="setCiteFmt('${ref.id}','mla',this)">MLA</button>
        <button class="cite-fmt-btn"        data-fmt="chicago" onclick="setCiteFmt('${ref.id}','chicago',this)">Chicago</button>
      </div>
      <div class="cite-text" id="cite-text-${ref.id}">${esc(fmtCiteApa(ref))}</div>
      <div class="cite-copy-row">
        <button class="btn btn-ghost btn-sm" id="cite-copy-${ref.id}"
                onclick="copyCitation('${ref.id}')">📋 Copy</button>
      </div>
    </div>

    <hr class="div">

    <div class="d-actions">
      ${openUrl ? `<a class="btn btn-ghost btn-sm" href="${openUrl}" target="_blank" rel="noopener">🔗 Open URL</a>` : ''}
      ${ref.has_pdf ? `<a class="btn btn-ghost btn-sm"
          href="${apiBase()}/api/refs/${ref.id}/pdf${getCfg().key ? '?api_key=' + encodeURIComponent(getCfg().key) : ''}"
          target="_blank" rel="noopener">📄 PDF</a>` : ''}
      <button class="btn btn-ghost btn-sm" onclick="copyDeepLink('${ref.id}')" title="Copy shareable link to this reference">⛓ Share</button>
      <button class="btn btn-ghost btn-sm" onclick="showSimilar('${ref.id}')">🔮 Similar</button>
      <button class="btn btn-ghost btn-sm" onclick="reenrich('${ref.id}')">↻ Refresh</button>
      <button class="btn btn-ghost btn-sm"
              onclick="exportRef('${ref.id}')">⬇ Export</button>
      <button class="btn btn-ghost btn-sm" style="color:var(--error)"
              onclick="delRef('${ref.id}')">🗑 Delete</button>
    </div>`;
}

// ── Citation formatting ─────────────────────────────────────────────────────

function _citeAuthors(authors, fmt) {
  if (!authors?.length) return '';
  if (fmt === 'mla') {
    if (authors.length === 1) return authors[0].family + (authors[0].given ? ', ' + authors[0].given : '');
    if (authors.length === 2) return `${authors[0].family}, ${authors[0].given || ''}, and ${authors[1].given || ''} ${authors[1].family}`;
    return `${authors[0].family}, ${authors[0].given || ''}, et al`;
  }
  if (fmt === 'chicago') {
    if (authors.length === 1) return authors[0].family + (authors[0].given ? ', ' + authors[0].given : '');
    const first = authors[0].family + (authors[0].given ? ', ' + authors[0].given : '');
    const rest  = authors.slice(1).map(a => (a.given ? a.given + ' ' : '') + a.family);
    return authors.length <= 3 ? [first, ...rest].join(', ') : first + ', et al';
  }
  // APA
  const fmt_apa = a => a.family + (a.given ? ', ' + a.given.split(' ').map(p=>p[0]+'.').join(' ') : '');
  if (authors.length <= 7) return authors.map(fmt_apa).join(', ');
  return authors.slice(0,6).map(fmt_apa).join(', ') + ', ... ' + fmt_apa(authors[authors.length-1]);
}

function fmtCiteApa(ref) {
  const auth  = _citeAuthors(ref.authors, 'apa');
  const year  = ref.year ? `(${ref.year})` : '';
  const title = ref.title || '';
  const venue = ref.journal || '';
  const vol   = ref.volume  ? `, ${ref.volume}` : '';
  const iss   = ref.issue   ? `(${ref.issue})`  : '';
  const pgs   = ref.pages   ? `, ${ref.pages}`  : '';
  const doi   = ref.doi     ? ` https://doi.org/${ref.doi}` : '';
  return [auth, year, title + '.', venue ? (venue + vol + iss + pgs + '.' + doi) : doi].filter(Boolean).join(' ');
}

function fmtCiteMla(ref) {
  const auth  = _citeAuthors(ref.authors, 'mla');
  const title = ref.title ? `"${ref.title}."` : '';
  const venue = ref.journal ? `*${ref.journal}*` : '';
  const vol   = ref.volume ? `, vol. ${ref.volume}` : '';
  const iss   = ref.issue  ? `, no. ${ref.issue}`  : '';
  const year  = ref.year   ? ` (${ref.year})`      : '';
  const pgs   = ref.pages  ? `, pp. ${ref.pages}`  : '';
  const doi   = ref.doi    ? ` https://doi.org/${ref.doi}.` : '.';
  return [auth + '.', title, venue + vol + iss + year + pgs + doi].filter(Boolean).join(' ');
}

function fmtCiteChicago(ref) {
  const auth  = _citeAuthors(ref.authors, 'chicago');
  const title = ref.title ? `"${ref.title}."` : '';
  const venue = ref.journal ? `*${ref.journal}*` : '';
  const vol   = ref.volume ? ` ${ref.volume}` : '';
  const iss   = ref.issue  ? `, no. ${ref.issue}` : '';
  const year  = ref.year   ? ` (${ref.year})`     : '';
  const pgs   = ref.pages  ? `: ${ref.pages}`     : '';
  const doi   = ref.doi    ? `. https://doi.org/${ref.doi}` : '';
  return [auth + '.', title, venue + vol + iss + year + pgs + doi].filter(Boolean).join(' ');
}

let _citeRefId = null;
let _citeFmt   = 'apa';

function setCiteFmt(refId, fmt, btn) {
  _citeFmt  = fmt;
  _citeRefId = refId;
  document.querySelectorAll('.cite-fmt-btn').forEach(b => b.classList.toggle('active', b === btn));
  const ref  = refs.find(r => r.id === refId);
  if (!ref) return;
  const text = fmt === 'mla' ? fmtCiteMla(ref) : fmt === 'chicago' ? fmtCiteChicago(ref) : fmtCiteApa(ref);
  document.getElementById(`cite-text-${refId}`).textContent = text;
}

function copyCitation(refId) {
  const el = document.getElementById(`cite-text-${refId}`);
  const btn = document.getElementById(`cite-copy-${refId}`);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent).then(() => {
    const orig = btn.textContent;
    btn.textContent = '✓ Copied!';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  }).catch(() => {});
}

// ── Re-enrich ───────────────────────────────────────────────────────────────
async function reenrich(refId) {
  const ref = refs.find(r => r.id === refId);
  if (!ref) return;
  const identifier = ref.doi || ref.arxiv_id || ref.url || ref.title;
  if (!identifier) return;
  $statusbar.innerHTML = '<span class="spin"></span>Re-enriching metadata…';
  try {
    const r  = await apiFetch('/api/refs', { method: 'POST', body: JSON.stringify({ text: identifier }) });
    const { job_id } = await r.json();
    const timer = setInterval(async () => {
      const jr  = await apiFetch(`/api/jobs/${job_id}`);
      const job = await jr.json();
      if (job.status !== 'running') {
        clearInterval(timer);
        $statusbar.textContent = job.status === 'done' ? `✓ ${job.message}` : `✗ ${job.message}`;
        await loadRefs();
        selectRef(refId);
        setTimeout(() => renderStatus(), 2000);
      }
    }, 800);
  } catch(e) { renderStatus(); }
}

// ── Single-ref export ────────────────────────────────────────────────────────
function exportRef(refId) {
  const key = getCfg().key;
  const url = apiBase() + `/api/export?fmt=bibtex&ref_ids=${refId}`;
  window.location.href = key ? url + `&api_key=${encodeURIComponent(key)}` : url;
}

// ── Cite key copy ───────────────────────────────────────────────────────────
function copyCiteKey(refId, citeKey) {
  navigator.clipboard.writeText(citeKey).then(() => {
    const el = document.getElementById(`ck-${refId}`);
    if (!el) return;
    el.classList.add('copied');
    el.textContent = '✓ Copied!';
    setTimeout(() => {
      el.classList.remove('copied');
      el.textContent = '@' + citeKey;
    }, 1500);
  }).catch(() => {});
}

// ── Status toggle ───────────────────────────────────────────────────────────
async function setStatus(refId, status) {
  await apiFetch(`/api/refs/${refId}`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  });
  const ref = refs.find(r => r.id === refId);
  if (ref) { ref.status = status; renderDetail(ref); }
}

// ── Notes ───────────────────────────────────────────────────────────────────
let _notesTimer = {};
async function saveNotes(refId) {
  const ta = document.getElementById(`notes-${refId}`);
  const st = document.getElementById(`notes-st-${refId}`);
  if (!ta) return;
  const notes = ta.value;
  try {
    await apiFetch(`/api/refs/${refId}`, {
      method: 'PATCH',
      body: JSON.stringify({ notes }),
    });
    const ref = refs.find(r => r.id === refId);
    if (ref) ref.notes = notes;
    if (st) { st.textContent = 'Saved'; setTimeout(() => { if (st) st.textContent = ''; }, 1500); }
  } catch(e) {
    if (st) { st.textContent = 'Save failed'; }
  }
}

// ── Collections in detail ───────────────────────────────────────────────────
async function addToColl(refId) {
  const sel = document.getElementById(`coll-sel-${refId}`);
  const collId = sel && parseInt(sel.value);
  if (!collId) return;
  await apiFetch(`/api/refs/${refId}/collections`, {
    method: 'POST',
    body: JSON.stringify({ collection_id: collId }),
  });
  // Refresh detail
  const r = await apiFetch(`/api/refs/${refId}`);
  const updated = await r.json();
  const ref = refs.find(x => x.id === refId);
  if (ref) {
    ref.collections = updated.collections;
    renderDetail(ref);
  }
  await loadCollections();
}

async function removeFromColl(refId, collId) {
  await apiFetch(`/api/refs/${refId}/collections/${collId}`, { method: 'DELETE' });
  const r = await apiFetch(`/api/refs/${refId}`);
  const updated = await r.json();
  const ref = refs.find(x => x.id === refId);
  if (ref) {
    ref.collections = updated.collections;
    renderDetail(ref);
  }
  await loadCollections();
}

// ── Batch selection ────────────────────────────────────────────────────────
function toggleSel(id, checked) {
  if (checked) selectedIds.add(id);
  else selectedIds.delete(id);
  updateSelToolbar();
  // Update card style without full re-render
  document.querySelectorAll(`.ref-card[data-id="${id}"]`).forEach(c =>
    c.classList.toggle('selected', checked));
}
function clearSel() {
  selectedIds.clear();
  updateSelToolbar();
  renderList();
}
function updateSelToolbar() {
  const n = selectedIds.size;
  const tb = document.getElementById('sel-toolbar');
  const ct = document.getElementById('sel-count');
  if (n > 0) {
    tb.classList.add('visible');
    ct.textContent = `${n} selected`;
  } else {
    tb.classList.remove('visible');
    ct.textContent = '';
  }
}
async function batchTagPrompt() {
  const tag = prompt('Tag to add to selected references:');
  if (!tag?.trim()) return;
  await apiFetch('/api/batch', {
    method: 'POST',
    body: JSON.stringify({ ref_ids: [...selectedIds], action: 'tag', tag: tag.trim().toLowerCase() }),
  });
  clearSel(); await loadRefs();
}
async function batchStatusPrompt() {
  const s = prompt('Set status for selected (unread / reading / read):');
  if (!['unread','reading','read'].includes(s)) return;
  await apiFetch('/api/batch', {
    method: 'POST',
    body: JSON.stringify({ ref_ids: [...selectedIds], action: 'set_status', status: s }),
  });
  clearSel(); await loadRefs();
}
async function batchCollPrompt() {
  if (!collections.length) { alert('No collections exist. Create one first.'); return; }
  const list = collections.map((c,i) => `${i+1}. ${c.name}`).join('\n');
  const n = parseInt(prompt(`Add to collection:\n${list}\nEnter number:`));
  if (!n || n < 1 || n > collections.length) return;
  const cid = collections[n-1].id;
  await apiFetch('/api/batch', {
    method: 'POST',
    body: JSON.stringify({ ref_ids: [...selectedIds], action: 'add_to_collection', collection_id: cid }),
  });
  clearSel(); await loadRefs(); await loadCollections();
}
async function batchDelete() {
  const n = selectedIds.size;
  if (!confirm(`Delete ${n} reference${n!==1?'s':''}? This cannot be undone.`)) return;
  await apiFetch('/api/batch', {
    method: 'POST',
    body: JSON.stringify({ ref_ids: [...selectedIds], action: 'delete' }),
  });
  if (selectedIds.has(selId)) {
    selId = null;
    $detail.innerHTML = '<div class="detail-ph">Select a reference to see details</div>';
  }
  clearSel(); await loadRefs();
}

// ── Similar papers ─────────────────────────────────────────────────────────
async function showSimilar(refId) {
  const modal = document.getElementById('similar-modal');
  const hint  = document.getElementById('similar-hint');
  const list  = document.getElementById('similar-list');
  hint.textContent = 'Searching semantic index…';
  list.innerHTML   = '<div style="text-align:center;padding:20px;color:var(--muted)"><span class="spin"></span></div>';
  modal.classList.add('open');
  try {
    const r = await apiFetch(`/api/refs/${refId}/similar?n=8`);
    const data = await r.json();
    if (data.error) {
      if (data.not_indexed) {
        hint.textContent = 'Semantic index not built. Run: zoterpile index-semantic';
      } else {
        hint.textContent = `Error: ${data.error}`;
      }
      list.innerHTML = '';
      return;
    }
    if (!data.length) {
      hint.textContent = 'No similar papers found — try running zoterpile index-semantic first.';
      list.innerHTML = '';
      return;
    }
    hint.textContent = `${data.length} similar papers from your library (sorted by similarity)`;
    list.innerHTML = data.map(ref => {
      const auth = fmtAuth(ref.authors);
      const pct  = Math.round(ref.similarity * 100);
      return `<div class="sim-row" onclick="simSelect('${ref.id}');document.getElementById('similar-modal').classList.remove('open')">
        <div class="sim-score">${pct}%</div>
        <div class="sim-body">
          <div class="sim-title">${esc(ref.title)}</div>
          <div class="sim-meta">${esc(auth)}${ref.year ? ' · ' + ref.year : ''}${ref.journal ? ' · ' + esc(ref.journal) : ''}</div>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    hint.textContent = `Error: ${e.message}`;
    list.innerHTML = '';
  }
}
function simSelect(id) {
  // Find in current list or reload detail
  const ref = refs.find(r => r.id === id);
  if (ref) {
    selectRef(id);
  } else {
    // Ref is in library but not in current filtered view — navigate to it
    activeColl = null;
    document.querySelectorAll('.f-btn').forEach(b => b.classList.toggle('active', !b.dataset.type && b.dataset.oa === '0'));
    filter = { type: '', oa: false };
    loadRefs().then(() => selectRef(id));
  }
}

// ── Tag management ─────────────────────────────────────────────────────────
function tagKey(e, id) { if (e.key === 'Enter') addTagBtn(id); }

async function addTagBtn(refId) {
  const inp = document.getElementById(`ti-${refId}`);
  const tag = inp.value.trim().toLowerCase();
  if (!tag) return;
  await apiFetch(`/api/refs/${refId}/tags`, {
    method: 'POST',
    body: JSON.stringify({ tag }),
  });
  inp.value = '';
  const ref = refs.find(r => r.id === refId);
  if (ref && !ref.tags.includes(tag)) { ref.tags.push(tag); renderDetail(ref); renderList(); }
}

async function rmTag(refId, tag) {
  await apiFetch(`/api/refs/${refId}/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' });
  const ref = refs.find(r => r.id === refId);
  if (ref) { ref.tags = ref.tags.filter(t => t !== tag); renderDetail(ref); renderList(); }
}

// ── Delete ─────────────────────────────────────────────────────────────────
// ── Add modal ──────────────────────────────────────────────────────────────
function openAdd() {
  $addModal.classList.add('open');
  $addTa.value = ''; $addSt.textContent = ''; $addSt.className = 'modal-status';
  $addBtn.disabled = false; $addTa.focus();
}
function closeAdd() {
  if (addTimer) { clearInterval(addTimer); addTimer = null; }
  $addModal.classList.remove('open');
}

async function submitAdd() {
  const text = $addTa.value.trim();
  if (!text) return;
  $addBtn.disabled = true;
  $addSt.className = 'modal-status s-run';
  $addSt.innerHTML = '<span class="spin"></span>Looking up…';
  try {
    const r = await apiFetch('/api/refs', {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
    const { job_id } = await r.json();
    addTimer = setInterval(() => pollJob(job_id), 800);
  } catch(e) {
    $addSt.className = 'modal-status s-err';
    $addSt.textContent = '✗ Network error';
    $addBtn.disabled = false;
  }
}

async function pollJob(jobId) {
  const r   = await apiFetch(`/api/jobs/${jobId}`);
  const job = await r.json();
  if (job.status === 'running') {
    $addSt.innerHTML = `<span class="spin"></span>${esc(job.message)}`;
    return;
  }
  clearInterval(addTimer); addTimer = null;
  if (job.status === 'done') {
    $addSt.className = 'modal-status s-ok';
    $addSt.textContent = `✓ ${job.message}`;
    setTimeout(async () => { closeAdd(); await loadRefs(); }, 1200);
  } else {
    $addSt.className = 'modal-status s-err';
    $addSt.textContent = `✗ ${job.message}`;
    $addBtn.disabled = false;
  }
}

// ── Status bar ─────────────────────────────────────────────────────────────
function renderStatus() {
  const n = refs.length;
  if (!n) { $statusbar.textContent = 'No references — [a] Add  [i] Import'; return; }
  const avg = refs.reduce((s, r) => s + r.completeness, 0) / n;
  const read = refs.filter(r => r.status === 'read').length;
  $statusbar.textContent =
    `${n} ref${n !== 1 ? 's' : ''}  ·  ${read} read  ·  avg ${Math.round(avg * 100)}% complete  ·  [a] Add  [i] Import  [s] Stats  [/] Search  [j/k] Navigate`;
}

// ── Utils ──────────────────────────────────────────────────────────────────
function dotCls(c) { return c >= .8 ? 'dot-g' : c >= .4 ? 'dot-y' : 'dot-r'; }
function fmtAuth(a) {
  if (!a?.length) return '—';
  const f = a[0].family || a[0].given || '?';
  return a.length === 1 ? f : `${f} et al.`;
}
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
                  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
const TYPE_LABELS = {
  'journal-article':'Article','preprint':'Preprint','book':'Book',
  'book-chapter':'Chapter','conference-paper':'Conference',
  'thesis':'Thesis','dataset':'Dataset','report':'Report','website':'Web',
};
function typeLabel(t) { return TYPE_LABELS[t] || t; }

// ── Deep-link / share ─────────────────────────────────────────────────────
function copyDeepLink(refId) {
  const url = `${location.origin}${location.pathname}#ref-${refId}`;
  navigator.clipboard.writeText(url).then(() => {
    const btn = document.querySelector(`button[onclick="copyDeepLink('${refId}')"]`);
    if (btn) { const orig = btn.textContent; btn.textContent = '✓ Copied!'; setTimeout(() => btn.textContent = orig, 1800); }
  }).catch(() => prompt('Copy this link:', url));
}

// Handle deep-link on load
function handleDeepLink() {
  const hash = location.hash;
  if (hash.startsWith('#ref-')) {
    const refId = hash.slice(5);
    // Wait for refs to load, then select
    const attempt = () => {
      if (refs.some(r => r.id === refId)) { selectRef(refId); scrollSelIntoView(); }
      else if (refs.length === 0) setTimeout(attempt, 300);
    };
    setTimeout(attempt, 500);
  }
}
window.addEventListener('load', handleDeepLink);

// ── Clipboard paste-to-add ─────────────────────────────────────────────────
document.getElementById('btn-paste-clipboard').addEventListener('click', async () => {
  try {
    const text = await navigator.clipboard.readText();
    const ta = document.getElementById('add-ta');
    if (ta) { ta.value = text.trim(); ta.focus(); }
  } catch(e) {
    const ta = document.getElementById('add-ta');
    if (ta) { ta.focus(); document.execCommand('paste'); }
  }
});

// Auto-detect DOI/arXiv paste in search box and open add modal
$search.addEventListener('paste', e => {
  const text = (e.clipboardData || window.clipboardData).getData('text').trim();
  const isDOI = /^10\.\d{4,}\/\S+/.test(text);
  const isArXiv = /^(arxiv:)?\d{4}\.\d{4,}(v\d+)?$/i.test(text);
  const isURL = /^https?:\/\//.test(text);
  if (isDOI || isArXiv || isURL) {
    e.preventDefault();
    openAdd();
    setTimeout(() => {
      const ta = document.getElementById('add-ta');
      if (ta) { ta.value = text; }
    }, 50);
  }
});

// ── Saved Searches ─────────────────────────────────────────────────────────
let savedSearches = [];

async function loadSavedSearches() {
  try {
    const r = await apiFetch('/api/saved-searches');
    savedSearches = await r.json();
    if (!Array.isArray(savedSearches)) savedSearches = [];
    renderSavedSearches();
  } catch(e) { console.error(e); }
}

function renderSavedSearches() {
  const $el = document.getElementById('saved-search-list');
  if (!$el) return;
  if (!savedSearches.length) {
    $el.innerHTML = '<div style="padding:6px 12px;font-size:11px;color:var(--muted)">No saved searches</div>';
    return;
  }
  $el.innerHTML = savedSearches.map(s => `
    <div class="coll-item" onclick="loadSavedSearch(${s.id})">
      <span class="coll-item-name" title="${esc(s.query || '')}">🔖 ${esc(s.name)}</span>
      <button class="coll-del" title="Delete saved search"
        onclick="event.stopPropagation();deleteSavedSearch(${s.id})">×</button>
    </div>`).join('');
}

async function saveCurrentSearch() {
  const q = $search.value.trim();
  const name = prompt('Save search as:');
  if (!name?.trim()) return;
  const filters = {
    type: advFilter.type,
    oa: advFilter.oa,
    status: advFilter.status,
    yearMin: advFilter.yearMin,
    yearMax: advFilter.yearMax,
    tags: [...advFilter.tags],
  };
  try {
    await apiFetch('/api/saved-searches', {
      method: 'POST',
      body: JSON.stringify({ name: name.trim(), query: q, filters }),
    });
    await loadSavedSearches();
  } catch(e) { alert('Failed to save search: ' + e.message); }
}

function loadSavedSearch(id) {
  const s = savedSearches.find(x => x.id === id);
  if (!s) return;
  $search.value = s.query || '';
  try {
    const f = JSON.parse(s.filters || '{}');
    advFilter.type   = f.type   || '';
    advFilter.oa     = f.oa     || false;
    advFilter.status = f.status || '';
    advFilter.yearMin = f.yearMin || '';
    advFilter.yearMax = f.yearMax || '';
    advFilter.tags   = new Set(f.tags || []);
  } catch(_) {}
  loadRefs();
}

async function deleteSavedSearch(id) {
  if (!confirm('Delete this saved search?')) return;
  await apiFetch(`/api/saved-searches/${id}`, { method: 'DELETE' });
  await loadSavedSearches();
}

// ── Collection inline rename ───────────────────────────────────────────────
function startCollRename(collId, currentName, itemEl) {
  const nameEl = itemEl.querySelector('.coll-item-name');
  if (!nameEl) return;
  const inp = document.createElement('input');
  inp.className = 'coll-item-inline-edit';
  inp.value = currentName;
  nameEl.replaceWith(inp);
  inp.focus(); inp.select();
  const finish = async () => {
    const newName = inp.value.trim();
    if (newName && newName !== currentName) {
      try {
        await apiFetch(`/api/collections/${collId}`, {
          method: 'PATCH',
          body: JSON.stringify({ name: newName }),
        });
        await loadCollections();
      } catch(e) { alert('Rename failed: ' + e.message); await loadCollections(); }
    } else {
      await loadCollections();
    }
  };
  inp.addEventListener('blur', finish);
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); inp.blur(); }
    if (e.key === 'Escape') { inp.value = currentName; inp.blur(); }
  });
}

// ── Kanban Reading Board ───────────────────────────────────────────────────
const KANBAN_COLS = [
  { id: 'unread',  label: 'To Read',   color: '#6b7280', next: 'reading' },
  { id: 'reading', label: 'Reading',   color: '#f59e0b', next: 'read'    },
  { id: 'read',    label: 'Done',      color: '#10b981', next: null       },
];

async function openKanban() {
  const modal = document.getElementById('kanban-modal');
  modal.classList.add('open');
  renderKanban();
}

async function renderKanban() {
  const board = document.getElementById('kanban-board');
  board.innerHTML = '<div class="spin" style="margin:40px auto"></div>';
  try {
    const r = await apiFetch('/api/refs?limit=2000');
    const all = await r.json();
    if (!Array.isArray(all)) { board.innerHTML = '<p style="color:var(--muted);padding:20px">Error loading references.</p>'; return; }
    const groups = { unread: [], reading: [], read: [] };
    all.forEach(ref => {
      const s = ref.status || 'unread';
      if (groups[s]) groups[s].push(ref); else groups.unread.push(ref);
    });
    board.innerHTML = KANBAN_COLS.map(col => {
      const items = groups[col.id];
      const cards = items.map(ref => {
        const auth = fmtAuth(ref.authors);
        const year = ref.year ? ` (${ref.year})` : '';
        const nextCol = col.next ? KANBAN_COLS.find(c => c.id === col.next) : null;
        const moveBtn = nextCol
          ? `<button class="kanban-move-btn" onclick="kanbanMove('${ref.id}','${nextCol.id}')">→ ${nextCol.label}</button>`
          : '';
        return `<div class="kanban-card" onclick="selectRef('${ref.id}');document.getElementById('kanban-modal').classList.remove('open')">
          <div class="kanban-card-title">${esc(ref.title || '(no title)')}</div>
          <div class="kanban-card-meta">${esc(auth)}${esc(year)}</div>
          ${moveBtn ? `<div style="margin-top:6px">${moveBtn}</div>` : ''}
        </div>`;
      }).join('') || '<div style="color:var(--muted);font-size:12px;text-align:center;padding:20px 0">Empty</div>';
      return `<div class="kanban-col">
        <div class="kanban-col-header" style="border-top:3px solid ${col.color}">
          <span style="color:${col.color}">${col.label}</span>
          <span style="color:var(--muted);font-weight:400">${items.length}</span>
        </div>
        <div class="kanban-col-items">${cards}</div>
      </div>`;
    }).join('');
  } catch(e) { board.innerHTML = `<p style="color:var(--error);padding:20px">${esc(e.message)}</p>`; }
}

async function kanbanMove(refId, newStatus) {
  await apiFetch(`/api/refs/${refId}`, {
    method: 'PATCH',
    body: JSON.stringify({ status: newStatus }),
  });
  renderKanban(); // refresh board in place
  // also update local refs cache
  const ref = refs.find(r => r.id === refId);
  if (ref) { ref.status = newStatus; renderList(); }
}

document.getElementById('btn-kanban-toggle').addEventListener('click', openKanban);

// ── Bulk tag dropdown ──────────────────────────────────────────────────────
async function batchTagPrompt() {
  const allTags = await apiFetch('/api/tags').then(r => r.json()).catch(() => []);
  const tagNames = Array.isArray(allTags) ? allTags.map(t => t.name) : [];
  const tag = prompt(`Tag to add to ${selectedIds.size} selected references:\nExisting tags: ${tagNames.slice(0,10).join(', ')}${tagNames.length>10?' …':''}`);
  if (!tag?.trim()) return;
  await apiFetch('/api/batch', {
    method: 'POST',
    body: JSON.stringify({ ref_ids: [...selectedIds], action: 'tag', tag: tag.trim().toLowerCase() }),
  });
  clearSel(); await loadRefs();
}

// ── Toast notifications ────────────────────────────────────────────────────
function showToast(msg, opts = {}) {
  const { duration = 3500, action = null, actionLabel = 'Undo' } = opts;
  const $c = document.getElementById('toast-container');
  const id = `toast-${Date.now()}`;
  const div = document.createElement('div');
  div.id = id;
  div.style.cssText = 'pointer-events:auto;background:var(--panel);border:1px solid var(--border-hi);' +
    'border-radius:8px;padding:10px 14px;font-size:13px;color:var(--text);display:flex;' +
    'align-items:center;gap:10px;box-shadow:0 4px 16px rgba(0,0,0,.3);' +
    'animation:fadeInUp .2s ease;max-width:360px';
  const btnHtml = action
    ? `<button style="background:var(--primary);border:none;color:#fff;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:12px;font-family:var(--font)" onclick="this.closest('#${id}')._undoAction?.()">${actionLabel}</button>`
    : '';
  div.innerHTML = `<span style="flex:1">${msg}</span>${btnHtml}
    <button onclick="this.parentElement.remove()" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;line-height:1;padding:0">✕</button>`;
  if (action) div._undoAction = action;
  $c.appendChild(div);
  setTimeout(() => div.remove(), duration);
  return div;
}

// ── Undo-safe delete ref ───────────────────────────────────────────────────
async function delRef(refId) {
  const ref = refs.find(r => r.id === refId);
  const title = ref?.title || 'reference';
  // Remove from local cache immediately
  refs = refs.filter(r => r.id !== refId);
  if (selId === refId) {
    selId = null;
    $detail.innerHTML = '<div class="detail-ph">Select a reference to see details</div>';
  }
  renderList(); renderStatus();
  let undone = false;
  showToast(`Deleted: "${title.length > 40 ? title.slice(0,37)+'…' : title}"`, {
    duration: 5000,
    actionLabel: 'Undo',
    action: async () => {
      undone = true;
      // Reload refs (server hasn't deleted yet — undo just cancels)
      await loadRefs();
      // We'll actually hit the server after 5s only if not undone
    },
  });
  // Delayed server delete — gives 5s for undo
  await new Promise(res => setTimeout(res, 5000));
  if (!undone) {
    await apiFetch(`/api/refs/${refId}`, { method: 'DELETE' });
  }
}

// ── Hover preview ─────────────────────────────────────────────────────────
const $hoverPreview = document.getElementById('hover-preview');
let _hoverTimer = null;
let _hoverActive = false;

document.getElementById('ref-list').addEventListener('mouseover', e => {
  const card = e.target.closest('.ref-card');
  if (!card) return;
  const id = card.dataset.id;
  const ref = refs.find(r => r.id === id);
  if (!ref?.abstract) return;
  clearTimeout(_hoverTimer);
  _hoverTimer = setTimeout(() => {
    const rect = card.getBoundingClientRect();
    document.getElementById('hp-title').textContent = ref.title || '';
    document.getElementById('hp-meta').textContent =
      [fmtAuth(ref.authors), ref.year, ref.journal || ref.container_title]
      .filter(Boolean).join(' · ');
    document.getElementById('hp-abstract').textContent = ref.abstract;
    $hoverPreview.style.display = 'block';
    // Position right of card, or left if near edge
    const x = rect.right + 12;
    const y = Math.min(rect.top, window.innerHeight - 200);
    $hoverPreview.style.left = (x + 320 < window.innerWidth ? x : rect.left - 332) + 'px';
    $hoverPreview.style.top  = y + 'px';
    _hoverActive = true;
  }, 600);
});

document.getElementById('ref-list').addEventListener('mouseout', e => {
  const card = e.target.closest('.ref-card');
  if (!card) return;
  clearTimeout(_hoverTimer);
  _hoverTimer = setTimeout(() => {
    $hoverPreview.style.display = 'none';
    _hoverActive = false;
  }, 100);
});

// ── Tag Manager ────────────────────────────────────────────────────────────
async function openTagManager() {
  document.getElementById('tags-modal').classList.add('open');
  await loadTagManager();
}

async function loadTagManager() {
  const el = document.getElementById('tags-manager-list');
  el.innerHTML = '<div class="spin" style="margin:30px auto"></div>';
  try {
    const r = await apiFetch('/api/tags');
    const tags = await r.json();
    if (!tags.length) { el.innerHTML = '<p style="color:var(--muted);padding:20px;text-align:center">No tags yet.</p>'; return; }
    el.innerHTML = tags.map(t => {
      const color = t.color || '#6366f1';
      const swatch = `<input type="color" value="${color}" title="Tag color"
        onchange="setTagColor('${esc(t.name)}',this.value);this.parentElement.querySelector('.tm-name').style.color=this.value"
        style="width:20px;height:20px;padding:0;border:none;border-radius:50%;cursor:pointer;background:none;flex-shrink:0">`;
      return `<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border)">
        ${swatch}
        <span class="tm-name" style="flex:1;color:${color};font-size:13px">${esc(t.name)}</span>
        <span style="font-size:11px;color:var(--muted);flex-shrink:0">${t.ref_count ?? ''} refs</span>
        <button class="btn btn-ghost btn-sm" style="color:var(--error);flex-shrink:0"
          onclick="deleteTag('${esc(t.name)}')">Delete</button>
      </div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<p style="color:var(--error);padding:20px">${esc(e.message)}</p>`; }
}

async function deleteTag(tagName) {
  if (!confirm(`Delete tag "${tagName}" from all references? This cannot be undone.`)) return;
  // Batch-remove from all refs using the batch API
  const tagRefs = refs.filter(r => r.tags.includes(tagName)).map(r => r.id);
  if (tagRefs.length) {
    await apiFetch('/api/batch', {
      method: 'POST',
      body: JSON.stringify({ ref_ids: tagRefs, action: 'untag', tag: tagName }),
    });
  }
  await loadRefs();
  await loadTagManager();
  delete _tagColorMap[tagName];
}

document.getElementById('btn-tags-mgr').addEventListener('click', openTagManager);

// ── Search history ─────────────────────────────────────────────────────────
let _searchHistory = JSON.parse(localStorage.getItem('zt-search-history') || '[]');
const $searchHistDD = document.getElementById('search-history-dd');

function saveSearchHistory(q) {
  if (!q) return;
  _searchHistory = [q, ..._searchHistory.filter(x => x !== q)].slice(0, 12);
  localStorage.setItem('zt-search-history', JSON.stringify(_searchHistory));
}

$search.addEventListener('focus', () => {
  if (!_searchHistory.length) return;
  renderSearchHistory();
  const rect = $search.getBoundingClientRect();
  $searchHistDD.style.top  = (rect.bottom + 4) + 'px';
  $searchHistDD.style.left = rect.left + 'px';
  $searchHistDD.style.width = rect.width + 'px';
  $searchHistDD.style.display = 'block';
});

$search.addEventListener('blur', () => setTimeout(() => { $searchHistDD.style.display = 'none'; }, 200));

function renderSearchHistory() {
  $searchHistDD.innerHTML = _searchHistory.map((q, i) =>
    `<div onclick="applyHistorySearch(${i})" style="padding:7px 12px;cursor:pointer;font-size:13px;
      display:flex;align-items:center;gap:8px;color:var(--text)"
      onmouseover="this.style.background='var(--panel)'" onmouseout="this.style.background=''">
      <span style="color:var(--muted);font-size:11px">🕐</span>
      <span style="flex:1">${esc(q)}</span>
      <button onclick="event.stopPropagation();removeHistoryItem(${i})" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:12px">✕</button>
    </div>`
  ).join('');
}

function applyHistorySearch(idx) {
  $search.value = _searchHistory[idx];
  $searchHistDD.style.display = 'none';
  loadRefs();
}

function removeHistoryItem(idx) {
  _searchHistory.splice(idx, 1);
  localStorage.setItem('zt-search-history', JSON.stringify(_searchHistory));
  renderSearchHistory();
  if (!_searchHistory.length) $searchHistDD.style.display = 'none';
}

// Hook into existing search handler to save history
const _origSearchInput = $search.oninput;
$search.addEventListener('change', () => {
  const q = $search.value.trim();
  if (q.length >= 3) saveSearchHistory(q);
});

// ── Theme toggle ───────────────────────────────────────────────────────────
(function initTheme() {
  const saved = localStorage.getItem('zt-theme') || 'dark';
  if (saved === 'light') document.documentElement.setAttribute('data-theme', 'light');
  const btn = document.getElementById('btn-theme-toggle');
  if (btn) btn.textContent = saved === 'light' ? '🌙' : '☀';
})();

document.getElementById('btn-theme-toggle').addEventListener('click', () => {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  const newTheme = isLight ? 'dark' : 'light';
  if (newTheme === 'light') document.documentElement.setAttribute('data-theme', 'light');
  else document.documentElement.removeAttribute('data-theme');
  localStorage.setItem('zt-theme', newTheme);
  document.getElementById('btn-theme-toggle').textContent = newTheme === 'light' ? '🌙' : '☀';
});

// ── Fullscreen detail ──────────────────────────────────────────────────────
let _detailFullscreen = false;
function toggleDetailFullscreen() {
  _detailFullscreen = !_detailFullscreen;
  $detail.classList.toggle('fullscreen', _detailFullscreen);
  const btn = document.getElementById('btn-detail-fullscreen');
  if (btn) btn.textContent = _detailFullscreen ? '⤡' : '⤢';
  if (!_detailFullscreen) $detail.scrollTop = 0;
}

// Close fullscreen on Escape
const _origEscHandler = null; // already handled in keydown listener above

// ── Quick type filter in list header ──────────────────────────────────────
// Add type filter chips above the list for quick access
function injectTypeChips() {
  const sortBar = document.querySelector('.sort-bar');
  if (!sortBar || document.getElementById('type-chips-row')) return;
  const types = [
    { v: '',                    l: 'All'      },
    { v: 'journal-article',     l: 'Articles' },
    { v: 'preprint',            l: 'Preprints'},
    { v: 'book',                l: 'Books'    },
    { v: 'conference-paper',    l: 'Conf.'    },
    { v: 'thesis',              l: 'Thesis'   },
  ];
  const row = document.createElement('div');
  row.id = 'type-chips-row';
  row.style.cssText = 'display:flex;gap:4px;padding:6px 12px;flex-wrap:wrap;border-bottom:1px solid var(--border);background:var(--surface)';
  row.innerHTML = types.map(t => `<button class="af-pill${t.v === (filter.type||'') ? ' on' : ''}"
    onclick="setTypeChip('${t.v}')">${t.l}</button>`).join('');
  sortBar.parentNode.insertBefore(row, sortBar);
}

function setTypeChip(val) {
  filter.type = val || undefined;
  document.querySelectorAll('#type-chips-row .af-pill').forEach(b => {
    const bv = b.getAttribute('onclick').match(/'([^']*)'/)?.[1] || '';
    b.classList.toggle('on', bv === (val || ''));
  });
  loadRefs();
}

// Run after initial render
setTimeout(injectTypeChips, 500);

// ── Init additions ─────────────────────────────────────────────────────────
(async () => {
  await loadSavedSearches();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(host: str = "127.0.0.1", port: int = 7274, debug: bool = False) -> None:
    """Start the web UI. Called by `zoterpile web` and `zoterpile-web` script."""
    api_key = _get_or_create_api_key()
    scheme = "http"
    print(
        f"\n  ✦ zoterpile web UI  →  {scheme}://{host}:{port}"
        f"\n  API Key             →  {api_key}"
        f"\n  (Set X-API-Key header or save key in the ⚙ Settings modal)"
        f"\n\n  Press Ctrl+C to stop.\n"
    )
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    run()
