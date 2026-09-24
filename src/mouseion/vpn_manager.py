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
    local_path = Path(__file__).resolve().parent.parent.parent / "openconnect-gui" / "openconnect.exe"
    search_paths = [
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
                # USP public subnet
                if ip.startswith("143.107."):
                    return True
                # Private subnets common for VPN interfaces (10.0.0.0/8, 172.16.0.0/12)
                if ip.startswith("10."):
                    return True
                if ip.startswith("172."):
                    try:
                        second_octet = int(ip.split(".")[1])
                        if 16 <= second_octet <= 31:
                            return True
                    except Exception:
                        pass
        except Exception as e:
            logger.error("Error checking local IPs: %s", e)
            
        # Fallback: try connecting to a USP internal DNS server over UDP (DNS default)
        try:
            import socket
            packet = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03usp\x02br\x00\x00\x01\x00\x01"
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2.0)
            s.sendto(packet, ("143.107.253.3", 53))
            data, addr = s.recvfrom(512)
            s.close()
            if len(data) >= 12 and data[:2] == b"\x12\x34":
                return True
        except Exception:
            pass
            
        return False
        
    return True


def get_vpn_status() -> Dict[str, Any]:
    """Return the current VPN connection status."""
    global _vpn_process
    with _vpn_lock:
        if _vpn_process is None:
            return {"status": "disconnected", "pid": None}
        
        # Check if process is still running
        poll = _vpn_process.poll()
        if poll is not None:
            # Process terminated
            code = _vpn_process.returncode
            _vpn_process = None
            logger.warning("VPN process terminated with code %d", code)
            return {"status": "disconnected", "pid": None, "exit_code": code}
            
        # Network level check
        cfg = get_config()
        if cfg.vpn_enabled and not is_vpn_connected_locally(cfg):
            # Grace period of 30 seconds to allow the connection to be established
            if time.time() - _vpn_start_time > 30.0:
                logger.warning("VPN process is running (PID: %d) but network connection is inactive/dropped.", _vpn_process.pid)
                return {"status": "disconnected", "pid": _vpn_process.pid, "network_dropped": True}
            else:
                return {
                    "status": "connected",
                    "pid": _vpn_process.pid,
                    "connecting": True,
                }
            
        return {
            "status": "connected",
            "pid": _vpn_process.pid,
        }


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
    global _vpn_process, _vpn_start_time
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
                logger.warning("VPN process started, but local connection check timed out (25s). Proceeding anyway...")

            return {"status": "connected", "pid": proc.pid}
        finally:
            pass


def stop_vpn() -> None:
    """Terminate the VPN connection process."""
    global _vpn_process
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
                names = {"openconnect.exe", "fortisslvpnclient.exe"}
                victims = [
                    proc
                    for proc in psutil.process_iter(["name"])
                    if (proc.info["name"] or "").lower() in names
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
                    if get_vpn_status().get("status") != "disconnected" or not c.vpn_gateway:
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
