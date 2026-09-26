"""Take Brazilian course material out of the library (the owner's call).

Exercise lists, exams, lecture slides/notes, syllabi, assignments and university
notices -- see mouseion.pdf_ingest.course_material / archive_course_material.
Nothing is destroyed: rows move to refs_duplicates (archive_rule 'course-material'),
tags to refs_removed_tags, PDF files stay. New PDFs are filtered at ingest by the
same rule, and scripts/ingest_folder.py runs this sweep at the end of each run.

Usage: python scripts/archive_course_material.py [dry|write]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from mouseion.config import get_config  # noqa: E402
from mouseion.pdf_ingest import archive_course_material  # noqa: E402

if __name__ == "__main__":
    write = len(sys.argv) > 1 and sys.argv[1] == "write"
    conn = sqlite3.connect(str(Path(get_config().db_path).expanduser()), timeout=120, isolation_level=None)
    hits = archive_course_material(conn, write)
    for _rid, m, t in hits:
        print(f"  {m[:24]:24s} | {t[:90]}")
    print(f"{len(hits)} course-material entries {'archived' if write else '(dry run)'}")
