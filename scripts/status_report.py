"""Library-wide status report with deltas since the previous report.

Measures outputs, not flags: completeness, PDFs, enrichment writes, the PDF
engine and its per-source results, the institutional VPN, library health and
which background jobs are alive. Each run stores a snapshot next to refs.db
(status_snapshot.json) and reports the change since the last one.

Usage: python scripts/status_report.py [--json]
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase  # noqa: E402

cfg = get_config()
DB = Path(cfg.db_path).expanduser()
DATA = DB.parent
SNAP = DATA / "status_snapshot.json"


def q(c, sql, *a):
    try:
        return c.execute(sql, a).fetchone()[0] or 0
    except sqlite3.Error:
        return 0


def measure() -> dict:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    m = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    m["refs"] = q(c, "SELECT COUNT(*) FROM refs")
    m["complete"] = q(c, "SELECT COUNT(*) FROM refs WHERE " + RefDatabase.COMPLETE_SQL)
    m["with_pdf"] = q(c, "SELECT COUNT(*) FROM refs WHERE COALESCE(pdf_local,'')!='' OR COALESCE(pdf_drive_id,'')!=''")
    m["with_doi"] = q(c, "SELECT COUNT(*) FROM refs WHERE COALESCE(doi,'')!=''")
    m["duplicates_archived"] = q(c, "SELECT COUNT(*) FROM refs_duplicates")
    m["lossy_accepts"] = q(c, "SELECT COUNT(*) FROM lossy_scan2 WHERE result IN ('accept','doi','arxiv')")
    m["lossy_scanned"] = q(c, "SELECT COUNT(*) FROM lossy_scan2")
    m["queue_pending"] = q(c, "SELECT COUNT(*) FROM enrich_queue WHERE status='pending'")
    m["queue_done"] = q(c, "SELECT COUNT(*) FROM enrich_queue WHERE status='done'")
    c.close()
    try:
        m["sources_all"] = json.loads((DATA / "pdf_source_stats.json").read_text(encoding="utf-8"))
    except Exception:
        m["sources_all"] = {}
    try:
        h = json.loads((DATA / "health.json").read_text(encoding="utf-8"))
        m["health_failed"] = h.get("failed") or []
    except Exception:
        m["health_failed"] = ["(no health report yet)"]
    try:
        import httpx
        ip = httpx.get("https://api.ipify.org", timeout=10).text.strip()
        m["vpn"] = "connected (USP)" if ip.startswith("143.107.") else "NOT connected"
    except Exception:
        m["vpn"] = "unknown (offline?)"
    try:
        out = subprocess.run(["wmic", "process", "where", "name='pythonw.exe' or name='python.exe' or name='Mouseion.exe'",
                              "get", "commandline"], capture_output=True, text=True, timeout=30,
                             creationflags=0x08000000).stdout
        jobs = {"Mouseion app": "Mouseion.exe" in out}
        for name in ("resolve_lossy", "recover_isbn", "recover_ids_from_pdfs", "pdf_sweep", "egon_core"):
            jobs[name] = name in out
        m["jobs"] = jobs
    except Exception:
        m["jobs"] = {}
    return m


def fmt(n) -> str:
    return f"{n:,}"


def delta(now: dict, prev: dict, key: str) -> str:
    if key not in prev:
        return ""
    d = now[key] - prev[key]
    return f" ({'+' if d >= 0 else ''}{fmt(d)})"


def report(now: dict, prev: dict) -> str:
    hrs = ""
    if prev.get("at"):
        span = (datetime.fromisoformat(now["at"]) - datetime.fromisoformat(prev["at"])).total_seconds() / 3600
        hrs = f" -- changes over the last {span:.1f} h in brackets"
    L = [f"MOUSEION STATUS {now['at'][:16].replace('T', ' ')} UTC{hrs}"]
    pct = 100 * now["complete"] / max(1, now["refs"])
    ppct = 100 * now["with_pdf"] / max(1, now["refs"])
    L.append(f"Library: {fmt(now['refs'])} refs | complete {fmt(now['complete'])} = {pct:.2f}%{delta(now, prev, 'complete')}"
             f" | with PDF {fmt(now['with_pdf'])} = {ppct:.1f}%{delta(now, prev, 'with_pdf')}")
    L.append(f"Enrichment: title-fixer accepts {fmt(now['lossy_accepts'])}{delta(now, prev, 'lossy_accepts')} of "
             f"{fmt(now['lossy_scanned'])} scanned{delta(now, prev, 'lossy_scanned')} | queue done "
             f"{fmt(now['queue_done'])}{delta(now, prev, 'queue_done')}, pending {fmt(now['queue_pending'])}")
    srcs = now.get("sources_all") or {}
    psrc = prev.get("sources_all") or {}
    rows = []
    for name, d in sorted(srcs.items(), key=lambda kv: -kv[1].get("found", 0)):
        p = psrc.get(name, {"tried": 0, "found": 0})
        dt, df = d.get("tried", 0) - p.get("tried", 0), d.get("found", 0) - p.get("found", 0)
        rate = f"{100 * df / dt:.1f}%" if dt else "-"
        rows.append(f"  {name}: +{fmt(df)} of {fmt(dt)} tried ({rate}) | all time {fmt(d.get('found', 0))}/{fmt(d.get('tried', 0))}")
    L.append("PDF sources (this period):" if rows else "PDF sources: no runs recorded yet")
    L += rows
    L.append(f"VPN: {now['vpn']} | health: " + (", ".join(now['health_failed']) + " failing" if now['health_failed'] else "all OK"))
    jobs = now.get("jobs") or {}
    must = ("Mouseion app", "egon_core")      # always-on; the rest are rotation / on-demand jobs
    down = [k for k in must if not jobs.get(k)]
    L.append("Running: " + (", ".join(k for k, v in jobs.items() if v) or "nothing") +
             (" | DOWN: " + ", ".join(down) if down else ""))
    return "\n".join(L)


def main() -> None:
    now = measure()
    try:
        prev = json.loads(SNAP.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    text = report(now, prev)
    SNAP.write_text(json.dumps(now, indent=1), encoding="utf-8")
    print(json.dumps({"report": text, "now": now}) if "--json" in sys.argv else text)


if __name__ == "__main__":
    main()
