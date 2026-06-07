"""
Google Drive sync daemon.

Runs as a daemon thread while the app is open, continuously syncing:
  1. Database backups  — periodic SQLite backup API snapshots to Drive
  2. PDF uploads       — new local PDFs uploaded to structured Drive folders
  3. Local cleanup     — (streaming mode) delete local PDFs after Drive upload

Follows the same threading pattern as enrich_daemon.py.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mouseion.sync_daemon")

# Daemon state
_daemon_thread: Optional[threading.Thread] = None
_daemon_running = threading.Event()   # set = running
_daemon_stop    = threading.Event()   # set = stop
_daemon_lock    = threading.Lock()
_force_sync     = threading.Event()   # set = run a cycle immediately

# Stats (read by the API)
_stats_lock = threading.Lock()
_stats = {
    "last_db_backup": None,
    "last_pdf_sync": None,
    "pdfs_synced": 0,
    "pdfs_pending": 0,
    "pdfs_failed": 0,
    "last_error": None,
    "cycles": 0,
}


def get_stats() -> dict:
    with _stats_lock:
        return dict(_stats)


def _update_stats(**kwargs):
    with _stats_lock:
        _stats.update(kwargs)


def is_running() -> bool:
    return _daemon_running.is_set() and not _daemon_stop.is_set()


def start():
    """Start the sync daemon (idempotent)."""
    global _daemon_thread
    with _daemon_lock:
        if _daemon_thread and _daemon_thread.is_alive():
            _daemon_running.set()
            logger.info("Sync daemon resumed")
            return
        _daemon_stop.clear()
        _daemon_running.set()
        _daemon_thread = threading.Thread(
            target=_daemon_loop, daemon=True, name="sync-daemon"
        )
        _daemon_thread.start()
        logger.info("Sync daemon started")


def pause():
    _daemon_running.clear()
    logger.info("Sync daemon paused")


def resume():
    _daemon_running.set()
    logger.info("Sync daemon resumed")


def stop():
    _daemon_stop.set()
    _daemon_running.set()  # unblock if paused
    logger.info("Sync daemon stopping")


def trigger():
    """Force an immediate sync cycle."""
    _force_sync.set()


def _daemon_loop():
    """Main daemon loop."""
    from .config import get_config
    from .db import RefDatabase

    logger.info("Sync daemon loop started")

    db = RefDatabase()
    cfg = get_config()

    # Build Drive service once (reused across cycles)
    service = None
    folders = None

    while not _daemon_stop.is_set():
        _daemon_running.wait()
        if _daemon_stop.is_set():
            break

        # Wait for the configured interval or a forced trigger
        interval = cfg.google_drive_sync_interval
        _force_sync.wait(timeout=interval)
        _force_sync.clear()

        if _daemon_stop.is_set():
            break
        if not _daemon_running.is_set():
            continue

        # Lazy-init Drive service
        if service is None:
            try:
                from .integrations.google_drive import (
                    _build_service,
                    ensure_folder_structure,
                )
                service = _build_service()
                folders = ensure_folder_structure(service)
                logger.info("Sync daemon: Drive connected, folders ready")
            except Exception as e:
                logger.error("Sync daemon: Drive init failed: %s", e)
                _update_stats(last_error=f"Drive init: {e}")
                time.sleep(30)
                continue

        try:
            # Phase 1: DB backup
            _sync_db_backup(db, service, folders)

            # Phase 2: PDF upload
            _sync_pdfs(db, service, folders, cfg)

            # Phase 3: Local cleanup (streaming mode)
            if cfg.google_drive_pdf_streaming:
                _cleanup_local_pdfs(db, cfg)

            _update_stats(
                cycles=get_stats()["cycles"] + 1,
                last_error=None,
            )
        except Exception as e:
            logger.exception("Sync daemon cycle error")
            _update_stats(last_error=str(e))
            # Reset service on error so it reconnects next cycle
            service = None
            folders = None
            time.sleep(10)

    logger.info("Sync daemon stopped")


def _sync_db_backup(db, service, folders: dict):
    """Create a SQLite backup and upload to Drive."""
    from .config import get_config

    cfg = get_config()
    db_path = Path(cfg.db_path)

    if not db_path.exists():
        return

    # Use SQLite backup API for a consistent snapshot
    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".db", prefix="mouseion_backup_", delete=False
        )
        tmp_path = Path(tmp.name)
        tmp.close()

        # backup API: source -> dest
        src_conn = sqlite3.connect(str(db_path))
        dst_conn = sqlite3.connect(str(tmp_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        # Upload to Drive
        from .integrations.google_drive import upload_db_backup
        upload_db_backup(tmp_path, service)

        now = datetime.now(timezone.utc).isoformat()
        _update_stats(last_db_backup=now)
        db.set_setting("drive_last_backup_time", now)
        logger.info("DB backup uploaded to Drive")

    except Exception as e:
        logger.warning("DB backup failed: %s", e)
        raise
    finally:
        if tmp:
            try:
                Path(tmp.name).unlink(missing_ok=True)
            except Exception:
                pass


def _sync_pdfs(db, service, folders: dict, cfg):
    """Upload local PDFs that haven't been synced to Drive yet."""
    from .integrations.google_drive import upload_pdf_to_folder

    pdfs_folder = folders["pdfs"]

    # Find refs with local PDFs but no Drive ID
    try:
        with db._db() as conn:
            rows = conn.execute(
                """
                SELECT id, pdf_path, pdf_local
                FROM refs
                WHERE (pdf_path IS NOT NULL AND pdf_path != '')
                  AND (pdf_drive_id IS NULL OR pdf_drive_id = '')
                LIMIT 50
                """
            ).fetchall()
    except Exception as e:
        logger.warning("Failed to query pending PDFs: %s", e)
        _update_stats(pdfs_failed=get_stats()["pdfs_failed"] + 1)
        return

    _update_stats(pdfs_pending=len(rows))

    if not rows:
        return

    logger.info("Syncing %d PDFs to Drive", len(rows))

    synced = 0
    failed = 0
    for row in rows:
        if _daemon_stop.is_set() or not _daemon_running.is_set():
            break

        ref_id = row["id"]
        pdf_path = row["pdf_path"] or row["pdf_local"] or ""

        if not pdf_path:
            continue

        local = Path(pdf_path)
        if not local.is_absolute():
            local = Path(cfg.pdf_storage_path) / pdf_path

        if not local.exists():
            logger.debug("PDF not found locally: %s", local)
            continue

        try:
            # Load the full ref for metadata (year, authors, title)
            ref = db.get(ref_id)
            if not ref:
                continue

            drive_id = upload_pdf_to_folder(
                local, ref, service=service, pdfs_folder_id=pdfs_folder
            )

            # Update ref with Drive ID
            db.update_integration_ids(ref_id, pdf_drive_id=drive_id)
            synced += 1

        except Exception as e:
            logger.warning("Failed to sync PDF %s: %s", ref_id[:8], e)
            failed += 1

        # Small pause to avoid hammering the API
        time.sleep(0.5)

    now = datetime.now(timezone.utc).isoformat()
    _update_stats(
        last_pdf_sync=now,
        pdfs_synced=get_stats()["pdfs_synced"] + synced,
        pdfs_failed=get_stats()["pdfs_failed"] + failed,
    )
    db.set_setting("drive_last_pdf_sync", now)
    db.set_setting("drive_synced_count", str(get_stats()["pdfs_synced"]))

    if synced:
        logger.info("Synced %d PDFs to Drive (%d failed)", synced, failed)


def _cleanup_local_pdfs(db, cfg):
    """In streaming mode, delete local PDF files that are safely on Drive."""
    try:
        with db._db() as conn:
            rows = conn.execute(
                """
                SELECT id, pdf_path, pdf_local, pdf_drive_id
                FROM refs
                WHERE pdf_drive_id IS NOT NULL AND pdf_drive_id != ''
                  AND (pdf_path IS NOT NULL AND pdf_path != '')
                LIMIT 100
                """
            ).fetchall()
    except Exception:
        return

    removed = 0
    for row in rows:
        pdf_path = row["pdf_path"] or row["pdf_local"] or ""
        if not pdf_path:
            continue

        local = Path(pdf_path)
        if not local.is_absolute():
            local = Path(cfg.pdf_storage_path) / pdf_path

        # Don't delete cache files
        if ".drive_cache" in str(local):
            continue

        if local.exists():
            try:
                local.unlink()
                removed += 1
            except Exception:
                pass

    if removed:
        logger.info("Streaming mode: removed %d local PDFs (backed up on Drive)", removed)
