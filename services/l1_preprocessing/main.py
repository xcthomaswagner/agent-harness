"""L1 Pre-Processing Service — Webhook receiver and ticket processing pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import re
import secrets
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.env_sanitize import sanitized_env

import tracer
from adapters.ado_adapter import AdoAdapter, extract_tag_transition
from adapters.jira_adapter import JiraAdapter
from autonomy_dashboard import router as autonomy_dashboard_router
from autonomy_ingest import ingest_jira_bug
from autonomy_ingest import router as autonomy_router
from autonomy_jira_bug import normalize_jira_bug
from autonomy_store import ensure_schema, open_connection, resolve_db_path
from client_profile import find_profile_by_ado_project
from config import settings
from diagnostic import run_diagnostic_checklist
from investigate_command import (
    DISCUSS_BASE_URL as _DISCUSS_BASE_URL,
)
from investigate_command import (
    build_investigate_command as _build_investigate_command,
)
from learning_api import router as learning_api_router
from learning_dashboard import router as learning_dashboard_router
from models import TicketPayload
from pipeline import Pipeline
from redaction import redact
from trace_dashboard import router as trace_router
from tracer import (
    ARTIFACT_CODE_REVIEW,
    ARTIFACT_EFFECTIVE_CLAUDE_MD,
    ARTIFACT_QA_MATRIX,
    ARTIFACT_SESSION_LOG,
    ARTIFACT_SESSION_STREAM,
    ARTIFACT_TOOL_INDEX,
    append_trace,
    atomic_write_text,
    consolidate_worktree_logs,
    find_artifact,
    generate_trace_id,
    latest_artifacts,
    read_trace,
    redact_entry_in_place,
)
from unified_dashboard import router as unified_router

logger = structlog.get_logger()


def _require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Dependency that enforces API key auth on internal control-plane endpoints.

    Skipped when API_KEY is not configured (local dev mode). Uses
    ``hmac.compare_digest`` for the comparison so the check is
    constant-time — a plain ``!=`` leaks byte-by-byte timing info
    about the configured secret because CPython short-circuits
    string equality on the common-prefix length.
    """
    if not settings.api_key:
        return  # No key configured — open access (local dev)
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


app = FastAPI(
    title="Agentic Harness L1 Pre-Processing",
    description="Receives Jira/ADO webhooks, enriches tickets, dispatches to Agent Teams.",
    version="0.1.0",
)
app.include_router(unified_router)
app.include_router(trace_router)
app.include_router(autonomy_router)
app.include_router(autonomy_dashboard_router)
app.include_router(learning_api_router)
app.include_router(learning_dashboard_router)


@app.on_event("startup")
async def _validate_config() -> None:
    """Warn about missing configuration at startup."""
    if not settings.webhook_secret:
        logger.warning(
            "webhook_secret_not_configured",
            hint="Webhook signature validation is DISABLED. Set WEBHOOK_SECRET in .env",
        )
    if not settings.anthropic_api_key:
        logger.error("anthropic_api_key_missing", hint="Analyst will fail without API key")
    if not settings.jira_base_url and not settings.ado_org_url:
        logger.warning("no_ticket_source_configured", hint="Set JIRA_BASE_URL or ADO_ORG_URL")

    # Check reference documentation URLs in background (non-blocking).
    # Use _spawn_background_task so the task auto-removes itself from
    # the tracking set when it finishes.
    from url_checker import check_reference_urls

    _spawn_background_task(check_reference_urls())

    # Start the self-learning outcomes scheduler when enabled.
    if settings.learning_outcomes_enabled:
        _spawn_background_task(_learning_outcomes_loop())

# Hold references to background tasks so they aren't garbage-collected.
# Tasks are removed from the set via ``_spawn_background_task`` below —
# without the done-callback the set grows unbounded for every
# fire-and-forget ``asyncio.create_task`` ever issued.
_background_tasks: set[asyncio.Task[Any]] = set()


def _spawn_background_task(coro: Any) -> asyncio.Task[Any]:
    """Fire-and-forget a coroutine with safe lifecycle management.

    Previously each caller did
    ``_background_tasks.add(asyncio.create_task(coro))`` without a
    matching ``add_done_callback(_background_tasks.discard)``, which
    turned the set into a permanent leak — every task ever spawned
    stayed referenced (and its result/exc_info retained) for the
    process lifetime. This helper wraps both halves: the task is
    held strongly until it completes, then auto-discarded.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _learning_outcomes_loop() -> None:
    """Periodically run the lesson-outcomes job.

    Runs ``run_outcomes`` in a thread (the job does subprocess calls
    that would block the event loop otherwise), sleeps the configured
    interval, repeats. Each iteration is isolated — a raised
    exception logs and the loop continues.

    Sleeps BEFORE the first run rather than after, so rapid L1
    restarts (deploy churn, crash loops) don't each fire a fresh
    gh-polling pass. A randomized initial delay de-syncs multiple
    L1 instances that start together.
    """
    import random

    from learning_miner.outcomes import run_outcomes

    interval_sec = max(
        60, int(settings.learning_outcomes_interval_hours * 3600)
    )
    initial_delay = min(interval_sec, 60 + int(random.random() * 60))
    logger.info(
        "learning_outcomes_scheduler_started",
        interval_sec=interval_sec,
        initial_delay_sec=initial_delay,
    )
    await asyncio.sleep(initial_delay)
    while True:
        try:
            await asyncio.to_thread(run_outcomes)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "learning_outcomes_loop_iteration_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        await asyncio.sleep(interval_sec)

_jira_adapter: JiraAdapter | None = None
_ado_adapter: AdoAdapter | None = None
_pipeline: Pipeline | None = None


def _get_jira_adapter() -> JiraAdapter:
    """Return the Jira adapter, creating it lazily on first use."""
    global _jira_adapter
    if _jira_adapter is None:
        _jira_adapter = JiraAdapter(settings=settings)
    return _jira_adapter


def _get_ado_adapter() -> AdoAdapter:
    """Return the ADO adapter, creating it lazily on first use."""
    global _ado_adapter
    if _ado_adapter is None:
        _ado_adapter = AdoAdapter(settings=settings)
    return _ado_adapter


def _get_pipeline() -> Pipeline:
    """Return the pipeline, creating it lazily on first use."""
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline(settings=settings)
    return _pipeline


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
_active_tickets_lock = __import__("threading").Lock()

# Per-ticket edge-detection memory for the trigger tag. See
# _check_trigger_edge below for semantics, and
# session_2026_04_10_p0_p2_sf_live.md Finding 4 for the cascade incident
# that motivated this.
_last_trigger_state: dict[str, bool] = {}
_last_trigger_state_lock = __import__("threading").Lock()

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
_webhook_counters_lock = __import__("threading").Lock()


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
        prev_mem = _last_trigger_state.get(ticket_id, False)
        _last_trigger_state[ticket_id] = tag_present_now
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


def _reset_state() -> None:
    """Reset all module-level singletons and per-ticket memory.

    For use by test fixtures that need a clean slate between tests. Avoids
    having test code reach into module internals individually — when a new
    singleton is added here, only this one function needs to learn about it.
    """
    global _jira_adapter, _ado_adapter, _pipeline
    _jira_adapter = None
    _ado_adapter = None
    _pipeline = None
    with _active_tickets_lock:
        _active_tickets.clear()
    with _last_trigger_state_lock:
        _last_trigger_state.clear()
    with _webhook_counters_lock:
        for key in _webhook_counters:
            _webhook_counters[key] = 0


# --- Pipeline processing (background) ---


def _enqueue_or_background(
    ticket: TicketPayload, background_tasks: BackgroundTasks,
    trace_id: str = "",
) -> str:
    """Try to enqueue via Redis, fall back to FastAPI BackgroundTasks.

    Returns "queued", "background", or "duplicate".
    """
    if not _try_claim_ticket(ticket.id):
        logger.info("ticket_duplicate_skipped", ticket_id=ticket.id)
        return "duplicate"

    from queue_worker import enqueue_ticket

    job_id = enqueue_ticket(ticket)
    if job_id:
        logger.info("ticket_queued", ticket_id=ticket.id, job_id=job_id)
        return "queued"

    background_tasks.add_task(_process_ticket, ticket, trace_id)
    return "background"


def _dispatch_ticket(
    ticket: TicketPayload,
    background_tasks: BackgroundTasks,
    *,
    source: str,
    event: str,
    trace_id: str | None = None,
) -> dict[str, str]:
    """Shared tail for every ticket-ingest endpoint.

    Handles the boilerplate that ``jira_webhook`` / ``ado_webhook`` /
    ``manual_process_ticket`` used to copy-paste: mint a trace id,
    emit the breadcrumb log line, append a ``webhook`` trace entry
    so the dashboard lists the run, hand off to
    ``_enqueue_or_background``, and return the ``accepted`` /
    ``skipped`` response dict. Previously ``manual_process_ticket``
    silently omitted the ``append_trace`` breadcrumb, so tickets
    dispatched via the manual endpoint were harder to find in the
    trace list; the helper fixes that drift by construction.
    """
    tid = trace_id or generate_trace_id()
    logger.info(
        event, ticket_id=ticket.id, ticket_type=ticket.ticket_type
    )
    append_trace(
        ticket.id, tid, "webhook", event,
        ticket_type=ticket.ticket_type, source=source,
        ticket_title=ticket.title,
    )
    dispatch = _enqueue_or_background(
        ticket, background_tasks, trace_id=tid
    )
    if dispatch == "duplicate":
        return {
            "status": "skipped",
            "ticket_id": ticket.id,
            "reason": "already processing",
        }
    return {
        "status": "accepted",
        "ticket_id": ticket.id,
        "dispatch": dispatch,
    }


async def _process_ticket(ticket: TicketPayload, trace_id: str = "") -> None:
    """Process a normalized ticket through the L1 pipeline."""
    log = logger.bind(ticket_id=ticket.id, source=ticket.source)
    log.info("processing_ticket_started")
    tid = trace_id or generate_trace_id()
    append_trace(ticket.id, tid, "pipeline", "processing_started",
                 ticket_type=ticket.ticket_type, source=ticket.source)

    try:
        result = await _get_pipeline().process(ticket, trace_id=tid)
        log.info("processing_ticket_completed", **result)
        trace_data = {k: v for k, v in result.items() if k != "ticket_id"}
        append_trace(ticket.id, tid, "pipeline", "processing_completed", **trace_data)
        # Only release if L2 was NOT spawned — spawned tickets stay claimed
        # until /api/agent-complete is called. This prevents duplicate processing
        # from ADO webhooks triggered by our own comment/status write-backs.
        # Edge-detection state is cleared alongside the active-ticket release so
        # a future re-trigger (user re-tags after fixing the ticket) is not
        # silently dropped as "not a new edge" — the trigger state otherwise
        # stays True from when _check_trigger_edge set it at webhook receipt.
        if not result.get("spawn_triggered"):
            _release_ticket(ticket.id)
            _clear_trigger_state(ticket.id)
    except Exception as exc:
        log.exception("processing_ticket_failed")
        append_trace(
            ticket.id, tid, "pipeline", "error",
            error_type=type(exc).__name__,
            error_message=str(exc)[:500],
        )
        # Same rationale as the no-spawn release above: clear edge-detection
        # state so the next webhook for this ticket can re-trigger after the
        # failure is addressed.
        _release_ticket(ticket.id)
        _clear_trigger_state(ticket.id)


# --- Endpoints ---


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check — minimal liveness probe.

    Previously this endpoint returned a boolean for each configured
    secret (anthropic_api_key, jira_configured, ado_configured,
    webhook_secret, client_repo). Those flags were reachable without
    auth and told an attacker exactly which integrations were wired
    up — useful for targeting. We no longer expose that surface; the
    endpoint now reports only liveness. Operators who need
    configuration visibility should look at the service logs or the
    internal /stats endpoints (which are API-key protected).
    """
    return {"status": "ok"}


@app.get("/stats/webhooks")
async def webhook_stats() -> dict[str, object]:
    """Cumulative webhook outcome counts since process start / last reset.

    Exposes edge-detection behavior so operators can see cascade pressure
    without grepping logs. A steady climb in `ado_skipped_not_edge` relative
    to `ado_accepted_edge` indicates ADO is firing many follow-up webhooks
    for the same tagged ticket — typically harmless (the fix for Finding 4
    is working), but worth investigating if the ratio seems wrong.
    """
    with _webhook_counters_lock:
        counters = dict(_webhook_counters)
    return {
        "counters": counters,
        "release_delay_sec": settings.agent_complete_release_delay_sec,
        "active_tickets": len(_active_tickets),
        "tracked_trigger_states": len(_last_trigger_state),
    }


# --- Trace bundle + individual artifact endpoints ---
#
# These power the post-mortem investigation workflow. `/bundle` packages the
# full trace context into a single gzipped tar for a developer to run a local
# `claude -p` investigation against. `/artifact/<type>` serves one file at a
# time — used by the dashboard Raw Downloads panel.
#
# Every file written into the bundle is passed through ``redact()`` before
# being tarred. The trace store itself is already redacted at consolidation
# time by ``consolidate_worktree_logs``, so most files get a belt-and-
# suspenders second pass (a no-op thanks to the redactor's idempotency
# guarantee). The one exception is ``session-stream.jsonl`` — stored by
# reference, redacted for the first time here. The readme.txt reports the
# total redaction count from the bundle pass.


def _validate_ticket_id(ticket_id: str) -> str:
    """Guard the ticket_id path parameter against traversal / injection.

    Must match ``[A-Za-z0-9_-]+`` — no dots, no slashes, no null bytes.
    Returns the ticket_id on success or raises HTTPException(400).
    """
    if not ticket_id or not re.match(r"^[A-Za-z0-9_-]+$", ticket_id):
        raise HTTPException(status_code=400, detail="Invalid ticket_id")
    return ticket_id




def _extract_ticket_payload(
    ticket_id: str, entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive a ticket.json payload for the bundle.

    Preference order:
    1. `<client_repo.parent>/worktrees/<branch>/.harness/ticket.json` if we can
       figure out the branch from trace entries and the file still exists.
    2. A minimal synthetic payload with the ticket_id and any metadata we can
       glean from the first webhook_received / processing_started entry.
    """
    branch = ""
    for e in entries:
        b = e.get("branch", "")
        if isinstance(b, str) and b:
            branch = b
            break

    branch_ok = re.fullmatch(r"[A-Za-z0-9_./][A-Za-z0-9_./ +-]*", branch)
    if branch and settings.default_client_repo and branch_ok:
        candidate = (
            Path(settings.default_client_repo).parent
            / "worktrees" / branch / ".harness" / "ticket.json"
        )
        try:
            if candidate.exists():
                return json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    # Fallback: synthesize from the first webhook / pipeline / analyst entry
    # so the bundle always contains *some* ticket.json the investigator can
    # anchor on. Field names here must match `models.TicketPayload`
    # (checked against models.py, not guessed): `id`, `title`, `ticket_type`,
    # `source`, `description`, `priority`, `acceptance_criteria`. The
    # post-mortem-analyst skill and downstream consumers read `title` and
    # `description` directly — using `ticket_title` would silently break them.
    synthetic: dict[str, Any] = {
        "id": ticket_id,
        # ticket_id kept as a non-TicketPayload hint so old bundle consumers
        # that keyed off it still work; do not rely on it going forward.
        "ticket_id": ticket_id,
    }
    payload_fields = (
        "title",
        "ticket_type",
        "source",
        "description",
        "priority",
        "acceptance_criteria",
        "labels",
    )
    for e in entries:
        event = e.get("event", "")
        if (
            "webhook_received" in event
            or "processing_started" in event
            or "analyst_completed" in event
        ):
            for key in payload_fields:
                val = e.get(key)
                if val:
                    synthetic[key] = val
            # Also scavenge a nested "ticket" / "enriched_ticket" object if
            # the entry wrapped the payload — the analyst_completed event
            # typically serializes the full EnrichedTicket under one of
            # these keys.
            for wrapper_key in ("ticket", "enriched_ticket", "payload"):
                wrapped = e.get(wrapper_key)
                if isinstance(wrapped, dict):
                    for key in payload_fields:
                        if key in wrapped and key not in synthetic:
                            synthetic[key] = wrapped[key]
            if any(k in synthetic for k in payload_fields):
                break
    return synthetic


_BUNDLE_README = """\
Trace Bundle for ticket {ticket_id}
Generated: {timestamp}

This archive contains the full trace context for an agent run — use it as a
self-contained directory to run `claude -p` against when post-morteming a
failed or surprising run.

Files:
  pipeline.jsonl         Full trace JSONL from the L1 trace store (all layers)
  session-stream.jsonl   Raw Claude Code stream (if captured)
  session.log            Narrative session log (preview, up to 5000 chars)
  effective-CLAUDE.md    CLAUDE.md the agent was operating under
  qa-matrix.md           QA validator report (if present)
  code-review.md         Code reviewer report (if present)
  tool-index.json        Declarative tool-call index from the stream (if present)
  diagnostic.json        Six-item diagnostic checklist (always present)
  ticket.json            Normalized ticket payload (models.TicketPayload shape)

{redaction_block}

How to investigate:

  mkdir -p /tmp/trace-{ticket_id}
  curl -sSf http://localhost:8000/traces/{ticket_id}/bundle | tar xz -C /tmp/trace-{ticket_id}
  cd /tmp/trace-{ticket_id}
  claude -p "Read all the files in this directory. Start by reading diagnostic.json (if it exists) and tool-index.json, then tell me what the first deviation point was. Cite specific line numbers for every claim."
"""  # noqa: E501 — investigation prompt is a single shell literal for easy copy-paste


_REDACTION_BLOCK_REDACTED = """\
*** REDACTED ({count} patterns) ***

This bundle has been passed through the L1 secret redactor. {count} token
patterns matching known-credential shapes (API keys, JWTs, PEM private keys,
bearer headers, etc.) were replaced with [REDACTED] placeholders. The
redactor is idempotent, so re-running it over this bundle is safe. If a
pattern update lands after this bundle was built, POST /admin/re-redact on
the L1 service will rescan the underlying trace store and re-export a clean
copy.

See services/l1_preprocessing/redaction.py for the full pattern list and the
entropy-fallback heuristic that catches novel high-entropy tokens."""


_REDACTION_BLOCK_CLEAN = """\
*** REDACTED (0 patterns) ***

The L1 secret redactor ran over this bundle and flagged no token patterns.
Every file was still scanned — a zero count means the trace genuinely
contained no recognized credential shapes, not that redaction was skipped.
If you think a secret leaked through, check the entropy-fallback threshold
in services/l1_preprocessing/redaction.py and consider adding an explicit
pattern."""


def _build_bundle(ticket_id: str, entries: list[dict[str, Any]]) -> bytes:
    """Build the in-memory gzipped tar bundle for a trace.

    Kept small and simple: for almost all traces the JSONL trace store is a
    few dozen KB, the session stream is the only thing that can be large, and
    even then we're copying a file off disk into a tar — well within what
    fits in memory without worrying about streaming.

    Every file written into the tar is first passed through ``redact()``.
    For artifact contents pulled from the trace store, this is a belt-and-
    suspenders pass — the store is already redacted by
    ``consolidate_worktree_logs`` — and the redactor's idempotency guarantee
    means the second pass is a no-op on already-clean content. The
    session-stream file is a special case: it's stored by reference rather
    than inline in the trace store, so the first real redaction pass on that
    data happens here.
    """
    buf = io.BytesIO()
    added: list[str] = []
    total_redacted = 0

    def _redact_bytes(payload: bytes) -> tuple[bytes, int]:
        """Decode, redact, re-encode.

        Uses ``errors="surrogateescape"`` on both ends so non-UTF-8 bytes
        round-trip losslessly through ``redact()``. Strict UTF-8 decoding
        would silently bypass redaction on any file containing stray
        bytes — common in ``session-stream.jsonl`` where tool output can
        include terminal escape sequences, latin-1 from legacy systems,
        or binary dumps from tools like ``grep``/``curl``. Since the
        bundle path is the ONLY redaction pass that ever runs over the
        session stream (consolidation deliberately stores it by
        reference), a silent bypass would leak credentials that happened
        to share a file with any non-UTF-8 byte.
        """
        text = payload.decode("utf-8", errors="surrogateescape")
        redacted_text, n = redact(text)
        return redacted_text.encode("utf-8", errors="surrogateescape"), n

    # Build the artifact index once — six lookups below would otherwise
    # each scan the full entries list in reverse. See tracer.latest_artifacts.
    artifacts = latest_artifacts(entries)

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:

        def _add_bytes(name: str, payload: bytes) -> None:
            nonlocal total_redacted
            redacted_payload, n = _redact_bytes(payload)
            total_redacted += n
            info = tarfile.TarInfo(name=name)
            info.size = len(redacted_payload)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(redacted_payload))
            added.append(name)

        # pipeline.jsonl — full trace store state serialized one entry per line
        pipeline_lines = [json.dumps(entry) for entry in entries]
        _add_bytes("pipeline.jsonl", ("\n".join(pipeline_lines) + "\n").encode())

        # session-stream.jsonl — copied byte-for-byte from disk if we have it.
        # This is the ONLY place the stream gets redacted: consolidation
        # leaves the on-disk file alone so it remains available as a raw
        # forensic escape hatch for dev-local access.
        stream_entry = artifacts.get(ARTIFACT_SESSION_STREAM)
        if stream_entry:
            stream_path_str = str(stream_entry.get("artifact_path", ""))
            if stream_path_str:
                stream_path = Path(stream_path_str)
                try:
                    if stream_path.exists():
                        _add_bytes("session-stream.jsonl", stream_path.read_bytes())
                except OSError:
                    pass

        # session.log — 5000-char preview is the best we have in the trace store
        log_entry = artifacts.get(ARTIFACT_SESSION_LOG)
        if log_entry and log_entry.get("content"):
            _add_bytes("session.log", str(log_entry["content"]).encode())

        # effective-CLAUDE.md — instructions the agent was actually running under
        claude_md_entry = artifacts.get(ARTIFACT_EFFECTIVE_CLAUDE_MD)
        if claude_md_entry and claude_md_entry.get("content"):
            _add_bytes("effective-CLAUDE.md", str(claude_md_entry["content"]).encode())

        # qa-matrix.md
        qa_entry = artifacts.get(ARTIFACT_QA_MATRIX)
        if qa_entry and qa_entry.get("content"):
            _add_bytes("qa-matrix.md", str(qa_entry["content"]).encode())

        # code-review.md
        review_entry = artifacts.get(ARTIFACT_CODE_REVIEW)
        if review_entry and review_entry.get("content"):
            _add_bytes("code-review.md", str(review_entry["content"]).encode())

        # diagnostic.json — computed inline at bundle time. Commit 3's
        # `run_diagnostic_checklist` is a pure analyzer (no persistence), so
        # it's safe to call from here without touching the trace store. This
        # keeps commit 3 reusable for both the dashboard render AND the
        # bundle export.
        try:
            checks = run_diagnostic_checklist(entries)
            _add_bytes("diagnostic.json", json.dumps(checks, indent=2).encode())
        except Exception:
            logger.exception("diagnostic_bundle_failed", ticket_id=ticket_id)
            # Don't fail the bundle if diagnostic computation crashes —
            # continue without it so the investigator still gets the other
            # artifacts.

        # tool-index.json — declarative tool-call summary
        tool_index_entry = artifacts.get(ARTIFACT_TOOL_INDEX)
        if tool_index_entry and "index" in tool_index_entry:
            _add_bytes(
                "tool-index.json",
                json.dumps(tool_index_entry["index"], indent=2).encode(),
            )

        # ticket.json
        ticket_payload = _extract_ticket_payload(ticket_id, entries)
        _add_bytes("ticket.json", json.dumps(ticket_payload, indent=2).encode())

        # readme.txt (always last so the investigator sees it alongside files).
        # NOT redacted — it's purely generated content, and it reports the
        # redaction count from the preceding files. Sent via raw tar addfile
        # to bypass _add_bytes which would double-count the redaction pass.
        redaction_block = (
            _REDACTION_BLOCK_REDACTED.format(count=total_redacted)
            if total_redacted > 0
            else _REDACTION_BLOCK_CLEAN
        )
        readme = _BUNDLE_README.format(
            ticket_id=ticket_id,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            redaction_block=redaction_block,
        )
        readme_bytes = readme.encode()
        info = tarfile.TarInfo(name="readme.txt")
        info.size = len(readme_bytes)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(readme_bytes))
        added.append("readme.txt")

    logger.info(
        "trace_bundle_built",
        ticket_id=ticket_id,
        files=added,
        redaction_count=total_redacted,
    )
    return buf.getvalue()


# Route-ordering note: this endpoint is registered AFTER `trace_router` is
# included (``app.include_router(trace_router)`` above, near the top of the
# file). It works regardless of declaration order because
# `/traces/{id}` and `/traces/{id}/bundle` differ in segment count and
# FastAPI/Starlette matches by segment count before falling back to dynamic
# path parameters. Do NOT add a catch-all like ``/{rest:path}`` to
# trace_router or this route will be silently shadowed.
@app.get("/traces/{ticket_id}/bundle")
async def trace_bundle(ticket_id: str) -> Response:
    """Return a gzipped tar of the full trace context for ``ticket_id``.

    See ``_BUNDLE_README`` above for the file listing and the redaction
    warning. The bundle is built entirely in-memory — fine for today because
    the largest payload is session-stream.jsonl and traces big enough to
    cause memory pressure are rare.
    """
    _validate_ticket_id(ticket_id)
    entries = read_trace(ticket_id)
    if not entries:
        raise HTTPException(status_code=404, detail=f"No trace for ticket '{ticket_id}'")

    payload = _build_bundle(ticket_id, entries)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    filename = f"trace-{ticket_id}-{timestamp}.tar.gz"
    return Response(
        content=payload,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Discuss-with-Claude (Tier 1 post-mortem investigation handoff) ---
#
# The dashboard's "Investigate this trace locally" disclosure renders a copy-
# paste shell snippet that downloads the bundle and launches a local
# ``claude -p`` session. This endpoint returns the same snippet as JSON, plus
# a short-lived opaque session token and the bundle URL, so a future dashboard
# button (or a CLI wrapper) can hand the developer everything needed to start
# an investigation in one call.
#
# Base URL is hard-coded to ``http://localhost:8000`` to match the dashboard's
# existing investigate_cmd template — the whole feature is Tier 1, single-dev,
# local-only. Don't be clever about deriving it from the request host; that
# breaks when ngrok forwards public traffic to loopback.

_DISCUSS_SESSION_TTL = timedelta(hours=1)
_DISCUSS_AUDIT_FILENAME = "discuss-audit.jsonl"


def _discuss_audit_path() -> Path:
    """Return the on-disk path for ``discuss-audit.jsonl``.

    Kept as a sibling directory next to ``LOGS_DIR`` (``<LOGS_DIR>.parent/
    audit/``) rather than inside it. If the audit file lived in
    ``LOGS_DIR`` it would be indistinguishable from a per-ticket trace
    file: ``_validate_ticket_id`` accepts ``discuss-audit``, so
    ``GET /traces/discuss-audit/bundle``, ``list_traces``, and the
    dashboard would all treat the audit log as a phantom ticket and
    expose its contents (cross-ticket ``session_token`` / ``source_ip`` /
    ``user_agent`` values) to unauthenticated readers. Returning a
    function instead of a module-level constant so tests that monkey-patch
    ``tracer.LOGS_DIR`` see the updated sibling path without extra plumbing.
    """
    return tracer.LOGS_DIR.parent / "audit" / _DISCUSS_AUDIT_FILENAME

# Serializes writes to discuss-audit.jsonl across concurrent discuss
# requests. Two callers interleaving their ``open("a") / write`` pair on
# the same file would otherwise race — the append-mode open ensures each
# ``write()`` lands at EOF, but a write without a trailing newline from
# one caller could still be stitched onto another caller's JSON object.
# The lock also keeps the blocking I/O explicitly off the event loop when
# combined with ``asyncio.to_thread``.
_discuss_audit_lock = asyncio.Lock()


def _write_discuss_audit_line(entry: dict[str, Any]) -> None:
    """Blocking helper: append one JSON line to discuss-audit.jsonl.

    Called from inside ``asyncio.to_thread`` under ``_discuss_audit_lock``.
    Kept tiny so the thread stays busy for the minimum time.
    """
    audit_path = _discuss_audit_path()
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


async def _append_discuss_audit(
    ticket_id: str,
    session_token: str,
    source_ip: str,
    user_agent: str,
) -> None:
    """Append one JSON line to ``<LOGS_DIR>.parent/audit/discuss-audit.jsonl``.

    This is a plain append-only file, NOT a per-ticket trace store entry —
    it serves as the cross-ticket "who investigated what and when" record.
    Stored in a sibling ``audit/`` directory (not inside ``LOGS_DIR``) so
    it cannot be read as a phantom ticket through the trace-bundle /
    list-traces endpoints. Created on first use. One line per endpoint
    invocation. Never rotated or truncated from here — operator can
    rotate manually if it grows.

    Async so callers don't block the event loop on disk I/O: the actual
    write runs in a worker thread, serialized by ``_discuss_audit_lock``
    so concurrent discuss requests can't interleave partial JSON lines.
    """
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "ticket_id": ticket_id,
        "session_token": session_token,
        "source_ip": source_ip,
        "user_agent": user_agent,
    }
    async with _discuss_audit_lock:
        await asyncio.to_thread(_write_discuss_audit_line, entry)


class DiscussResponse(BaseModel):
    """Response body for ``POST /traces/{ticket_id}/discuss``."""

    ticket_id: str
    session_token: str
    bundle_url: str
    investigate_command: str
    expires_at: str


@app.post(
    "/traces/{ticket_id}/discuss",
    response_model=DiscussResponse,
    dependencies=[Depends(_require_api_key)],
)
async def discuss_trace(ticket_id: str, request: Request) -> DiscussResponse:
    """Mint a short-lived investigation handoff for a trace.

    Returns a JSON body containing:

    * ``session_token`` — an opaque random string. **Write-only**: the
      token appears in this response and in the audit log, and is NOT
      validated on any subsequent request. A future commit may add
      validation (e.g. a registry or signed token) but for Tier 1 the
      token is purely a correlation ID in the audit log.
    * ``bundle_url`` — the URL a developer or UI can hit to download the
      trace bundle (pre-existing ``/traces/<id>/bundle`` endpoint).
    * ``investigate_command`` — the copy-paste shell snippet that the
      dashboard investigate disclosure already renders, returned as a
      string so a future UI button can launch it programmatically.
    * ``expires_at`` — ISO-8601 UTC timestamp one hour from now. Advisory
      only (nothing enforces it — see write-only note above).

    Every invocation writes one line to
    ``<LOGS_DIR>.parent/audit/discuss-audit.jsonl`` with timestamp, ticket_id, token,
    source IP, and user-agent. This is append-only and intentionally
    separate from the per-ticket trace store — it's the cross-trace
    "who investigated what" record, useful even on a solo harness for
    remembering what you looked at across sessions.

    **Out of scope for Tier 1:** token validation, a token registry,
    rate limiting, token revocation or blacklisting. The endpoint is
    protected by the existing ``X-API-Key`` gate and that's the whole
    security model.
    """
    _validate_ticket_id(ticket_id)
    entries = read_trace(ticket_id)
    if not entries:
        raise HTTPException(status_code=404, detail=f"No trace for ticket '{ticket_id}'")

    session_token = secrets.token_urlsafe(24)
    expires_at = datetime.now(UTC) + _DISCUSS_SESSION_TTL

    source_ip = request.client.host if request.client else ""
    user_agent = request.headers.get("user-agent", "")
    await _append_discuss_audit(
        ticket_id=ticket_id,
        session_token=session_token,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    logger.info(
        "discuss_session_minted",
        ticket_id=ticket_id,
        source_ip=source_ip,
    )

    return DiscussResponse(
        ticket_id=ticket_id,
        session_token=session_token,
        bundle_url=f"{_DISCUSS_BASE_URL}/traces/{ticket_id}/bundle",
        investigate_command=_build_investigate_command(ticket_id),
        expires_at=expires_at.isoformat(),
    )


# Map the public artifact name (what shows up in the URL) to the internal
# artifact event name and the media type we serve. Kept narrow on purpose —
# only the three artifacts the dashboard "Raw Downloads" panel needs today.
_ARTIFACT_DOWNLOAD_MAP: dict[str, tuple[str, str]] = {
    "session_log": (ARTIFACT_SESSION_LOG, "text/plain; charset=utf-8"),
    "session_stream": (ARTIFACT_SESSION_STREAM, "application/json"),
    "effective_claude_md": (ARTIFACT_EFFECTIVE_CLAUDE_MD, "text/markdown; charset=utf-8"),
}


@app.get("/traces/{ticket_id}/artifact/{artifact_type}")
async def trace_artifact(ticket_id: str, artifact_type: str) -> Response:
    """Return a single raw artifact file from a trace.

    session_log and effective_claude_md are served from the trace store's
    inlined ``content`` field (already redacted at consolidation time).
    session_stream is served from disk via the ``artifact_path`` stored on
    the reference entry — 404 if the file was cleaned up (e.g. worktree
    already purged and not archived).

    FORENSIC ESCAPE HATCH: unlike the ``/bundle`` endpoint, this endpoint
    serves ``session_stream`` bytes directly from disk without running them
    through ``redact()``. That's deliberate — the raw stream is preserved on
    the operator's local filesystem as a last-resort debugging source. If
    you're routing the response off-box, use ``/bundle`` instead so the
    stream gets redacted on the way out.
    """
    _validate_ticket_id(ticket_id)
    mapping = _ARTIFACT_DOWNLOAD_MAP.get(artifact_type)
    if mapping is None:
        raise HTTPException(status_code=404, detail=f"Unknown artifact type: {artifact_type}")

    event_name, media_type = mapping
    entries = read_trace(ticket_id)
    if not entries:
        raise HTTPException(status_code=404, detail=f"No trace for ticket '{ticket_id}'")

    entry = find_artifact(entries, event_name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_type}' not in trace")

    if artifact_type == "session_stream":
        stream_path_str = str(entry.get("artifact_path", ""))
        if not stream_path_str:
            raise HTTPException(status_code=404, detail="session_stream has no artifact_path")
        stream_path = Path(stream_path_str)
        if not stream_path.exists():
            raise HTTPException(status_code=404, detail="session_stream file missing on disk")
        try:
            body = stream_path.read_bytes()
        except OSError as exc:
            # Never echo the OSError message in the response — it
            # typically contains the filesystem path ("/.../worktrees/ai-
            # PROJ-1/... permission denied") and leaks operator topology
            # to any caller of /traces/<id>/artifact/session_stream. Log
            # the full exception (paths are fine in structlog sinks, those
            # are local) and serve a generic 500.
            logger.error(
                "artifact_read_failed",
                ticket_id=ticket_id,
                artifact_type=artifact_type,
                stream_path=str(stream_path),
                exc_info=exc,
            )
            raise HTTPException(
                status_code=500, detail="Failed to read artifact"
            ) from exc
        # Force a download rather than letting the browser try to render a
        # multi-MB JSONL blob as a syntax-highlighted JSON document in a new
        # tab. The dashboard Raw Downloads panel expects a file save.
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Content-Disposition": 'attachment; filename="session-stream.jsonl"',
            },
        )

    content = entry.get("content")
    if content is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_type}' has no content")
    return PlainTextResponse(content=str(content), media_type=media_type)


# --- Admin: re-redact all existing trace entries ---
#
# When a redaction pattern update lands, existing traces don't automatically
# benefit — the redactor ran against them at consolidation time with the old
# pattern set. This endpoint rescans every trace file in the trace store and
# re-runs ``redact()`` over every known-risky field on every entry.
#
# Because the redactor is idempotent, running this when patterns haven't
# changed is a no-op — ``additional_patterns_found`` should be 0. A non-zero
# count on an unchanged pattern set indicates an idempotency bug.
@app.post(
    "/admin/re-redact",
    status_code=200,
    dependencies=[Depends(_require_api_key)],
)
async def admin_re_redact() -> dict[str, int]:
    """Re-run the redactor over every trace entry in the store.

    Cleans the same field set that ``consolidate_worktree_logs`` redacts on
    import, so a pattern update can catch secrets that were already written
    into non-``content`` fields on earlier runs:

    * Top-level string fields in ``_REDACT_IMPORTED_FIELDS``: ``content``,
      ``data``, ``error``, ``message``, ``output``, ``stderr``, ``stdout``,
      ``debug_payload``, ``tool_result``, ``details``, ``evidence``.
    * ``tool_index`` entries: the ``index.first_tool_error.message`` field
      (which captures up to 500 chars of raw tool-error content and can
      easily hold a live credential from a shell command).
    * Corrupt/unparseable lines: run through ``redact()`` as a raw string
      and written back redacted, so a partial-write during a crash that
      left a token on disk gets cleaned up on the next admin pass.

    Fields NOT covered: nested dicts/lists inside risky fields, and any
    top-level field name not in ``_REDACT_IMPORTED_FIELDS``. Nested leaks
    are rare in practice; if that assumption stops holding, extend the
    walk here and in ``tracer.consolidate_worktree_logs``.

    Returns a summary of the scan:

    * ``traces_processed`` — number of trace files walked.
    * ``entries_redacted`` — number of entries whose content changed.
    * ``additional_patterns_found`` — total *new* redaction hits on this pass.
      Expected to be 0 if patterns haven't changed since the original
      consolidation. Non-zero indicates either a pattern update or an
      idempotency violation in the redactor.

    Concurrency: this endpoint rewrites trace files in place without file
    locking. Run during quiet periods — a concurrent ``append_trace`` that
    interleaves with the rewrite can race and lose the appended entry.
    """
    logs_dir = tracer.LOGS_DIR
    if not logs_dir.exists():
        return {
            "traces_processed": 0,
            "entries_redacted": 0,
            "additional_patterns_found": 0,
        }

    traces_processed = 0
    entries_redacted = 0
    additional_patterns = 0

    for trace_file in sorted(logs_dir.glob("*.jsonl")):
        traces_processed += 1
        try:
            raw = trace_file.read_text()
        except OSError:
            logger.exception("re_redact_read_failed", trace_file=str(trace_file))
            continue

        out_lines: list[str] = []
        file_changed = False  # only rewrite the file if at least one line differed
        for line in raw.splitlines():
            if not line.strip():
                out_lines.append(line)
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # Corrupt/partial-write line (e.g. crash mid-append). A
                # partial write may contain a live token, so run the raw
                # string through redact() before writing it back rather
                # than passing it through verbatim.
                redacted_line, n = redact(line)
                if n:
                    additional_patterns += n
                    entries_redacted += 1
                    file_changed = True
                out_lines.append(redacted_line)
                continue

            # One helper call covers every known-risky string pocket in
            # the entry (top-level _REDACT_IMPORTED_FIELDS + the nested
            # index.first_tool_error.message pocket). Single source of
            # truth shared with tracer.consolidate_worktree_logs, so new
            # risky fields only need to be added in one place.
            n = redact_entry_in_place(entry)
            if n:
                additional_patterns += n
                entries_redacted += 1
                file_changed = True
            out_lines.append(json.dumps(entry))

        # Skip the write entirely when nothing changed. This closes a real
        # data-loss window: if a live pipeline was appending to this file
        # between our read_text() and write_text(), an unconditional rewrite
        # silently clobbered the appended entries. A no-op rewrite has no
        # reason to exist and no reason to take that risk.
        if not file_changed:
            continue

        new_text = "\n".join(out_lines)
        if raw.endswith("\n") and not new_text.endswith("\n"):
            new_text += "\n"
        # Atomic replace via the shared tracer helper — a crash mid-write
        # leaves the original file intact instead of producing a
        # truncated one.
        try:
            atomic_write_text(trace_file, new_text)
        except OSError:
            logger.exception("re_redact_write_failed", trace_file=str(trace_file))

    if additional_patterns:
        logger.warning(
            "re_redact_found_new_patterns",
            additional_patterns_found=additional_patterns,
            hint=(
                "Either patterns were updated since last consolidation, or "
                "the redactor failed its idempotency guarantee. Investigate."
            ),
        )
    logger.info(
        "re_redact_complete",
        traces_processed=traces_processed,
        entries_redacted=entries_redacted,
        additional_patterns_found=additional_patterns,
    )
    return {
        "traces_processed": traces_processed,
        "entries_redacted": entries_redacted,
        "additional_patterns_found": additional_patterns,
    }


async def _validate_and_parse_webhook(
    request: Request, signature: str | None,
) -> dict[str, Any]:
    """Validate webhook signature and parse JSON body.

    Shared by Jira and ADO webhook handlers.
    """
    body = await request.body()

    if settings.webhook_secret:
        if not signature:
            raise HTTPException(status_code=401, detail="Missing webhook signature")
        expected = hmac.new(
            settings.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        sig_value = signature.removeprefix("sha256=")
        if not hmac.compare_digest(expected, sig_value):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Webhook payload must be a JSON object")
    return payload


async def _validate_and_parse_webhook_dual_auth(
    request: Request,
    signature: str | None,
    *,
    bearer_token: str | None,
    bearer_token_secret: str,
    no_secret_behavior: str,
    auth_label: str,
) -> dict[str, Any]:
    """Dual-mode webhook auth: HMAC signature OR shared bearer token.

    Shared by ``jira_bug_webhook`` and ``ado_webhook`` — both need to
    accept either a signed body (for CI/CD pipelines that can compute
    HMAC) or a shared header token (for Jira Automation / ADO Service
    Hooks that cannot). Previously each handler had its own ~28 line
    implementation, and one used raw ``==`` to compare the bearer
    token (timing-attack hazard) while the other used
    ``hmac.compare_digest``. Consolidating here guarantees both paths
    use constant-time comparison.

    ``no_secret_behavior``:
      * ``"fail_closed_503"`` — when neither the HMAC secret nor the
        bearer token is configured, raise 503. Use this for endpoints
        that must never accept unauthenticated requests.
      * ``"open_local_dev"`` — same scenario, accept the request. Use
        this for endpoints whose local-dev workflow relies on running
        the service without any secrets at all.

    ``auth_label`` goes into the 401 detail so the rejected side can
    distinguish which webhook's auth failed.
    """
    body = await request.body()

    auth_ok = False
    # HMAC path — only when both the secret is configured AND a
    # signature header was sent. Missing header isn't an error here;
    # we fall through to the bearer path.
    if settings.webhook_secret and signature:
        expected = hmac.new(
            settings.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        sig_value = signature.removeprefix("sha256=")
        if hmac.compare_digest(expected, sig_value):
            auth_ok = True
    # Bearer token path — constant-time comparison so we don't leak
    # the token prefix via response timing.
    if (
        not auth_ok
        and bearer_token_secret
        and bearer_token
        and hmac.compare_digest(bearer_token, bearer_token_secret)
    ):
        auth_ok = True
    if not auth_ok:
        if not settings.webhook_secret and not bearer_token_secret:
            # Neither secret configured — behavior depends on endpoint.
            if no_secret_behavior == "fail_closed_503":
                raise HTTPException(
                    status_code=503,
                    detail=f"{auth_label} not configured",
                )
            if no_secret_behavior == "open_local_dev":
                auth_ok = True
            else:
                raise ValueError(
                    f"Invalid no_secret_behavior: {no_secret_behavior!r}"
                )
        else:
            raise HTTPException(
                status_code=401,
                detail=f"Invalid {auth_label} auth",
            )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid JSON body: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422, detail="Webhook payload must be a JSON object"
        )
    return payload


@app.post("/webhooks/jira", status_code=202)
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
) -> dict[str, str]:
    """Receive a Jira automation webhook and enqueue for processing."""
    payload = await _validate_and_parse_webhook(request, x_hub_signature)
    ticket = _get_jira_adapter().normalize(payload)
    return _dispatch_ticket(
        ticket,
        background_tasks,
        source="jira",
        event="jira_webhook_received",
    )


@app.post("/webhooks/jira-bug", status_code=202)
async def jira_bug_webhook(
    request: Request,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
    x_jira_bug_token: str | None = Header(default=None, alias="x-jira-bug-token"),
) -> dict[str, Any]:
    """Receive a Jira bug-created webhook and record a defect_link.

    Accepts EITHER HMAC signature (webhook_secret) OR bearer token
    (jira_bug_webhook_token), since Jira Automation cannot compute HMAC.
    Fails closed (503) when neither secret is configured — bug ingest
    must never accept unauthenticated requests.
    """
    payload = await _validate_and_parse_webhook_dual_auth(
        request,
        x_hub_signature,
        bearer_token=x_jira_bug_token,
        bearer_token_secret=settings.jira_bug_webhook_token or "",
        no_secret_behavior="fail_closed_503",
        auth_label="Bug webhook",
    )
    bug = normalize_jira_bug(payload, settings)

    conn = open_connection(resolve_db_path(settings.autonomy_db_path))
    try:
        ensure_schema(conn)
        result = ingest_jira_bug(conn, bug)
    finally:
        conn.close()

    logger.info("jira_bug_webhook_processed", **result)
    return result


@app.post("/webhooks/ado", status_code=202)
async def ado_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
    x_ado_webhook_token: str | None = Header(default=None, alias="x-ado-webhook-token"),
) -> dict[str, str]:
    """Receive an Azure DevOps Service Hook webhook and enqueue for processing.

    Auth: accepts HMAC signature (x-hub-signature) OR a shared token
    (X-ADO-Webhook-Token). Opens access when neither secret is
    configured so local development can send webhooks without any
    secret management. Rejects otherwise.
    """
    payload = await _validate_and_parse_webhook_dual_auth(
        request,
        x_hub_signature,
        bearer_token=x_ado_webhook_token,
        bearer_token_secret=settings.ado_webhook_token or "",
        no_secret_behavior="open_local_dev",
        auth_label="ADO webhook",
    )

    # --- Normalize the ADO payload ---
    ticket = _get_ado_adapter().normalize(payload)

    # --- Profile resolution + ticket ID remapping ---
    resource = payload.get("resource", {})
    work_item = resource.get("revision", resource)
    fields = work_item.get("fields", {})
    ado_project = fields.get("System.TeamProject", "")
    work_item_id = str(work_item.get("id", resource.get("workItemId", "")))

    # ADO work item webhook payloads do not contain repository information —
    # work items are project-scoped and only loosely linked to repos via
    # outbound ArtifactLinks at commit/branch/PR time. The finest routing
    # granularity we get from the webhook alone is System.TeamProject, so
    # client profiles are keyed 1:1 on ado_project_name. If two profiles share
    # the same ado_project_name, the alphabetically-first one wins and the
    # second is unreachable via this path. L3 (PR webhooks) has repo GUID in
    # its payload and uses find_profile_by_ado_repo instead.
    profile = find_profile_by_ado_project(ado_project) if ado_project else None
    if profile:
        # Remap ticket ID to use the profile's project_key prefix
        ticket.id = f"{profile.project_key}-{work_item_id}"
        # Register mapping so write-back methods resolve the real ADO project name
        AdoAdapter._project_key_map[profile.project_key] = ado_project

    # --- Tag check: skip if neither ai_label nor quick_label is present ---
    # Use the already-tokenized labels from the adapter (exact match, not substring)
    ticket_labels_lower = {lbl.lower() for lbl in ticket.labels}
    ai_label = (profile.ai_label if profile else "ai-implement").lower()
    quick_label = (profile.quick_label if profile else "ai-quick").lower()
    tag_present = (
        ai_label in ticket_labels_lower or quick_label in ticket_labels_lower
    )

    # If the tag is absent, clear any remembered state so a future re-add of
    # the tag triggers a fresh edge. Then skip — there's nothing to do.
    if not tag_present:
        _clear_trigger_state(ticket.id)
        _bump_webhook_counter(COUNTER_SKIPPED_NO_TAG)
        reason = f"Neither '{ai_label}' nor '{quick_label}' tag found"
        append_trace(
            ticket.id, generate_trace_id(), "webhook", "ado_webhook_skipped_no_tag",
            ticket_type=ticket.ticket_type, source="ado",
            ticket_title=ticket.title, reason=reason,
        )
        return {"status": "skipped", "ticket_id": ticket.id, "reason": reason}

    # Edge-detect the trigger tag to skip non-dispatching cascade webhooks
    # (PR merge ArtifactLinks, comments, field edits). Prefer the payload's
    # tag delta when present; fall back to in-process memory otherwise.
    # See _check_trigger_edge for semantics.
    was_before, _ = extract_tag_transition(payload, [ai_label, quick_label])
    if not _check_trigger_edge(ticket.id, tag_present, was_present_before=was_before):
        _bump_webhook_counter(COUNTER_SKIPPED_NOT_EDGE)
        signal = "payload_delta" if was_before is not None else "process_memory"
        logger.info(
            "ado_webhook_not_edge",
            ticket_id=ticket.id,
            reason="trigger tag already present; not a new edge",
            signal=signal,
        )
        append_trace(
            ticket.id, generate_trace_id(), "webhook", "ado_webhook_skipped_not_edge",
            ticket_type=ticket.ticket_type, source="ado",
            ticket_title=ticket.title,
            reason="tag already present; not a new edge",
            signal=signal,
        )
        return {
            "status": "skipped",
            "ticket_id": ticket.id,
            "reason": "trigger tag already present on previous webhook (not a new edge)",
        }

    _bump_webhook_counter(COUNTER_ACCEPTED_EDGE)
    return _dispatch_ticket(
        ticket,
        background_tasks,
        source="ado",
        event="ado_webhook_received",
    )


@app.post("/api/process-ticket", status_code=202, dependencies=[Depends(_require_api_key)])
async def manual_process_ticket(
    ticket: TicketPayload,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Manual trigger — accepts a TicketPayload directly (bypasses webhook).

    Essential for testing the pipeline without Jira/ADO webhooks configured.
    Previously this endpoint skipped the ``append_trace`` webhook
    breadcrumb, so manually-submitted tickets were harder to find in
    the trace dashboard. The shared ``_dispatch_ticket`` helper fixes
    that drift by construction.
    """
    return _dispatch_ticket(
        ticket,
        background_tasks,
        source="manual",
        event="manual_ticket_submitted",
    )


_TICKET_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+-[0-9]+$")
# Branch names: alphanumeric + slashes/underscores/dots/hyphens, but the
# ``..`` sequence is forbidden to prevent path traversal into sibling
# directories of the worktrees parent. The containment check below uses
# Path.is_relative_to as the real guardrail; this regex is belt-and-
# braces so a bad branch fails fast at input validation.
_BRANCH_PATTERN = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9/_.-]*$")
# Reserved git ref names — a branch literally named ``HEAD`` satisfies
# the regex but makes every ``git`` invocation ambiguous because
# ``HEAD`` is the symbolic ref for the current checkout. The rest of
# the *_HEAD names are checked for defense-in-depth even though the
# leading-underscore/capital form would need them to be uppercase to
# matter; we include them so a future regex relaxation doesn't silently
# open the door.
_RESERVED_BRANCH_NAMES = frozenset({"HEAD", "ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD"})
_VALID_PHASES = {"qa", "e2e", "review"}


def _is_safe_branch(name: str) -> bool:
    """Return True iff ``name`` is a safe branch name to use in filesystem
    and git command construction.

    Rejects:
      - Anything the regex rejects (``..``, bad chars, empty, etc.)
      - Reserved git refs (``HEAD``, ``ORIG_HEAD``, ``FETCH_HEAD``,
        ``MERGE_HEAD``) — these satisfy the regex but make git commands
        ambiguous against the live symbolic refs.
      - Names that start or end with ``/`` — git treats these as
        invalid refs, but the regex was permissive.
      - Names ending with ``.lock`` — git's lockfile suffix; creating a
        branch with that name corrupts the ref database.
      - The literal ``.git`` — a directory name we never want to
        collide with.

    Callers must route every filesystem or git command involving a
    user-supplied branch through this function, not the raw regex.
    """
    if not name or not _BRANCH_PATTERN.fullmatch(name):
        return False
    if name in _RESERVED_BRANCH_NAMES:
        return False
    if name.startswith("/") or name.endswith("/"):
        return False
    if name.endswith(".lock"):
        return False
    return name != ".git"


def _resolve_worktree_dir(client_repo: str, branch: str) -> Path:
    """Validate ``branch`` and return the resolved worktree directory.

    Shared by ``/api/retest`` and ``/api/agent-complete`` — both
    construct ``<client_repo>/../worktrees/<branch>`` from a
    request-supplied branch name and must defend against ``..`` and
    sibling-prefix traversal (e.g. ``worktrees-evil``). Raises
    ``HTTPException(400)`` on invalid regex, empty branch, or a path
    that resolves outside the worktrees parent directory.

    Callers should still verify ``result.exists()`` themselves since
    the semantics of "worktree doesn't exist yet" vs "branch is
    invalid" differ across the two endpoints.
    """
    if not _is_safe_branch(branch):
        raise HTTPException(
            status_code=400,
            detail="Invalid branch name (alphanumeric, slashes, dots, hyphens only)",
        )

    worktrees_parent = (Path(client_repo).parent / "worktrees").resolve()
    worktree_resolved = (worktrees_parent / branch).resolve()
    # Path containment guard — use Path.relative_to which checks path
    # components, not string prefixes. A sibling directory like
    # ``worktrees-evil`` would pass a naive startswith check because
    # its resolved path literally starts with ``/.../worktrees``.
    try:
        worktree_resolved.relative_to(worktrees_parent)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Branch resolves outside worktree directory",
        ) from None
    return worktree_resolved


class RetestPayload(BaseModel):
    """Payload for re-running specific pipeline phases on an existing branch."""

    ticket_id: str
    phase: str = "qa"  # "qa", "e2e", "review"
    branch: str = ""  # defaults to ai/<ticket-id>


@app.post("/api/retest", status_code=202, dependencies=[Depends(_require_api_key)])
async def retest(payload: RetestPayload, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Re-run a specific phase on an existing branch.

    Usage:
        curl -X POST localhost:8000/api/retest -H 'Content-Type: application/json' \
            -d '{"ticket_id": "SCRUM-8", "phase": "e2e"}'

    Phases:
        - qa: full QA validation (unit + integration + e2e)
        - e2e: E2E browser tests only
        - review: code review only
    """
    # Input validation — ticket_id used in filesystem paths
    if not _TICKET_ID_PATTERN.match(payload.ticket_id):
        raise HTTPException(
            status_code=400, detail="Invalid ticket_id format (expected: PROJ-123)"
        )
    if payload.phase not in _VALID_PHASES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid phase '{payload.phase}'. Must be one of: {_VALID_PHASES}",
        )

    branch = payload.branch or f"ai/{payload.ticket_id}"
    client_repo = settings.default_client_repo
    if not client_repo:
        return {"status": "error", "detail": "No default_client_repo configured"}

    worktree_resolved = _resolve_worktree_dir(client_repo, branch)
    if not worktree_resolved.exists():
        return {
            "status": "error",
            "detail": f"Worktree not found for branch '{branch}'. Run the ticket first.",
        }
    worktree_dir = str(worktree_resolved)

    log = logger.bind(ticket_id=payload.ticket_id, phase=payload.phase)
    log.info("retest_requested", branch=branch)

    phase_prompts = {
        "qa": (
            f"You are a QA validator. The code is already implemented on branch {branch}. "
            f"Read the enriched ticket at .harness/ticket.json. "
            f"Run the full test suite. If playwright.config.ts exists, also run E2E tests "
            f"by starting the dev server and using Playwright MCP. "
            f"Write your QA matrix to .harness/logs/qa-matrix.md. "
            f"If any tests were previously skipped, try to run them now and explain "
            f"any failures with exact error messages and remediation steps."
        ),
        "e2e": (
            f"You are a QA validator focused on E2E tests only. "
            f"The code is already implemented on branch {branch}. "
            f"Kill any process on port 3000 first: lsof -ti:3000 | xargs kill 2>/dev/null. "
            f"Start the dev server. Run E2E tests using Playwright MCP: "
            f"navigate pages, interact with UI, take screenshots, validate acceptance criteria. "
            f"Write results to .harness/logs/qa-e2e-retest.md. "
            f"If tests fail, include the exact error, what you tried, and how to fix."
        ),
        "review": (
            f"You are a code reviewer. The code is already on branch {branch}. "
            f"Run git diff main...HEAD and review for correctness, security, style, "
            f"and test coverage. Write your review to .harness/logs/code-review-retest.md."
        ),
    }

    prompt = phase_prompts.get(payload.phase, phase_prompts["qa"])

    env = sanitized_env()
    cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]

    def run_retest() -> None:
        try:
            log_file = Path(worktree_dir) / ".harness" / "logs" / f"retest-{payload.phase}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("w") as f:
                subprocess.run(cmd, cwd=worktree_dir, env=env, stdout=f, stderr=subprocess.STDOUT)
            log.info("retest_complete", log_file=str(log_file))
        except Exception:
            log.exception("retest_failed")

    background_tasks.add_task(run_retest)
    return {"status": "accepted", "ticket_id": payload.ticket_id, "phase": payload.phase}


@app.post("/webhooks/github", status_code=202)
async def github_webhook_proxy(request: Request) -> dict[str, str]:
    """Proxy GitHub webhooks to the L3 PR Review Service.

    Since ngrok free tier only supports one tunnel, GitHub webhooks arrive
    at L1 (port 8000) and are forwarded to L3 (port 8001).
    """
    body = await request.body()
    headers = dict(request.headers)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "http://localhost:8001/webhooks/github",
                content=body,
                headers={
                    k: v for k, v in headers.items()
                    if k.lower() in (
                        "content-type", "x-github-event",
                        "x-hub-signature-256", "x-github-delivery",
                        "user-agent",
                    )
                },
                timeout=30.0,
            )
            if response.status_code >= 400:
                logger.warning(
                    "l3_proxy_http_error",
                    status=response.status_code,
                )
                return {"status": "l3_error", "http_status": str(response.status_code)}
            result: dict[str, str] = response.json()
            return result
        except httpx.ConnectError:
            logger.warning("l3_service_unavailable")
            return {"status": "l3_unavailable"}
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("l3_proxy_failed", error=str(exc)[:200])
            return {"status": "l3_error"}


class FailedUnit(BaseModel):
    """A blocked/failed implementation unit."""

    unit_id: str = ""
    description: str = ""
    failure_reason: str = ""


class CompletionPayload(BaseModel):
    """Payload sent by the spawn script when an agent finishes."""

    ticket_id: str
    source: str = "jira"
    trace_id: str = ""  # From trace-config.json — correlates with live-reported entries
    status: str  # "complete", "partial", "escalated"
    pr_url: str = ""
    branch: str = ""
    repo_full_name: str = ""
    head_sha: str = ""
    failed_units: list[FailedUnit] = []


def _derive_head_sha(worktree_path: str) -> str:
    """Run 'git rev-parse HEAD' in the worktree. Returns '' on any failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


def _derive_repo_full_name(worktree_path: str) -> str:
    """Parse 'git config --get remote.origin.url' into 'owner/repo'.

    Returns '' on any failure.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
            if m:
                return m.group(1)
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


@app.post("/api/agent-trace", status_code=200)
async def agent_trace(request: Request) -> dict[str, str]:
    """Accept live trace events from running agents.

    Called by the file-watcher thread in spawn_team.py as the agent
    writes to pipeline.jsonl. Entries appear in the dashboard in real-time.
    No auth required — internal network only (same host as spawn_team.py).

    ``ticket_id`` is validated against the trace-store id pattern BEFORE
    being passed to ``append_trace``: without validation, a path-like
    value (``../../tmp/pwn``) would escape ``LOGS_DIR`` and have
    attacker-controlled JSON appended to an arbitrary ``.jsonl`` file
    since ``append_trace`` builds ``LOGS_DIR / f"{ticket_id}.jsonl"``.
    The endpoint is intentionally open to the local file-watcher, so
    input sanitisation is the sole guardrail.
    """
    body = await request.json()
    ticket_id = str(body.pop("ticket_id", ""))
    trace_id = str(body.pop("trace_id", ""))
    phase = str(body.pop("phase", ""))
    event = str(body.pop("event", ""))
    body.pop("timestamp", None)  # append_trace generates its own timestamp
    if not ticket_id or not event:
        return {"status": "ok"}
    # Reject path-like ticket_ids (``..``, slashes, absolute paths).
    # Raises HTTPException(400) before any filesystem access.
    _validate_ticket_id(ticket_id)
    append_trace(ticket_id, trace_id, phase, event, source="agent", **body)
    return {"status": "ok"}


@app.post("/api/agent-complete", status_code=200, dependencies=[Depends(_require_api_key)])
async def agent_complete(payload: CompletionPayload) -> dict[str, str]:
    """Called by the spawn script when the agent finishes.

    Updates the Jira/ADO ticket with the PR link and transitions to Done.

    Validates both ``ticket_id`` and ``branch`` because both flow into
    filesystem paths: ``ticket_id`` becomes ``LOGS_DIR/{ticket_id}.jsonl``
    via ``append_trace`` and ``branch`` becomes the worktree path that
    ``consolidate_worktree_logs`` reads from. Without validation, a
    caller with a leaked API key could plant ``.jsonl`` files outside
    ``LOGS_DIR`` or point consolidation at an arbitrary git repo. The
    ``/api/retest`` endpoint validates both the same way; keeping the
    two endpoints symmetrical closes the defense-in-depth gap.
    """
    if not _TICKET_ID_PATTERN.match(payload.ticket_id):
        raise HTTPException(
            status_code=400,
            detail="Invalid ticket_id format (expected: PROJ-123)",
        )
    # Validate branch via the shared helper. Fall back to ``ai/<ticket_id>``
    # if the caller omitted a branch (matches the retest convention and
    # the spawn_team.py default).
    branch = payload.branch or f"ai/{payload.ticket_id}"
    client_repo = settings.default_client_repo
    worktree_resolved: Path | None = None
    if client_repo:
        worktree_resolved = _resolve_worktree_dir(client_repo, branch)

    log = logger.bind(ticket_id=payload.ticket_id, status=payload.status)
    log.info("agent_completion_received", pr_url=payload.pr_url)

    # Delay releasing the ticket — ADO fires webhooks when we post comments
    # and transition status below. Keep the ticket claimed to absorb those
    # self-triggered webhooks before allowing reprocessing. Edge-detection
    # memory is cleared on the same schedule so the lifecycles align and a
    # future re-add of the trigger tag produces a fresh edge as expected.
    # Window is tunable via settings.agent_complete_release_delay_sec.
    async def _delayed_release(ticket_id: str) -> None:
        await asyncio.sleep(settings.agent_complete_release_delay_sec)
        _release_ticket(ticket_id)
        _clear_trigger_state(ticket_id)

    _spawn_background_task(_delayed_release(payload.ticket_id))

    # Trace: record completion — reuse the trace_id from the spawn chain
    # so live-reported entries and completion entries share the same trace_id
    trace_id = payload.trace_id or generate_trace_id()
    append_trace(payload.ticket_id, trace_id, "completion", "agent_finished",
                 status=payload.status, pr_url=payload.pr_url, branch=branch)

    # Consolidate worktree artifacts into the persistent trace
    worktree_path = (
        str(worktree_resolved)
        if worktree_resolved is not None
        else f"{settings.default_client_repo}/../worktrees/{branch}"
    )
    try:
        repo = payload.repo_full_name or _derive_repo_full_name(worktree_path)
        sha = payload.head_sha or _derive_head_sha(worktree_path)
        consolidate_worktree_logs(
            payload.ticket_id,
            trace_id,
            worktree_path,
            repo_full_name=repo,
            head_sha=sha,
        )
    except Exception:
        log.exception("worktree_consolidation_failed", worktree=worktree_path)
        # Continue — don't block Jira updates because consolidation failed

    # Route to the correct adapter based on ticket source
    if payload.source == "ado":
        adapter: JiraAdapter | AdoAdapter = _get_ado_adapter()
    else:
        adapter = _get_jira_adapter()

    try:
        if payload.pr_url:
            comment = (
                f"*AI Pipeline — Complete*\n\n"
                f"PR: {payload.pr_url}\n"
                f"Branch: {payload.branch}\n"
                f"Status: {payload.status}"
            )
            await adapter.write_comment(payload.ticket_id, comment)

        # Link ADO work item to PR (if source is ADO and PR was created)
        if payload.pr_url and isinstance(adapter, AdoAdapter):
            try:
                await adapter.link_work_item_to_pr(payload.ticket_id, payload.pr_url)
                log.info("ado_work_item_linked_to_pr")
            except Exception:
                log.warning("ado_work_item_pr_link_failed")

        # Upload final screenshot if it exists in the worktree
        # Note: ADO adapter doesn't have upload_attachment yet — skip for ADO
        screenshot_path = Path(worktree_path) / ".harness" / "screenshots" / "final.png"
        if screenshot_path.exists() and isinstance(adapter, JiraAdapter):
            await adapter.upload_attachment(
                payload.ticket_id,
                str(screenshot_path),
                filename=f"{payload.ticket_id}-implementation.png",
            )
            log.info("screenshot_uploaded", path=str(screenshot_path))

        if payload.status not in ("complete", "partial", "escalated"):
            log.warning("unknown_completion_status", status=payload.status)
        elif payload.status == "complete":
            await adapter.transition_status(payload.ticket_id, "Done")
            log.info("ticket_transitioned_to_done")
        elif payload.status in ("partial", "escalated"):
            label = "needs-human" if payload.status == "escalated" else "partial-implementation"
            await adapter.add_label(payload.ticket_id, label)

            for unit in payload.failed_units:
                sub_comment = (
                    f"*AI Pipeline — Failed Unit: {unit.unit_id}*\n\n"
                    f"*Description:* {unit.description}\n"
                    f"*Failure:* {unit.failure_reason}\n\n"
                    f"This unit needs manual implementation or investigation."
                )
                await adapter.write_comment(payload.ticket_id, sub_comment)
                log.info("failed_unit_reported", unit_id=unit.unit_id)

    except Exception:
        log.exception("completion_update_failed")

    return {"status": "ok", "ticket_id": payload.ticket_id}
