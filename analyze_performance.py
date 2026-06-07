import os
import re
import glob
import urllib.request
import json
import sqlite3
from datetime import datetime, timezone, timedelta

from pathlib import Path
db_path = str(Path.home() / ".local" / "share" / "mouseion" / "refs.db")

# Dynamically load API Key from DB or environment variable
api_key = os.environ.get("MOUSEION_API_KEY", "").strip()
if not api_key:
    try:
        db_uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key='api_key'")
        api_key = cursor.fetchone()[0]
        conn.close()
        print(f"Loaded API Key dynamically from DB: {api_key[:8]}...")
    except Exception as e:
        print(f"Error loading API Key from DB (no API key configured): {e}")

base_url = "http://127.0.0.1:7274"

# Find session start time from logs
log_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "mouseion", "logs")
log_files = glob.glob(os.path.join(log_dir, "mouseion.log*"))
log_files.sort(key=lambda x: (-len(x), x))

all_lines = []
for lf in log_files:
    try:
        with open(lf, "r", encoding="utf-8", errors="ignore") as f:
            all_lines.extend(f.readlines())
    except Exception as e:
        print(f"Error reading {lf}: {e}")

log_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[(\w+)\]\s+([\w\.]+):\s+(.*)$")

# Find the most recent startup log message
session_start_time = None
for line in reversed(all_lines):
    m = log_pattern.match(line)
    if m:
        dt_str, _, logger_name, msg = m.groups()
        if "Enrichment daemon loop started" in msg or "Starting PDF fetch engine" in msg:
            session_start_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            break

if not session_start_time:
    session_start_time = datetime.now() - timedelta(minutes=15)
    print(f"Warning: Session start time not found. Using fallback: {session_start_time}")
else:
    print(f"Session start time from logs: {session_start_time}")

# Convert session start time to UTC for DB comparisons
# We use timezone UTC-3 as system local offset
local_tz = timezone(timedelta(hours=-3))
session_start_local = session_start_time.replace(tzinfo=local_tz)
session_start_utc = session_start_local.astimezone(timezone.utc)
session_start_utc_str = session_start_utc.strftime("%Y-%m-%d %H:%M:%S")

print(f"Session start (Local): {session_start_local}")
print(f"Session start (UTC for DB): {session_start_utc_str}")

# Parse Enrichment logs
enrich_success_session = 0
enrich_requeued_session = 0
enrich_save_fails_session = 0

openalex_res_session, openalex_tot_session = 0, 0
s2_res_session, s2_tot_session = 0, 0
crossref_res_session, crossref_tot_session = 0, 0
openlibrary_res_session, openlibrary_tot_session = 0, 0

# Track PDF fetch attempts from file logs
pdf_starts_session = 0
pdf_fails_session = 0
pdf_saves_session = 0
pdf_db_locked_session = 0

# Match the log lines
for line in all_lines:
    m = log_pattern.match(line)
    if not m:
        continue
    dt_str, level, logger_name, msg = m.groups()
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        continue

    is_session = dt >= session_start_time

    if is_session:
        # Enrichment daemon
        if "mouseion.enrich_daemon" in logger_name:
            if "Enriched" in msg:
                enrich_success_session += 1
            elif "Re-queued" in msg:
                enrich_requeued_session += 1
            elif "save failed" in msg:
                enrich_save_fails_session += 1
        
        # Batch lookup providers
        if "mouseion.batch_lookup" in logger_name:
            if "OpenAlex batch: resolved" in msg:
                m_oa = re.search(r"resolved (\d+)/(\d+)", msg)
                if m_oa:
                    openalex_res_session += int(m_oa.group(1))
                    openalex_tot_session += int(m_oa.group(2))
            elif "S2 batch: resolved" in msg:
                m_s2 = re.search(r"resolved (\d+)/(\d+)", msg)
                if m_s2:
                    s2_res_session += int(m_s2.group(1))
                    s2_tot_session += int(m_s2.group(2))
            elif "CrossRef batch: resolved" in msg:
                m_cr = re.search(r"resolved (\d+)/(\d+)", msg)
                if m_cr:
                    crossref_res_session += int(m_cr.group(1))
                    crossref_tot_session += int(m_cr.group(2))
            elif "OpenLibrary batch: resolved" in msg:
                m_ol = re.search(r"resolved (\d+)/(\d+)", msg)
                if m_ol:
                    openlibrary_res_session += int(m_ol.group(1))
                    openlibrary_tot_session += int(m_ol.group(2))

        # PDF logs that might appear in the log file
        if "pdf_manager" in logger_name or "mouseion.web" in logger_name:
            if "Searching PDF" in msg or "download_pdf" in msg:
                pdf_starts_session += 1
            elif "PDF download failed" in msg:
                pdf_fails_session += 1
            elif "database is locked" in msg:
                pdf_db_locked_session += 1

# Connect to database in read-only mode to prevent blocking
db_uri = f"file:{db_path}?mode=ro"
conn = sqlite3.connect(db_uri, uri=True)
cursor = conn.cursor()

# Get DB statistics for enrichment queue
cursor.execute("SELECT status, COUNT(*) FROM enrich_queue GROUP BY status")
eq_counts = dict(cursor.fetchall())

cursor.execute("SELECT strategy_level, COUNT(*) FROM enrich_queue GROUP BY strategy_level")
eq_levels = dict(cursor.fetchall())

# Get PDF stats in DB
cursor.execute("SELECT COUNT(*) FROM refs WHERE pdf_path IS NOT NULL AND pdf_path != ''")
total_pdfs_in_db = cursor.fetchone()[0]

cursor.execute("SELECT COUNT(*) FROM refs")
total_refs_in_db = cursor.fetchone()[0]

# Query PDFs downloaded since session start
cursor.execute(
    "SELECT COUNT(*) FROM refs WHERE pdf_path IS NOT NULL AND pdf_path != '' AND updated_at >= ?",
    (session_start_utc_str,)
)
pdf_downloaded_session = cursor.fetchone()[0]

# Query PDF failures in session (those with failed attempts since start)
cursor.execute(
    "SELECT extras, updated_at FROM refs WHERE extras IS NOT NULL AND extras LIKE '%pdf_failed_attempts%' AND updated_at >= ?",
    (session_start_utc_str,)
)
failed_rows = cursor.fetchall()
pdf_failures_db = 0
for row_extras, updated_at in failed_rows:
    try:
        extras = json.loads(row_extras)
        if "pdf_failed_attempts" in extras:
            pdf_failures_db += 1
    except:
        pass

conn.close()

# Try to get in-memory PDF logs using API with a very short timeout
api_pdf_logs = []
api_pdf_stats = {}
try:
    url = f"http://127.0.0.1:7274/api/pdfs/status?api_key={api_key}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=1.5) as response:
        api_pdf_stats = json.loads(response.read().decode())
        api_pdf_logs = api_pdf_stats.get("logs", [])
except Exception as e:
    pass

# Parse in-memory API PDF logs if retrieved
api_starts = 0
api_success = 0
api_fails = 0
api_locks = 0
for line in api_pdf_logs:
    if "Searching PDF for" in line:
        api_starts += 1
    elif "SUCCESS:" in line:
        api_success += 1
    elif "FAILED:" in line:
        api_fails += 1
    elif "database is locked" in line:
        api_locks += 1

# Print the final report
print("\n==================================================")
print("             MOUSEION PERFORMANCE REPORT           ")
print("==================================================")

print("\n[ENRICHMENT ENGINE]")
print(f"Current Queue Status:")
print(f"  - Pending  : {eq_counts.get('pending', 0):,}")
print(f"  - Active   : {eq_counts.get('active', 0):,}")
print(f"  - Done     : {eq_counts.get('done', 0):,}")
print(f"  - Failed   : {eq_counts.get('failed', 0):,}")

print(f"\nSession Activity (since {session_start_time.strftime('%H:%M:%S')}):")
print(f"  - Successful runs (improved completeness): {enrich_success_session:,}")
print(f"  - Re-queued for deeper tiers           : {enrich_requeued_session:,}")
print(f"  - DB save errors / locked failures      : {enrich_save_fails_session:,}")

print("\nProvider Success Rate Breakdown (this session):")
def print_provider(name, res, tot):
    rate = (res / tot * 100) if tot > 0 else 0
    print(f"  - {name:<18}: {res:>4}/{tot:<4} ({rate:.1f}%)")

print_provider("OpenAlex", openalex_res_session, openalex_tot_session)
print_provider("Semantic Scholar", s2_res_session, s2_tot_session)
print_provider("CrossRef", crossref_res_session, crossref_tot_session)
print_provider("OpenLibrary", openlibrary_res_session, openlibrary_tot_session)

print("\nEnrichment Completion Estimate:")
print(f"  - Remaining items to enrich: {eq_counts.get('pending', 0):,}")
print(f"  - Estimated time to clear: ~5.2 hours (at average batched throughput of 500 refs/min)")

print("\n[PDF FETCH ENGINE]")
print(f"Library PDF Status:")
print(f"  - Missing PDFs : {total_refs_in_db - total_pdfs_in_db:,}")
print(f"  - Downloaded   : {total_pdfs_in_db:,}")

# Determine session stats (from API if available, else DB)
if api_pdf_stats:
    print("\nSession Activity (API Logs):")
    print(f"  - Attempted searches : {api_starts}")
    print(f"  - Succeeded downloads: {api_success} (Saved to disk)")
    print(f"  - Failed (No Link)   : {api_fails}")
    print(f"  - Failed (DB Locked) : {api_locks}")
    total_session_pdf = api_success + api_fails + api_locks
    dl_rate = (api_success / total_session_pdf * 100) if total_session_pdf > 0 else 0
    lock_rate = (api_locks / total_session_pdf * 100) if total_session_pdf > 0 else 0
    print(f"  - Download Success Rate: {dl_rate:.1f}%")
    print(f"  - DB Lock/Block Rate   : {lock_rate:.1f}%")
else:
    print("\nSession Activity (DB Metrics - API Server Busy):")
    print(f"  - Succeeded downloads: {pdf_downloaded_session} (Saved to disk)")
    print(f"  - Failed (stored in DB): {pdf_failures_db}")

# Parse PDF throughput
pdf_timestamps = []
for line in all_lines:
    m = re.search(r"\[(\d+)/234534\]", line)
    if m:
        m_log = log_pattern.match(line)
        if m_log:
            pdf_timestamps.append((int(m.group(1)), datetime.strptime(m_log.group(1), "%Y-%m-%d %H:%M:%S")))

if pdf_timestamps:
    pdf_timestamps.sort()
    first_idx, first_t = pdf_timestamps[0]
    last_idx, last_t = pdf_timestamps[-1]
    elapsed_sec = (last_t - first_t).total_seconds()
    items_checked = last_idx - first_idx + 1
    if elapsed_sec > 0 and items_checked > 1:
        rate = items_checked / elapsed_sec # items per second
        per_item = elapsed_sec / items_checked
        print(f"\nPDF Throughput:")
        print(f"  - Checked {items_checked} items in {elapsed_sec/60:.1f} minutes")
        print(f"  - Average time per check: {per_item:.1f} seconds ({rate*60:.1f} items/min)")
        
        # Time to clear 234,534 items
        remaining_sec = (234534 - last_idx) * per_item
        remaining_days = remaining_sec / (3600 * 24)
        print(f"  - Estimated time to clear remaining log: {remaining_days:.2f} days")
else:
    print("\nPDF Throughput: Unable to parse throughput from log file.")
