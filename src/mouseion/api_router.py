"""
Master API Router — the single authoritative gateway for EVERY outbound API
call in Mouseion (enrichment providers AND PDF sources AND web search).

Why this exists
---------------
Before this, three different subsystems metered themselves independently:
  * enrichment providers  -> quota.py + network_budget.py
  * batch_lookup          -> its own qm.acquire() calls
  * pdf_manager           -> ad-hoc asyncio.Lock + min_interval (UNMETERED by quota)
…and the quota counters lived only in memory, so every rebuild/relaunch reset
them. The result was uncoordinated hammering and daily-quota exhaustion across
every service, plus endless re-trying of the same doomed entries.

What this provides
------------------
1. ONE `acquire(api)` gate that every call site awaits. It enforces per-API
   rate limits (per-minute sliding window) AND per-day budgets, and honours
   adaptive cooldowns derived from real 429/403/5xx responses.
2. PERSISTENT state in a dedicated side-database (`api_router.db`, separate from
   refs.db so it never causes lock contention). Daily budgets and cooldowns
   survive restarts — a relaunch can no longer "forget" that S2's day is spent.
3. A per-entry/per-API ATTEMPT LEDGER: `was_tried(ref_id, api, entry_hash)`.
   If an entry was already tried against an API and returned nothing, it is
   NEVER tried again with that API unless the entry's content changed (its hash
   differs). This kills the constant-retry churn.
4. Live per-API status (`status()`), so the UI and the records ledger can show
   exactly what every service is doing.

All writes are batched and flushed on a background cadence to keep the hot path
fast; `flush()` is also safe to call directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Per-API limits.  Conservative, below each service's published ceiling.
# requests_per_min  : sliding-window cap (short term, in-memory)
# requests_per_day  : hard daily budget (PERSISTED — the thing that was dying)
# min_interval      : minimum spacing between two calls to the same API
# ---------------------------------------------------------------------------

@dataclass
class Limit:
    per_min: int
    per_day: int
    min_interval: float


# Defaults. semantic_scholar/openalex/crossref get upgraded at runtime if a
# key/email is configured (see _apply_credentials).
_DEFAULT_LIMITS: Dict[str, Limit] = {
    # --- enrichment metadata ---
    "crossref":         Limit(per_min=300, per_day=80_000,  min_interval=0.10),
    "openalex":         Limit(per_min=300, per_day=90_000,  min_interval=0.12),
    "semantic_scholar": Limit(per_min=55,  per_day=9_000,   min_interval=1.05),
    "pubmed":           Limit(per_min=150, per_day=80_000,  min_interval=0.12),
    "doi_org":          Limit(per_min=120, per_day=50_000,  min_interval=0.10),
    "dblp":             Limit(per_min=60,  per_day=10_000,  min_interval=0.5),
    "arxiv":            Limit(per_min=30,  per_day=20_000,  min_interval=0.3),
    "arxiv_api":        Limit(per_min=30,  per_day=20_000,  min_interval=0.3),
    "openlibrary":      Limit(per_min=60,  per_day=30_000,  min_interval=0.2),
    "google_books":     Limit(per_min=60,  per_day=10_000,  min_interval=0.2),
    # --- PDF / OA discovery (these were the silent quota killers) ---
    "unpaywall":        Limit(per_min=60,  per_day=90_000,  min_interval=0.15),
    "core":             Limit(per_min=6,   per_day=900,     min_interval=2.0),
    "scihub":           Limit(per_min=10,  per_day=5_000,   min_interval=3.0),
    "annas":            Limit(per_min=10,  per_day=5_000,   min_interval=3.0),
    "pdf_host":         Limit(per_min=120, per_day=200_000, min_interval=0.0),
    # --- web search (gray) ---
    "duckduckgo":       Limit(per_min=30,  per_day=20_000,  min_interval=1.0),
    "web_scrape":       Limit(per_min=120, per_day=200_000, min_interval=0.0),
}


@dataclass
class _ApiState:
    limit: Limit
    recent: Deque[float] = field(default_factory=deque)  # in-memory minute window
    day: str = ""               # YYYY-MM-DD of day_count
    day_count: int = 0          # PERSISTED daily tally
    last_call: float = 0.0
    cooldown_until: float = 0.0  # PERSISTED — survives restart
    consec_errors: int = 0
    total_ok: int = 0
    total_fail: int = 0


def _today() -> str:
    # localtime day boundary; time.time avoids the workflow Date restrictions.
    return time.strftime("%Y-%m-%d", time.localtime())


class APIRouter:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            db_path = _default_db_path()
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._states: Dict[str, _ApiState] = {}
        self._dirty_budgets = False
        self._pending_attempts: list[tuple] = []
        self._last_flush = 0.0
        self._init_db()
        self._load_state()
        self._apply_credentials()

    # ------------------------------------------------------------------ DB
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def _init_db(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS api_budget (
                api TEXT PRIMARY KEY,
                day TEXT, day_count INTEGER DEFAULT 0,
                cooldown_until REAL DEFAULT 0,
                consec_errors INTEGER DEFAULT 0,
                total_ok INTEGER DEFAULT 0, total_fail INTEGER DEFAULT 0
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS attempt_ledger (
                ref_id TEXT NOT NULL,
                api TEXT NOT NULL,
                entry_hash TEXT NOT NULL,
                result TEXT NOT NULL,        -- 'hit' | 'miss' | 'error'
                ts REAL NOT NULL,
                PRIMARY KEY (ref_id, api)
            )""")
            c.commit()

    def _load_state(self) -> None:
        with self._conn() as c:
            rows = c.execute(
                "SELECT api, day, day_count, cooldown_until, consec_errors, total_ok, total_fail FROM api_budget"
            ).fetchall()
        today = _today()
        for api, day, day_count, cd, ce, ok, fail in rows:
            lim = _DEFAULT_LIMITS.get(api, Limit(60, 10_000, 0.2))
            st = _ApiState(limit=lim)
            st.day = day or today
            st.day_count = day_count if day == today else 0  # reset if a new day began
            st.cooldown_until = cd or 0.0
            st.consec_errors = ce or 0
            st.total_ok = ok or 0
            st.total_fail = fail or 0
            self._states[api] = st

    def _state(self, api: str) -> _ApiState:
        st = self._states.get(api)
        if st is None:
            st = _ApiState(limit=_DEFAULT_LIMITS.get(api, Limit(60, 10_000, 0.2)))
            st.day = _today()
            self._states[api] = st
        return st

    def _apply_credentials(self) -> None:
        """Upgrade limits when a key/email is present (matches provider tiers)."""
        try:
            from .config import get_config
            cfg = get_config()
        except Exception:
            return
        if getattr(cfg, "semantic_scholar_api_key", ""):
            self._state("semantic_scholar").limit = Limit(60, 40_000, 1.0)
        if getattr(cfg, "openalex_email", "") or getattr(cfg, "openalex_api_key", ""):
            self._state("openalex").limit = Limit(540, 100_000, 0.11)
        if getattr(cfg, "crossref_email", "") or getattr(cfg, "openalex_email", ""):
            self._state("crossref").limit = Limit(540, 100_000, 0.11)

    # -------------------------------------------------------------- acquire
    def _wait_seconds(self, st: _ApiState, now: float) -> float:
        # cooldown from prior 429/error
        if st.cooldown_until > now:
            return st.cooldown_until - now
        # daily budget
        if st.day != _today():
            st.day, st.day_count = _today(), 0
            self._dirty_budgets = True
        if st.day_count >= st.limit.per_day:
            # exhausted for the day: wait until local midnight
            tomorrow = time.mktime(time.strptime(_today(), "%Y-%m-%d")) + 86400
            return max(60.0, tomorrow - now)
        # per-minute sliding window
        while st.recent and st.recent[0] < now - 60:
            st.recent.popleft()
        waits = []
        if len(st.recent) >= st.limit.per_min:
            waits.append(st.recent[0] + 60 - now)
        # min spacing
        if st.limit.min_interval > 0:
            gap = st.last_call + st.limit.min_interval - now
            if gap > 0:
                waits.append(gap)
        return max(waits) if waits else 0.0

    async def acquire(self, api: str, max_wait: float = 10.0) -> bool:
        """Wait (up to ``max_wait`` s) until a call to `api` is permitted, then
        claim+record the slot. Returns True if claimed, False if the wait would
        exceed max_wait (caller should SKIP this API for now — e.g. it is in a
        long 429 cooldown or its daily budget is spent). A skip is NOT an
        attempt, so the entry stays eligible for that API once it recovers.
        """
        deadline = time.time() + max_wait
        while True:
            with self._lock:
                now = time.time()
                st = self._state(api)
                wait = self._wait_seconds(st, now)
                if wait <= 0:
                    st.recent.append(now)
                    st.last_call = now
                    st.day_count += 1
                    self._dirty_budgets = True
                    self._maybe_flush()
                    return True
            if now + wait > deadline:
                return False
            await asyncio.sleep(min(wait, 2.0))

    def can_call(self, api: str) -> bool:
        """Non-blocking check (used to skip dead APIs cheaply)."""
        with self._lock:
            return self._wait_seconds(self._state(api), time.time()) <= 0

    # --------------------------------------------------------------- report
    def report(self, api: str, status_code: Optional[int], ok: bool) -> None:
        """Feed the real response back so the router adapts."""
        with self._lock:
            st = self._state(api)
            if ok:
                st.consec_errors = 0
                st.total_ok += 1
            else:
                st.consec_errors += 1
                st.total_fail += 1
                # 429 / 403 / 5xx → progressive cooldown, capped at 1h
                if status_code in (429, 403) or (status_code or 0) >= 500 or status_code is None:
                    backoff = min(300.0, 15.0 * (2 ** min(st.consec_errors, 5)))
                    st.cooldown_until = max(st.cooldown_until, time.time() + backoff)
            self._dirty_budgets = True
        self._maybe_flush()

    # -------------------------------------------------- per-entry attempt ledger
    @staticmethod
    def entry_hash(ref) -> str:
        """Content hash of the fields that determine an API lookup's outcome.
        If any change, the entry is 'different' and may be retried."""
        parts = [
            (getattr(ref, "doi", "") or "").lower().strip(),
            (getattr(ref, "pmid", "") or ""),
            (getattr(ref, "arxiv_id", "") or ""),
            (getattr(ref, "isbn", "") or ""),
            (getattr(ref, "url", "") or "").lower().strip(),
            (getattr(ref, "title", "") or "").lower().strip()[:200],
            str(getattr(ref, "year", "") or ""),
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8", "ignore")).hexdigest()[:16]

    def was_tried(self, ref_id: str, api: str, entry_hash: str) -> bool:
        """True if this exact entry was already tried against `api` and missed.
        A differing hash (entry changed) returns False so it can be retried."""
        if not ref_id:
            return False
        with self._lock:
            for r_id, a, h, res in self._pending_attempts_iter():
                if r_id == ref_id and a == api:
                    return h == entry_hash and res in ("miss", "error")
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT entry_hash, result FROM attempt_ledger WHERE ref_id=? AND api=?",
                    (ref_id, api),
                ).fetchone()
        except Exception:
            return False
        if not row:
            return False
        return row[0] == entry_hash and row[1] in ("miss", "error")

    def _pending_attempts_iter(self):
        for rec in self._pending_attempts:
            yield rec[0], rec[1], rec[2], rec[3]

    def record_attempt(self, ref_id: str, api: str, entry_hash: str, result: str) -> None:
        if not ref_id:
            return
        with self._lock:
            self._pending_attempts.append((ref_id, api, entry_hash, result, time.time()))
        self._maybe_flush()

    def clear_attempt(self, ref_id: str, api: str) -> None:
        """Clear attempt record for a reference and API (e.g. on update)."""
        if not ref_id:
            return
        with self._lock:
            # Remove from pending if not flushed yet
            self._pending_attempts = [
                rec for rec in self._pending_attempts
                if not (rec[0] == ref_id and rec[1] == api)
            ]
        try:
            with self._conn() as c:
                c.execute("DELETE FROM attempt_ledger WHERE ref_id=? AND api=?", (ref_id, api))
                c.commit()
        except Exception:
            pass

    # --------------------------------------------------------------- persist
    def _maybe_flush(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            due = force or (now - self._last_flush > 5.0) or len(self._pending_attempts) >= 200
            if not due:
                return
            self._last_flush = now
            budgets = [
                (api, st.day, st.day_count, st.cooldown_until, st.consec_errors, st.total_ok, st.total_fail)
                for api, st in self._states.items()
            ] if self._dirty_budgets else []
            attempts = self._pending_attempts
            self._pending_attempts = []
            self._dirty_budgets = False
        try:
            with self._conn() as c:
                if budgets:
                    c.executemany(
                        "INSERT INTO api_budget (api,day,day_count,cooldown_until,consec_errors,total_ok,total_fail) "
                        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(api) DO UPDATE SET "
                        "day=excluded.day, day_count=excluded.day_count, cooldown_until=excluded.cooldown_until, "
                        "consec_errors=excluded.consec_errors, total_ok=excluded.total_ok, total_fail=excluded.total_fail",
                        budgets,
                    )
                if attempts:
                    c.executemany(
                        "INSERT INTO attempt_ledger (ref_id,api,entry_hash,result,ts) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(ref_id,api) DO UPDATE SET entry_hash=excluded.entry_hash, "
                        "result=excluded.result, ts=excluded.ts",
                        attempts,
                    )
                c.commit()
        except Exception:
            # never let persistence errors break the hot path
            pass

    def flush(self) -> None:
        self._maybe_flush(force=True)

    # ----------------------------------------------------------------- status
    def status(self) -> Dict[str, dict]:
        out = {}
        now = time.time()
        with self._lock:
            for api, st in self._states.items():
                out[api] = {
                    "day_count": st.day_count,
                    "per_day": st.limit.per_day,
                    "cooling": st.cooldown_until > now,
                    "cooldown_s": max(0, int(st.cooldown_until - now)),
                    "consec_errors": st.consec_errors,
                    "ok": st.total_ok,
                    "fail": st.total_fail,
                }
        return out


# ---------------------------------------------------------------------------
def _default_db_path() -> Path:
    try:
        from .config import get_config
        return Path(get_config().db_path).parent / "api_router.db"
    except Exception:
        return Path.home() / ".local" / "share" / "mouseion" / "api_router.db"


_router: Optional[APIRouter] = None
_router_lock = threading.Lock()


def get_router() -> APIRouter:
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = APIRouter()
    return _router
