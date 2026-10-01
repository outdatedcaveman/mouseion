"""Apply the owner's decisions from the junk-review page.

Input: the page's `decisions` collection saved as JSON files (ArtifactData list with
out_dir), one file per chunk: {"<ref id>": {"decision": "keep"|"fix"|"archive", "note", "at"}}.

  archive -> out of the library, recoverably (pdf_ingest.archive_refs, rule 'junk-review')
  keep    -> extras.junk_review = keep: never shown as a candidate again
  fix     -> the title the owner typed becomes the entry's title (a "Title / Author / Year"
             note fills author and year too), and the entry is searched again by web_trail

Every applied decision is logged in junk_review_applied (idempotent: re-running skips it).
A changed row is backed up in junk_review_bak_<date> first.

Usage: python scripts/apply_junk_decisions.py <decisions_dir> <dry|write>
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from mouseion import pdf_ingest as PI  # noqa: E402
from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase, _authors_json  # noqa: E402
from mouseion.models import Author  # noqa: E402

WRITE = len(sys.argv) > 2 and sys.argv[2] == "write"
STAMP = date.today().strftime("%Y%m%d")


def parse_note(note: str) -> dict:
    """'Title / Author[; Author] / Year' or just a title."""
    parts = [x.strip() for x in re.split(r"\s+/\s+|\s*\|\s*", note or "") if x.strip()]
    out = {}
    if parts:
        out["title"] = parts[0][:500]
    for x in parts[1:]:
        if re.fullmatch(r"(1[5-9]|20)\d\d", x):
            out["year"] = int(x)
        else:
            names = [n.strip() for n in re.split(r";|\band\b|&", x) if n.strip()]
            authors = []
            for n in names:
                if "," in n:
                    fam, giv = [y.strip() for y in n.split(",", 1)]
                else:
                    bits = n.split()
                    fam, giv = bits[-1], " ".join(bits[:-1])
                authors.append(Author(family=fam, given=giv))
            if authors:
                out["authors_json"] = _authors_json(authors)
    return out


def main() -> None:
    src = Path(sys.argv[1])
    decisions = {}
    for f in sorted(src.rglob("*.json")):
        body = json.loads(f.read_text(encoding="utf-8"))
        body = body.get("data", body) if isinstance(body, dict) else {}
        for rid, d in body.items():
            if isinstance(d, dict) and d.get("decision") in ("keep", "fix", "archive"):
                decisions[rid] = d
    conn = sqlite3.connect(str(Path(get_config().db_path).expanduser()), timeout=120, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS junk_review_applied (ref_id TEXT PRIMARY KEY, decision TEXT, note TEXT, "
                 "applied_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS junk_review_bak_{STAMP} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    done = {r[0]: r[1] for r in conn.execute("SELECT ref_id, decision FROM junk_review_applied")}
    db = RefDatabase()
    cols = [d[0] for d in conn.execute("SELECT * FROM refs LIMIT 0").description]
    stats: Counter = Counter()
    to_archive = []
    for rid, d in decisions.items():
        dec, note = d["decision"], (d.get("note") or "").strip()
        if done.get(rid) == dec and dec != "fix":
            stats["already"] += 1
            continue
        row = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
        if row is None:
            stats["gone"] += 1
            continue
        if dec == "archive":
            to_archive.append((rid, "owner's junk review"))
        else:
            if WRITE:
                conn.execute(f"INSERT OR IGNORE INTO junk_review_bak_{STAMP} VALUES (?,?)",
                             (rid, json.dumps(dict(zip(cols, row)), default=str)))
                ex = json.loads(dict(zip(cols, row)).get("extras") or "{}")
                ex["junk_review"] = {"decision": dec, "note": note, "at": d.get("at", "")}
                conn.execute("UPDATE refs SET extras=? WHERE id=?", (json.dumps(ex, ensure_ascii=False), rid))
                if dec == "fix" and note:
                    up = parse_note(note)
                    if up:
                        PI.apply_fill(db, rid, up)
                        conn.execute("DELETE FROM web_trail_scan WHERE ref_id=?", (rid,))
                        conn.execute("DELETE FROM lossy_scan2 WHERE ref_id=?", (rid,))
        stats[dec] += 1
        if WRITE and dec != "archive":
            conn.execute("INSERT OR REPLACE INTO junk_review_applied (ref_id, decision, note) VALUES (?,?,?)",
                         (rid, dec, note))
    if WRITE and to_archive:
        PI.archive_refs(conn, to_archive, "junk-review")
        for rid, _ in to_archive:
            conn.execute("INSERT OR REPLACE INTO junk_review_applied (ref_id, decision, note) VALUES (?,?,?)",
                         (rid, "archive", ""))
    print(f"{'applied' if WRITE else 'dry run'}: {dict(stats)} of {len(decisions)} decisions", flush=True)


if __name__ == "__main__":
    main()
