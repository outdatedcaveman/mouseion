"""Standing rule: Mouseion never opens a browser tab on its own.

Four fallbacks used to (one fired whenever the window took >8 s to paint).
A failed window now shows a native message box instead (__main__._window_failed).
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "mouseion"


def test_no_webbrowser_module_anywhere():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if re.search(r"^\s*(import webbrowser|from webbrowser)|webbrowser\.open",
                              p.read_text(encoding="utf-8", errors="replace"), re.M)]
    assert not offenders, f"browser-opening code is back in: {offenders}"


def test_no_os_startfile_on_urls():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if re.search(r"os\.startfile\(\s*f?[\"']https?://|start\s+https?://",
                              p.read_text(encoding="utf-8", errors="replace"))]
    assert not offenders, f"URL launch via the shell in: {offenders}"


def test_oauth_consent_only_on_explicit_request():
    """google_auth_oauthlib's run_local_server opens a browser tab: it must sit
    behind the `interactive` guard (only `mouseion drive-auth` passes it)."""
    for p in SRC.rglob("*.py"):
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"run_local_server\(", text):
            window = text[max(0, m.start() - 900):m.start()]
            assert "interactive" in window, f"unguarded OAuth browser flow in {p.relative_to(SRC)}"
