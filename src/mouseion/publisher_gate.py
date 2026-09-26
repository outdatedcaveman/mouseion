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
# escalating back-off: 1 h after a first bot check, doubling while the host keeps
# challenging (2, 4, 8 ... capped), back to zero after a successful download
COOLDOWN_BASE_H = float(os.environ.get("MOUSEION_PUBLISHER_COOLDOWN_H", "1"))
COOLDOWN_MAX_H = float(os.environ.get("MOUSEION_PUBLISHER_COOLDOWN_MAX_H", "12"))

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


def _entry(state: dict, host: str) -> dict:
    v = state.get(host)
    if isinstance(v, (int, float)):            # old format: just a timestamp
        v = {"until": float(v), "strikes": 1}
    return v or {"until": 0.0, "strikes": 0}


def blocked_until(host: str) -> float:
    return float(_entry(_load(), host)["until"])


def is_blocked(host: str) -> bool:
    return blocked_until(host) > time.time()


def _save(state: dict) -> None:
    try:
        p = _state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        pass


def mark_ok(host: str) -> None:
    """A PDF came through: the host is fine again, forget its strikes."""
    with _lock:
        state = _load()
        if host in state:
            del state[host]
            _save(state)


def mark_blocked(host: str) -> None:
    with _lock:
        state = _load()
        e = _entry(state, host)
        strikes = int(e.get("strikes", 0)) + 1
        hours = min(COOLDOWN_MAX_H, COOLDOWN_BASE_H * 2 ** (strikes - 1))
        state[host] = {"until": time.time() + hours * 3600, "strikes": strikes}
        # keep strikes for a day after a pause ends, so a host that challenges again escalates
        state = {h: v for h, v in state.items() if _entry(state, h)["until"] > time.time() - 86400}
        try:
            p = _state_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(state, indent=1), encoding="utf-8")
        except OSError:
            pass


def looks_like_bot_check(text: str) -> bool:
    return bool(_BOT_CHECK.search(text[:20000]))


# Open repositories / archives are built for this traffic: a short spacing only.
REPOSITORY_HOSTS = ("pmc-oa-opendata.s3.amazonaws.com", "europepmc.org", "ncbi.nlm.nih.gov", "arxiv.org", "zenodo.org", "osf.io", "scielo",
                    "hal.science", "archives-ouvertes.fr", "core.ac.uk", "semanticscholar.org", "biorxiv.org",
                    "medrxiv.org", "researchgate.net", "philarchive.org", "philpapers.org", "ssrn.com",
                    "repositorio", "repository", "eprints", "dspace", "handle.net", "digital.library")
REPO_PACE_S = float(os.environ.get("MOUSEION_REPOSITORY_PACE_S", "1.5"))


def is_repository(host: str) -> bool:
    return any(k in host for k in REPOSITORY_HOSTS) or host.endswith(".edu") or ".edu." in host


async def pace(host: str) -> None:
    """Reserve this host's next slot; concurrent tasks queue up behind each other."""
    with _lock:
        now = time.time()
        slot = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = slot + (REPO_PACE_S if is_repository(host) else PACE_S)
    if slot > now:
        await asyncio.sleep(slot - now)


def status() -> dict:
    """Hosts currently paused, with hours left (for health / UI)."""
    now = time.time()
    st = _load()
    return {h: round((_entry(st, h)["until"] - now) / 3600, 1) for h in st if _entry(st, h)["until"] > now}
