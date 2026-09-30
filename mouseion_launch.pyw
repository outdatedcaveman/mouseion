"""Start Mouseion with NO console window (the Desktop shortcut runs this).

Why not `.venv\\Scripts\\pythonw.exe -m mouseion`: a uv/venv `pythonw.exe` is a small
trampoline that starts the base interpreter's console `python.exe` -- hidden when a job
is launched with a hidden window, but a visible terminal when started from a shortcut
(2026-09-30). So the shortcut targets the BASE interpreter's pythonw.exe (GUI subsystem:
no console can exist) and this file loads the project's environment itself.
"""
import os
import site
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
site.addsitedir(os.path.join(ROOT, ".venv", "Lib", "site-packages"))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.chdir(ROOT)

if os.environ.get("MOUSEION_LAUNCH_CHECK"):          # used to test the launcher without opening the app
    import mouseion.__main__  # noqa: F401
    import webview  # noqa: F401
    sys.exit(0)

from mouseion.__main__ import main  # noqa: E402

main()
