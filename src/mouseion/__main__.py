"""
Mouseion desktop launcher.

This is the PyInstaller entry point. It:
1. Starts the Flask server in a background thread
2. Opens a native desktop window via pywebview
3. Exits cleanly when the window is closed

Running via `python -m mouseion` or the .exe both land here.
"""

import logging
import os
import sys
import threading
import time
import socket
from pathlib import Path


# ---------------------------------------------------------------------------
# Single-instance guard
# ---------------------------------------------------------------------------

_mutex_handle = None  # prevent GC on Windows


def _acquire_instance_lock():
    """Return True if we are the only instance, False if another is running."""
    global _mutex_handle

    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        mutex_name = "Global\\MouseionSingleInstance"
        _mutex_handle = kernel32.CreateMutexW(None, False, mutex_name)
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        return True
    else:
        # Unix: simple lock file
        import fcntl
        lock_path = os.path.join(
            os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "mouseion.lock"
        )
        try:
            _acquire_instance_lock._lock_fd = open(lock_path, "w")
            fcntl.flock(_acquire_instance_lock._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, IOError):
            return False


def _focus_existing_window():
    """Try to bring an existing Mouseion window to the foreground (Windows)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "Mouseion")
        if hwnd:
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Port helpers
# ---------------------------------------------------------------------------

def _find_free_port(start: int = 7274, end: int = 7284) -> int:
    """Return the first free port in [start, end], or raise RuntimeError."""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}-{end}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _setup_crash_logging():
    """Configure file-based logging so crashes are captured even without a console."""
    log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mouseion" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "mouseion.log"

    # Rotate: keep last 3 log files, max 5 MB each
    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(
        str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Idempotent: drop any prior RotatingFileHandler pointing at the same file
    # so a relaunch within a stale interpreter state can't end up with a dead
    # handler that silently writes nowhere (the cause of the frozen log).
    for h in list(root.handlers):
        if isinstance(h, RotatingFileHandler):
            try:
                root.removeHandler(h); h.close()
            except Exception:
                pass
    root.addHandler(handler)
    # Immediate startup marker + flush: proves the log file is live from the
    # first moment of every launch (so a broken handler is obvious at once).
    try:
        logging.getLogger("mouseion").info("Logging initialised -> %s", log_file)
        handler.flush()
    except Exception:
        pass

    # Also log to stderr if available
    if sys.stderr and not getattr(sys, "frozen", False):
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(logging.Formatter(
            "[%(levelname)s] %(name)s: %(message)s"
        ))
        root.addHandler(stderr_handler)

    # Redirect uncaught exceptions to log
    def _exception_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _exception_hook

    # Capture threading exceptions (Python 3.8+)
    if hasattr(threading, "excepthook"):
        _orig_hook = threading.excepthook
        def _thread_exception_hook(args):
            if args.exc_type is SystemExit:
                return
            logging.critical(
                "Uncaught exception in thread %s", args.thread.name if args.thread else "?",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        threading.excepthook = _thread_exception_hook

    logging.info("Mouseion starting — log file: %s", log_file)
    return log_file


def main():
    # Set up crash logging FIRST, before anything can fail
    log_file = _setup_crash_logging()

    # Single-instance check
    if not _acquire_instance_lock():
        _focus_existing_window()
        sys.exit(0)

    # Ensure the package is importable (PyInstaller sets this up, but be safe)
    if getattr(sys, "frozen", False):
        # Running as .exe — sys._MEIPASS has the bundled modules
        bundle_dir = sys._MEIPASS
        if bundle_dir not in sys.path:
            sys.path.insert(0, bundle_dir)

    from mouseion.web import app, run as _configure_run
    import mouseion.web as web_mod

    port = _find_free_port(int(os.environ.get("PORT", 7274)))
    url = f"http://127.0.0.1:{port}"

    # Run the startup configuration (API key, banner, etc.)
    _configure_run.__wrapped__ = True  # flag to skip app.run()

    # Generate API key and print banner (reuse the run() setup logic)
    api_key = os.environ.get("MOUSEION_API_KEY", "").strip()
    if not api_key:
        import secrets
        api_key = secrets.token_hex(32)

    # Set the key on the app
    app.config["API_KEY"] = api_key

    def _safe_print(*args, **kwargs):
        try:
            print(*args, **kwargs)
        except OSError:
            pass

    _safe_print(f"\n  Mouseion is starting...")
    _safe_print(f"  Port     ->  {port}")
    _safe_print(f"  API Key  ->  {api_key}")
    _safe_print(f"  Log file ->  {log_file}")
    _safe_print()
    logging.info("Server on port %d", port)

    # Start Flask in a daemon thread
    def _serve():
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True,
                use_reloader=False)

    server_thread = threading.Thread(target=_serve, daemon=True)
    server_thread.start()

    # Wait for the server to be ready
    import urllib.request
    for _ in range(50):  # up to 5 seconds
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    # Start background enrichment daemon
    try:
        from mouseion.enrich_daemon import start as _start_daemon
        _start_daemon()
        logging.info("Enrichment daemon started")
    except Exception:
        logging.exception("Enrichment daemon failed to start")

    # Start Google Drive sync daemon if enabled
    try:
        from mouseion.config import get_config as _get_cfg
        if _get_cfg().google_drive_sync_enabled:
            from mouseion.sync_daemon import start as _start_sync
            _start_sync()
            logging.info("Drive sync daemon started")
    except Exception:
        logging.exception("Drive sync daemon failed to start")

    # Auto-start PDF fetching CONCURRENTLY with enrichment (if enabled in config).
    # Uses a localhost self-POST so it reuses the exact /api/pdfs/fetch-all path
    # (which spawns its own background thread and returns immediately). Runs in a
    # short-delayed thread so Flask is up first. Both engines then run in parallel.
    try:
        from mouseion.config import get_config as _get_cfg_pdf
        if _get_cfg_pdf().auto_fetch_pdfs:
            import threading as _th, time as _tm, urllib.request as _ur
            def _autostart_pdf():
                _tm.sleep(20)
                try:
                    from mouseion.web import _get_or_create_api_key
                    _key = _get_or_create_api_key()
                    _req = _ur.Request(
                        f"http://127.0.0.1:{port}/api/pdfs/fetch-all",
                        method="POST", data=b"{}",
                        headers={"X-API-Key": _key, "Content-Type": "application/json"},
                    )
                    _ur.urlopen(_req, timeout=15)
                    logging.info("PDF auto-fetch started (concurrent with enrichment)")
                except Exception:
                    logging.exception("PDF auto-fetch failed to start")
            _th.Thread(target=_autostart_pdf, daemon=True).start()
    except Exception:
        logging.exception("PDF auto-start wiring failed")

    # Start VPN if configured to run on startup
    try:
        from mouseion.vpn_manager import initialize_vpn
        initialize_vpn()
    except Exception:
        logging.exception("VPN failed to initialize")

    # Try to open a native desktop window; fall back to browser if pywebview
    # is not available (e.g. missing system dependencies)
    try:
        import webview
        window = webview.create_window(
            "Mouseion",
            url,
            width=1280,
            height=860,
            min_size=(800, 500),
            background_color='#0d0d12',
        )
        webview.start()
    except ImportError:
        _safe_print(f"  pywebview not available — opening in browser")
        _safe_print(f"  -> {url}")
        import webbrowser
        webbrowser.open(url)
        # Keep the process alive until Ctrl+C
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
    except Exception as e:
        _safe_print(f"  Desktop window failed ({e}), opening in browser")
        import webbrowser
        webbrowser.open(url)
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
    finally:
        # Clean up VPN on exit
        try:
            from mouseion.vpn_manager import stop_vpn
            stop_vpn()
        except Exception:
            pass


if __name__ == "__main__":
    main()
