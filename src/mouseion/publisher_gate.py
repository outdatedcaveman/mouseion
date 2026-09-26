"""Polite, license-respecting access to publisher hosts (2026-09-26).

Over the institutional VPN most publishers serve subscribed PDFs -- to a person.
A burst of machine requests gets a bot check instead ("Just a moment...",
"Client Challenge"): Springer served a PDF, then after a sweep of requests
challenged every one. Mouseion does NOT try to get past such checks; it treats
them as the publisher saying "not like this":

  * pacing  -- at most one PDF request per publisher host every PACE_S seconds;
  * back-off -- a bot check pauses that host for COOLDOWN_H hours, shared by
    every Mouseion process through a small state file;
  * no false misses -- a blocked reference is not marked "tried", so a
    sanctioned channel (publisher text-mining API, later retry) still gets it.

Publisher licences forbid systematic downloading; hammering a host can get the
whole institution cut off. These limits are deliberately conservative.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

PACE_S = float(os.environ.get("MOUSEION_PUBLISHER_PACE_S", "15"))
COOLDOWN_H = float(os.environ.get("MOUSEION_PUBLISHER_COOLDOWN_H", "12"))

_BOT_CHECK = re.compile(r"<title>\s*(Just a moment|Client Challenge|Attention Required|Access Denied)|"
                        r"challenge-platform|cf-chl-|/cdn-cgi/challenge|captcha-delivery|px-captcha", re.I)

_lock = threading.Lock()
_next_slot: dict[str, float] = {}


class PublisherBlocked(Exception):
    """The host answered with a bot check, or is in its cool-down."""


def _state_file() -> Path:
    try:
        from .config import get_config
        return Path(get_config().db_path).expanduser().parent / "publisher_gate.json"
    except Exception:
        return Path.home() / ".local" / "share" / "mouseion" / "publisher_gate.json"


def _load() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def blocked_until(host: str) -> float:
    return float(_load().get(host, 0))


def is_blocked(host: str) -> bool:
    return blocked_until(host) > time.time()


def mark_blocked(host: str) -> None:
    with _lock:
        state = _load()
        state[host] = time.time() + COOLDOWN_H * 3600
        state = {h: t for h, t in state.items() if t > time.time()}
        try:
            p = _state_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(state, indent=1), encoding="utf-8")
        except OSError:
            pass


def looks_like_bot_check(text: str) -> bool:
    return bool(_BOT_CHECK.search(text[:20000]))


async def pace(host: str) -> None:
    """Reserve this host's next slot; concurrent tasks queue up behind each other."""
    with _lock:
        now = time.time()
        slot = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = slot + PACE_S
    if slot > now:
        await asyncio.sleep(slot - now)


def status() -> dict:
    """Hosts currently paused, with hours left (for health / UI)."""
    now = time.time()
    return {h: round((t - now) / 3600, 1) for h, t in _load().items() if t > now}
