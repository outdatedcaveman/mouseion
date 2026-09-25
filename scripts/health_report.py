"""Write the library health report (health.json next to refs.db). Run by Egon's rotation."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from mouseion.health import run  # noqa: E402

r = run()
print(json.dumps({"failed": r["failed"], "refs": r["refs"], "complete_pct": r["complete_pct"], "seconds": r["seconds"]}))
