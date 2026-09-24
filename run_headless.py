"""
Headless runner for Mouseion — Flask server + enrichment/sync daemons, NO GUI.

Why this exists
---------------
The normal entrypoint (``python -m mouseion``) opens a native pywebview window
and runs Flask in a *daemon* thread, so the whole process exits the moment the
window closes. During a long unattended batch run that window is a liability:
the Edge WebView2 renderer can crash (e.g. if the UI loads the full 250k-row
library into the DOM), and when it does it takes the enrichment daemon down with
it. This runner keeps the server + daemons in the foreground with no window, so
the pipelines run for days regardless of any UI.

It reuses the EXACT same app, config, database, master API router and daemons as
the .exe — it only swaps the fragile GUI harness for a stable headless one.

Usage:
    MOUSEION_API_KEY=<hex> PORT=7274 python run_headless.py
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.request


def main() -> None:
    import logging
    import socket
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    port = int(os.environ.get("PORT", "7274"))

    # Port bind check to ensure only ONE instance runs system-wide
    try:
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_sock.bind(("127.0.0.1", port))
        test_sock.close()
    except OSError:
        logging.error("HEADLESS ERROR: Port %d is already in use. Another instance of Mouseion is likely running. Aborting startup.", port)
        sys.exit(1)

    from mouseion.web import app
    from mouseion.config import get_config

    # Known API key so external callers (the PDF fetch-all trigger) can auth.
    api_key = os.environ.get("MOUSEION_API_KEY", "").strip()
    if api_key:
        app.config["API_KEY"] = api_key

    # --- Flask server in a background thread -------------------------------
    def _serve() -> None:
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True,
                use_reloader=False)

    server_thread = threading.Thread(target=_serve, name="flask", daemon=True)
    server_thread.start()
    logging.info("HEADLESS: server starting on port %d", port)

    # Wait for the server to accept connections.
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    # --- VPN Connection (if enabled) ---------------------------------------
    try:
        from mouseion.vpn_manager import initialize_vpn
        initialize_vpn()
        logging.info("HEADLESS: VPN initialization complete")
    except Exception:
        logging.exception("HEADLESS: VPN initialization failed")

    # --- Enrichment daemon -------------------------------------------------
    try:
        from mouseion.enrich_daemon import start as _start_daemon
        _start_daemon()
        logging.info("HEADLESS: enrichment daemon started")
    except Exception:
        logging.exception("HEADLESS: enrichment daemon failed to start")

    # --- Google Drive sync daemon (if enabled) -----------------------------
    try:
        if get_config().google_drive_sync_enabled:
            from mouseion.sync_daemon import start as _start_sync
            _start_sync()
            logging.info("HEADLESS: drive sync daemon started")
    except Exception:
        logging.exception("HEADLESS: sync daemon failed to start")

    # --- PDF auto-fetch daemon (if enabled) --------------------------------
    try:
        if get_config().auto_fetch_pdfs:
            import threading as _th, time as _tm, urllib.request as _ur
            def _autostart_pdf():
                _tm.sleep(5)
                try:
                    from mouseion.web import _get_or_create_api_key
                    _key = _get_or_create_api_key()
                    _req = _ur.Request(
                        f"http://127.0.0.1:{port}/api/pdfs/fetch-all",
                        method="POST", data=b"{}",
                        headers={"X-API-Key": _key, "Content-Type": "application/json"},
                    )
                    _ur.urlopen(_req, timeout=15)
                    logging.info("HEADLESS: PDF auto-fetch started (concurrent with enrichment)")
                except Exception:
                    logging.exception("HEADLESS: PDF auto-fetch failed to start")
            _th.Thread(target=_autostart_pdf, daemon=True).start()
    except Exception:
        logging.exception("HEADLESS: PDF auto-start wiring failed")

    logging.info("HEADLESS: up. Enrichment running; PDF auto-fetch started/running concurrently.")

    # Keep the process alive in the foreground forever (no window to crash).
    while True:
        time.sleep(3600)



if __name__ == "__main__":
    main()
