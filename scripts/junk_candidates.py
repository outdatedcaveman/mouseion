"""Entries whose stored title is not a work: candidates for the owner's review.

Nothing is changed here. A candidate is an incomplete entry whose title is
  * empty / a placeholder ('untitled', 'unknown', '[no title]')
  * a file name or a program's print header ('...pdf', '13-05.dvi', 'Microsoft Word - ...', 'ACDSee print job')
  * a page artefact ('Download Limit Exceeded', 'Just a moment', 'Access denied')
  * only a publisher's name ('McGraw Hill', 'Oxford University Press')
  * only digits and file tokens ('9912074 pdf')
  * unreadable: letter salad judged on CHARACTERS ('I T IImIIT', 'M11p T x11 lp A I') --
    never on English vocabulary: Polish, Turkish, Latin titles and names are real
A rich string is NOT junk: a series + volume ('LNCS 3796'), a JSTOR number, an Elsevier
PII, a journal-issue file name ('JMP1984V25N12') -- those get a "suggestion" instead.
A series + volume ('LNCS 3796') is NOT junk: it names a book (see mouseion.web_trail).

Writes a JSON list (default: junk_candidates.json next to refs.db) with the reason, whether
the entry has a PDF, and what the web search found for it (web_trail_scan), if it ran.

Usage: python scripts/junk_candidates.py [out.json]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from mouseion import pdf_ingest as PI  # noqa: E402
from mouseion import web_trail as W  # noqa: E402
from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase  # noqa: E402

PLACEHOLDER = re.compile(r"^\W*(untitled(-\d+)?|unknown|unkown|no title|\[no title\]|title|none|null|n/?a|test|document|"
                         r"doc\d*|scan\w*|image\d*|img\d*|new document|page \d+|cover|contents|index|front ?matter|"
                         r"title page|copyright|preface|table of contents)\W*$", re.I)
FILEISH = re.compile(r"(\.(pdf|dvi|ps|docx?|djvu|tex|pptx?|txt|rtf|odt|epub|mobi|html?)\b|^microsoft (word|powerpoint|excel)\b|"
                     r"\bprint job\b|^acdsee\b|^untitled-\d+|\bscanned (by|with)\b|^camscanner)", re.I)
ARTEFACT = re.compile(r"(download limit exceeded|just a moment|access denied|page not found|404 not found|"
                      r"sign in to|log ?in required|captcha|verify you are|this is pag\b|cookies? (policy|consent)|"
                      r"error \d{3}|forbidden|temporarily unavailable)", re.I)
PUBLISHERS = {"mcgraw hill", "mcgraw-hill", "springer", "springer verlag", "elsevier", "wiley", "john wiley sons",
              "routledge", "oxford university press", "cambridge university press", "princeton university press",
              "harvard university press", "mit press", "the mit press", "academic press", "pearson", "prentice hall",
              "addison wesley", "north holland", "kluwer", "birkhauser", "de gruyter", "taylor francis", "sage",
              "blackwell", "palgrave macmillan", "macmillan", "penguin", "dover", "university of chicago press"}


VOWELS = set("aeiouyAEIOUYàáâãäåèéêëìíîïòóôõöùúûüýÿæœøıАЕИОУЫЭЮЯаеиоуыэюяΑΕΗΙΟΥΩαεηιουω")


def gibberish_token(t: str) -> bool:
    """Letter salad, not language: judged on characters, never on English vocabulary
    (Polish, Turkish, Norwegian, Latin titles and names are real words)."""
    core = re.sub(r"[^\w]", "", t)
    if len(core) < 2:
        return False
    if re.search(r"\d", core) and re.search(r"[A-Za-z]", core) and not re.fullmatch(r"\d{1,4}(st|nd|rd|th|s)?|[A-Z]?\d+[a-z]?", core):
        return True                                   # 'Ehaobagzmackkhsiy4Nillipgi66', 'M11p', 'x11'
    if re.fullmatch(r"[0-9A-Fa-f]{6,}", core) and re.search(r"[A-Fa-f]", core):
        return True                                   # hex ids
    letters = [c for c in core if c.isalpha()]
    if len(letters) >= 4 and not any(c in VOWELS for c in letters):
        return True                                   # 'IImIIT', 'Ctn'
    if re.search(r"[bcdfghjklmnpqrstvwxz]{6,}", core.lower()):
        return True                                   # six consonants in a row
    if re.search(r"[a-z][A-Z]{2,}[a-z]", core):
        return True                                   # 'lpAIx' case salad
    return False


JOURNAL_FILE = re.compile(r"^(?P<j>[A-Z]{2,6})(?P<y>1[89]\d\d|20\d\d)V(?P<v>\d{1,3})N(?P<n>\d{1,3})\b")
JOURNAL_CODES = {"JMP": "Journal of Mathematical Physics"}
PII_TITLE = re.compile(r"^PII\s*:?\s*S?\s*(\d{4})\s*-?\s*(\d{3}[\dX])\s*\(?(\d{2})\)?\s*(\d{5})", re.I)


def suggestion(title: str, pdf: str) -> str:
    """A rich string that names something findable -- not junk."""
    t = (title or "").strip()
    stem = re.sub(r"\.(pdf|djvu)$", "", (pdf or "").split("\\")[-1], flags=re.I)
    for x in (t, stem):
        m = JOURNAL_FILE.match(x or "")
        if m:
            j = JOURNAL_CODES.get(m.group("j"), m.group("j"))
            return f"whole journal issue: {j} vol. {int(m.group('v'))} no. {int(m.group('n'))} ({m.group('y')})"
    m = PII_TITLE.match(t)
    if m:
        return f"Elsevier article id S{m.group(1)}-{m.group(2)}({m.group(3)}){m.group(4)} -> DOI lookup"
    for x in (t, stem):
        if re.fullmatch(r"\d{6,9}", x or ""):
            return f"JSTOR stable id {x} -> 10.2307/{x}"
    return ""


def reason(title: str, common: set, fam: Counter) -> str:
    t = (title or "").strip()
    n = W.norm(t)
    if not n:
        return "empty title"
    if W.understand(t).get("series"):
        return ""                                   # 'LNCS 3796' names a book
    if PLACEHOLDER.match(t):
        return "placeholder title"
    if ARTEFACT.search(t):
        return "web page / download artefact"
    if FILEISH.search(t):
        return "file name / print header as title"
    if n in PUBLISHERS:
        return "publisher name only"
    toks = n.split()
    if all(re.fullmatch(r"\d+|pdf|v\d+|doc|dvi|ps", x) for x in toks):
        return "digits only"
    raw = [x for x in re.split(r"\s+", t) if x]
    if len(raw) >= 2 and all(len(re.sub(r"\W", "", x)) <= 1 for x in raw):
        return "unreadable (single letters)"          # 'J H S', 'I T'
    bad = sum(gibberish_token(x) for x in raw)
    if raw and bad / len(raw) >= 0.4:
        return "unreadable (letter salad)"
    return ""


def main() -> None:
    cfg = get_config()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(cfg.db_path).expanduser().parent / "junk_candidates.json"
    conn = sqlite3.connect(f"file:{Path(cfg.db_path).expanduser()}?mode=ro", uri=True, timeout=120)
    vocab = PI.NameVocab(conn)
    try:
        trails = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT ref_id, result, url FROM web_trail_scan")}
    except sqlite3.Error:
        trails = {}
    rows = []
    for rid, title, authors, year, rtype, pl, pd, src in conn.execute(
            f"""SELECT id, title, authors, year, ref_type, pdf_local, pdf_drive_id, sources FROM refs
                WHERE NOT ({RefDatabase.COMPLETE_SQL})
                AND COALESCE(json_extract(extras, '$.junk_review.decision'), '') != 'keep'"""):
        why = reason(title, vocab.common, vocab.fam) if hasattr(vocab, "fam") else reason(title, vocab.common, Counter())
        if not why:
            continue
        try:
            au = ", ".join(a.get("family", "") for a in json.loads(authors or "[]")[:3])
        except Exception:
            au = ""
        try:
            srcs = ", ".join(sorted(json.loads(src or "{}").keys())[:3])
        except Exception:
            srcs = ""
        tr = trails.get(rid)
        rows.append({"id": rid, "title": (title or "")[:200], "authors": au, "year": year, "type": rtype,
                     "reason": why, "suggestion": suggestion(title, pl or ""), "pdf": (pl or "").split("\\")[-1][:120] if pl else ("drive" if pd else ""),
                     "imported_from": srcs, "web": (tr[0] if tr else "not searched yet"), "web_url": (tr[1] if tr else "")})
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{len(rows):,} candidates -> {out}")
    print(Counter(r["reason"] for r in rows).most_common())
    print("with a PDF:", sum(1 for r in rows if r["pdf"]))


if __name__ == "__main__":
    main()
