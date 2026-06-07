import sys
import os
import time
from pathlib import Path

# Reconfigure stdout/stderr to handle Unicode on Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Add src directory to path
src_dir = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(src_dir))

# Configure environment variables to use refs.db and SEMANTIC_SCHOLAR_API_KEY if present
os.environ["PYTHONPATH"] = str(src_dir)

from mouseion.db import RefDatabase
from mouseion.enrich_daemon import start as start_daemon, stop as stop_daemon, _daemon_thread
import logging

def get_log_file_path():
    log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mouseion" / "logs"
    return log_dir / "mouseion.log"

def tail_file(filepath, n_lines=15):
    try:
        if not os.path.exists(filepath):
            return ""
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            return "".join(lines[-n_lines:])
    except Exception as e:
        return f"Error reading log: {e}"

def main():
    print("=" * 60)
    print("            MOUSEION ENRICHMENT DAEMON RUNTIME TEST            ")
    print("=" * 60)

    db = RefDatabase()
    
    # 1. Reset any stale active items from previous runs
    print("Resetting stale active items...")
    reset_count = db.reset_stale_active()
    print(f"Reset {reset_count} stale items to pending.")

    # 2. Get starting stats
    stats = db.enrich_queue_stats()
    print("\nStarting Queue Stats:")
    for k, v in stats.items():
        if k != "active_items":
            print(f"  {k}: {v}")
    
    total_entries = stats["pending"] + stats["active"]
    print(f"\nTotal entries currently in queue to process: {total_entries}")
    if total_entries < 2000:
        print("Queue has fewer than 2000 entries. Enqueuing more incomplete references...")
        enqueued = db.enqueue_incomplete(threshold=0.85, skip_done=True)
        print(f"Enqueued {enqueued} references.")
        stats = db.enrich_queue_stats()
        print(f"New total pending: {stats['pending']}")
    else:
        print("Queue already has 2000+ entries. Ready to start.")

    # 3. Start daemon
    print("\nStarting enrichment daemon thread...")
    # Configure root logger to output to console and file
    from mouseion.__main__ import _setup_crash_logging
    _setup_crash_logging()
    start_daemon()
    
    print("Daemon started. Monitoring for 30 minutes...")
    print("=" * 60)

    start_time = time.time()
    test_duration = 30 * 60  # 30 minutes in seconds
    last_position = 0
    log_path = get_log_file_path()

    # Clear/rotate log at start so we only see new lines
    if os.path.exists(log_path):
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"--- TEST RUN START AT {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        except Exception:
            pass

    try:
        while True:
            elapsed = time.time() - start_time
            if elapsed >= test_duration:
                print("\n[TEST COMPLETED] 30 minutes have elapsed.")
                break

            # Print stats
            stats = db.enrich_queue_stats()
            mins, secs = divmod(int(elapsed), 60)
            print(f"\n[{mins:02d}:{secs:02d}] Queue: Pending={stats['pending']} | Active={stats['active']} | Done={stats['done']} | Failed={stats['failed']} | Total Attempts={stats['total_attempts']}")
            
            # Print recent log lines
            log_content = tail_file(log_path, 15)
            if log_content:
                print("-" * 40)
                print(log_content.strip())
                print("-" * 40)

            time.sleep(30)

    except KeyboardInterrupt:
        print("\n[TEST INTERRUPTED] Keyboard interrupt received.")

    finally:
        print("\nStopping enrichment daemon...")
        stop_daemon()
        if _daemon_thread:
            _daemon_thread.join(timeout=10)
        print("Daemon stopped.")
        
        # Print final stats
        stats = db.enrich_queue_stats()
        print("\nFinal Queue Stats:")
        for k, v in stats.items():
            if k != "active_items":
                print(f"  {k}: {v}")
        print("=" * 60)

if __name__ == "__main__":
    main()
