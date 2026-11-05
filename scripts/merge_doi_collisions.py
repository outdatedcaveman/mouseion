"""Merge the duplicate pairs that a verified DOI exposed (table doi_collisions).

db.replace_ref records a pair whenever a verified DOI already belongs to another row
(grounded_meta's arXiv pass finds about one in six). This merges the guarded pairs into
the DOI holder: titles must agree strictly, years within 3, no book vs chapter/article,
no author conflict, no venue-name titles. Nothing is deleted: merged-away rows go to
refs_duplicates (duplicate_of = keeper), keepers' prior rows to refs_dedup_keep_bak.
First run 2026-11-05: 2,221 merged of 2,731 candidates; hand sample 49-50/50 correct.

Usage: python scripts/merge_doi_collisions.py <dry|write> [max_merges]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mouseion.maintenance_dedup import run_doi_collision_merge  # noqa: E402


def main() -> None:
    write = len(sys.argv) > 1 and sys.argv[1] == "write"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    r = run_doi_collision_merge(dry_run=not write, max_merges=limit)
    r.pop("samples", None)
    st = r.get("stats", {})
    # the line Egon's rotation reads: improved / checked / rejected
    print(json.dumps({"updated": r.get("merged", 0), "checked": st.get("candidates", 0),
                      "rejected": sum(v for k, v in st.items() if k.startswith("veto_")),
                      "dry_run": not write, "report": r.get("report_path"), **st}), flush=True)


if __name__ == "__main__":
    main()
