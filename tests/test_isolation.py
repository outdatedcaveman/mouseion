"""The conftest guard is active: tests never see the owner's real library or ledger."""
def test_isolation_is_active():
    from pathlib import Path

    from mouseion.api_router import _default_db_path
    from mouseion.config import get_config
    real = Path.home() / ".local" / "share" / "mouseion"
    assert real not in Path(get_config().db_path).parents
    assert real not in _default_db_path().parents
