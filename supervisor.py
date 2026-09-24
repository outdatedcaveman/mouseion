"""
Mouseion continuous unattended orchestrator — "just leave it running".

Loops forever:
  1. DOI-recovery pass — crossref/OpenAlex *bibliographic* search (router-metered)
     over the no-identifier, sub-0.80 tail, net-positive merged (never clobbers).
     Every recovered DOI both raises completeness AND becomes a fetchable PDF target.
  2. Trigger the app's PDF fetch-all — sweeps everything still missing a PDF,
     including the DOIs just recovered (the VPN resolves the IP-authenticated ones).
  3. Sleep, repeat. When the tail stops shrinking it backs off to a slow
     maintenance cadence so it isn't busy-spinning.

Runs ALONGSIDE a running Mouseion (it drives PDF over the local API). The master
API router coordinates the recovery's calls with the app's enrichment/PDF calls,
so the combined load still can't trip a rate-limit/ban.

Usage:  python supervisor.py            # leave it running
        BATCH=2000 python supervisor.py # smaller recovery passes
"""
from __future__ import annotations
import logging
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
DB = os.path.join(os.path.expanduser("~"), ".local", "share", "mouseion", "refs.db")
PORT = int(os.environ.get("PORT", "7274"))
BATCH = os.environ.get("BATCH", "100000")          # default: whole tail in one pass
RECOVERY_TIMEOUT = int(os.environ.get("RECOVERY_TIMEOUT", str(4 * 3600)))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [SUPERVISOR] %(message)s")
log = logging.getLogger("supervisor")


def _scalar(sql: str):
    c = sqlite3.connect(DB, timeout=60)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


def tail_remaining() -> int:
    """No-identifier, sub-0.80 refs with enough signal for a bibliographic match."""
    return _scalar(
        "SELECT COUNT(*) FROM refs WHERE completeness < 0.8 "
        "AND (doi IS NULL OR doi='') AND (arxiv_id IS NULL OR arxiv_id='') "
        "AND title IS NOT NULL AND LENGTH(title) > 10 "
        "AND authors IS NOT NULL AND authors != '[]'")


def pdf_local() -> int:
    return _scalar("SELECT COUNT(*) FROM refs WHERE pdf_local IS NOT NULL AND pdf_local!=''")


def trigger_pdf() -> None:
    try:
        key = _scalar("SELECT value FROM settings WHERE key='api_key'")
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/pdfs/fetch-all",
            method="POST", data=b"{}",
            headers={"X-API-Key": key, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20)
        log.info("PDF fetch-all triggered")
    except Exception as e:
        log.warning("PDF trigger failed (app down?) — will retry next cycle: %s", e)


def main() -> None:
    env = dict(os.environ, PYTHONPATH=os.path.join(REPO, "src"))
    quiet = 0
    cycle = 0
    log.info("orchestrator up. repo=%s db=%s port=%d batch=%s", REPO, DB, PORT, BATCH)
    while True:
        cycle += 1
        before, pdf0 = tail_remaining(), pdf_local()
        log.info("cycle %d — %d no-id incomplete refs to recover; %d PDFs so far",
                 cycle, before, pdf0)
        try:
            subprocess.run([PY, os.path.join(REPO, "recover_hardtail.py"), BATCH, "write", "12"],
                           env=env, cwd=REPO, timeout=RECOVERY_TIMEOUT,
                           # never pop a console window (global rule: no stray shells)
                           creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0))
        except subprocess.TimeoutExpired:
            log.warning("recovery pass hit the %ss timeout — continuing", RECOVERY_TIMEOUT)
        except Exception as e:
            log.warning("recovery pass error: %s", e)
        after = tail_remaining()
        found = max(0, before - after)
        log.info("cycle %d — recovered ~%d DOIs (%d remain)", cycle, found, after)

        trigger_pdf()

        quiet = quiet + 1 if found < 25 else 0
        if quiet >= 2:
            log.info("tail no longer shrinking — maintenance mode (recheck in 6h)")
            time.sleep(6 * 3600)
        else:
            time.sleep(20 * 60)  # breather so PDF + enrichment get bandwidth


if __name__ == "__main__":
    main()
