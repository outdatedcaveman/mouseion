"""
Recompute the stored `completeness` column for every ref using the current
formula in models.Reference.completeness (abstract & citations are bonuses, never
penalties). Kept in sync with that property — change both together.

Also scopes the refs_au FTS trigger to FIRE ONLY on FTS-relevant columns, so a
completeness-only UPDATE (this script, pdf_local writes, etc.) no longer rebuilds
the search index for every row — a permanent speed win.

Safe: backs up the old values to completeness_bak_<date> first; only the
`completeness` column is touched.

Usage:  python recompute_completeness.py <path-to-refs.db>
"""
import sqlite3
import sys
import time

DB = sys.argv[1] if len(sys.argv) > 1 else None
if not DB:
    print("usage: recompute_completeness.py <refs.db>"); raise SystemExit(2)

# FTS-relevant columns: the refs_au trigger only needs to fire when one of these
# changes (they feed the refs_fts row). Everything else (completeness, pdf_local,
# year, volume, …) can be updated without rebuilding the FTS index.
SCOPE_COLS = ("title, abstract, authors, keywords, journal, "
              "doi, url, isbn, pmid, arxiv_id, publisher")

# MUST mirror models.Reference.completeness exactly. Abstract weight (0.12) is a
# CONSTANT — granted whether or not an abstract exists — so the new score can only
# be >= the old one (no already-complete reference is ever demoted).
_RED = "(CASE WHEN ref_type IN ('book','book-chapter','preprint')"
FORMULA = f"""ROUND(MIN(1.0,
   (CASE WHEN title IS NOT NULL AND title!='' THEN 0.22 ELSE 0 END)
 + (CASE WHEN authors IS NOT NULL AND authors!='[]' AND authors!='' THEN 0.15 ELSE 0 END)
 + (CASE WHEN year IS NOT NULL AND year!=0 THEN 0.10 ELSE 0 END)
 + (CASE WHEN (doi IS NOT NULL AND doi!='') OR (arxiv_id IS NOT NULL AND arxiv_id!='')
            OR (pmid IS NOT NULL AND pmid!='') OR (isbn IS NOT NULL AND isbn!='') THEN 0.15 ELSE 0 END)
 + (CASE WHEN (journal IS NOT NULL AND journal!='') OR (container_title IS NOT NULL AND container_title!='')
            OR (publisher IS NOT NULL AND publisher!='') THEN 0.08 ELSE 0 END)
 + (CASE WHEN (title IS NOT NULL AND title!='') OR (abstract IS NOT NULL AND abstract!='') THEN 0.12 ELSE 0 END)
 + (CASE WHEN volume IS NOT NULL AND volume!='' THEN {_RED} THEN 0.03 ELSE 0.05 END) ELSE 0 END)
 + (CASE WHEN issue IS NOT NULL AND issue!='' THEN {_RED} THEN 0.02 ELSE 0.04 END) ELSE 0 END)
 + (CASE WHEN (pages IS NOT NULL AND pages!='') OR (article_number IS NOT NULL AND article_number!='')
        THEN {_RED} THEN 0.04 ELSE 0.05 END) ELSE 0 END)
 + (CASE WHEN citation_count IS NOT NULL AND citation_count>0 THEN 0.04 ELSE 0 END)
), 4)"""

c = sqlite3.connect(DB, timeout=120)
c.execute("PRAGMA busy_timeout = 120000")

total = c.execute("SELECT COUNT(*) FROM refs").fetchone()[0]

# 1) Back up the ORIGINAL completeness values once (reversible). If a backup
#    already exists it holds the original values from the first run, so RESTORE
#    from it first — that makes this script idempotent (safe to re-run) and means
#    drops are always measured against the true original.
bak_exists = c.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='completeness_bak_20260609'"
).fetchone()
if not bak_exists:
    c.execute("CREATE TABLE completeness_bak_20260609 AS SELECT id, completeness, ref_type FROM refs")
    print(f"backed up {total:,} original completeness values -> completeness_bak_20260609")
else:
    print("original completeness backup already present (preserved — NOT overwritten)")
# Index so the drop-check JOIN below is O(n), not O(n^2).
c.execute("CREATE INDEX IF NOT EXISTS idx_cbak_id ON completeness_bak_20260609(id)")
# `before` = ORIGINAL complete count (from the backup), so the report is honest
# even on a re-run where the live column already holds a recomputed value.
before = c.execute("SELECT COUNT(*) FROM completeness_bak_20260609 WHERE completeness>=0.8").fetchone()[0]

# 2) Scope the FTS update trigger to FTS columns (so this UPDATE is cheap)
au = c.execute("SELECT sql FROM sqlite_master WHERE name='refs_au'").fetchone()[0]
if "AFTER UPDATE OF" not in au:
    scoped = au.replace("AFTER UPDATE ON refs", f"AFTER UPDATE OF {SCOPE_COLS} ON refs", 1)
    c.executescript("DROP TRIGGER refs_au;\n" + scoped)
    print("scoped refs_au trigger -> fires only on FTS columns")
else:
    print("refs_au already scoped")

# 3) Recompute
t = time.time()
c.execute(f"UPDATE refs SET completeness = {FORMULA}")
c.commit()
after = c.execute("SELECT COUNT(*) FROM refs WHERE completeness>=0.8").fetchone()[0]
print(f"recomputed {total:,} refs in {time.time()-t:.1f}s")
print(f"COMPLETE (>=0.80): {before:,} -> {after:,}   (+{after-before:,})")

# 4) Which types newly crossed into complete?
print("-- newly complete by type --")
for x in c.execute("""SELECT r.ref_type, COUNT(*) n FROM refs r
    JOIN completeness_bak_20260609 b ON b.id=r.id
    WHERE b.completeness<0.8 AND r.completeness>=0.8
    GROUP BY r.ref_type ORDER BY n DESC"""):
    print(f"   {x[0] or '(none)'}: {x[1]:,}")
# Anything that DROPPED (should be ~none)
drop = c.execute("""SELECT COUNT(*) FROM refs r JOIN completeness_bak_20260609 b ON b.id=r.id
    WHERE b.completeness>=0.8 AND r.completeness<0.8""").fetchone()[0]
print(f"-- refs that dropped below 0.80: {drop:,} (expected ~0)")
print(f"-- still <0.80: {total-after:,}")
c.close()
