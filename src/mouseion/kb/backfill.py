"""Backfill project notes from the bibliographies the projects already keep.

Sources:
- a BibTeX file (Synesism `bibliography/references.bib`): each entry with a `note` field becomes one note,
  "how the project uses this source";
- a Markdown reference table (semplex `charter/REFERENCES.md`) with the columns
  Key | Reference | Level | Used in | Used for: each row becomes one note.

Each record is matched to the library by DOI, then arXiv id, then exact normalized title. Records that match
nothing are reported, not guessed: they are the list of sources the library lacks.

These notes say what a project uses a source for. They carry no quote, so they are stored with kind
'project-use' and never count as textual support for a claim. Rows at verification level MEMORY are skipped:
semplex's own rule is that a recalled source is never cited.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Optional

from mouseion.kb import search

ARXIV = re.compile(r"(?:arxiv[:\s/]*|abs/)(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
DOI = re.compile(r"\b(10\.\d{4,9}/[^\s,;}\"'<>|]+)", re.I)


def norm_title(s: str) -> str:
    s = re.sub(r"\\[`'^\"~=.uvHc]\{?(\w)\}?", r"\1", s or "")  # BibTeX accents: {\"o} -> o
    s = unicodedata.normalize("NFKD", re.sub(r"[{}\\]", "", s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def norm_doi(s: Optional[str]) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", s)
    return s.rstrip(".")


# --------------------------------------------------------------------------------------------- parsers


def _bib_fields(body: str) -> Dict[str, str]:
    """fields of one BibTeX entry body; values in braces (nested) or quotes."""
    out, i, n = {}, 0, len(body)
    while i < n:
        m = re.compile(r"\s*,?\s*([A-Za-z][\w-]*)\s*=\s*").match(body, i)
        if not m:
            break
        key, i = m.group(1).lower(), m.end()
        if i < n and body[i] == "{":
            depth, j = 0, i
            while j < n:
                depth += {"{": 1, "}": -1}.get(body[j], 0)
                if depth == 0:
                    break
                j += 1
            val, i = body[i + 1 : j], j + 1
        elif i < n and body[i] == '"':
            j = body.find('"', i + 1)
            j = n if j < 0 else j
            val, i = body[i + 1 : j], j + 1
        else:
            m2 = re.compile(r"[^,]*").match(body, i)
            val, i = m2.group(0), m2.end()
        out[key] = re.sub(r"\s+", " ", val).strip()
    return out


def parse_bibtex(text: str) -> List[dict]:
    recs = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", text):
        if m.group(1).lower() in ("comment", "string", "preamble"):
            continue
        start, depth, j = m.end(), 1, m.end()
        while j < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        f = _bib_fields(text[start : j - 1])
        arx = f.get("eprint") if "arxiv" in f.get("archiveprefix", "").lower() else None
        if not arx:
            am = ARXIV.search(f.get("url", "") + " " + f.get("journal", ""))
            arx = am.group(1) if am else None
        recs.append(
            {
                "key": m.group(2),
                "title": f.get("title", ""),
                "doi": norm_doi(f.get("doi")),
                "arxiv": arx,
                "use": f.get("note", ""),
            }
        )
    return recs


def parse_reference_table(md: str) -> List[dict]:
    """rows of a `| Key | Reference | Level | Used in | Used for |` table."""
    recs, cols = [], None
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            cols = None if not line.strip() else cols
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        low = [c.lower() for c in cells]
        if "reference" in low and "used for" in low:
            cols = {c: k for k, c in enumerate(low)}
            continue
        if not cols or set(line) <= set("|-: "):
            continue
        get = lambda name: cells[cols[name]] if cols.get(name, 99) < len(cells) else ""  # noqa: E731
        ref = get("reference")
        tm = re.search(r"[\"“]([^\"”]+)[\"”]", ref)
        am, dm = ARXIV.search(ref), DOI.search(ref)
        recs.append(
            {
                "key": get("key"),
                "title": tm.group(1) if tm else "",
                "doi": norm_doi(dm.group(1)) if dm else "",
                "arxiv": am.group(1) if am else None,
                "level": get("level"),
                "used_in": get("used in"),
                "use": get("used for"),
                "reference": ref,
            }
        )
    return recs


# --------------------------------------------------------------------------------------------- matching


class Matcher:
    def __init__(self, lib):
        cols = {r[1] for r in lib.execute("PRAGMA table_info(refs)")}
        self.doi, self.arxiv, self.title = {}, {}, {}
        sel = ["id", "title"] + [c for c in ("doi", "arxiv_id") if c in cols]
        for r in lib.execute(f"SELECT {', '.join(sel)} FROM refs"):
            r = dict(r)
            if r.get("doi"):
                self.doi.setdefault(norm_doi(r["doi"]), r["id"])
                am = re.search(r"arxiv\.(\d{4}\.\d{4,5})", r["doi"], re.I)
                if am:
                    self.arxiv.setdefault(am.group(1), r["id"])
            if r.get("arxiv_id"):
                self.arxiv.setdefault(re.sub(r"v\d+$", "", r["arxiv_id"].split(":")[-1]), r["id"])
            t = norm_title(r.get("title") or "")
            if len(t) >= 12:
                self.title.setdefault(t, r["id"])

    def match(self, rec: dict) -> tuple:
        if rec.get("doi") and rec["doi"] in self.doi:
            return self.doi[rec["doi"]], "doi"
        if rec.get("doi"):
            am = re.search(r"arxiv\.(\d{4}\.\d{4,5})", rec["doi"])
            if am and am.group(1) in self.arxiv:
                return self.arxiv[am.group(1)], "arxiv"
        if rec.get("arxiv") and rec["arxiv"] in self.arxiv:
            return self.arxiv[rec["arxiv"]], "arxiv"
        t = norm_title(rec.get("title", ""))
        if t and t in self.title:
            return self.title[t], "title"
        return None, None


# --------------------------------------------------------------------------------------------- backfill


def claim_for(project: str, rec: dict) -> str:
    c = f"{project} uses this source for: {rec['use']}"
    if rec.get("used_in"):
        c += f" (in {rec['used_in']})"
    if rec.get("level"):
        c += f" [verification level: {rec['level']}]"
    return c


def backfill(lib, kb, project: str, records: Iterable[dict], dry_run: bool = False) -> dict:
    m = Matcher(lib)
    rep: dict = {"project": project, "added": 0, "existing": 0, "skipped_memory": 0, "no_use": 0,
                 "matched_by": {}, "unmatched": []}
    for rec in records:
        if not rec.get("use"):
            rep["no_use"] += 1
            continue
        if rec.get("level", "").upper().startswith("MEMORY"):
            rep["skipped_memory"] += 1
            continue
        ref_id, how = m.match(rec)
        if not ref_id:
            rep["unmatched"].append(
                {k: rec[k] for k in ("key", "title", "doi", "arxiv") if rec.get(k)}
            )
            continue
        rep["matched_by"][how] = rep["matched_by"].get(how, 0) + 1
        claim = claim_for(project, rec)
        if kb.execute(
            "SELECT 1 FROM notes WHERE project=? AND ref_id=? AND claim=? AND status='active'",
            (project, ref_id, claim),
        ).fetchone():
            rep["existing"] += 1
            continue
        if not dry_run:
            search.add_note(lib, kb, project, claim, ref_id, kind="project-use", author="backfill")
        rep["added"] += 1
    return rep
