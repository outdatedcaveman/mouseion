"""Publisher gate: bot checks pause a host (shared state); pacing spaces requests."""
import asyncio
import time

from mouseion import publisher_gate as g


def test_bot_check_pages_are_recognised():
    assert g.looks_like_bot_check("<html><title>Just a moment...</title>")
    assert g.looks_like_bot_check("<title>Client Challenge</title>")
    assert not g.looks_like_bot_check("<html><title>Article page</title><meta name='citation_pdf_url'>")


def test_block_is_shared_and_expires(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "_state_file", lambda: tmp_path / "gate.json")
    assert not g.is_blocked("www.example-publisher.org")
    g.mark_blocked("www.example-publisher.org")
    assert g.is_blocked("www.example-publisher.org")
    assert "www.example-publisher.org" in g.status()


def test_pacing_spaces_one_host(monkeypatch):
    monkeypatch.setattr(g, "PACE_S", 0.3)
    g._next_slot.clear()

    async def two():
        t = time.time()
        await g.pace("h.example"); await g.pace("h.example")
        return time.time() - t
    assert asyncio.run(two()) >= 0.25


def test_backoff_escalates_and_resets(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "_state_file", lambda: tmp_path / "gate.json")
    g.mark_blocked("p.example")
    first = g.blocked_until("p.example") - time.time()
    g.mark_blocked("p.example")
    second = g.blocked_until("p.example") - time.time()
    assert 0.9 * 3600 < first < 1.1 * 3600 and second > 1.8 * 3600      # 1 h, then 2 h
    g.mark_ok("p.example")
    assert not g.is_blocked("p.example")
