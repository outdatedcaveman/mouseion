"""OpenConnect >= 9 on Windows: authenticate unprivileged, tunnel elevated.

The tunnel needs Administrator (openconnect creates a Wintun adapter and sets
routes/DNS through vpnc-script-win.js); logging in does not. So:

  1. `openconnect --authenticate` runs as the user; the password goes to its
     stdin and never leaves this process. It prints a session COOKIE and the
     server certificate FINGERPRINT.
  2. One UAC prompt starts a hidden PowerShell runner (elevated) that feeds the
     cookie to `openconnect --cookie-on-stdin --servercert=<fingerprint>` and
     watches for a stop flag, so Mouseion (unprivileged) can end the elevated
     tunnel without a second prompt.

The session cookie lives in %LOCALAPPDATA%\\mouseion\\vpn (user-only) for the
life of the tunnel and is deleted when the tunnel ends.

Why this exists (2026-09-25): 15,168 attempts with openconnect 7.08 against a
retired Cisco gateway never produced a tunnel; USP's live VPN is a FortiGate,
which needs openconnect's `fortinet` protocol (>= 8.10) and admin rights.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

RUN_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "mouseion" / "vpn"
IFNAME = "openconnect-mouseion"          # matched by vpn_manager.vpn_adapter_up()
_NO_WINDOW = 0x08000000

last_error = ""
declined = False                          # UAC refused: no automatic retries until a click
auth_failed = False                       # login rejected: no automatic retries (account lockout)

_RUNNER = r'''param([string]$Oc, [string]$Proto, [string]$Cert, [string]$Url, [string]$Ifname, [string]$Dir)
$ErrorActionPreference = "Continue"
$cookie = Join-Path $Dir "cookie.txt"
$stop   = Join-Path $Dir "stop.flag"
$pidf   = Join-Path $Dir "tunnel.pid"
Remove-Item $stop -ErrorAction SilentlyContinue
$a = @("--protocol=$Proto", "--cookie-on-stdin", "--servercert=$Cert", "--interface=$Ifname", "--non-inter", "--timestamp", $Url)
$p = Start-Process -FilePath $Oc -ArgumentList $a -RedirectStandardInput $cookie `
     -RedirectStandardOutput (Join-Path $Dir "tunnel.log") -RedirectStandardError (Join-Path $Dir "tunnel.err") `
     -NoNewWindow -PassThru
Set-Content -Path $pidf -Value $p.Id
while (-not $p.HasExited) {
    if (Test-Path $stop) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; break }
    Start-Sleep -Milliseconds 1000
}
Start-Sleep -Milliseconds 500
Remove-Item $cookie, $pidf, $stop -ErrorAction SilentlyContinue
'''


def is_v9(exe: Path) -> bool:
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=15,
                             creationflags=_NO_WINDOW).stdout
        m = re.search(r"version v?(\d+)\.", out)
        return bool(m and int(m.group(1)) >= 9)
    except Exception:
        return False


def tunnel_pid() -> Optional[int]:
    try:
        pid = int((RUN_DIR / "tunnel.pid").read_text().strip())
    except Exception:
        return None
    try:
        import psutil
        return pid if psutil.pid_exists(pid) else None
    except Exception:
        return pid


def tunnel_output(n: int = 8) -> str:
    lines: list[str] = []
    for name in ("tunnel.log", "tunnel.err"):
        try:
            lines += (RUN_DIR / name).read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            pass
    return "\n".join(l for l in lines if l.strip())[-1500:] if lines else ""


def _parse_auth(out: str) -> Dict[str, str]:
    vals = {}
    for key in ("COOKIE", "HOST", "CONNECT_URL", "FINGERPRINT", "RESOLVE"):
        m = re.search(rf"^{key}='?([^'\r\n]*)'?\s*$", out, re.M)
        if m:
            vals[key] = m.group(1)
    return vals


def _explain(out: str) -> str:
    low = out.lower()
    # After logincheck, FortiGate re-presents the login form when the credentials
    # are wrong: openconnect then asks for "Password:" again (seen 2026-09-25).
    if "logincheck" in low and "password:" in low and "token" not in low:
        return ("USP rejected the username/password (the gateway asked for the password again). "
                "Check them in Settings > Institutional VPN -- the FortiGate login may differ from "
                "the old Cisco one. Automatic retries are paused to protect the account.")
    if "user input required" in low:
        return ("The gateway asked for more than a password (e.g. a FortiToken code). "
                "Automatic login can't answer that; tell Claude which prompt it shows.")
    if "login failed" in low or "invalid" in low and "password" in low or "authentication failed" in low \
            or "failed to complete authentication" in low:
        return "USP rejected the username/password. Check them in Settings > Institutional VPN."
    if "certificate" in low:
        return "The gateway's certificate was not accepted."
    return "Login did not complete."


def connect(cfg, exe: Path) -> Dict[str, Any]:
    """Blocking (~5-30 s). Returns a status dict; never raises for expected failures."""
    global last_error, declined
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if tunnel_pid():
        return {"status": "connecting", "pid": tunnel_pid()}
    proto = cfg.vpn_protocol or "anyconnect"

    # 1. authenticate as the user; the password never leaves this process
    cmd = [str(exe), f"--protocol={proto}", "--authenticate", "-u", cfg.vpn_username,
           "--passwd-on-stdin", "--non-inter", cfg.vpn_gateway]
    try:
        r = subprocess.run(cmd, input=(cfg.vpn_password or "") + "\n", capture_output=True, text=True,
                           timeout=90, creationflags=_NO_WINDOW)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
    except subprocess.TimeoutExpired:
        last_error = "The VPN login step timed out (90 s)."
        return {"status": "error", "error": last_error}
    shown = out.replace(cfg.vpn_password, "***") if cfg.vpn_password else out
    shown = re.sub(r"(?m)^COOKIE=.*$", "COOKIE=<hidden>", shown)
    _append_log(cfg, "AUTH", shown)
    vals = _parse_auth(out)
    if not vals.get("COOKIE") or not vals.get("FINGERPRINT"):
        global auth_failed
        auth_failed = True                    # never auto-retry a rejected login
        last_error = _explain(out)
        return {"status": "error", "error": last_error}

    # 2. elevated tunnel (one UAC prompt), fed the session cookie only
    (RUN_DIR / "cookie.txt").write_text(vals["COOKIE"] + "\n", encoding="ascii")
    for stale in ("tunnel.log", "tunnel.err", "stop.flag"):
        try:
            (RUN_DIR / stale).unlink()
        except FileNotFoundError:
            pass
    script = RUN_DIR / "tunnel_runner.ps1"
    script.write_text(_RUNNER, encoding="utf-8")
    url = vals.get("CONNECT_URL") or cfg.vpn_gateway
    params = (f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script}" '
              f'-Oc "{exe}" -Proto "{proto}" -Cert "{vals["FINGERPRINT"]}" -Url "{url}" '
              f'-Ifname "{IFNAME}" -Dir "{RUN_DIR}"')
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", params, None, 0)
    if rc <= 32:
        try:
            (RUN_DIR / "cookie.txt").unlink()
        except FileNotFoundError:
            pass
        if rc == 5:
            declined = True
            last_error = "Windows admin permission was declined, so the tunnel was not started. Click Connect VPN to try again."
        else:
            last_error = f"Could not start the elevated VPN runner (ShellExecute code {rc})."
        return {"status": "error", "error": last_error}
    declined = False
    last_error = ""
    for _ in range(40):                               # adapter + routes take a few seconds
        time.sleep(1)
        from .vpn_manager import vpn_adapter_up
        if vpn_adapter_up():
            return {"status": "connected", "adapter": vpn_adapter_up(), "pid": tunnel_pid()}
        if tunnel_pid() is None and (RUN_DIR / "tunnel.log").exists() and _ > 5:
            break
    last_error = "The tunnel did not come up. " + tunnel_output(6)
    return {"status": "error", "error": last_error}


def disconnect(wait: float = 8.0) -> None:
    if tunnel_pid() is None:
        return
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "stop.flag").write_text("stop", encoding="ascii")
    end = time.time() + wait
    while time.time() < end and tunnel_pid():
        time.sleep(0.5)


def _append_log(cfg, tag: str, text: str) -> None:
    try:
        p = Path(cfg.db_path).parent / "vpn_stdout.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n--- VPN {tag} {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text.strip()}\n")
    except Exception:
        pass
