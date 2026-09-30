"""No control characters in source files.

Shell heredocs have turned regex word boundaries (backslash-b) into literal
backspace characters three times (pdf_ingest 2026-09-26, web_trail 2026-09-30):
the patterns still compile, they just silently never match.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAD = {chr(c) for c in range(32) if chr(c) not in "\t\n\r"}


def test_sources_have_no_control_characters():
    found = []
    for p in list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").glob("*.py")) + list((ROOT / "tests").glob("*.py")):
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if any(ch in BAD for ch in line):
                found.append(f"{p.relative_to(ROOT)}:{i}")
    assert not found, "control characters (a mangled escape?) at: " + ", ".join(found[:20])
