"""Test isolation: no test may touch the owner's real Mouseion data.

2026-09-25 audit: enrichment tests wrote their fake providers ("always_found_<id>")
into the LIVE ~/.local/share/mouseion/api_router.db, because the router's ledger
path derives from the real config's db_path. Every test session now runs against
a throwaway config file, library and ledger.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_user_data(tmp_path_factory):
    home = tmp_path_factory.mktemp("mouseion_home")
    mp = pytest.MonkeyPatch()
    mp.setenv("MOUSEION_DB_PATH", str(home / "refs.db"))
    from mouseion import config as cfg_mod
    mp.setattr(cfg_mod, "_CONFIG_PATH", home / "config.toml")
    cfg_mod._instance = None
    try:
        from mouseion import api_router
        mp.setattr(api_router, "_router", None)
        mp.setattr(api_router, "_default_db_path", lambda: home / "api_router.db")
    except Exception:
        pass
    yield home
    mp.undo()
    cfg_mod._instance = None

