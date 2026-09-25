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
from typing import Optional


def _safe_print(*args, **kwargs):
    """Print to stdout safely, ignoring errors if stdout is None or closed."""
    try:
        if sys.stdout is not None:
            print(*args, **kwargs)
    except Exception:
        pass


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


def _window_failed(url: str, why: str) -> None:
    """The owner's standing rule: Mouseion NEVER opens a browser tab. If the
    native window cannot open, say so in a native message box (and the log);
    the server keeps running at `url` for anyone who wants it."""
    logging.error("Desktop window unavailable (%s); server stays at %s", why, url)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                f"Mouseion's window could not open ({why}).\n\nThe library server is still "
                f"running at {url}.\nClose this and start Mouseion again; if it keeps "
                f"happening, check that Microsoft Edge WebView2 Runtime is installed.",
                "Mouseion", 0x30)          # MB_ICONWARNING
        except Exception:
            pass


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


def _is_server_responsive(port: int) -> bool:
    """Check if the Mouseion server is responding on the given port."""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.0)
        return True
    except Exception:
        return False


def _check_existing_instances() -> Optional[int]:
    """Check if there is a responsive instance running in the port range 7274-7284."""
    for port in range(7274, 7285):
        if _is_server_responsive(port):
            return port
    return None


def _kill_process_on_port(port: int):
    """Find and kill any process listening on the given port (Windows only)."""
    if sys.platform != "win32":
        return
    try:
        import psutil
        pids_to_kill = {
            conn.pid
            for conn in psutil.net_connections(kind="tcp")
            if conn.pid
            and conn.pid != os.getpid()
            and conn.status == psutil.CONN_LISTEN
            and conn.laddr.port == port
        }
        for pid in sorted(pids_to_kill):
            logging.info("Killing process %d holding port %d", pid, port)
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except psutil.TimeoutExpired:
                proc.kill()
    except Exception as e:
        logging.warning("Failed to kill process on port %d: %s", port, e)


def _terminate_processes_by_name(names: set[str]) -> None:
    """Terminate stale helper processes without spawning taskkill.exe."""
    import psutil

    current_pid = os.getpid()
    targets = {
        name.lower()
        for name in names
    }
    victims = [
        proc
        for proc in psutil.process_iter(["pid", "name"])
        if proc.info["pid"] != current_pid
        and (proc.info["name"] or "").lower() in targets
    ]
    for proc in victims:
        proc.terminate()
    _, alive = psutil.wait_procs(victims, timeout=2)
    for proc in alive:
        proc.kill()


def main():
    # Set up crash logging FIRST, before anything can fail
    log_file = _setup_crash_logging()

    # Kill any other running Mouseion.exe processes first to ensure clean start and update
    if sys.platform == "win32":
        try:
            _terminate_processes_by_name({
                "Mouseion.exe",
                "Mouseion.old.exe",
                "openconnect.exe",
                "FortiSSLVPNclient.exe",
            })
            # Also kill any other processes holding our ports 7274-7284 to clean up background zombies
            for port in range(7274, 7285):
                _kill_process_on_port(port)
            time.sleep(0.5)
        except Exception:
            pass

    # If running elevated, clean up/unregister the scheduled task to prevent "running in the dark"
    if sys.platform == "win32":
        try:
            import ctypes
            import subprocess
            if ctypes.windll.shell32.IsUserAnAdmin():
                logging.info("Running elevated. Ensuring Scheduled Task 'MouseionServer' is unregistered/deleted...")
                # Run PowerShell to delete the task if it exists
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Get-ScheduledTask -TaskName 'MouseionServer' -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                logging.info("Scheduled Task 'MouseionServer' cleanup completed.")
        except Exception as e:
            logging.warning("Failed to unregister scheduled task: %s", e)

    # Check if a responsive server is already running
    existing_port = _check_existing_instances()
    if existing_port is not None:
        import ctypes
        window_exists = False
        if sys.platform == "win32":
            window_exists = bool(ctypes.windll.user32.FindWindowW(None, "Mouseion"))
        
        if window_exists:
            _focus_existing_window()
            sys.exit(0)
            
        # No window exists, but server is running -> open a native webview window pointing to it
        url = f"http://127.0.0.1:{existing_port}"
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
            sys.exit(0)
        except Exception as e:
            _window_failed(url, f"{type(e).__name__}: {e}")
            sys.exit(0)

    # If no responsive server is running, kill any hung processes holding our ports
    if sys.platform == "win32":
        for port in range(7274, 7285):
            _kill_process_on_port(port)
        time.sleep(0.5)

    # Single-instance check (retry after killing any hung processes)
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

    # Port bind check to ensure only ONE instance runs system-wide
    try:
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_sock.bind(("127.0.0.1", port))
        test_sock.close()
    except OSError:
        _safe_print(f"\n  Error: Port {port} is already in use by another instance.")
        _safe_print("  Please make sure you only run one instance of Mouseion.")
        logging.error("Server startup aborted: port %d already in use", port)
        sys.exit(1)

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
    def _vpn_start():
        try:
            from mouseion.vpn_manager import initialize_vpn
            initialize_vpn()
        except Exception:
            logging.exception("VPN failed to initialize")
    # Never make the window wait on a VPN login (up to ~2 min + a UAC prompt).
    threading.Thread(target=_vpn_start, daemon=True, name="vpn-autostart").start()

    # Try to open a native desktop window; fall back to browser if pywebview
    # is not available (e.g. missing system dependencies)
    _window_loaded = False

    def _on_loaded():
        nonlocal _window_loaded
        _window_loaded = True
        logging.info("Webview window loaded successfully.")

    def _webview_watchdog():
        # A slow first paint (large library, busy disk) is NOT a failure: this
        # watchdog used to open a browser tab after 8 s, on top of the window.
        time.sleep(60.0)
        if not _window_loaded:
            logging.warning("Webview window has not reported 'loaded' after 60 s (still waiting; no browser).")

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
        window.events.loaded += _on_loaded

        # Start the watchdog thread to open browser if the window hangs/fails to show
        threading.Thread(target=_webview_watchdog, daemon=True, name="webview-watchdog").start()

        webview.start()
    except ImportError:
        _safe_print(f"  pywebview not available -- server at {url} (no browser is opened)")
        _window_failed(url, "pywebview missing")
        # Keep the process alive until Ctrl+C
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
    except Exception as e:
        _safe_print(f"  Desktop window failed ({e}) -- server at {url} (no browser is opened)")
        _window_failed(url, f"{type(e).__name__}: {e}")
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
