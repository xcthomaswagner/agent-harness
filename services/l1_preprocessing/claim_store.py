"""Per-ticket claim + edge-detection state.

Extracted from ``main.py`` as part of the Phase 4 structural refactor.
Owns the in-process bookkeeping that protects the ingest path from
duplicate processing and from cascade webhooks firing follow-up runs on
tickets the trigger tag never left.

Exposed symbols:
  * ``_ACTIVE_TICKET_TTL_SEC`` — grace window before a stale claim
    auto-expires (reconnects a ticket that a crashed worker forgot to
    release).
  * ``_active_tickets`` / ``_active_tickets_lock`` — the claim map.
  * ``_last_trigger_state`` / ``_last_trigger_state_lock`` — edge memory
    for the trigger tag.
  * ``COUNTER_ACCEPTED_EDGE`` / ``COUNTER_SKIPPED_NOT_EDGE`` /
    ``COUNTER_SKIPPED_NO_TAG`` — counter key constants.
  * ``_webhook_counters`` / ``_webhook_counters_lock`` — cumulative
    outcome counts since process start (or last reset).
  * ``_bump_webhook_counter``, ``_try_claim_ticket``, ``_release_ticket``,
    ``_check_trigger_edge``, ``_clear_trigger_state``, ``_reset_state``,
    ``_get_webhook_counters`` — the surface the rest of the service (and
    the tests) rely on.

``main.py`` re-exports everything here for test back-compat.
"""

from __future__ import annotations

import collections
import threading
import time
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger()


def _load_trigger_state_from_db(ticket_id: str) -> bool | None:
    """Read persisted tag state for ticket_id. Returns None if no row."""
    try:
        from autonomy_store import autonomy_conn
        with autonomy_conn() as conn:
            row = conn.execute(
                "SELECT tag_present FROM trigger_state WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
            return bool(row[0]) if row is not None else None
    except Exception:
        return None


def _save_trigger_state_to_db(ticket_id: str, tag_present: bool) -> None:
    """Persist tag state for ticket_id to survive restarts."""
    try:
        from autonomy_store import autonomy_conn
        with autonomy_conn() as conn, conn:
            conn.execute(
                """
                INSERT INTO trigger_state (ticket_id, tag_present, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (ticket_id) DO UPDATE SET
                    tag_present = excluded.tag_present,
                    updated_at = excluded.updated_at
                """,
                (ticket_id, int(tag_present), datetime.now(UTC).isoformat()),
            )
    except Exception:
        pass


# --- Idempotency: prevent duplicate processing ---
#
# Claims are stored as ``{ticket_id: claim_timestamp_unix_seconds}``
# rather than a bare set so we can TTL-expire them. Before the TTL,
# the Redis worker path (queue_worker.process_ticket_sync runs in a
# separate process and never calls _release_ticket) could leak a
# claim forever on any ticket that didn't trigger L2 — the FastAPI
# process would then permanently reject every future webhook for
# that ticket with a 202/"already processing" response until
# restart. TTL matches the /api/agent-complete release window so a
# stuck claim auto-clears within a reasonable timebox.
_ACTIVE_TICKET_TTL_SEC = 15 * 60  # 15 minutes
_active_tickets: dict[str, float] = {}
_active_tickets_lock = threading.Lock()

# Jira webhook delivery dedup (Phase 5). Jira retries webhooks on any
# 5xx response — if L1 hiccups mid-processing after accepting the
# payload, Jira re-sends the same logical event and the ticket gets
# processed twice (double-enriched comments, double analyst runs,
# double-spawned agent teams). Dedup on the
# ``X-Atlassian-Webhook-Identifier`` header Jira attaches to each
# delivery. FIFO-evicted at _JIRA_DELIVERY_DEDUP_MAX to bound memory.
_JIRA_DELIVERY_DEDUP_MAX = 10_000
_processed_jira_deliveries: collections.OrderedDict[str, None] = (
    collections.OrderedDict()
)
_processed_jira_deliveries_lock = threading.Lock()

# Per-ticket edge-detection memory for the trigger tag. See
# _check_trigger_edge below for semantics, and
# session_2026_04_10_p0_p2_sf_live.md Finding 4 for the cascade incident
# that motivated this.
_last_trigger_state: dict[str, bool] = {}
_last_trigger_state_lock = threading.Lock()

# Webhook outcome counter keys — module-level constants so the three call
# sites and the tests don't drift on string literals.
COUNTER_ACCEPTED_EDGE = "ado_accepted_edge"
COUNTER_SKIPPED_NOT_EDGE = "ado_skipped_not_edge"
COUNTER_SKIPPED_NO_TAG = "ado_skipped_no_tag"

# Webhook outcome counters — give operators visibility into how often the
# edge-detection path is blocking cascades vs. accepting fresh triggers.
# Values are monotonic counts since process start (or last _reset_state).
# Exposed via GET /stats/webhooks.
_webhook_counters: dict[str, int] = {
    COUNTER_ACCEPTED_EDGE: 0,
    COUNTER_SKIPPED_NOT_EDGE: 0,
    COUNTER_SKIPPED_NO_TAG: 0,
}
_webhook_counters_lock = threading.Lock()


def _bump_webhook_counter(key: str) -> None:
    """Increment a webhook outcome counter. Thread-safe."""
    with _webhook_counters_lock:
        _webhook_counters[key] = _webhook_counters.get(key, 0) + 1


def _try_claim_ticket(ticket_id: str) -> bool:
    """Atomically check if a ticket is active and claim it if not.

    Returns True if claimed (caller should process), False if already
    active. Thread-safe via lock — prevents TOCTOU race between check
    and add. TTL-expires stale claims so a dropped release (Redis
    worker crash, cross-process claim leak, forgotten _release_ticket
    in a code path that didn't bubble up) unblocks future webhooks
    after ``_ACTIVE_TICKET_TTL_SEC`` seconds instead of wedging until
    a process restart.
    """
    now = time.time()
    with _active_tickets_lock:
        claimed_at = _active_tickets.get(ticket_id)
        if claimed_at is not None and (now - claimed_at) < _ACTIVE_TICKET_TTL_SEC:
            return False
        if claimed_at is not None:
            logger.warning(
                "ticket_claim_ttl_expired",
                ticket_id=ticket_id,
                stale_age_sec=round(now - claimed_at),
            )
        _active_tickets[ticket_id] = now
        return True


def _release_ticket(ticket_id: str) -> None:
    """Release a ticket from the active set."""
    with _active_tickets_lock:
        _active_tickets.pop(ticket_id, None)


def _jira_delivery_seen(delivery_id: str) -> bool:
    """Check + record a Jira webhook delivery ID atomically (Phase 5).

    Returns True if this delivery was already processed (caller should
    skip), False otherwise. FIFO-evicts the oldest entry when the set
    exceeds ``_JIRA_DELIVERY_DEDUP_MAX`` so a long-running process
    doesn't leak memory one delivery ID at a time.

    Callers should only invoke this with a non-empty ID — a missing
    header is handled by the caller and does NOT land here (we can't
    dedup what we can't identify).
    """
    with _processed_jira_deliveries_lock:
        if delivery_id in _processed_jira_deliveries:
            return True
        _processed_jira_deliveries[delivery_id] = None
        while len(_processed_jira_deliveries) > _JIRA_DELIVERY_DEDUP_MAX:
            _processed_jira_deliveries.popitem(last=False)
        return False


def _check_trigger_edge(
    ticket_id: str,
    tag_present_now: bool,
    was_present_before: bool | None = None,
) -> bool:
    """Edge-detect the trigger tag for a ticket.

    Returns True if this webhook represents a new trigger — i.e., the tag
    transitioned from absent to present *on this webhook*. Returns False
    when the tag was already present beforehand, in which case this
    webhook is almost certainly a non-trigger side effect (PR merge,
    comment, field edit, etc.) and should not start a new pipeline run.

    Two signals are considered, in order:

    1. **Payload-based** (``was_present_before`` is not ``None``): the
       ADO ``resource.fields`` delta gave us the prior value directly.
       This is authoritative for *this* webhook and survives L1 restarts
       because the signal is embedded in the webhook. Update in-process
       memory too so a later no-delta webhook has a fresh baseline.

    2. **In-process memory fallback** (``was_present_before`` is ``None``):
       the delta wasn't in the payload (typical for non-tag field edits,
       or ``workitem.created``). Compare against the last remembered
       state; treat a never-seen ticket with the tag present as a fresh
       edge. NOTE: this path is vulnerable to L1 restarts — the first
       post-restart webhook for a ticket that had the tag before the
       restart will be treated as a fresh edge even if it shouldn't be.
       The payload path above does not have this weakness.

    Thread-safe via lock. Also records the current tag state for the
    next webhook to compare against.
    """
    with _last_trigger_state_lock:
        if ticket_id not in _last_trigger_state:
            db_state = _load_trigger_state_from_db(ticket_id)
            if db_state is not None:
                _last_trigger_state[ticket_id] = db_state
        prev_mem = _last_trigger_state.get(ticket_id, False)
        _last_trigger_state[ticket_id] = tag_present_now
        _save_trigger_state_to_db(ticket_id, tag_present_now)
        if was_present_before is not None:
            # Payload delta is authoritative for this webhook's transition.
            return tag_present_now and not was_present_before
        # Fallback: memory-based dedupe. New trigger iff tag is present
        # now AND was NOT present on the last webhook we saw.
        return tag_present_now and not prev_mem


def _clear_trigger_state(ticket_id: str) -> None:
    """Forget the last known tag state for a ticket.

    Used when the tag is observed absent — ensures the next time the tag
    comes back we treat it as a fresh edge. Also used for manual reset in
    tests.
    """
    with _last_trigger_state_lock:
        _last_trigger_state.pop(ticket_id, None)
    try:
        from autonomy_store import autonomy_conn
        with autonomy_conn() as conn, conn:
            conn.execute(
                "DELETE FROM trigger_state WHERE ticket_id = ?",
                (ticket_id,),
            )
    except Exception:
        pass


def _reset_state(*extra: Any) -> None:
    """Reset all claim-store in-memory state.

    Centralised so test fixtures only need to learn one callable.
    ``main._reset_state`` wraps this and also clears the adapter /
    pipeline singletons it owns; the extra-args signature is accepted
    but ignored to keep compatibility with any caller that passes a
    side-argument (none do today, but cheap insurance against drift).
    """
    del extra  # kept for forward-compat; no current callers pass args
    with _active_tickets_lock:
        _active_tickets.clear()
    with _last_trigger_state_lock:
        _last_trigger_state.clear()
    with _webhook_counters_lock:
        for key in _webhook_counters:
            _webhook_counters[key] = 0
    with _processed_jira_deliveries_lock:
        _processed_jira_deliveries.clear()


def _get_webhook_counters() -> dict[str, int]:
    """Return a thread-safe snapshot of the current counter values."""
    with _webhook_counters_lock:
        return dict(_webhook_counters)
