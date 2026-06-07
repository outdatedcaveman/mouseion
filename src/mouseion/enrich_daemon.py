"""
Background enrichment daemon.

Processes references in strict priority tiers:

  Tier 1 — Has DOI/PMID/arXiv/ISBN: Direct identifier lookup (fast, reliable)
  Tier 2 — Has URL from academic publisher: Extract identifiers from URL, then lookup
  Tier 3 — Has title + other metadata (year/authors): Title search on multiple APIs
  Tier 4 — Has title only: Broader title search with cleaning
  Tier 5 — Junk/minimal info: Deep search with substring variations (last resort)

The daemon exhausts each tier before moving to the next. Within each tier,
refs are processed in order of completeness gap (most incomplete first).
This ensures we never waste API calls on hopeless cases while easy DOI
lookups are waiting.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from .semaphore import SafeSemaphore
import threading
import time
from typing import List, Optional

from .models import Author, Reference

logger = logging.getLogger("mouseion.enrich_daemon")

# Daemon state
_daemon_thread: Optional[threading.Thread] = None
_daemon_running = threading.Event()
_daemon_stop    = threading.Event()
_daemon_lock    = threading.Lock()

_BATCH_SIZE = 250        # keep asyncio.gather bounded; batch APIs handle >=50 fine
_CYCLE_PAUSE = 1.0      # between batches within a tier
_TIER_PAUSE  = 2.0      # between tiers
_IDLE_PAUSE  = 30.0     # when all tiers are empty
_AUTO_QUEUE_THRESHOLD = 0.85

# Tier focus -- which tiers the daemon will process.
# Persisted to disk so it survives app restarts.
_focus_min_tier: int = 1
_focus_lock = threading.Lock()


def _focus_config_path() -> str:
    import os
    data_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "mouseion")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "daemon_config.json")


def _load_focus() -> None:
    """Load persisted focus setting from disk (called once at import time)."""
    global _focus_min_tier
    import json, os
    path = _focus_config_path()
    if os.path.exists(path):
        try:
            cfg = json.loads(open(path).read())
            _focus_min_tier = max(1, min(5, int(cfg.get("focus_min_tier", 1))))
        except Exception:
            pass


def _save_focus(min_tier: int) -> None:
    """Persist focus setting to disk."""
    import json
    try:
        with open(_focus_config_path(), "w") as f:
            json.dump({"focus_min_tier": min_tier}, f)
    except Exception as e:
        logger.warning("Could not save focus config: %s", e)


def get_focus() -> dict:
    """Return current daemon focus configuration."""
    with _focus_lock:
        return {"focus_min_tier": _focus_min_tier}


def set_focus(min_tier: int) -> None:
    """Set the minimum tier the daemon will process (1-5).

    Tier mapping:
      1 = All tiers (DOI/ID, URL, Title+meta, Title-only, Junk)
      2 = Tier 2+ (skip direct-ID lookups)
      3 = Tier 3+ (skip ID and URL tiers)
      4 = Tier 4+ (Title-only + Junk)  <- "L3-L4 focus"
      5 = Tier 5 only (last-resort junk)

    Setting is saved to disk and restored on next app start.
    """
    global _focus_min_tier
    with _focus_lock:
        _focus_min_tier = max(1, min(5, int(min_tier)))
    _save_focus(_focus_min_tier)
    logger.info("Daemon focus set to Tier %d+ (persisted)", _focus_min_tier)


# Load persisted setting immediately on module import
_load_focus()



def is_running() -> bool:
    return _daemon_running.is_set() and not _daemon_stop.is_set()


def start():
    global _daemon_thread
    with _daemon_lock:
        if _daemon_thread and _daemon_thread.is_alive():
            _daemon_running.set()
            return
        _daemon_stop.clear()
        _daemon_running.set()
        _daemon_thread = threading.Thread(target=_daemon_loop, daemon=True, name="enrich-daemon")
        _daemon_thread.start()
        logger.info("Daemon started")


def pause():
    _daemon_running.clear()
    logger.info("Daemon paused")


def resume():
    _daemon_running.set()
    logger.info("Daemon resumed")


def stop():
    _daemon_stop.set()
    _daemon_running.set()
    logger.info("Daemon stopping")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _daemon_loop():
    try:
        _daemon_loop_inner()
    except Exception:
        logger.exception("Daemon loop crashed")


def _daemon_loop_inner():
    from .db import RefDatabase

    logger.info("Enrichment daemon loop started")

    db = RefDatabase()

    def _reconnect():
        nonlocal db
        db = RefDatabase()
        logger.info("Daemon: reconnected to DB")

    # Reset stale active items from previous crash
    try:
        reset = db.reset_stale_active()
        if reset:
            logger.info("Reset %d stale active items", reset)
    except Exception:
        logger.exception("Reset stale active failed")

    while not _daemon_stop.is_set():
        _daemon_running.wait()
        if _daemon_stop.is_set():
            break

        # Periodically reset stale active items back to pending
        try:
            reset = db.reset_stale_active()
            if reset:
                logger.info("Reset %d stale active items", reset)
        except Exception:
            pass

        try:
            total = _process_all_tiers(db)
            if total == 0:
                _auto_queue(db)
                time.sleep(_IDLE_PAUSE)
        except Exception:
            logger.exception("Daemon cycle error — reconnecting")
            try:
                _reconnect()
            except Exception:
                logger.exception("Reconnect failed")
            time.sleep(5.0)

    logger.info("Enrichment daemon stopped")


def _auto_queue(db):
    try:
        with _focus_lock:
            min_tier = _focus_min_tier
        # Pass focus tier directly to enqueue_incomplete so only refs
        # in the selected tiers get queued. E.g. Tier 4+ = only title-only
        # and junk refs, skipping all identifier/URL/title+meta refs.
        added = db.enqueue_incomplete(
            threshold=_AUTO_QUEUE_THRESHOLD,
            min_tier=min_tier,
        )
        if added:
            logger.info(
                "Auto-queued %d incomplete refs (focus Tier %d+)",
                added, min_tier,
            )
    except Exception:
        logger.exception("Auto-queue error")


# ---------------------------------------------------------------------------
# Tier-based processing
# ---------------------------------------------------------------------------

def _process_all_tiers(db) -> int:
    """Process all tiers in order. Returns total refs processed.

    Respects _focus_min_tier: tiers below that number are skipped entirely.
    After completing all in-focus tiers, re-checks Tier 1 only if it is
    within the focus range (title searches in later tiers often discover
    DOIs that get re-queued as Tier 1 pending).
    """
    with _focus_lock:
        min_tier = _focus_min_tier

    total = 0
    for tier_num, (query, handler, batch_size) in enumerate(TIERS, 1):
        if _daemon_stop.is_set() or not _daemon_running.is_set():
            break
        if tier_num < min_tier:
            continue  # skip tiers below focus level
        count = _process_tier(db, tier_num, query, handler, batch_size)
        total += count
        if count > 0:
            time.sleep(_TIER_PAUSE)

    # Re-check Tier 1 only if it is within the focus range
    if min_tier == 1 and total > 0 and not _daemon_stop.is_set() and _daemon_running.is_set():
        tier1_query, tier1_handler, tier1_batch_size = TIERS[0]
        extra = _process_tier(db, 1, tier1_query, tier1_handler, tier1_batch_size)
        if extra:
            logger.info("Tier 1 re-pass: processed %d re-queued refs", extra)
            total += extra

    return total


# Each tier is (SQL WHERE clause, handler function)
# The WHERE clause selects pending refs matching the tier criteria.
# Refs are processed in order of completeness (most incomplete first).

TIERS = []  # populated after handler definitions below


def _process_tier(db, tier_num: int, extra_where: str, handler, batch_size: int) -> int:
    """Process one full pass of a tier. Returns count processed."""
    from .db import RefDatabase
    processed = 0

    while not _daemon_stop.is_set() and _daemon_running.is_set():
        # Fetch next batch for this tier
        try:
            batch = _dequeue_tier(db, extra_where, batch_size)
        except Exception:
            logger.exception("Tier %d: dequeue error", tier_num)
            break

        if not batch:
            break

        logger.info("Tier %d: processing %d refs", tier_num, len(batch))

        # Load all refs for this batch
        refs_by_id = {}
        rows_by_id = {}
        for row in batch:
            ref = db.get(row["ref_id"])
            if ref:
                refs_by_id[row["ref_id"]] = ref
                rows_by_id[row["ref_id"]] = row

        if not refs_by_id:
            time.sleep(_CYCLE_PAUSE)
            continue

        try:
            original_by_id = {
                ref_id: copy.deepcopy(ref)
                for ref_id, ref in refs_by_id.items()
            }
            # handler returns dict[ref_id -> enriched_ref] for batch handlers
            # or processes one-at-a-time for legacy handlers
            result = handler(list(refs_by_id.values()), list(rows_by_id.values()))

            if isinstance(result, dict):
                # Check which providers are cooled down
                from .cache import get_default_cache
                cache = get_default_cache()
                s2_cd = cache.get_cooldown("semantic_scholar")
                oa_cd = cache.get_cooldown("openalex")
                cr_cd = cache.get_cooldown("crossref")
                s2_cooled = bool(s2_cd and time.time() < s2_cd)
                oa_cooled = bool(oa_cd and time.time() < oa_cd)
                cr_cooled = bool(cr_cd and time.time() < cr_cd)

                # Batch handler: result is {ref_id: enriched_ref}
                with RefDatabase() as batch_db:
                    for ref_id, enriched in result.items():
                        row = rows_by_id.get(ref_id)
                        original = original_by_id.get(ref_id) or refs_by_id.get(ref_id)
                        if row and original and enriched:
                            try:
                                _save_result(batch_db, original, enriched, row)
                                processed += 1
                            except Exception as e:
                                logger.warning("Tier %d: save failed %s: %s", tier_num, ref_id[:8], e)
                                if "locked" in str(e).lower() or "timeout" in str(e).lower():
                                    raise
                                try:
                                    batch_db.complete_enrich(ref_id, new_completeness=original.completeness, error=str(e))
                                except Exception as e2:
                                    if "locked" in str(e2).lower() or "timeout" in str(e2).lower():
                                        raise
                                    pass
                    # Mark unresolved refs as completed with no change
                    for ref_id in refs_by_id:
                        if ref_id not in result:
                            row = rows_by_id[ref_id]
                            ref = original_by_id.get(ref_id) or refs_by_id[ref_id]

                            # Title-recovery salvage: the handler may have
                            # extracted a real identifier/URL in place from a
                            # polluted title (e.g. an Elsevier PII or wrapped
                            # URL). Even though no provider matched THIS pass,
                            # persist the recovered id/url and re-queue (Tier 1
                            # for ids, Tier 2 for urls) instead of discarding it
                            # as "no match". Without this the recovery is lost.
                            mutated = refs_by_id.get(ref_id)
                            orig = original_by_id.get(ref_id)
                            if mutated is not None and orig is not None and (
                                (mutated.doi and not orig.doi)
                                or (mutated.arxiv_id and not orig.arxiv_id)
                                or (mutated.pmid and not orig.pmid)
                                or (mutated.url and not orig.url)
                            ):
                                try:
                                    _save_result(batch_db, orig, mutated, row)
                                    continue
                                except Exception as e:
                                    if "locked" in str(e).lower() or "timeout" in str(e).lower():
                                        raise
                                    # fall through to no-match handling on failure

                            # Unrecoverable junk/filename title with no identifier
                            # or URL to fall back on: PARK it (mark done) instead
                            # of re-queuing forever. No tier — API, web, or LLM —
                            # can resolve a title like '0038932 pdf', so retrying
                            # it only churns and (at higher tiers) wastes quota.
                            # Lossless: the row is kept and the reason recorded.
                            check_ref = mutated if mutated is not None else ref
                            if (
                                _is_junk_title(getattr(check_ref, "title", None), check_ref, strict=True)
                                and not (check_ref.doi or check_ref.url
                                         or check_ref.arxiv_id or check_ref.pmid)
                            ):
                                try:
                                    with batch_db._db() as _c:
                                        _c.execute(
                                            "UPDATE enrich_queue SET status='done', "
                                            "attempts = attempts + 1, "
                                            "last_error='parked: unrecoverable junk/filename title' "
                                            "WHERE ref_id = ?",
                                            (ref_id,),
                                        )
                                    continue
                                except Exception as e:
                                    if "locked" in str(e).lower() or "timeout" in str(e).lower():
                                        raise
                                    # fall through to no-match handling on failure

                            error_msg = "no match"
                            if tier_num in (1, 2):
                                if (ref.doi and (s2_cooled or oa_cooled or cr_cooled)) or \
                                   (ref.pmid and (s2_cooled or oa_cooled)) or \
                                   (ref.arxiv_id and (s2_cooled or oa_cooled)):
                                    error_msg = "cooldown"
                            elif tier_num == 3:
                                if s2_cooled or cr_cooled or oa_cooled:
                                    error_msg = "cooldown"
                            elif tier_num == 4:
                                if cr_cooled or oa_cooled or s2_cooled:
                                    error_msg = "cooldown"
                            elif tier_num == 5:
                                if cr_cooled or oa_cooled or s2_cooled:
                                    error_msg = "cooldown"

                            try:
                                batch_db.complete_enrich(ref_id, new_completeness=ref.completeness, error=error_msg)
                            except Exception as e:
                                if "locked" in str(e).lower() or "timeout" in str(e).lower():
                                    raise
                                pass

                    # Break the loop only if ALL relevant providers for this tier are cooled down
                    tier_fully_cooled = False
                    if tier_num in (1, 2):
                        tier_fully_cooled = (s2_cooled and oa_cooled and cr_cooled)
                    elif tier_num in (3, 4, 5):
                        tier_fully_cooled = (s2_cooled and cr_cooled and oa_cooled)

                    if tier_fully_cooled:
                        logger.info("Tier %d: all providers are cooled down, breaking loop to pause", tier_num)
                        break
            else:
                # Legacy single-ref handler (shouldn't happen with new code)
                logger.warning("Tier %d: handler returned non-dict", tier_num)
        except Exception as e:
            logger.exception("Tier %d: batch handler error", tier_num)
            # Requeue on error
            _requeue_batch(db, batch)
            break

        time.sleep(_CYCLE_PAUSE)

    return processed


def _dequeue_tier(db, extra_where: str, limit: int):
    """Fetch pending refs matching tier criteria."""
    with db._db() as conn:
        sql = f"""
            SELECT eq.ref_id, eq.priority, eq.difficulty, eq.strategy_level,
                   eq.attempts, r.doi, r.pmid, r.arxiv_id, r.isbn, r.url,
                   r.title, r.completeness, r.year
            FROM enrich_queue eq
            JOIN refs r ON r.id = eq.ref_id
            WHERE eq.status = 'pending'
              AND (eq.last_attempt IS NULL OR eq.last_attempt < datetime('now', '-5 minutes'))
              AND ({extra_where})
            ORDER BY eq.priority DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (limit,)).fetchall()
        ids = [r["ref_id"] for r in rows]
        if ids:
            ph = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE enrich_queue SET status = 'active', last_attempt = datetime('now') WHERE ref_id IN ({ph})",
                ids,
            )
        rows_list = [dict(r) for r in rows]
    # Commit the dequeue transaction BEFORE the handler runs, so any handler
    # that opens its own DB connection (e.g. to persist cleaned DOIs) doesn't
    # hit "database is locked" from the uncommitted UPDATE above.
    if db._conn:
        db._conn.commit()
    return rows_list


def _requeue_batch(db, rows):
    """Put refs back to pending."""
    try:
        with db._db() as conn:
            for r in rows:
                conn.execute(
                    "UPDATE enrich_queue SET status = 'pending' WHERE ref_id = ?",
                    (r["ref_id"],),
                )
    except Exception:
        pass


def _save_result(db, original, enriched, queue_item):
    """Save enrichment result and update queue.

    If a title-only ref gained a DOI/PMID/arXiv from this pass, re-queue it
    for Tier 1 (batch ID lookup) to fetch full metadata (abstract, authors, etc).
    """
    from .tagger import auto_tag, tag_from_keywords
    from .config import get_config
    cfg = get_config()

    tags = list(set(auto_tag(enriched, cfg) + tag_from_keywords(enriched)))
    # Use replace_ref (not upsert) so enriched data is written into the
    # ORIGINAL row — same DB id, same queue entry, no orphaned duplicates.
    # upsert() would compute a new DOI-based id and silently create a second
    # row for the same paper, leaving the original untouched.
    db.replace_ref(queue_item["ref_id"], enriched, tags=tags)

    # Reload from DB to get the actually saved state (handles DOI collision fallback)
    saved = db.get(queue_item["ref_id"]) or enriched

    # Did this ref gain a new identifier it didn't have before?
    gained_id = (
        (saved.doi and not original.doi)
        or (saved.pmid and not original.pmid)
        or (saved.arxiv_id and not original.arxiv_id)
    )
    gained_url = saved.url and not original.url
    gained_metadata = (
        (saved.year and not original.year)
        or (saved.authors and not original.authors)
        or ((saved.journal or saved.publisher or saved.container_title)
            and not (original.journal or original.publisher or original.container_title))
    )
    # Is it still below 80% and could benefit from a Tier 1 pass?
    # Cap re-queues: after 3 re-queues the ref clearly isn't improving via
    # Tier 1 batch lookup — accept the current state rather than looping forever.
    already_requeued = (queue_item.get("last_error") or "").startswith("requeued:")
    attempt_count = queue_item.get("attempts", 0)
    needs_deeper = (
        (gained_id or gained_url or gained_metadata)
        and (saved.completeness or 0) < 0.80
        and not (already_requeued and attempt_count >= 3)
    )

    if needs_deeper:
        next_strategy = 0 if gained_id else 1 if gained_url else 2
        reason = (
            "requeued: gained identifier" if gained_id
            else "requeued: gained url" if gained_url
            else "requeued: gained metadata"
        )
        # Re-queue for another pass: new identifiers go to Tier 1, URLs to
        # Tier 2, while author/year/title salvage goes to Tier 3.
        try:
            with db._db() as conn:
                conn.execute(
                    """UPDATE enrich_queue SET
                         status = 'pending', strategy_level = ?,
                         difficulty = 0, priority = 1.0,
                         last_error = ?, attempts = attempts + 1
                       WHERE ref_id = ?""",
                    (next_strategy, reason, queue_item["ref_id"]),
                )
            logger.info(
                "Re-queued %s for Tier %d (%s): %.0f%% [%s]",
                queue_item["ref_id"][:8],
                1 if next_strategy == 0 else 2 if next_strategy == 1 else 3,
                "gained DOI" if saved.doi and not original.doi else
                "gained PMID" if saved.pmid and not original.pmid else
                "gained arXiv" if saved.arxiv_id and not original.arxiv_id else
                "gained URL" if gained_url else
                "gained author/year metadata",
                (saved.completeness or 0) * 100,
                saved.title[:40] if saved.title else "?"
            )
        except Exception:
            logger.exception("Re-queue failed for %s", queue_item["ref_id"][:8])
            db.complete_enrich(queue_item["ref_id"], new_completeness=saved.completeness, error=None)
    elif (
        _is_junk_title(saved.title, strict=True)
        and not (saved.doi or saved.url or saved.arxiv_id or saved.pmid)
        and (saved.completeness or 0) < 0.5
    ):
        # Junk/filename title that didn't improve and has nothing to recover —
        # PARK it (done) instead of letting complete_enrich re-queue it to churn
        # forever through the web-search/LLM tiers (which burn quota + LLM spend
        # on something unresolvable). This closes the bypass where junk reached
        # the deep tiers via `results` instead of the no-match path.
        try:
            with db._db() as conn:
                conn.execute(
                    "UPDATE enrich_queue SET status='done', attempts = attempts + 1, "
                    "last_error='parked: unrecoverable junk/filename title' WHERE ref_id = ?",
                    (queue_item["ref_id"],),
                )
        except Exception:
            db.complete_enrich(queue_item["ref_id"], new_completeness=saved.completeness, error=None)
    else:
        db.complete_enrich(
            queue_item["ref_id"],
            new_completeness=saved.completeness,
            error=None,
        )

    old_c = queue_item.get("completeness", 0) or 0
    new_c = saved.completeness or 0
    if new_c > old_c + 0.01:
        logger.info(
            "Enriched %s: %.0f%% -> %.0f%% [%s]",
            queue_item["ref_id"][:8], old_c * 100, new_c * 100,
            saved.title[:40] if saved.title else "?"
        )


# ---------------------------------------------------------------------------
# Tier handlers
# ---------------------------------------------------------------------------

# Persistent event loop for the daemon thread — keeps provider rate-limiting
# state (semaphores, _last_request_time) alive across calls.
_loop: Optional[asyncio.AbstractEventLoop] = None
_providers_cache: Optional[dict] = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def _get_providers_map() -> dict:
    global _providers_cache
    if _providers_cache is None:
        from .providers import _make_providers
        _providers_cache = {p.name: p for p in _make_providers()}
    return _providers_cache


def _enrich_with_providers(ref, provider_names):
    """Look up ref using specific providers. Returns enriched ref."""
    from .lookup import enrich_one
    by_name = _get_providers_map()
    providers = [by_name[n] for n in provider_names if n in by_name]
    if not providers:
        return ref

    loop = _get_loop()
    return loop.run_until_complete(enrich_one(ref, providers))


def _enrich_many_concurrent(refs: List[Reference], provider_names, concurrency: int = 20) -> List[Reference]:
    """Enrich multiple refs concurrently using asyncio.gather with a semaphore.

    concurrency=20: each ref hits ~3 providers in parallel. Measured against
    the live APIs this sustains ~1.6 title-search refs/s while CrossRef
    (~2 req/s vs ~50 limit) and OpenAlex (~2 req/s vs 10 limit) keep ample
    headroom. Each provider call self-regulates (429 backoff + 8s ceiling),
    so the pool degrades gracefully rather than getting throttled.
    A small stagger delay spreads requests to avoid bursts.
    """
    from .lookup import enrich_one
    by_name = _get_providers_map()
    providers = [by_name[n] for n in provider_names if n in by_name]
    if not providers:
        return list(refs)

    # A ref-level concurrency of 20 can fan out into 40-80 provider requests
    # for title search. Clamp it for externally throttled providers; batch APIs
    # still handle bulk identifier lookups separately.
    provider_set = set(provider_names)
    # Clamp ref-level concurrency to what the downstream layers can actually
    # absorb. NOTE: this is NOT the only ceiling — per-provider semaphores
    # (_max_concurrent) and the global metadata network budget
    # (MOUSEION_NET_METADATA) also cap throughput, so they were raised in
    # lockstep with this value. Raising this number alone does nothing.
    if provider_set & {"semantic_scholar", "crossref", "openalex", "dblp"}:
        concurrency = min(concurrency, 10)
    if provider_set == {"semantic_scholar"}:
        # S2 alone stays low: tiny daily quota (10k) + aggressive 429s.
        concurrency = min(concurrency, 2)

    async def _run():
        sem = SafeSemaphore(concurrency)

        async def _one(ref):
            async with sem:
                result = await enrich_one(ref, providers)
                # Small stagger to spread requests and avoid bursts
                await asyncio.sleep(0.15)
                return result

        return await asyncio.gather(
            *[_one(r) for r in refs],
            return_exceptions=True,
        )

    loop = _get_loop()
    results = loop.run_until_complete(_run())
    # Replace exceptions with original refs
    return [r if isinstance(r, Reference) else refs[i] for i, r in enumerate(results)]


def _clean_title(ref):
    """Clean the ref's title in place. Returns True if title is usable."""
    from .providers.base import clean_query_title
    if ref.title:
        ref.title = clean_query_title(ref.title)
    return bool(ref.title and len(ref.title) >= 5)


def _clean_title_all(refs):
    """Clean titles on a list of refs in place."""
    for ref in refs:
        _clean_title(ref)


def _split_author_names(raw: str) -> List[Author]:
    """Best-effort author parsing for page titles like 'A. Name & B. Name, Title'."""
    raw = re.sub(r"\s+", " ", raw or "").strip(" ,;")
    if not raw:
        return []
    pieces = re.split(r"\s+(?:&|and)\s+|;\s*", raw)
    authors: List[Author] = []
    for piece in pieces[:8]:
        piece = piece.strip(" ,")
        if not piece or len(piece) > 80:
            continue
        parts = piece.rsplit(" ", 1)
        if len(parts) == 2:
            authors.append(Author(given=parts[0].strip(), family=parts[1].strip()))
        else:
            authors.append(Author(family=piece))
    return authors


def _is_url(s: str) -> bool:
    """Check if a string looks like a URL."""
    return bool(s and re.match(r"https?://", s.strip(), re.I))


def _smart_parse_title(ref) -> bool:
    """Rule-based 'smart parser' for messy titles that need semantic understanding.

    Handles patterns like:
      - 'Etext of Meditations, by Marcus Aurelius'
      - 'Lecture Notes in Computer Science 2235'
      - 'PDF) Some Paper Title'
      - 'Author1 Author2 Actual Paper Title'
      - Series or volume identifiers misidentified as titles

    Returns True if any changes were made.
    """
    if not ref.title:
        return False
    title = ref.title
    changed = False

    # --- 'by Author' suffix: 'Meditations, by Marcus Aurelius' ---
    m = re.match(r"^(.{5,}?),?\s+by\s+(.{3,60})$", title, re.I)
    if m and not ref.authors:
        ref.title = m.group(1).strip()
        ref.authors = _split_author_names(m.group(2))
        changed = True
        title = ref.title

    # --- Series/volume identifiers: 'Lecture Notes in Computer Science 2235' ---
    series_patterns = [
        (r"^(Lecture Notes in (?:Computer Science|Mathematics|Physics|AI))\s+(\d{3,5})$",
         lambda m: (m.group(1), m.group(2))),
        (r"^(Graduate Texts in Mathematics)\s+(\d+)", lambda m: (m.group(1), m.group(2))),
        (r"^(Cambridge Tracts in Mathematics)\s+(\d+)", lambda m: (m.group(1), m.group(2))),
        (r"^(Springer Lecture Notes)\s+(\d+)", lambda m: (m.group(1), m.group(2))),
    ]
    for pat, extractor in series_patterns:
        sm = re.match(pat, title, re.I)
        if sm:
            series_name, vol = extractor(sm)
            if not ref.journal:
                ref.journal = series_name
            if not ref.volume:
                ref.volume = vol
            changed = True
            break

    # --- 'Author1 Author2 Title' without comma (2+ capitalized names then title) ---
    if not ref.authors and not changed:
        # Pattern: 2-6 capitalized words (names) followed by a longer phrase
        m = re.match(
            r"^((?:[A-Z][a-z]+\.?\s+){1,5}[A-Z][a-z]+)\s+"
            r"([A-Z].{10,})$",
            title
        )
        if m:
            candidate_authors = m.group(1).strip()
            candidate_title = m.group(2).strip()
            # The title part should contain articles/prepositions (common in real titles)
            has_title_words = re.search(
                r"\b(?:of|the|and|in|on|for|a|an|to|with|from|between|about|towards?)\b",
                candidate_title, re.I
            )
            # Author part shouldn't look like a title itself
            author_is_title = re.search(
                r"\b(?:of|the|and|in|on|for|to|with|from)\b",
                candidate_authors, re.I
            )
            if has_title_words and not author_is_title:
                ref.authors = _split_author_names(candidate_authors)
                ref.title = candidate_title
                changed = True

    return changed


def _recover_polluted_title(ref) -> bool:
    """Recover a real identifier/URL from a title that is actually a mangled
    locator (scraped-import pollution): an embedded DOI, an Elsevier PII, an
    arXiv id, a Google/Scholar redirect, or a bare/space-mangled URL.

    These refs otherwise fail title search forever because their 'title' is a
    URL fragment, not a title. On success it populates ref.doi / ref.arxiv_id /
    ref.url, blanks the junk title, and preserves the original in extras
    (lossless). Conservative by design: it only fires when the title is clearly
    pathological and only sets identifiers the ref doesn't already have, so it
    can't corrupt a legitimate title.
    """
    t = (ref.title or "").strip()
    if len(t) < 8:
        return False

    low = t.lower()
    # Underscore-encoded DOI from a filename, e.g.
    # '10_5486_PMD_1972_19_1_4_37 pdf' -> 10.5486/PMD.1972.19.1.4.37.
    # Handle this first since the generic 'looks_polluted' gate would miss it.
    if not ref.doi:
        mdoi = re.match(r"^10[._](\d{4,})[._](.+?)(?:[\s._-]*\.?(?:pdf|docx?))?$", t.strip(), re.I)
        if mdoi:
            suffix = re.sub(r"[._]+", ".", mdoi.group(2)).strip(".")
            if suffix:
                ref.doi = f"10.{mdoi.group(1)}/{suffix}"
                if ref.extras is None:
                    ref.extras = {}
                ref.extras.setdefault("original_page_title", t)
                ref.sources["title_recovery"] = 0.55
                ref.title = ""
                return True

    # Pollution gate: only attempt recovery on titles that look like locators,
    # not normal prose titles. This is what keeps false positives near zero.
    looks_polluted = (
        low.startswith(("http://", "https://"))
        or "google.com/url" in low or "scholar_url" in low
        or "doi.org/" in low or "doi=" in low or "doi:" in low
        or "/pii/" in low or " pii " in low or "retrieve pii" in low
        or "arxiv" in low
        or "linkinghub" in low or "sciencedirect" in low
        or (len(t) >= 40 and " " not in t)            # base64-ish / URL blob
    )
    if not looks_polluted:
        return False

    from urllib.parse import unquote

    def _stash():
        if ref.extras is None:
            ref.extras = {}
        ref.extras.setdefault("original_page_title", t)
        ref.sources["title_recovery"] = 0.6

    # 1) Wrapped / redirect / plain URL -> unwrap FIRST so we extract a CLEAN
    #    identifier from the real target rather than capturing a DOI glued to
    #    Google tracking params (&ved=, &usg=, ...).
    if low.startswith(("http://", "https://")) or "google.com/url" in low or "scholar_url" in low:
        url = _unwrap_url(t)
        if url:
            ref.url = url
            _stash()
            ref.title = ""
            _extract_ids_from_url(ref)   # may set doi/arxiv/pmid directly
            if ref.doi:
                _clean_doi(ref)          # strip &ved/&usg/session tails, validate
            return True

    # 2) Explicit DOI embedded in a non-URL fragment
    #    (skip CiteSeerX pseudo-DOIs like 10.1.1.23.8845)
    m = re.search(r'(10\.\d{4,}/[^\s"\'<>]+)', unquote(t))
    if m and not ref.doi:
        ref.doi = m.group(1)
        _clean_doi(ref)                  # strips tracking params + validates
        if ref.doi and not re.match(r'^10\.\d+\.\d+\.\d+$', ref.doi):
            _stash()
            ref.title = ""
            return True
        ref.doi = None                   # pseudo-DOI or junk after cleaning

    # 3) Elsevier PII (requires the 'pii' keyword for safety) -> linkinghub URL,
    #    which Tier-2/URL handling + CrossRef can resolve to a DOI.
    m = re.search(r'pii[\s/:_-]+([SB]\d{15,17}[\dXx]?)\b', t, re.I)
    if m and not (ref.doi or ref.url):
        pii = m.group(1).upper()
        ref.url = f"https://linkinghub.elsevier.com/retrieve/pii/{pii}"
        _stash()
        ref.title = ""
        return True

    # 4) arXiv id (requires the 'arxiv' keyword for safety)
    if "arxiv" in low:
        m = re.search(r'(\d{4}\.\d{4,5})(?:v\d+)?', t)
        if m and not ref.arxiv_id:
            ref.arxiv_id = m.group(1)
            _stash()
            ref.title = ""
            return True

    return False


def _salvage_from_title(ref) -> bool:
    """Infer minimum metadata from imported browser/page-title strings.

    This intentionally uses low-confidence provenance. It fills blanks and
    improves search queries, but verified provider data should still win later.
    """
    # First: if the "title" is really a mangled locator (URL fragment, PII,
    # embedded DOI/arXiv), recover the identifier instead of searching on junk.
    if _recover_polluted_title(ref):
        return True

    if not ref.title:
        return False
    import html

    original = ref.title
    title = html.unescape(original)
    title = re.sub(r"\s+", " ", title).strip()
    changed = False

    if ref.extras is None:
        ref.extras = {}
    ref.extras.setdefault("original_page_title", original)

    # --- URL-as-title: if the "title" is actually a URL, store it as url ---
    if _is_url(title):
        if not ref.url:
            ref.url = title.strip()
        ref.title = ""  # will be filled by _resolve_url_titles later
        ref.sources["offline_title_salvage"] = 0.42
        return True

    # --- Strip common prefixes that aren't part of the actual title ---
    title = re.sub(r"^\(?PDF\)?\s*", "", title, flags=re.I)
    title = re.sub(r"^(?:Etext|Full\s+text|Text)\s+of\s+", "", title, flags=re.I)
    # Strip filename extensions
    title = re.sub(r"\.(?:pdf|docx?|html?|txt)$", "", title, flags=re.I)

    if not ref.year:
        years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", title)]
        plausible = [y for y in years if 1500 <= y <= 2100]
        if plausible:
            ref.year = plausible[-1]
            changed = True

    # Capture common platform/institution suffixes before cleaning them away.
    suffix_parts = re.split(r"\s+(?:[-–—]|\|)\s+", title)
    if len(suffix_parts) > 1:
        tail = suffix_parts[-1].strip()
        if tail and not ref.publisher and len(tail) <= 80:
            platform = tail
            if re.search(r"philpapers", platform, re.I):
                platform = "PhilPapers"
            elif re.search(r"misinformation review", platform, re.I):
                platform = "HKS Misinformation Review"
            elif re.search(r"inria|institut national de recherche", platform, re.I):
                platform = "Inria"
            if re.search(r"[A-Za-z]", platform):
                ref.publisher = platform
                changed = True
        title = suffix_parts[0].strip()

    # Page titles from PhilPapers and similar often start 'Author, Title'.
    if not ref.authors:
        m = re.match(r"^(.{2,120}?),\s+(.{8,})$", title)
        if m:
            lead, rest = m.group(1).strip(), m.group(2).strip()
            lead_words = lead.split()
            looks_like_author = (
                len(lead_words) <= 8
                and not re.search(r"\b(?:review|introduction|overview|chapter|volume|issue)\b", lead, re.I)
                and re.search(r"[A-Z]", lead)
            )
            if looks_like_author:
                authors = _split_author_names(lead)
                if authors:
                    ref.authors = authors
                    title = rest
                    changed = True

    # Remove site tails from the title after extracting them.
    title = re.sub(r"\s+", " ", title).strip(" -–—|:;,")
    if title and title != ref.title and len(title) >= 5:
        ref.title = title
        changed = True

    # Apply smart parser for remaining patterns (series names, embedded authors, etc.)
    if _smart_parse_title(ref):
        changed = True

    if changed:
        ref.sources["offline_title_salvage"] = max(ref.sources.get("offline_title_salvage", 0.0), 0.42)
        ref.extras["salvage_note"] = "metadata inferred from imported page title"
    return changed


def _clean_doi(ref):
    """Clean DOI in place — strip whitespace, fragments, URL params, control chars."""
    if not ref.doi:
        return
    doi = ref.doi.strip()
    # Remove control characters (\r, \n, \t)
    doi = re.sub(r'[\r\n\t]', '', doi)
    # Remove URL fragments (#supplementary-data, etc.)
    doi = doi.split('#')[0]
    # Remove trailing punctuation that's not part of a DOI
    doi = doi.rstrip('.,;:) ')
    # Remove query strings (?type=printable, etc.)
    doi = doi.split('?')[0]
    # Remove Google/URL tracking params appended with & (e.g. &ved=2a, &utm_source=...)
    # Safe to strip because valid DOI suffixes never start with lowercase word=value pairs
    doi = re.sub(r'&(?:ved|utm_\w+|source|rct|oi|ct|esrc|usg|sa|url)=.*$', '', doi)
    # More broadly: strip any &key=value tail where the part before & looks like a valid DOI
    if re.match(r'^10\.\d{4,}/', doi) and '&' in doi:
        doi = re.sub(r'&\w+=.*$', '', doi)
    # Strip OUP/publisher session hashes: 10.1093/jigpal/jzae114/7815949 → 10.1093/jigpal/jzae114
    # These are numeric suffixes appended to a DOI that already has a valid suffix
    # Pattern: DOI-with-suffix /DIGITS (where suffix already contains letters)
    doi = re.sub(r'^(10\.\d{4,}/[^/]+/[a-zA-Z][^/]*/)/?\d{7,}$', r'\1', doi)
    doi = doi.rstrip('/')
    ref.doi = doi if doi else None


# --- Tier 1: Has identifier (DOI, PMID, arXiv, ISBN) — BATCH MODE ---

def _handle_tier1_batch(refs: List[Reference], rows: List[dict]) -> dict:
    """Batch identifier lookup using S2, OpenAlex, and CrossRef batch APIs."""
    from .batch_lookup import enrich_batch_by_id
    from .config import get_config

    cfg = get_config()

    # Attach ref_id and clean all refs.
    # Persist the cleaned DOI to the DB right away — even if the lookup fails
    # the dirty version (e.g. DOI with ?utm_source= params) won't be retried.
    dirty_dois: dict = {}  # ref_id → original (dirty) doi
    for ref, row in zip(refs, rows):
        ref._batch_id = row["ref_id"]
        original_doi = ref.doi
        _clean_doi(ref)
        _clean_title(ref)
        if ref.doi != original_doi:
            dirty_dois[ref._batch_id] = (original_doi, ref.doi)

    if dirty_dois:
        try:
            from .db import RefDatabase
            with RefDatabase() as db:
                for ref_id, (old_doi, new_doi) in dirty_dois.items():
                    db._conn.execute(
                        "UPDATE refs SET doi = ? WHERE id = ? AND doi = ?",
                        (new_doi, ref_id, old_doi),
                    )
            logger.debug("Cleaned %d dirty DOIs", len(dirty_dois))
        except Exception:
            logger.exception("Failed to persist cleaned DOIs")

    loop = _get_loop()
    enriched_map = loop.run_until_complete(
        enrich_batch_by_id(
            refs,
            s2_api_key=cfg.semantic_scholar_api_key,
            oa_email=cfg.openalex_email,
            oa_api_key=cfg.openalex_api_key,
            cr_email=cfg.crossref_email or cfg.openalex_email,
        )
    )

    # ISBN refs are now handled by batch_openlibrary inside enrich_batch_by_id.
    # Fall back to Google Books only for unresolved ISBN refs (no batch API).
    unresolved_isbns = [
        ref for ref in refs
        if (ref._batch_id not in enriched_map or enriched_map[ref._batch_id] is ref) and ref.isbn
    ]
    if unresolved_isbns:
        enriched_isbns = _enrich_many_concurrent(unresolved_isbns, ["google_books"], concurrency=10)
        for ref, enriched in zip(unresolved_isbns, enriched_isbns):
            enriched_map[ref._batch_id] = enriched

    return enriched_map


# --- Tier 2: Has URL — extract IDs then batch ---

def _handle_tier2_batch(refs: List[Reference], rows: List[dict]) -> dict:
    """Extract identifiers from URLs, then batch lookup."""
    from .batch_lookup import enrich_batch_by_id
    from .config import get_config

    cfg = get_config()

    # Attach ref_id and extract IDs from URLs
    for ref, row in zip(refs, rows):
        ref._batch_id = row["ref_id"]
        _salvage_from_title(ref)
        _extract_ids_from_url(ref)
        _clean_title(ref)

    # Refs that now have identifiers go through batch lookup
    id_refs = [r for r in refs if r.doi or r.arxiv_id or r.pmid]

    results = {}

    if id_refs:
        loop = _get_loop()
        batch_results = loop.run_until_complete(
            enrich_batch_by_id(
                id_refs,
                s2_api_key=cfg.semantic_scholar_api_key,
                oa_email=cfg.openalex_email,
                oa_api_key=cfg.openalex_api_key,
                cr_email=cfg.crossref_email or cfg.openalex_email,
            )
        )
        results.update(batch_results)

    return results


# --- Tier 3: Has title + metadata — single lookup per ref ---

def _handle_tier3_batch(refs: List[Reference], rows: List[dict]) -> dict:
    """Title search enriched by author/year metadata — concurrent."""
    # Attach batch IDs and filter to usable refs
    usable = []
    for ref, row in zip(refs, rows):
        ref._batch_id = row["ref_id"]
        _salvage_from_title(ref)
        _clean_title(ref)
        # Do NOT spend metadata-API quota on filename/junk titles — no title
        # search can resolve '0038932 pdf'. They fall through to _process_tier,
        # which parks them (or routes recovered identifiers to Tier 1).
        if ref.title and not _is_junk_title(ref.title, ref):
            usable.append(ref)

    if not usable:
        return {}

    # Concurrent multi-provider title search. Each ref queries CrossRef,
    # OpenAlex, and S2 in parallel inside enrich_one(); _enrich_many_concurrent
    # clamps concurrency to 6 internally (see clamp at top of that function)
    # and each provider self-regulates on 429 via its own backoff. A previous
    # version ran a serial "S2 first, then fallback" path that took ~20 minutes
    # per 250-ref batch because S2's title search is slow AND has the worst
    # match rate of the three — most refs missed S2 anyway and still hit the
    # fallback. Going back to concurrent fan-out cuts wall time by ~8x.
    enriched_list = _enrich_many_concurrent(
        usable, ["crossref", "openalex", "semantic_scholar"]
    )
    results = {ref._batch_id: enriched for ref, enriched in zip(usable, enriched_list)}

    # IN-PLACE UPGRADE (no re-queue): any ref that GAINED a DOI/arXiv/PMID from
    # the title match but is still incomplete gets its canonical identifier-based
    # lookup right now and is merged into the best possible version — collapsing
    # what used to be a separate Tier-1 re-queue cycle into this same pass.
    upgrade = []
    for ref, enriched in zip(usable, enriched_list):
        if enriched and (enriched.completeness or 0) < 0.85 and (
            enriched.doi or enriched.arxiv_id or enriched.pmid
        ):
            enriched._batch_id = ref._batch_id
            upgrade.append(enriched)
    if upgrade:
        try:
            from .batch_lookup import enrich_batch_by_id
            from .config import get_config as _gc
            from .merge import merge as _merge
            cfg = _gc()
            loop = _get_loop()
            byid = loop.run_until_complete(enrich_batch_by_id(
                upgrade,
                s2_api_key=cfg.semantic_scholar_api_key,
                oa_email=cfg.openalex_email,
                oa_api_key=cfg.openalex_api_key,
                cr_email=cfg.crossref_email or cfg.openalex_email,
            ))
            for enriched in upgrade:
                better = byid.get(enriched._batch_id)
                if not better:
                    continue
                try:
                    results[enriched._batch_id] = _merge(enriched, [(better, 0.97)])
                except Exception:
                    if (better.completeness or 0) > (enriched.completeness or 0):
                        results[enriched._batch_id] = better
        except Exception:
            logger.exception("Tier 3 in-place ID upgrade failed")
    return results


# --- Tier 4: Has title only ---

def _resolve_url_titles(refs: List[Reference]) -> None:
    """For refs whose title was a URL, fetch the page and extract the real title.

    Modifies refs in place. Only processes refs where title is empty and url is set.
    """
    url_refs = [r for r in refs if not r.title and r.url]
    if not url_refs:
        return

    async def _fetch_all():
        import httpx
        from .web_search import _scrape_page_metadata
        sem = SafeSemaphore(50)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        }
        timeout = httpx.Timeout(5.0, connect=3.0)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            async def _one(ref):
                async with sem:
                    try:
                        scraped = await _scrape_page_metadata(ref.url, client)
                        if scraped:
                            if scraped.title and len(scraped.title) >= 5:
                                ref.title = scraped.title
                            if scraped.doi and not ref.doi:
                                ref.doi = scraped.doi
                            if scraped.arxiv_id and not ref.arxiv_id:
                                ref.arxiv_id = scraped.arxiv_id
                            if scraped.pmid and not ref.pmid:
                                ref.pmid = scraped.pmid
                            if scraped.authors and not ref.authors:
                                ref.authors = scraped.authors
                            if scraped.year and not ref.year:
                                ref.year = scraped.year
                            if scraped.abstract and not ref.abstract:
                                ref.abstract = scraped.abstract
                            logger.info("URL-title resolved: %s -> %s",
                                        ref.url[:40], (ref.title or "?")[:40])
                    except Exception as e:
                        logger.debug("URL-title fetch failed %s: %s", ref.url[:40], e)
            await asyncio.gather(*[_one(r) for r in url_refs], return_exceptions=True)

    loop = _get_loop()
    loop.run_until_complete(_fetch_all())
    logger.info("URL-title resolution: processed %d refs", len(url_refs))


def _handle_tier4_batch(refs: List[Reference], rows: List[dict]) -> dict:
    """Multi-phase title search with aggressive cleaning, DBLP, and web search fallback.

    Phases:
      0. Pre-process: salvage metadata, resolve URL-as-titles
      1. API lookup: CrossRef, OpenAlex, Semantic Scholar, DBLP (concurrent)
      2. Retry with aggressively cleaned titles
      3. Web search fallback (DuckDuckGo) for still-unresolved entries
    """
    # Phase 0: Attach IDs, salvage metadata, resolve URL-as-titles
    for ref, row in zip(refs, rows):
        ref._batch_id = row["ref_id"]
        _salvage_from_title(ref)

    _resolve_url_titles(refs)

    usable = []
    for ref in refs:
        _clean_title(ref)
        if ref.title and not _is_junk_title(ref.title, ref):
            usable.append(ref)

    if not usable:
        return {}

    # Phase 1: concurrent API lookup (now includes DBLP for CS papers)
    enriched_list = _enrich_many_concurrent(
        usable, ["crossref", "openalex", "semantic_scholar", "dblp"]
    )

    results = {}
    retry_refs = []
    for ref, enriched in zip(usable, enriched_list):
        if enriched.completeness > (ref.completeness or 0) + 0.05:
            results[ref._batch_id] = enriched
        else:
            _aggressive_clean(ref)
            if ref.title and len(ref.title) >= 10:
                retry_refs.append(ref)
            else:
                results[ref._batch_id] = enriched

    # Phase 2: retry with cleaned titles
    unresolved_for_web = []
    if retry_refs:
        retry_results = _enrich_many_concurrent(retry_refs, ["crossref", "openalex"])
        for ref, enriched in zip(retry_refs, retry_results):
            if enriched.completeness > (ref.completeness or 0) + 0.05:
                results[ref._batch_id] = enriched
            else:
                unresolved_for_web.append(ref)

    # Phase 3: Web search fallback for still-unresolved entries
    if unresolved_for_web:
        web_searchable = [r for r in unresolved_for_web
                          if r.title and len(r.title) >= 8
                          and not _is_junk_title(r.title, r)]
        if web_searchable:
            try:
                from .web_search import web_search_refs
                from .merge import merge as _merge
                loop = _get_loop()
                web_results = loop.run_until_complete(web_search_refs(web_searchable))
                for ref in web_searchable:
                    candidates = web_results.get(ref._batch_id, [])
                    if candidates:
                        try:
                            results[ref._batch_id] = _merge(ref, candidates)
                        except Exception:
                            results[ref._batch_id] = candidates[0][0]
                    elif ref._batch_id not in results:
                        results[ref._batch_id] = ref
            except Exception:
                logger.exception("Tier 4 web search failed")

        # Mark remaining as attempted
        for ref in unresolved_for_web:
            if ref._batch_id not in results:
                results[ref._batch_id] = ref

    return results


# --- Tier 5: Last resort ---

def _handle_tier5_batch(refs: List[Reference], rows: List[dict]) -> dict:
    """Last-ditch effort with URL resolution and substring variations — concurrent."""
    # Attach batch IDs, clean identifiers, and try to extract IDs from URLs.
    # _clean_doi is critical here because dirty DOIs (UTM params, fragments) were
    # never cleaned in earlier tiers for refs that were re-queued at level 4.
    for ref, row in zip(refs, rows):
        ref._batch_id = row["ref_id"]
        _clean_doi(ref)
        if ref.url and not ref.doi:
            _extract_ids_from_url(ref)

    # Refs with newly-extracted IDs go through batch ID lookup
    id_refs = [r for r in refs if r.doi or r.arxiv_id or r.pmid]
    title_refs = []
    url_repair_refs = []
    for ref in refs:
        if ref not in id_refs:
            _salvage_from_title(ref)
            _derive_title_from_url(ref)
            _clean_title(ref)
            if ref.url and (_is_junk_title(ref.title, ref) or not (ref.authors or ref.year or ref.doi)):
                url_repair_refs.append(ref)
            if ref.title and len(ref.title) >= 5 and not _is_junk_title(ref.title, ref):
                title_refs.append(ref)

    results = {}

    if url_repair_refs:
        try:
            loop = _get_loop()
            results.update(loop.run_until_complete(_repair_from_urls(url_repair_refs)))
        except Exception:
            logger.exception("Tier 5 URL repair failed")

    title_refs = [r for r in title_refs if r._batch_id not in results]

    # Batch lookup for refs where we extracted IDs from URLs
    if id_refs:
        from .batch_lookup import enrich_batch_by_id
        from .config import get_config
        cfg = get_config()
        loop = _get_loop()
        batch_results = loop.run_until_complete(
            enrich_batch_by_id(
                id_refs,
                s2_api_key=cfg.semantic_scholar_api_key,
                oa_email=cfg.openalex_email,
                oa_api_key=cfg.openalex_api_key,
                cr_email=cfg.crossref_email or cfg.openalex_email,
            )
        )
        results.update(batch_results)

    # Title refs: concurrent first pass
    if title_refs:
        enriched_list = _enrich_many_concurrent(title_refs, ["crossref", "openalex", "semantic_scholar"])

        retry_refs = []
        for ref, enriched in zip(title_refs, enriched_list):
            if enriched.completeness > (ref.completeness or 0) + 0.05:
                results[ref._batch_id] = enriched
            else:
                _aggressive_clean(ref)
                if ref.title and len(ref.title) >= 10:
                    retry_refs.append(ref)
                else:
                    results[ref._batch_id] = enriched

        # Second pass with cleaned titles
        if retry_refs:
            retry_results = _enrich_many_concurrent(retry_refs, ["crossref", "openalex"])
            for ref, enriched in zip(retry_refs, retry_results):
                results[ref._batch_id] = enriched

    # Phase 3: Web search for anything still unresolved
    unresolved = [r for r in refs if r._batch_id not in results or results[r._batch_id] is r]
    if unresolved:
        _clean_title_all(unresolved)
        searchable = [r for r in unresolved if r.title and len(r.title) >= 8]
        if searchable:
            try:
                from .web_search import web_search_refs
                from .merge import merge as _merge
                loop = _get_loop()
                web_results = loop.run_until_complete(web_search_refs(searchable))
                for ref in searchable:
                    candidates = web_results.get(ref._batch_id, [])
                    if candidates:
                        try:
                            results[ref._batch_id] = _merge(ref, candidates)
                        except Exception:
                            results[ref._batch_id] = candidates[0][0]
            except Exception:
                logger.exception("Tier 5 web search failed")

    # Phase 4: LLM Rescue for anything still highly incomplete
    still_unresolved = [r for r in refs if r._batch_id not in results or (results[r._batch_id].completeness or 0) < 0.4]
    if still_unresolved:
        try:
            from .llm import parse_citation_to_ref
            from .merge import merge as _merge
            loop = _get_loop()
            
            async def _llm_rescue_all(refs_to_rescue):
                sem = SafeSemaphore(5)
                async def _rescue_one(r):
                    async with sem:
                        # Gather every scrap of text we have to feed the LLM parser.
                        # NOTE: the Reference dataclass has no `notes` attribute
                        # (only the DB row does), so access everything defensively
                        # via getattr to avoid AttributeError crashing the whole
                        # Tier-5 batch. Pull abstract + any raw page title captured
                        # at import time (extras) for extra parsing context.
                        parts = [r.title or "", r.url or ""]
                        abstract = getattr(r, "abstract", None)
                        if abstract:
                            parts.append(abstract)
                        notes = getattr(r, "notes", None)
                        if notes:
                            parts.append(str(notes))
                        extras = getattr(r, "extras", None)
                        if isinstance(extras, dict):
                            for _k in ("original_page_title", "raw", "raw_citation", "unparsed"):
                                _v = extras.get(_k)
                                if _v:
                                    parts.append(str(_v))
                        text_to_parse = "\n".join(p for p in parts if p).strip()
                        if len(text_to_parse) < 10:
                            return r
                        llm_ref = await parse_citation_to_ref(text_to_parse)
                        if llm_ref:
                            try:
                                return _merge(r, [(llm_ref, 0.95)])
                            except Exception:
                                return llm_ref
                        return r
                return await asyncio.gather(*[_rescue_one(r) for r in refs_to_rescue])
            
            llm_results = loop.run_until_complete(_llm_rescue_all(still_unresolved))
            for original_r, rescued_r in zip(still_unresolved, llm_results):
                if rescued_r is not original_r:
                    results[original_r._batch_id] = rescued_r
        except Exception:
            logger.exception("Tier 5 LLM rescue failed")

    return results


# ---------------------------------------------------------------------------
# Tier definitions (query + handler)
# ---------------------------------------------------------------------------

# Tier 1: Has any standard identifier — BATCH MODE
TIERS.append((
    "eq.strategy_level <= 0 AND (r.doi IS NOT NULL AND r.doi != '' "
    "OR r.pmid IS NOT NULL AND r.pmid != '' "
    "OR r.arxiv_id IS NOT NULL AND r.arxiv_id != '' "
    "OR r.isbn IS NOT NULL AND r.isbn != '')",
    _handle_tier1_batch,
    500,
))

# Tier 2: Has URL but no identifier — BATCH MODE (extract IDs then batch)
TIERS.append((
    "eq.strategy_level <= 1 AND r.url IS NOT NULL AND r.url != '' "
    "AND (r.doi IS NULL OR r.doi = '') "
    "AND (r.pmid IS NULL OR r.pmid = '') "
    "AND (r.arxiv_id IS NULL OR r.arxiv_id = '') "
    "AND (r.isbn IS NULL OR r.isbn = '')",
    _handle_tier2_batch,
    100,
))

# Tier 3: Has title + metadata (year or authors present) but no identifier/URL
TIERS.append((
    "eq.strategy_level <= 2 AND r.title IS NOT NULL AND r.title != '' AND LENGTH(r.title) > 10 "
    "AND (r.year IS NOT NULL OR r.authors IS NOT NULL AND r.authors != '[]') "
    "AND (r.doi IS NULL OR r.doi = '') "
    "AND (r.url IS NULL OR r.url = '') "
    "AND (r.pmid IS NULL OR r.pmid = '') "
    "AND (r.arxiv_id IS NULL OR r.arxiv_id = '') "
    "AND (r.isbn IS NULL OR r.isbn = '')",
    _handle_tier3_batch,
    25,
))

# Tier 4: Has title only (no other metadata)
TIERS.append((
    "eq.strategy_level <= 3 AND r.title IS NOT NULL AND r.title != '' AND LENGTH(r.title) > 10 "
    "AND (r.doi IS NULL OR r.doi = '') "
    "AND (r.url IS NULL OR r.url = '') "
    "AND (r.pmid IS NULL OR r.pmid = '') "
    "AND (r.arxiv_id IS NULL OR r.arxiv_id = '') "
    "AND (r.isbn IS NULL OR r.isbn = '') "
    "AND (r.year IS NULL) "
    "AND (r.authors IS NULL OR r.authors = '[]')",
    _handle_tier4_batch,
    20,
))

# Tier 5: Everything else (garbage titles, no info, etc.)
TIERS.append((
    "1=1",  # catches anything still pending after tiers 1-4
    _handle_tier5_batch,
    20,
))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_junk_title(title: Optional[str], ref: Optional[Reference] = None,
                   strict: bool = False) -> bool:
    """True if the 'title' is not a real article title but scrape/file junk.

    strict=True restricts detection to CLEARLY non-title junk (filenames, hex
    blobs, bare domains, mastheads, the exact junk set) and skips the softer
    "short/generic search term" heuristics. Use strict=True for PARKING
    decisions (permanent give-up) so a real short title like 'On War' is never
    parked; use the default (lenient) for deciding whether to spend a search.

    Critically catches FILENAME-style titles — '0038932 pdf', 'Scan_Doc0040 pdf',
    hex/sha blobs, bare numeric codes — which must NEVER be sent to metadata APIs
    (no title search can ever resolve them, so they only burn quota). The
    aggressive structural checks are gated behind "looks like a filename"
    (had a file extension, or is a single token) so real article titles — even
    short ones like 'On War' — are never misclassified.
    """
    if not title:
        return True
    norm = re.sub(r"\s+", " ", title).strip().lower()
    if len(norm) < 5:
        return True
    junk = {
        "[no title]", "no title", "untitled", "just a moment",
        "just a moment...", "download limit exceeded", "limit exceeded",
        "access denied", "forbidden", "403 forbidden", "page not found",
        "google books", "science | aaas", "wspc scienceconnect io",
        "researchgate", "academia.edu", "pdf", "index",
    }
    if norm in junk or "just a moment" in norm:
        return True

    # Scrape artifacts that slip past the exact set:
    _toks = norm.split()
    # Bare site/domain name, e.g. 'sciencedirect com', 'jstor org' (<=3 tokens
    # ending in a TLD word) — a scraped hostname, never a real title.
    if len(_toks) <= 3 and _toks[-1] in (
        "com", "org", "net", "io", "edu", "gov", "ac", "co", "de", "uk"
    ):
        return True
    # Known aggregator/scrape page names when the title is short.
    if len(norm) < 40 and re.search(
        r"\b(sciencedirect|researchgate|jstor|springerlink|tandfonline|"
        r"semanticscholar|scienceconnect|ssrn|oup academic|google scholar)\b", norm
    ):
        return True
    # Journal masthead, e.g. 'Volume 3 | Issue 2 | Journal of ...'.
    if re.search(r"\bvol(?:ume)?\b[\s.\d|,]*\biss(?:ue)?\b", norm):
        return True

    # Check for short or highly generic search terms without authors or years.
    # These are SOFT signals (weak search terms, not definitively junk), so they
    # are skipped in strict mode to avoid permanently parking real short titles.
    has_meta = bool(ref and (ref.year or (ref.authors and len(ref.authors) > 0)))
    if not strict and not has_meta:
        # If it's a single word (no spaces)
        if " " not in norm:
            return True
        # If it's less than 8 characters
        if len(norm) < 8:
            return True
        # Common highly generic words or search terms
        generic_words = {
            "physics", "regret", "relay station", "relay stations", "relay-station",
            "introduction", "conclusion", "abstract", "index", "draft", "test",
            "untitled", "notes", "chapter 1", "chapter 2", "chapter 3",
            "discussion", "preface", "foreword", "editorial", "erratum",
            "appendix", "supplement", "bibliography", "references", "review",
            "book review", "comments", "reply", "response", "letter",
            "newsletter", "announcement", "obituary", "in memoriam"
        }
        if norm in generic_words:
            return True

    # Did it end in a file extension (incl. bare ' pdf')? Strip it for analysis.
    had_extension = bool(re.search(r"[\s._-]*\.?(pdf|docx?|html?|txt|epub|djvu|ps)$", norm))
    stem = re.sub(r"[\s._-]*\.?(pdf|docx?|html?|txt|epub|djvu|ps)$", "", norm).strip()
    if had_extension and len(stem) < 3:
        return True

    compact = re.sub(r"[\s._\-]+", "", stem)
    is_single_token = " " not in stem

    # Structural junk checks only apply to filename-shaped strings, so normal
    # multi-word article titles are never touched.
    if had_extension or is_single_token:
        n_digit = sum(c.isdigit() for c in compact)
        n_alpha = sum(c.isalpha() for c in compact)
        # Hex / sha-like blob, e.g. '609618d93c005a5387de7049cd2ccac65c01c064'
        if re.fullmatch(r"[0-9a-f]{12,}", compact):
            return True
        # Predominantly numeric code, e.g. '0038932', '8712294', '09149258 mit'
        if n_digit >= 5 and n_alpha <= 3:
            return True
        # Generic auto/scanner file names, e.g. 'scan_doc0040', 'img_0420'
        if re.match(r"^(scan|doc|document|img|image|file|untitled|copy|newdoc|"
                    r"output|attachment|tmp|temp|download|page|fulltext)\b",
                    re.sub(r"[\s._\-]+", " ", stem)) and re.search(r"\d", stem):
            return True
        # No real word anywhere: a real title has >=1 purely-alphabetic token of
        # >=4 chars containing a vowel; codes like 'scjrv016n001a004' do not.
        tokens = re.split(r"[\s._\-]+", stem)
        has_real_word = any(
            len(t) >= 4 and t.isalpha() and re.search(r"[aeiou]", t)
            for t in tokens
        )
        if not has_real_word:
            return True

    return False


def _unwrap_url(url: str) -> str:
    """Decode nested Google sorry/search URLs and repair malformed prefixes."""
    if not url:
        return url
    from urllib.parse import parse_qs, unquote, urlparse

    current = url.strip()
    for _ in range(6):
        previous = current
        current = unquote(current).strip()

        # Common bad joins seen in imported URL fields:
        # http://https://..., http://http://..., httphttps://...
        current = re.sub(r"^https?://(https?://)", r"\1", current, flags=re.I)
        current = re.sub(r"^httphttps://", "https://", current, flags=re.I)
        current = re.sub(r"^httphttp://", "http://", current, flags=re.I)

        parsed = urlparse(current)
        host = (parsed.netloc or "").lower()
        qs = parse_qs(parsed.query)

        candidate = ""
        if "google." in host and parsed.path.startswith("/sorry"):
            candidate = (qs.get("q") or qs.get("continue") or [""])[0]
        elif "google." in host and parsed.path.startswith(("/url", "/search")):
            candidate = (qs.get("url") or qs.get("q") or [""])[0]
        elif "url" in qs:
            raw = qs["url"][0]
            if raw.startswith("http") or "%3A%2F%2F" in raw:
                candidate = raw

        if candidate:
            current = candidate
            continue
        if current == previous:
            break

    return current


def _derive_title_from_url(ref):
    """Last-resort title seed from a readable URL slug."""
    if not ref.url or not _is_junk_title(ref.title, ref):
        return
    ref.url = _unwrap_url(ref.url)
    try:
        from urllib.parse import urlparse, unquote
        import re
        path = unquote(urlparse(ref.url).path or "")
        slug = path.rstrip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"\.(?:html?|pdf|aspx?)$", "", slug, flags=re.I)
        slug = re.sub(r"[_+.-]+", " ", slug).strip()
        if len(slug) >= 8 and re.search(r"[A-Za-z]", slug):
            ref.title = slug.title()
    except Exception:
        return


async def _repair_from_urls(refs) -> dict:
    """Scrape landing pages for citation metadata before giving up."""
    if not refs:
        return {}
    import httpx, asyncio
    from .web_search import _scrape_page_metadata
    from .merge import merge as _merge

    results = {}
    sem = SafeSemaphore(6)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        )
    }
    timeout = httpx.Timeout(15.0, connect=8.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        async def _one(ref):
            async with sem:
                scraped = await _scrape_page_metadata(ref.url, client) if ref.url else None
                if scraped:
                    try:
                        results[ref._batch_id] = _merge(ref, [(scraped, 0.72)])
                    except Exception:
                        results[ref._batch_id] = scraped
        await asyncio.gather(*[_one(r) for r in refs], return_exceptions=True)
    return results


def _extract_ids_from_url(ref):
    """Try to extract DOI, arXiv ID, PMID from a URL."""
    if not ref.url:
        return
    original_url = ref.url
    ref.url = _unwrap_url(ref.url)
    if ref.url != original_url:
        if ref.extras is None:
            ref.extras = {}
        ref.extras.setdefault("original_url", original_url)
        ref.sources["url_repair"] = 0.5
    url = ref.url
    import re
    from urllib.parse import unquote

    # --- Publisher URL Heuristics (Zero-Latency DOIs) ---
    # These run BEFORE the generic DOI patterns and set ref.doi when the DOI
    # is embedded in the URL path (e.g. Springer/ACM/Wiley use /doi/10.xxx/).
    # NOTE: IEEE is intentionally excluded here — ieeexplore.ieee.org URLs
    # don't contain the real DOI; the arnumber in the URL is NOT a valid DOI
    # suffix (IEEE DOIs look like 10.1109/TPAMI.2023.3241234, not 10.1109/IEEESTD.7784537).
    # IEEE refs fall through to the generic DOI pattern below, or get enriched
    # by title search in later tiers.

    # Springer / Nature — DOI appears in the URL path
    if "springer.com" in url or "nature.com" in url:
        m = re.search(r'/(10\.\d{4,}/[^\s"\'<>?&]+)', unquote(url))
        if m:
            ref.doi = m.group(1)

    # ACM
    elif "dl.acm.org" in url:
        m = re.search(r'doi/(?:abs|pdf|full)/(10\.\d{4,}/[^\s"\'<>?&]+)', url)
        if m:
            ref.doi = m.group(1)

    # Wiley
    elif "onlinelibrary.wiley.com" in url:
        m = re.search(r'doi/(?:abs|pdf|full)/(10\.\d{4,}/[^\s"\'<>?&]+)', url)
        if m:
            ref.doi = m.group(1)

    # DOI patterns
    for pat in [
        r'doi\.org/(10\.\d{4,}/[^\s"\'<>]+)',
        r'/doi/(?:abs|full|pdf)?/?(10\.\d{4,}/[^\s"\'<>]+)',
        r'doi[=:]\s*(10\.\d{4,}/[^\s"\'<>&]+)',
    ]:
        m = re.search(pat, url, re.I)
        if m:
            ref.doi = m.group(1).rstrip('.,;)')
            return

    # arXiv
    m = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})', url, re.I)
    if m:
        ref.arxiv_id = m.group(1)
        return

    # PubMed
    m = re.search(r'pubmed\.ncbi\.nlm\.nih\.gov/(\d+)', url, re.I)
    if m:
        ref.pmid = m.group(1)
        return


def _aggressive_clean(ref):
    """Aggressively clean title for last-resort searches."""
    import unicodedata
    if not ref.title:
        return
    s = ref.title.strip()
    s = re.sub(r'^\([^)]*\)\s*', '', s)
    s = re.sub(r'\s*\([^)]*\)$', '', s)
    s = s.strip('"\'""''')
    m = re.match(r'^(.{15,}?)\s*[|—–\-:]\s*(.{1,30})$', s)
    if m:
        s = m.group(1).strip()
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = unicodedata.normalize('NFC', s)
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) >= 10:
        ref.title = s
