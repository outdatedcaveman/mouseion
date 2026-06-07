"""Head-to-head test of Tier 3 strategies on a REAL sample.

Compares:
  A) NEW (mine):  concurrent fan-out crossref+openalex+s2
  B) OLD (other AI): serial S2-first (conc=2), then crossref+openalex fallback

Measures for each: wall time, resolution rate, and 429 count (the thing the
OLD version was trying to minimize). Read-only — does NOT write to the DB.
"""
import asyncio
import os
import sqlite3
import sys
import time
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# Count 429s by intercepting the warning logger the providers use
class _429Counter(logging.Handler):
    def __init__(self):
        super().__init__()
        self.count = 0
    def emit(self, record):
        msg = record.getMessage().lower()
        if "429" in msg or "too many requests" in msg or "cooldown" in msg or "rate" in msg:
            self.count += 1

counter = _429Counter()
logging.getLogger().addHandler(counter)
logging.getLogger().setLevel(logging.WARNING)

from mouseion.models import Reference  # noqa: E402
from mouseion import enrich_daemon as ed  # noqa: E402

SAMPLE = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def load_sample(n, seed_offset=0):
    db = os.path.expanduser("~/.local/share/mouseion/refs.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    # Tier 3 = pending, has title+year, no DOI/URL (matches tier-3 WHERE clause)
    rows = conn.execute(
        """SELECT r.id, r.title, r.year, r.completeness
           FROM enrich_queue eq JOIN refs r ON r.id = eq.ref_id
           WHERE eq.status='pending'
             AND r.title IS NOT NULL AND LENGTH(r.title) > 10
             AND r.year IS NOT NULL
             AND (r.doi IS NULL OR r.doi='')
             AND (r.url IS NULL OR r.url='')
           ORDER BY r.id LIMIT ? OFFSET ?""",
        (n, seed_offset),
    ).fetchall()
    conn.close()
    refs, rowdicts = [], []
    for row in rows:
        ref = Reference(title=row["title"] or "", year=row["year"])
        ref.completeness = row["completeness"] or 0.0
        refs.append(ref)
        rowdicts.append({"ref_id": row["id"]})
    return refs, rowdicts


def resolution(results, refs):
    """Count refs that meaningfully improved (the daemon's own +0.05 bar)."""
    improved = 0
    for ref, rd in zip(refs, []):  # placeholder
        pass
    n = 0
    for batch_id, enr in results.items():
        if enr and (enr.completeness or 0) > 0.0:
            n += 1
    return n


# ---- OLD strategy reconstructed exactly from the reverted code ----
def old_tier3(refs, rows):
    usable = []
    for ref, row in zip(refs, rows):
        ref._batch_id = row["ref_id"]
        ed._salvage_from_title(ref)
        ed._clean_title(ref)
        if ref.title:
            usable.append(ref)
    if not usable:
        return {}
    s2 = ed._enrich_many_concurrent(usable, ["semantic_scholar"], concurrency=2)
    results, missed = {}, []
    for ref, enr in zip(usable, s2):
        if enr.completeness > (ref.completeness or 0) + 0.05:
            results[ref._batch_id] = enr
        else:
            missed.append(ref)
    if missed:
        me = ed._enrich_many_concurrent(missed, ["crossref", "openalex"], concurrency=6)
        for ref, enr in zip(missed, me):
            results[ref._batch_id] = enr
    return results


def improved_count(results, refs):
    by_id = {r._batch_id: r for r in refs if hasattr(r, "_batch_id")}
    n = 0
    for bid, enr in results.items():
        base = by_id.get(bid)
        base_c = (base.completeness or 0) if base else 0
        if enr and (enr.completeness or 0) > base_c + 0.05:
            n += 1
    return n


def run(label, fn, sample, offset):
    counter.count = 0
    refs, rows = load_sample(sample, offset)
    base_total = sum((r.completeness or 0) for r in refs)
    t0 = time.monotonic()
    results = fn(refs, rows)
    dt = time.monotonic() - t0
    imp = improved_count(results, refs)
    after_total = sum((results.get(r._batch_id, r).completeness or 0) for r in refs if hasattr(r, "_batch_id"))
    print(f"\n  {label}")
    print(f"    wall:        {dt:6.1f}s   ({sample/dt:.2f} refs/s)")
    print(f"    resolved:    {imp}/{sample}  ({100*imp/sample:.0f}%)")
    print(f"    completeness:{base_total/sample*100:5.0f}% -> {after_total/sample*100:.0f}% avg")
    print(f"    429/cooldown warnings: {counter.count}")
    return dt, imp, counter.count


def main():
    print(f"Tier 3 head-to-head | sample={SAMPLE} real pending refs each (disjoint offsets)")
    # Disjoint samples so neither benefits from the other's cache warming
    run("B) OLD serial S2-first (other AI's version)", old_tier3, SAMPLE, 0)
    time.sleep(5)  # let any cooldown settle between runs
    run("A) NEW concurrent fan-out (my revert)", ed._handle_tier3_batch, SAMPLE, SAMPLE)


if __name__ == "__main__":
    main()
