"""PDF misses are remembered per network: the institution's tunnel gets its own ledger key."""
from mouseion import pdf_manager, vpn_manager
from mouseion.config import get_config


def test_ledger_key_follows_the_tunnel(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(cfg, "vpn_enabled", True)
    monkeypatch.setattr(cfg, "vpn_gateway", "gw.example:443")
    monkeypatch.setattr(vpn_manager, "vpn_adapter_up", lambda: None)
    pdf_manager._LEDGER_KEY_CACHE[:] = [0.0, "pdf"]
    assert not pdf_manager._pdf_ledger_key().startswith("pdf_inst")
    monkeypatch.setattr(vpn_manager, "vpn_adapter_up", lambda: "openconnect-mouseion")
    pdf_manager._LEDGER_KEY_CACHE[:] = [0.0, "pdf"]
    assert pdf_manager._pdf_ledger_key().startswith("pdf_inst")
    assert pdf_manager._pdf_ledger_key().endswith(pdf_manager.FETCHER_VERSION)   # improvements re-try old misses
