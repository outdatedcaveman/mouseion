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
$a = @("--protocol=$Proto", "--cookie-on-stdin", "--servercert=$Cert", "--interface=$Ifname", "--no-dtls", "--non-inter", "--timestamp", $Url)
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


# ---------------------------------------------------------------------------
# No prompt per connect (2026-09-25: a UAC prompt on every reconnect "every
# minute"). One elevated setup installs a runner in an admin-only folder and a
# scheduled task that runs it elevated ON DEMAND; afterwards Mouseion starts the
# tunnel with `schtasks /run` -- no prompt. The runner accepts only validated
# values from the user's folder (protocol allow-list, fingerprint and URL
# patterns) and only the gateway host recorded at setup, with the openconnect
# binary from its own protected copy: it cannot be used to run anything else
# as administrator.
# ---------------------------------------------------------------------------
TASK = "MouseionVPN"
RUNNER_VERSION = "2"      # bump when _TASK_RUNNER changes: triggers the (one) re-setup
PROTECTED = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Mouseion" / "vpn"

_TASK_RUNNER = r"""$ErrorActionPreference = 'Continue'
$prot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dir  = Join-Path $env:LOCALAPPDATA 'mouseion\vpn'
$p = Get-Content (Join-Path $dir 'params.json') -Raw | ConvertFrom-Json
$allowed = (Get-Content (Join-Path $prot 'allowed_host.txt') -Raw).Trim().ToLower()
if ($p.proto -notin @('fortinet','anyconnect','gp','nc','pulse','f5','array')) { exit 2 }
if ($p.cert -notmatch '^(pin-sha256:[A-Za-z0-9+/=]{20,100}|sha1:[0-9a-fA-F]{40}|sha256:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})$') { exit 3 }
if ($p.url -notmatch '^https://([A-Za-z0-9.-]+)(:\d{1,5})?(/[A-Za-z0-9._~/?=&%-]*)?$') { exit 4 }
if ($Matches[1].ToLower() -ne $allowed) { exit 5 }
$if = if ($p.ifname) { [string]$p.ifname } else { 'openconnect-mouseion' }
if ($if -notmatch '^[A-Za-z0-9 ._()-]{1,64}$') { exit 6 }
$oc = Join-Path $prot 'openconnect\openconnect.exe'
$stop = Join-Path $dir 'stop.flag'; $pidf = Join-Path $dir 'tunnel.pid'
Remove-Item $stop -ErrorAction SilentlyContinue
$a = @("--protocol=$($p.proto)", '--cookie-on-stdin', "--servercert=$($p.cert)", "--interface=$if",
       '--no-dtls', '--non-inter', '--timestamp', $p.url)
$proc = Start-Process -FilePath $oc -ArgumentList $a -RedirectStandardInput (Join-Path $dir 'cookie.txt') `
        -RedirectStandardOutput (Join-Path $dir 'tunnel.log') -RedirectStandardError (Join-Path $dir 'tunnel.err') `
        -NoNewWindow -PassThru
Set-Content -Path $pidf -Value $proc.Id
while (-not $proc.HasExited) {
    if (Test-Path $stop) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue; break }
    Start-Sleep -Milliseconds 1000
}
Start-Sleep -Milliseconds 500
Remove-Item (Join-Path $dir 'cookie.txt'), $pidf, $stop -ErrorAction SilentlyContinue
"""

_SETUP = r"""param([string]$Src, [string]$Prot, [string]$HostName, [string]$User, [string]$Version)
$ErrorActionPreference = 'Stop'
$log = Join-Path $Src 'setup.log'
try {
  if (Test-Path $Prot) {
    # an earlier setup may have left files with no readable ACL: take them back first
    & takeown /f $Prot /r /d y | Out-Null
    & icacls $Prot /reset /T /Q | Out-Null
  }
  New-Item -ItemType Directory -Force -Path $Prot | Out-Null
  Copy-Item -Path (Join-Path $Src 'openconnect') -Destination $Prot -Recurse -Force
  Copy-Item -Path (Join-Path $Src 'runner.ps1') -Destination (Join-Path $Prot 'runner.ps1') -Force
  Set-Content -Path (Join-Path $Prot 'allowed_host.txt') -Value $HostName -Encoding ASCII
  Set-Content -Path (Join-Path $Prot 'runner_version.txt') -Value $Version -Encoding ASCII
  # folder: admins + SYSTEM write, users read; files inherit it (OI/CI flags only mean
  # something on a folder -- applied to files they left them unreadable, 2026-09-25)
  & icacls $Prot /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /Q | Out-Null
  & icacls (Join-Path $Prot '*') /reset /T /Q | Out-Null
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $Prot 'runner.ps1') + '"')
  $pri = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 30) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName 'MouseionVPN' -Action $act -Principal $pri -Settings $set -Force | Out-Null
  Set-Content -Path (Join-Path $Src 'setup.ok') -Value 'ok'
} catch { Set-Content -Path $log -Value ($_ | Out-String) }
"""


def task_installed() -> bool:
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", TASK], capture_output=True, text=True, timeout=20,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0 or not (PROTECTED / "runner.ps1").exists():
            return False
        return (PROTECTED / "runner_version.txt").read_text(encoding="utf-8-sig").strip() == RUNNER_VERSION
    except Exception:
        return False


def pick_adapter() -> str:
    """openconnect's Wintun support is experimental: its vpnc-script cannot set the
    address on a fresh Wintun adapter ("did not complete within 10 seconds",
    netsh error 123 -- vpnc-scripts#30). A TAP-Windows adapter works; use a free one
    (not another VPN product's), else fall back to Wintun."""
    try:
        out = subprocess.check_output(
            ["wmic", "nic", "where", "NetConnectionID is not null", "get", "NetConnectionID,Description", "/format:csv"],
            text=True, errors="ignore", timeout=15, creationflags=_NO_WINDOW)
        import csv
        import io
        rows = csv.DictReader(io.StringIO("\n".join(l for l in out.splitlines() if l.strip())))
        for r in rows:
            desc = (r.get("Description") or "").lower()
            name = r.get("NetConnectionID") or ""
            if desc.startswith("tap-windows adapter") and re.fullmatch(r"[A-Za-z0-9 ._()-]{1,64}", name):
                return name
    except Exception:
        pass
    return IFNAME


def _allowed_host() -> str:
    try:
        return (PROTECTED / "allowed_host.txt").read_text(encoding="utf-8-sig").strip().lower()
    except OSError:
        return ""


_setup_attempted = False


def install_task(exe: Path, gateway_host: str) -> tuple[bool, str]:
    """The one elevated step: protected runner + on-demand scheduled task.
    At most once per session: a setup that did not take must not prompt again."""
    global _setup_attempted
    if _setup_attempted:
        return False, "The one-time VPN setup already ran this session and did not complete; see setup.log."
    _setup_attempted = True
    import ctypes
    import shutil
    stage = RUN_DIR / "setup"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copytree(exe.parent, stage / "openconnect")
    (stage / "runner.ps1").write_text(_TASK_RUNNER, encoding="utf-8")
    (stage / "setup.ps1").write_text(_SETUP, encoding="utf-8")
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
    params = (f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{stage / "setup.ps1"}" '
              f'-Src "{stage}" -Prot "{PROTECTED}" -HostName "{gateway_host}" -User "{user}" -Version "{RUNNER_VERSION}"')
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", params, None, 0)
    if rc <= 32:
        return False, ("Windows admin permission was declined -- the one-time VPN setup was not installed."
                       if rc == 5 else f"Could not start the one-time VPN setup (code {rc}).")
    for _ in range(120):
        if (stage / "setup.ok").exists() and task_installed() and _allowed_host() == gateway_host:
            shutil.rmtree(stage, ignore_errors=True)
            return True, "installed"
        if (stage / "setup.log").exists():
            return False, "The one-time VPN setup failed: " + (stage / "setup.log").read_text(errors="ignore")[:300]
        time.sleep(1)
    return False, "The one-time VPN setup did not finish (120 s)."


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
    url = vals.get("CONNECT_URL") or cfg.vpn_gateway
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
    if not task_installed() or _allowed_host() != host:
        ok, why = install_task(exe, host)             # the one prompt, once per gateway
        if not ok:
            declined = "declined" in why
            last_error = why
            try:
                (RUN_DIR / "cookie.txt").unlink()
            except FileNotFoundError:
                pass
            return {"status": "error", "error": last_error}
    import json as _json
    (RUN_DIR / "params.json").write_text(_json.dumps({"proto": proto, "cert": vals["FINGERPRINT"], "url": url,
                                                      "ifname": pick_adapter()}), encoding="utf-8")
    r = subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True, text=True, timeout=30,
                       creationflags=_NO_WINDOW)
    if r.returncode == 0:
        return _await_tunnel()
    script = RUN_DIR / "tunnel_runner.ps1"
    script.write_text(_RUNNER, encoding="utf-8")
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
    return _await_tunnel()


def _await_tunnel() -> Dict[str, Any]:
    global last_error
    last_error = ""
    for _ in range(90):                               # adapter + routes take a while
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
