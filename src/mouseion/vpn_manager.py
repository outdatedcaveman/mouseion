"""
VPN connection manager for Mouseion.
Supports OpenConnect and FortiClient command line clients.
"""

from __future__ import annotations

import os
import sys
import subprocess
import threading
import time
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from .config import Config, get_config, save_config

logger = logging.getLogger("mouseion.vpn")

# Global reference to the spawned VPN process
_vpn_process: Optional[subprocess.Popen] = None
_vpn_start_time: float = 0.0
_vpn_lock = threading.RLock()


def find_openconnect_path() -> Optional[Path]:
    """Search for the openconnect.exe executable in common Windows paths."""
    # Try project-local unpacked GUI directory first
    repo = Path(__file__).resolve().parent.parent.parent
    local_path = repo / "openconnect-gui" / "openconnect.exe"
    search_paths = [
        repo / "openconnect9" / "openconnect.exe",            # v9.x: fortinet protocol, Wintun
        Path.home() / "Desktop" / "mnt" / "outputs" / "zoterpile-main" / "openconnect9" / "openconnect.exe",
        local_path,
        Path.home() / "Desktop" / "mnt" / "outputs" / "zoterpile-main" / "openconnect-gui" / "openconnect.exe",
        Path.home() / "Desktop" / "zoterpile-main" / "openconnect-gui" / "openconnect.exe",
        Path("C:/Program Files/OpenConnect-GUI/openconnect.exe"),
        Path("C:/Program Files (x86)/OpenConnect-GUI/openconnect.exe"),
        Path("C:/Program Files/OpenConnect/openconnect.exe"),
    ]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        search_paths.insert(0, exe_dir / "mnt" / "outputs" / "zoterpile-main" / "openconnect-gui" / "openconnect.exe")
        search_paths.insert(0, exe_dir / "zoterpile-main" / "openconnect-gui" / "openconnect.exe")
        search_paths.insert(0, exe_dir / "openconnect-gui" / "openconnect.exe")
        search_paths.insert(0, exe_dir / "mnt" / "outputs" / "zoterpile-main" / "openconnect9" / "openconnect.exe")
        search_paths.insert(0, exe_dir / "openconnect9" / "openconnect.exe")

    for p in search_paths:
        if p.exists():
            return p
    # Fall back to PATH environment variable
    try:
        import shutil
        path_match = shutil.which("openconnect.exe")
        if path_match:
            return Path(path_match)
    except Exception:
        pass
    return None


def find_forticlient_path() -> Optional[Path]:
    """Search for the legacy FortiSSLVPNclient.exe executable."""
    search_paths = [
        Path("C:/Program Files/Fortinet/FortiClient/FortiSSLVPNclient.exe"),
        Path("C:/Program Files (x86)/Fortinet/FortiClient/FortiSSLVPNclient.exe"),
    ]
    for p in search_paths:
        if p.exists():
            return p
    try:
        import shutil
        path_match = shutil.which("FortiSSLVPNclient.exe")
        if path_match:
            return Path(path_match)
    except Exception:
        pass
    return None


def is_vpn_connected_locally(cfg: Config) -> bool:
    """Return True if the VPN connection is active based on network status."""
    if not cfg.vpn_gateway:
        return True
        
    # 1. If USP gateway, check if we have a USP IP or a VPN-related private IP
    if "usp.br" in cfg.vpn_gateway.lower():
        try:
            import socket
            try:
                import psutil
                ips = [
                    addr.address
                    for addrs in psutil.net_if_addrs().values()
                    for addr in addrs
                    if addr.family == socket.AF_INET
                ]
            except (ImportError, OSError):
                ips = [
                    item[4][0]
                    for item in socket.getaddrinfo(
                        socket.gethostname(), None, socket.AF_INET
                    )
                ]
            
            for ip in ips:
                # USP address space: only a tunnel (or being on campus) gives one
                if ip.startswith("143.107."):
                    return True
            # A VPN adapter (FortiClient, OpenConnect's TAP/Wintun, AnyConnect)
            # that is UP and has an IPv4 address. The old 10.x/172.16-31.x rule
            # matched WSL/Hyper-V virtual switches and reported a tunnel that
            # did not exist.
            if vpn_adapter_up():
                return True
        except Exception as e:
            logger.error("Error checking local IPs: %s", e)
        # (The old UDP probe of USP's DNS is gone: 143.107.253.3 answers from
        # the public internet, so it "proved" a tunnel that was not there.)
        return False
        
    return True


_ADAPTER_DESC: Dict[str, str] = {}
_ADAPTER_DESC_AT = 0.0
_VPN_WORDS = ("fortinet", "fortissl", "tap-windows", "tap-", "wintun", "openconnect", "anyconnect", "cisco")


def _adapter_descriptions() -> Dict[str, str]:
    """{interface friendly name: adapter description}, cached 5 min (wmic is slow)."""
    global _ADAPTER_DESC, _ADAPTER_DESC_AT
    if sys.platform != "win32":
        return {}
    if time.time() - _ADAPTER_DESC_AT < 300 and _ADAPTER_DESC:
        return _ADAPTER_DESC
    try:
        out = subprocess.check_output(
            ["wmic", "nic", "where", "NetConnectionID is not null", "get", "NetConnectionID,Description", "/format:csv"],
            text=True, errors="ignore", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        import csv
        import io
        rows = csv.DictReader(io.StringIO("\n".join(l for l in out.splitlines() if l.strip())))
        _ADAPTER_DESC = {r["NetConnectionID"]: r.get("Description") or "" for r in rows if r.get("NetConnectionID")}
        _ADAPTER_DESC_AT = time.time()
    except Exception as e:
        logger.debug("adapter descriptions unavailable: %s", e)
    return _ADAPTER_DESC


def vpn_adapter_up() -> Optional[str]:
    """Name of a VPN adapter that is up with an IPv4 address, else None."""
    try:
        import socket
        import psutil
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        desc = _adapter_descriptions()
        for name, st in stats.items():
            label = f"{name} {desc.get(name, '')}".lower()
            if not st.isup or not any(w in label for w in _VPN_WORDS):
                continue
            if any(a.family == socket.AF_INET and not a.address.startswith("169.254.") for a in addrs.get(name, [])):
                return name
    except Exception as e:
        logger.debug("vpn adapter check failed: %s", e)
    return None


def _log_tail(n: int = 12) -> str:
    try:
        cfg = get_config()
        lines = (Path(cfg.db_path).parent / "vpn_stdout.log").read_text(encoding="utf-8", errors="ignore").splitlines()
        body = [l for l in lines if l.strip() and not l.startswith("--- VPN SESSION")]
        return "\n".join(body[-n:])
    except Exception:
        return ""


_vpn_last_error = ""


def get_vpn_status() -> Dict[str, Any]:
    """Return the current VPN connection status."""
    global _vpn_process, _vpn_last_error
    cfg = get_config()
    tunnel = is_vpn_connected_locally(cfg) if cfg.vpn_gateway else False
    from . import vpn_elevated
    epid = vpn_elevated.tunnel_pid()
    with _vpn_lock:
        proc = _vpn_process
        if proc is None and epid:
            if tunnel:
                return {"status": "connected", "pid": epid, "via": "mouseion", "adapter": vpn_adapter_up()}
            if time.time() - _vpn_start_time <= 45.0:
                return {"status": "connecting", "pid": epid}
            return {"status": "error", "pid": epid,
                    "error": "The tunnel process is running but no VPN adapter is up. " + vpn_elevated.tunnel_output(6)}
        if proc is not None and proc.poll() is not None:
            code = proc.returncode
            _vpn_process = proc = None
            _vpn_last_error = (f"VPN client exited (code {code}). " + _log_tail(6)).strip()
            logger.warning("VPN process terminated with code %d", code)
        if tunnel:
            # Ours, or one another client (FortiClient, AnyConnect) holds open.
            return {"status": "connected", "pid": proc.pid if proc else None,
                    "via": "mouseion" if proc else "system", "adapter": vpn_adapter_up()}
        if proc is not None:
            if time.time() - _vpn_start_time <= 30.0:
                return {"status": "connecting", "pid": proc.pid}
            return {"status": "error", "pid": proc.pid,
                    "error": "The VPN client is running but no tunnel came up. " + (_log_tail(6) or "")}
        err = _vpn_last_error or vpn_elevated.last_error
        return {"status": "error" if err else "disconnected", "pid": None, "error": err}


def _ensure_vpn_adapters_enabled() -> None:
    """Ensure that any disabled Fortinet, TAP, or Cisco network adapters are enabled."""
    if sys.platform != "win32":
        return
    try:
        logger.info("Checking for any disabled VPN network adapters...")
        # Run wmic to get disabled adapters in CSV format
        cmd = [
            "wmic", "path", "win32_networkadapter",
            "where", "ConfigManagerErrorCode=22",
            "get", "NetConnectionID,Name,Description",
            "/format:csv"
        ]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        out = subprocess.check_output(cmd, text=True, creationflags=flags, errors="ignore")
        
        # Parse CSV output
        import csv
        import io
        
        # Filter lines and parse
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if len(lines) > 1:
            reader = csv.DictReader(io.StringIO("\n".join(lines)))
            for row in reader:
                net_id = row.get("NetConnectionID")
                name = row.get("Name", "")
                desc = row.get("Description", "")
                if not net_id:
                    continue
                
                # Check if it matches Fortinet, TAP, or Cisco
                match_terms = ["fortinet", "tap", "cisco"]
                is_vpn = any(term in name.lower() or term in desc.lower() or term in net_id.lower() for term in match_terms)
                if is_vpn:
                    logger.info("Found disabled VPN adapter: %s (%s). Enabling it...", net_id, name)
                    # Run netsh to enable it
                    enable_cmd = ["netsh", "interface", "set", "interface", f"name={net_id}", "admin=enabled"]
                    subprocess.run(enable_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, timeout=5)
        logger.info("VPN adapter enabling check completed.")
    except Exception as e:
        logger.error("Failed to enable disabled VPN network adapters: %s", e)


def _log_subprocess_output(proc: subprocess.Popen, log_path: Path) -> None:
    """Read subprocess stdout line by line in a background thread and write it to log file."""
    def _read():
        try:
            with open(log_path, "a", encoding="utf-8", errors="ignore") as f:
                f.write(f"\n--- VPN SESSION STARTED (PID: {proc.pid}) ---\n")
                f.flush()
                # Read line by line until process exits
                for line in proc.stdout:
                    f.write(line)
                    f.flush()
                f.write(f"\n--- VPN SESSION ENDED ---\n")
                f.flush()
        except Exception as e:
            logger.error("Error in VPN log reader: %s", e)
    threading.Thread(target=_read, name=f"vpn_log_reader_{proc.pid}", daemon=True).start()


def start_vpn(cfg: Config) -> Dict[str, Any]:
    """Start the VPN tunnel using the configuration."""
    global _vpn_process, _vpn_start_time, _vpn_last_error
    if sys.platform == "win32" and (cfg.vpn_type or "openconnect") == "openconnect":
        exe9 = find_openconnect_path()
        from . import vpn_elevated
        if exe9 and vpn_elevated.is_v9(exe9):
            if is_vpn_connected_locally(cfg):
                return get_vpn_status()
            if not cfg.vpn_gateway or not cfg.vpn_username:
                raise ValueError("VPN gateway and username must be configured.")
            _vpn_start_time = time.time()
            logger.info("Starting OpenConnect 9 (elevated tunnel) to %s", cfg.vpn_gateway)
            vpn_elevated.auth_failed = False          # an explicit attempt clears the pause
            res = vpn_elevated.connect(cfg, exe9)
            _vpn_last_error = res.get("error", "")
            return res
    _ensure_vpn_adapters_enabled()
    with _vpn_lock:
        # If already running and healthy, return status
        status = get_vpn_status()
        if status["status"] == "connected":
            return status

        # If disconnected or dropped, clean up first (guarantees strictly only one process)
        logger.info("VPN not active or dropped. Cleaning up any lingering VPN processes before starting...")
        stop_vpn()

        if not cfg.vpn_gateway:
            raise ValueError("VPN gateway address is not configured.")
        if not cfg.vpn_username:
            raise ValueError("VPN username is not configured.")

        # Determine path
        vpn_type = cfg.vpn_type or "openconnect"
        proc = None

        log_dir = Path(cfg.db_path).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "vpn_stdout.log"

        try:
            if vpn_type == "openconnect":
                exe_path = find_openconnect_path()
                if not exe_path:
                    raise FileNotFoundError(
                        "OpenConnect executable not found. Please install OpenConnect GUI "
                        "from https://openconnect-vpn.net/ and ensure it is installed to the default path."
                    )

                # command syntax: openconnect --protocol=<protocol> -u <user> <gateway> --passwd-on-stdin --no-cert-check
                protocol = cfg.vpn_protocol or "anyconnect"
                cmd = [
                    str(exe_path),
                    f"--protocol={protocol}",
                    "-u", cfg.vpn_username,
                    "--passwd-on-stdin",
                    "--non-inter",          # exit with a message instead of waiting on a prompt forever
                    cfg.vpn_gateway
                ]

                logger.info("Starting OpenConnect VPN: %s", " ".join(cmd))
                
                # Start process redirecting output to log files (prevents pipe deadlock)
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                )

                # Send password
                if cfg.vpn_password:
                    proc.stdin.write(cfg.vpn_password + "\n")
                    proc.stdin.flush()
                proc.stdin.close()

            elif vpn_type == "forticlient":
                exe_path = find_forticlient_path()
                if not exe_path:
                    raise FileNotFoundError(
                        "FortiSSLVPNclient.exe not found. Please copy it into the FortiClient directory or use Option 2."
                    )

                # command syntax: FortiSSLVPNclient.exe connect -s <name> -h <host:port> -u <user:pass> -i -m -q
                gateway = cfg.vpn_gateway
                if ":" not in gateway:
                    gateway = f"{gateway}:31443"

                user_pass = f"{cfg.vpn_username}"
                if cfg.vpn_password:
                    user_pass = f"{cfg.vpn_username}:{cfg.vpn_password}"

                cmd = [
                    str(exe_path),
                    "connect",
                    "-s", "USP",
                    "-h", gateway,
                    "-u", user_pass,
                    "-i", "-m", "-q"
                ]

                logger.info("Starting FortiSSLVPNclient: %s", " ".join(cmd[:-2]) + " [credentials hidden]")
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                )

            else:
                raise ValueError(f"Unsupported VPN type: {vpn_type}")

            # Start real-time log streaming
            _log_subprocess_output(proc, stdout_path)

            # Sleep briefly to see if it exits immediately (e.g. bad parameters)
            time.sleep(1.0)
            poll = proc.poll()
            if poll is not None:
                # Failed to start or exited immediately
                try:
                    with open(stdout_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                        stderr_out = "".join(lines[-20:])
                except Exception:
                    stderr_out = "Could not retrieve process output."
                raise RuntimeError(f"VPN connection failed to start. Code: {poll}. Error: {stderr_out}")

            _vpn_process = proc
            _vpn_start_time = time.time()
            _vpn_last_error = ""

            # Wait for connection to establish locally
            logger.info("Waiting for VPN connection to establish locally...")
            for i in range(25):
                if proc.poll() is not None:
                    break
                if is_vpn_connected_locally(cfg):
                    logger.info("VPN connection established locally in %d seconds.", i + 1)
                    break
                time.sleep(1.0)
            else:
                logger.warning("VPN process started, but no tunnel after 25 s.")
            return get_vpn_status()
        finally:
            pass


def stop_vpn() -> None:
    """Terminate the VPN connection process."""
    global _vpn_process
    try:
        from . import vpn_elevated
        vpn_elevated.disconnect()
    except Exception as e:
        logger.warning("elevated tunnel stop failed: %s", e)
    with _vpn_lock:
        if _vpn_process is not None:
            logger.info("Stopping active VPN process (PID: %d)", _vpn_process.pid)
            try:
                _vpn_process.terminate()
                _vpn_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _vpn_process.kill()
            except Exception:
                pass
            _vpn_process = None

        # Clean up any lingering clients system-wide
        if sys.platform == "win32":
            try:
                import psutil
                ours = {str(p).lower() for p in (find_openconnect_path(), find_forticlient_path()) if p}
                victims = [
                    proc
                    for proc in psutil.process_iter(["name", "exe"])
                    if (proc.info.get("exe") or "").lower() in ours
                ]
                for proc in victims:
                    proc.terminate()
                _, alive = psutil.wait_procs(victims, timeout=2)
                for proc in alive:
                    proc.kill()
            except Exception:
                pass


def initialize_vpn() -> None:
    """Called at application startup. Automatically starts VPN if configured to be enabled."""
    cfg = get_config()
    if cfg.vpn_enabled:
        try:
            logger.info("Automatically establishing configured VPN connection...")
            start_vpn(cfg)
        except Exception as e:
            logger.error("Failed to automatically start VPN: %s", e)

        # Background watchdog: keep the VPN up, but with EXPONENTIAL BACKOFF on
        # repeated failures. The old loop retried every 15s forever — with a wrong
        # or expired password that hammers the institutional SSO ~240x/hour and will
        # LOCK the account. Backoff spaces retries 15s→30→60→…→600s so a transient
        # drop still reconnects fast, but a bad credential can't trigger a lockout.
        # It self-heals: once the tunnel is back (e.g. the password is fixed) the
        # next check resets the backoff.
        def _watchdog():
            fails = 0
            interval = 15.0
            while True:
                time.sleep(interval)
                try:
                    c = get_config()
                    if not c.vpn_enabled:
                        break  # stop watchdog if disabled dynamically
                    from . import vpn_elevated
                    if vpn_elevated.declined or vpn_elevated.auth_failed:
                        continue          # a click on Connect (or new credentials) resumes
                    if get_vpn_status().get("status") not in ("disconnected", "error") or not c.vpn_gateway:
                        fails = 0
                        interval = 15.0
                        continue
                    # Disconnected — attempt a reconnect, then verify it took.
                    logger.warning("VPN WATCHDOG: connection dropped. Reconnect attempt %d...", fails + 1)
                    try:
                        start_vpn(c)
                    except Exception as e:
                        logger.error("VPN WATCHDOG: reconnect raised: %s", e)
                    time.sleep(4.0)  # allow the tunnel to establish
                    if get_vpn_status().get("status") == "connected":
                        fails = 0
                        interval = 15.0
                        logger.info("VPN WATCHDOG: reconnected.")
                    else:
                        fails += 1
                        interval = min(15.0 * (2 ** fails), 600.0)  # backoff, cap 10 min
                        if fails == 4:
                            logger.error("VPN WATCHDOG: %d consecutive reconnect failures — backing off "
                                         "to avoid locking the institutional account. Check the VPN "
                                         "password in Settings; it resumes automatically once fixed.", fails)
                except Exception as err:
                    logger.error("VPN WATCHDOG error: %s", err)

        threading.Thread(target=_watchdog, name="vpn_watchdog", daemon=True).start()
