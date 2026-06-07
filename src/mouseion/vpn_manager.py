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
_vpn_lock = threading.Lock()


def find_openconnect_path() -> Optional[Path]:
    """Search for the openconnect.exe executable in common Windows paths."""
    search_paths = [
        Path("C:/Program Files/OpenConnect-GUI/openconnect.exe"),
        Path("C:/Program Files (x86)/OpenConnect-GUI/openconnect.exe"),
        Path("C:/Program Files/OpenConnect/openconnect.exe"),
    ]
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
            
        return {
            "status": "connected",
            "pid": _vpn_process.pid,
        }


def start_vpn(cfg: Config) -> Dict[str, Any]:
    """Start the VPN tunnel using the configuration."""
    global _vpn_process
    with _vpn_lock:
        # If already running, return status
        status = get_vpn_status()
        if status["status"] == "connected":
            return status

        if not cfg.vpn_gateway:
            raise ValueError("VPN gateway address is not configured.")
        if not cfg.vpn_username:
            raise ValueError("VPN username is not configured.")

        # Determine path
        vpn_type = cfg.vpn_type or "openconnect"
        proc = None

        if vpn_type == "openconnect":
            exe_path = find_openconnect_path()
            if not exe_path:
                raise FileNotFoundError(
                    "OpenConnect executable not found. Please install OpenConnect GUI "
                    "from https://openconnect-vpn.net/ and ensure it is installed to the default path."
                )

            # command syntax: openconnect --protocol=fortinet -u <user> <gateway> --passwd-on-stdin
            cmd = [
                str(exe_path),
                "--protocol=fortinet",
                "-u", cfg.vpn_username,
                cfg.vpn_gateway,
                "--passwd-on-stdin"
            ]

            logger.info("Starting OpenConnect VPN: %s", " ".join(cmd[:-1]) + " [passwd hidden]")
            
            # Start process in background
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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
            # Note: gateway might contain port, if not we add default 443 or 31443
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
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )

        else:
            raise ValueError(f"Unsupported VPN type: {vpn_type}")

        # Sleep briefly to see if it exits immediately (e.g. bad parameters)
        time.sleep(0.5)
        poll = proc.poll()
        if poll is not None:
            # Failed to start or exited immediately
            stderr_out = ""
            if proc.stderr:
                stderr_out = proc.stderr.read()
            raise RuntimeError(f"VPN connection failed to start. Code: {poll}. Error: {stderr_out}")

        _vpn_process = proc
        return {"status": "connected", "pid": proc.pid}


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
                subprocess.run(
                    ["taskkill", "/f", "/im", "openconnect.exe"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                subprocess.run(
                    ["taskkill", "/f", "/im", "FortiSSLVPNclient.exe"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
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
