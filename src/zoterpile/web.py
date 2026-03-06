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


def _ref_to_dict(ref, tags: List[str], ref_id: str, has_pdf: bool = False) -> dict:
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
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/refs")
def list_refs():
    from .db import RefDatabase
    q        = request.args.get("q", "").strip()
    ref_type = request.args.get("type") or None
    oa_only  = request.args.get("oa", "").lower() == "true"
    limit    = min(int(request.args.get("limit", 500)), 2000)
    try:
        with RefDatabase() as db:
            raw = db.search(q or "", ref_type=ref_type, oa_only=oa_only, limit=limit)
            ref_ids = [_ref_id(ref) for ref, _ in raw]
            tags_map = db.get_tags_batch(ref_ids)
            extras_map = db.get_extras_bulk(ref_ids)
            result = [
                _ref_to_dict(
                    ref, tags_map[_ref_id(ref)], _ref_id(ref),
                    has_pdf=bool(
                        extras_map.get(_ref_id(ref), {}).get("pdf_drive_id")
                        or extras_map.get(_ref_id(ref), {}).get("pdf_local")
                    ),
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
            tags = db.get_tags(ref_id)
            extra = db.get_extra(ref_id)
        has_pdf = bool(extra.get("pdf_drive_id") or extra.get("pdf_local"))
        return jsonify(_ref_to_dict(ref, tags, ref_id, has_pdf=has_pdf))
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
    fmt = request.args.get("fmt", "bibtex")
    try:
        from .db import RefDatabase
        from .exporters.bibtex   import to_bibtex_string
        from .exporters.ris      import to_ris_string
        from .exporters.markdown import to_markdown_string
        with RefDatabase() as db:
            refs = db.list_all(limit=10_000)
        if fmt == "ris":
            content, mime, fname = to_ris_string(refs), "application/x-research-info-systems", "refs.ris"
        elif fmt == "markdown":
            content, mime, fname = to_markdown_string(refs), "text/markdown", "refs.md"
        else:
            content, mime, fname = to_bibtex_string(refs), "application/x-bibtex", "refs.bib"
        return Response(content, mimetype=mime,
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats")
def stats():
    try:
        from .db import RefDatabase
        with RefDatabase() as db:
            refs = db.list_all(limit=10_000)
        n = len(refs)
        avg = sum(r.completeness for r in refs) / n if n else 0.0
        return jsonify({"count": n, "avg_completeness": round(avg, 3)})
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
</style>
</head>
<body>

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
    <div class="dd-wrap">
      <button class="btn btn-ghost" id="btn-export-toggle">Export ▾</button>
      <div class="dd-menu" id="dd-export">
        <div class="dd-item" data-fmt="bibtex">BibTeX (.bib)</div>
        <div class="dd-item" data-fmt="ris">RIS (.ris)</div>
        <div class="dd-item" data-fmt="markdown">Markdown (.md)</div>
      </div>
    </div>
    <button class="btn btn-ghost" id="btn-settings" title="Settings">⚙</button>
  </div>
</header>

<!-- ── Main ── -->
<main>
  <div class="ref-list" id="ref-list">
    <div class="empty"><div class="spin"></div></div>
  </div>
  <div class="detail" id="detail">
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

<script>
'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let refs      = [];
let selId     = null;
let filter    = { type: '', oa: false };
let addTimer  = null;

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
  // Register service worker for PWA
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
  // If no key stored, try to detect we're on the same origin (key not needed)
  const { key } = getCfg();
  if (!key) {
    // First load — open settings if we get a 401
    loadRefs();
  } else {
    loadRefs();
  }
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
});
document.addEventListener('click', () => $ddExport.classList.remove('open'));
$ddExport.addEventListener('click', e => e.stopPropagation());
document.querySelectorAll('.dd-item').forEach(el => {
  el.addEventListener('click', () => {
    const key = getCfg().key;
    const url = apiBase() + `/api/export?fmt=${el.dataset.fmt}`;
    // Exports need auth — append key as query param for direct download
    window.location.href = key ? url + `&api_key=${encodeURIComponent(key)}` : url;
    $ddExport.classList.remove('open');
  });
});

// ── Keyboard shortcuts ─────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeAdd(); closeSettings(); $ddExport.classList.remove('open'); }
  if (e.key === 'a' && !isEditing()) openAdd();
  if (e.key === '/' && !isEditing()) { e.preventDefault(); $search.focus(); }
});
function isEditing() {
  const t = document.activeElement?.tagName;
  return t === 'INPUT' || t === 'TEXTAREA';
}

// ── Load & render list ─────────────────────────────────────────────────────
async function loadRefs() {
  const q = $search.value.trim();
  const params = new URLSearchParams({ q, limit: 500 });
  if (filter.type) params.set('type', filter.type);
  if (filter.oa)   params.set('oa', 'true');
  try {
    const r = await apiFetch('/api/refs?' + params);
    refs = await r.json();
    if (!Array.isArray(refs)) { refs = []; }
    renderList();
    renderStatus();
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
    const tags = ref.tags.slice(0, 3).map(t => `<span class="tag">${esc(t)}</span>`).join('');
    const more = ref.tags.length > 3 ? `<span class="tag">+${ref.tags.length - 3}</span>` : '';
    const pdf  = ref.has_pdf ? `<span class="tag" title="PDF available" style="opacity:.7">📄</span>` : '';
    const act  = ref.id === selId ? ' active' : '';
    return `<div class="ref-card${act}" data-id="${ref.id}" onclick="selectRef('${ref.id}')">
      <div class="dot ${d}"></div>
      <div class="rc-body">
        <div class="rc-title">${esc(ref.title)}</div>
        <div class="rc-meta">${esc(auth)} · ${year}</div>
        <div class="rc-tags">${tags}${more}${pdf}</div>
      </div>
    </div>`;
  }).join('');
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
  ].filter(Boolean).join('');

  const tagsHtml = ref.tags.map(t =>
    `<span class="tag-edit">${esc(t)}<button class="tag-x"
       onclick="rmTag('${ref.id}','${esc(t)}')">×</button></span>`
  ).join('');

  const pct = Math.round(ref.completeness * 100);
  const bc  = ref.completeness >= .8 ? 'bar-g' : ref.completeness >= .4 ? 'bar-y' : 'bar-r';

  const openUrl = ref.oa_url || ref.url ||
    (ref.doi ? `https://doi.org/${encodeURIComponent(ref.doi)}` : null);

  $detail.innerHTML = `
    <div class="d-title">${esc(ref.title)}</div>
    <div class="d-authors">${esc(authors)}${esc(year)}</div>
    ${venue ? `<div class="d-venue">${esc(venue)}</div>` : ''}
    <div class="d-badges">${badges}</div>

    <div class="section-label">Tags</div>
    <div class="tags-row" id="tl-${ref.id}">${tagsHtml}</div>
    <div class="tag-add-row">
      <input class="tag-inp" id="ti-${ref.id}" placeholder="Add tag…"
             onkeydown="tagKey(event,'${ref.id}')">
      <button class="btn btn-ghost btn-sm" onclick="addTagBtn('${ref.id}')">Add</button>
    </div>

    <hr class="div">

    ${ref.abstract ? `
      <div class="section-label">Abstract</div>
      <div class="abstract">${esc(ref.abstract)}</div>
      <hr class="div">
    ` : ''}

    <div class="section-label">Quality</div>
    <div class="comp-row">
      <div class="bar"><div class="bar-fill ${bc}" style="width:${pct}%"></div></div>
      <span>${pct}% complete</span>
      ${ref.citation_count != null ? `<span>· ${ref.citation_count.toLocaleString()} citations</span>` : ''}
    </div>

    <hr class="div">

    <div class="d-actions">
      ${openUrl ? `<a class="btn btn-ghost btn-sm" href="${openUrl}" target="_blank" rel="noopener">🔗 Open URL</a>` : ''}
      ${ref.has_pdf ? `<a class="btn btn-ghost btn-sm"
          href="${apiBase()}/api/refs/${ref.id}/pdf${getCfg().key ? '?api_key=' + encodeURIComponent(getCfg().key) : ''}"
          target="_blank" rel="noopener">📄 PDF</a>` : ''}
      <button class="btn btn-ghost btn-sm" style="color:var(--error)"
              onclick="delRef('${ref.id}')">🗑 Delete</button>
    </div>`;
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
async function delRef(refId) {
  const ref = refs.find(r => r.id === refId);
  if (!confirm(`Delete "${ref?.title || 'this reference'}"?\nThis cannot be undone.`)) return;
  await apiFetch(`/api/refs/${refId}`, { method: 'DELETE' });
  selId = null;
  $detail.innerHTML = '<div class="detail-ph">Select a reference to see details</div>';
  await loadRefs();
}

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
  if (!n) { $statusbar.textContent = 'No references — press A to add one'; return; }
  const avg = refs.reduce((s, r) => s + r.completeness, 0) / n;
  $statusbar.textContent =
    `${n} reference${n !== 1 ? 's' : ''}  ·  avg completeness ${Math.round(avg * 100)}%  ·  [a] Add  [/] Search`;
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
