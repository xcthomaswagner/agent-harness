"""L1 Pre-Processing Service — Webhook receiver and ticket processing pipeline.

Thin composition layer after the Phase 4 structural refactor. The real
endpoint handlers live in the dedicated modules this file mounts:

* ``webhooks``     — Jira / Jira-bug / ADO / GitHub-proxy / manual-trigger
* ``trace_bundle`` — bundle / artifact / discuss / admin re-redact
* ``completion``   — agent-trace, retest, agent-complete
* ``claim_store``  — per-ticket claim + edge-detection state
* ``auth``         — ``_require_api_key`` dependency

The module still owns:
* the ``FastAPI()`` instance
* singleton adapters (``_get_jira_adapter`` / ``_get_ado_adapter`` / ``_get_pipeline``)
* startup validation + learning-outcomes scheduler
* background-task bookkeeping
* the pipeline entry (``_process_ticket``)
* ``/health`` and ``/stats/webhooks``
* re-exports of every symbol the test suite imports via
  ``from main import ...`` — do not remove these without updating callers.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.ado_adapter import AdoAdapter
from adapters.jira_adapter import JiraAdapter
from auth import _require_api_key as _require_api_key
from auth import _require_dashboard_auth as _require_dashboard_auth
from autonomy_dashboard import router as autonomy_dashboard_router
from autonomy_ingest import router as autonomy_router

# Re-exports for test back-compat. ``from X import Y as Y`` is the
# mypy-approved re-export form (it bumps the symbol into the module's
# public interface without needing an ``__all__``). Every symbol below
# is imported by at least one test via ``from main import ...`` or
# ``main.<name>``; removing any one will break the suite.
from claim_store import _ACTIVE_TICKET_TTL_SEC as _ACTIVE_TICKET_TTL_SEC
from claim_store import COUNTER_ACCEPTED_EDGE as COUNTER_ACCEPTED_EDGE
from claim_store import COUNTER_SKIPPED_NO_TAG as COUNTER_SKIPPED_NO_TAG
from claim_store import COUNTER_SKIPPED_NOT_EDGE as COUNTER_SKIPPED_NOT_EDGE
from claim_store import _active_tickets as _active_tickets
from claim_store import _active_tickets_lock as _active_tickets_lock
from claim_store import _bump_webhook_counter as _bump_webhook_counter
from claim_store import _check_trigger_edge as _check_trigger_edge
from claim_store import _clear_trigger_state as _clear_trigger_state
from claim_store import _get_webhook_counters as _get_webhook_counters
from claim_store import _last_trigger_state as _last_trigger_state
from claim_store import _last_trigger_state_lock as _last_trigger_state_lock
from claim_store import _release_ticket as _release_ticket
from claim_store import _reset_state as _reset_claim_store_state
from claim_store import _try_claim_ticket as _try_claim_ticket
from claim_store import _webhook_counters as _webhook_counters
from claim_store import _webhook_counters_lock as _webhook_counters_lock
from client_profile import find_profile_by_ado_project as find_profile_by_ado_project
from completion import _BRANCH_PATTERN as _BRANCH_PATTERN
from completion import _TICKET_ID_PATTERN as _TICKET_ID_PATTERN
from completion import _VALID_PHASES as _VALID_PHASES
from completion import CompletionPayload as CompletionPayload
from completion import FailedUnit as FailedUnit
from completion import RetestPayload as RetestPayload
from completion import _derive_head_sha as _derive_head_sha
from completion import _derive_repo_full_name as _derive_repo_full_name
from completion import _is_safe_branch as _is_safe_branch
from completion import _resolve_worktree_dir as _resolve_worktree_dir
from completion import _validate_ticket_id as _validate_ticket_id
from completion import router as completion_router
from config import settings as settings
from learning_api import router as learning_api_router
from learning_dashboard import router as learning_dashboard_router
from live_stream import router as live_stream_router
from models import TicketPayload
from pipeline import Pipeline
from trace_bundle import _ARTIFACT_DOWNLOAD_MAP as _ARTIFACT_DOWNLOAD_MAP
from trace_bundle import _BUNDLE_README as _BUNDLE_README
from trace_bundle import _DISCUSS_AUDIT_FILENAME as _DISCUSS_AUDIT_FILENAME
from trace_bundle import _DISCUSS_SESSION_TTL as _DISCUSS_SESSION_TTL
from trace_bundle import _REDACTION_BLOCK_CLEAN as _REDACTION_BLOCK_CLEAN
from trace_bundle import _REDACTION_BLOCK_REDACTED as _REDACTION_BLOCK_REDACTED
from trace_bundle import DiscussResponse as DiscussResponse
from trace_bundle import _append_discuss_audit as _append_discuss_audit
from trace_bundle import _build_bundle as _build_bundle
from trace_bundle import _discuss_audit_lock as _discuss_audit_lock
from trace_bundle import _discuss_audit_path as _discuss_audit_path
from trace_bundle import _extract_ticket_payload as _extract_ticket_payload
from trace_bundle import _write_discuss_audit_line as _write_discuss_audit_line
from trace_bundle import router as trace_bundle_router
from trace_dashboard import router as trace_router
from tracer import (
    append_trace,
    generate_trace_id,
)
from operator_api import router as operator_router
from operator_api_data import router as operator_data_router
from unified_dashboard import router as unified_router
from webhooks import _dispatch_ticket as _dispatch_ticket
from webhooks import _enqueue_or_background as _enqueue_or_background
from webhooks import _validate_and_parse_webhook as _validate_and_parse_webhook
from webhooks import _validate_and_parse_webhook_dual_auth as _validate_and_parse_webhook_dual_auth
from webhooks import router as webhook_router

logger = structlog.get_logger()


app = FastAPI(
    title="Agentic Harness L1 Pre-Processing",
    description="Receives Jira/ADO webhooks, enriches tickets, dispatches to Agent Teams.",
    version="0.1.0",
)
# Dashboard routers are pure-GET views — apply _require_dashboard_auth
# globally at the include site so every route requires X-API-Key (or
# DASHBOARD_ALLOW_ANONYMOUS=true for local dev).
app.include_router(
    unified_router, dependencies=[Depends(_require_dashboard_auth)]
)
app.include_router(
    trace_router, dependencies=[Depends(_require_dashboard_auth)]
)
# autonomy_router mixes internal-POST (own admin-token auth) and
# dashboard-GET endpoints; leave existing per-route auth untouched.
app.include_router(autonomy_router)
app.include_router(
    autonomy_dashboard_router, dependencies=[Depends(_require_dashboard_auth)]
)
# learning_api_router has mixed GET/POST — GETs get dashboard auth via
# the router-include dependency; POST admin writes retain their own
# ``_guard_admin_request`` token check layered on top.
app.include_router(
    learning_api_router, dependencies=[Depends(_require_dashboard_auth)]
)
app.include_router(
    learning_dashboard_router, dependencies=[Depends(_require_dashboard_auth)]
)
app.include_router(webhook_router)
# trace_bundle exposes /traces/{id}/bundle + artifact reads — same
# dashboard-auth posture as the other GET surfaces.
app.include_router(
    trace_bundle_router, dependencies=[Depends(_require_dashboard_auth)]
)
app.include_router(completion_router)
# live_stream handles its own auth per-route (query-param-or-header)
# because EventSource cannot send custom headers. Mounted without the
# global dashboard-auth dependency to keep both routes on the same
# permission surface.
app.include_router(live_stream_router)
# operator_api_data serves /api/operator/* JSON endpoints. Mount BEFORE
# operator_router so the SPA catch-all route (/operator/{path:path})
# doesn't swallow requests intended for /api/operator/*.
app.include_router(operator_data_router)
# operator_api serves the /operator Preact SPA. Auth is applied
# per-route inside the module (query-param-or-header on HTML shell,
# none on static assets). Mount LAST so the SPA catch-all route
# doesn't shadow any other app route.
app.include_router(operator_router)


@app.on_event("startup")
async def _validate_config() -> None:
    """Warn about missing configuration at startup."""
    if not settings.webhook_secret:
        logger.warning(
            "webhook_secret_not_configured",
            hint="Webhook signature validation is DISABLED. Set WEBHOOK_SECRET in .env",
        )
    if settings.allow_unsigned_webhooks:
        logger.error(
            "allow_unsigned_webhooks_enabled",
            hint=(
                "ALLOW_UNSIGNED_WEBHOOKS=true — webhook auth is DISABLED. "
                "This MUST only be used for local development. Unset in "
                "production or anyone on the network can inject tickets."
            ),
        )
    if not settings.api_key and settings.dashboard_allow_anonymous:
        logger.error(
            "dashboard_allow_anonymous_enabled",
            hint=(
                "DASHBOARD_ALLOW_ANONYMOUS=true and no API_KEY set — "
                "dashboards and trace bundles are unauthenticated. "
                "Set API_KEY in production."
            ),
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
    # Defer the claim/edge/counter reset to the claim_store owner of that state.
    _reset_claim_store_state()


# --- Pipeline processing (background) ---


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


# --- Trivial endpoints that stay in main.py ---


@app.get("/health")
async def health() -> dict[str, object]:
    """Health check — minimal liveness probe.

    Intentionally returns only liveness status — no secret-presence
    booleans. Previously this endpoint returned flags per configured
    secret (anthropic_api_key, jira_configured, ado_configured, etc.)
    reachable without auth, which told an attacker exactly which
    integrations were wired up — useful for targeting. Operators
    checking config should look at startup logs (warnings + errors
    for missing / open-mode config) or the API-key-protected
    /stats/webhooks endpoint.
    """
    return {"status": "ok"}


@app.get("/stats/webhooks", dependencies=[Depends(_require_dashboard_auth)])
async def webhook_stats() -> dict[str, object]:
    """Cumulative webhook outcome counts since process start / last reset.

    Exposes edge-detection behavior so operators can see cascade pressure
    without grepping logs. A steady climb in `ado_skipped_not_edge` relative
    to `ado_accepted_edge` indicates ADO is firing many follow-up webhooks
    for the same tagged ticket — typically harmless (the fix for Finding 4
    is working), but worth investigating if the ratio seems wrong.
    """
    return {
        "counters": _get_webhook_counters(),
        "release_delay_sec": settings.agent_complete_release_delay_sec,
        "active_tickets": len(_active_tickets),
        "tracked_trigger_states": len(_last_trigger_state),
    }
