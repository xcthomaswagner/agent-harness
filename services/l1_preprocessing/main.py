"""L1 Pre-Processing Service — Webhook receiver and ticket processing pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.env_sanitize import sanitized_env

from adapters.ado_adapter import AdoAdapter
from adapters.jira_adapter import JiraAdapter
from autonomy_dashboard import router as autonomy_dashboard_router
from autonomy_ingest import ingest_jira_bug
from autonomy_ingest import router as autonomy_router
from autonomy_jira_bug import normalize_jira_bug
from autonomy_store import ensure_schema, open_connection, resolve_db_path
from client_profile import find_profile_by_ado_project
from config import settings
from models import TicketPayload
from pipeline import Pipeline
from trace_dashboard import router as trace_router
from tracer import append_trace, consolidate_worktree_logs, generate_trace_id
from unified_dashboard import router as unified_router

logger = structlog.get_logger()


def _require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Dependency that enforces API key auth on internal control-plane endpoints.

    Skipped when API_KEY is not configured (local dev mode).
    """
    if not settings.api_key:
        return  # No key configured — open access (local dev)
    if not x_api_key or x_api_key != settings.api_key:
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

    # Check reference documentation URLs in background (non-blocking)
    import asyncio

    from url_checker import check_reference_urls

    _background_tasks.add(asyncio.create_task(check_reference_urls()))

# Hold references to background tasks so they aren't garbage-collected
_background_tasks: set[object] = set()

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

_active_tickets: set[str] = set()
_active_tickets_lock = __import__("threading").Lock()
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()  # prevent GC of fire-and-forget tasks

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

    Returns True if claimed (caller should process), False if already active.
    Thread-safe via lock — prevents TOCTOU race between check and add.
    """
    with _active_tickets_lock:
        if ticket_id in _active_tickets:
            return False
        _active_tickets.add(ticket_id)
        return True


def _release_ticket(ticket_id: str) -> None:
    """Release a ticket from the active set."""
    with _active_tickets_lock:
        _active_tickets.discard(ticket_id)


def _check_trigger_edge(ticket_id: str, tag_present_now: bool) -> bool:
    """Edge-detect the trigger tag for a ticket.

    Returns True if this webhook represents a new trigger (i.e., the tag
    transitioned from absent on the previous webhook to present on this one,
    OR we've never seen this ticket before and the tag is present now).
    Returns False if the tag has been continuously present across the last
    webhook AND this one, in which case this webhook is almost certainly a
    non-trigger side effect (PR merge, comment, field edit, etc.) and should
    not start a new pipeline run.

    Also records the current tag state so the next webhook for the same
    ticket can compare against it.

    Thread-safe via lock.
    """
    with _last_trigger_state_lock:
        prev = _last_trigger_state.get(ticket_id, False)
        _last_trigger_state[ticket_id] = tag_present_now
        # New trigger iff tag is present now AND was NOT present last time.
        return tag_present_now and not prev


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
async def health() -> dict[str, object]:
    """Health check with configuration status."""
    return {
        "status": "ok",
        "anthropic_api_key": bool(settings.anthropic_api_key),
        "jira_configured": bool(settings.jira_base_url and settings.jira_api_token),
        "ado_configured": bool(settings.ado_org_url and settings.ado_pat),
        "webhook_secret": bool(settings.webhook_secret),
        "client_repo": bool(settings.default_client_repo),
    }


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


@app.post("/webhooks/jira", status_code=202)
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
) -> dict[str, str]:
    """Receive a Jira automation webhook and enqueue for processing."""
    payload = await _validate_and_parse_webhook(request, x_hub_signature)
    ticket = _get_jira_adapter().normalize(payload)

    trace_id = generate_trace_id()
    logger.info("jira_webhook_received", ticket_id=ticket.id, ticket_type=ticket.ticket_type)
    append_trace(ticket.id, trace_id, "webhook", "jira_webhook_received",
                 ticket_type=ticket.ticket_type, source="jira",
                 ticket_title=ticket.title)

    dispatch = _enqueue_or_background(ticket, background_tasks, trace_id=trace_id)
    if dispatch == "duplicate":
        return {"status": "skipped", "ticket_id": ticket.id, "reason": "already processing"}
    return {"status": "accepted", "ticket_id": ticket.id, "dispatch": dispatch}


@app.post("/webhooks/jira-bug", status_code=202)
async def jira_bug_webhook(
    request: Request,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
    x_jira_bug_token: str | None = Header(default=None, alias="x-jira-bug-token"),
) -> dict[str, Any]:
    """Receive a Jira bug-created webhook and record a defect_link.

    Accepts EITHER HMAC signature (webhook_secret) OR bearer token
    (jira_bug_webhook_token), since Jira Automation cannot compute HMAC.
    """
    body = await request.body()

    # Auth: try HMAC first, fallback to bearer token
    auth_ok = False
    if settings.webhook_secret and x_hub_signature:
        expected = hmac.new(
            settings.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        sig = x_hub_signature.removeprefix("sha256=")
        if hmac.compare_digest(expected, sig):
            auth_ok = True
    if (
        not auth_ok
        and settings.jira_bug_webhook_token
        and x_jira_bug_token
        and x_jira_bug_token == settings.jira_bug_webhook_token
    ):
        auth_ok = True
    if not auth_ok:
        # If neither secret configured, fail closed
        if not settings.webhook_secret and not settings.jira_bug_webhook_token:
            raise HTTPException(status_code=503, detail="Bug webhook not configured")
        raise HTTPException(status_code=401, detail="Invalid webhook auth")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Webhook payload must be a JSON object")

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
    (X-ADO-Webhook-Token). Rejects if neither succeeds and at least one
    secret is configured.
    """
    body = await request.body()

    # --- Auth: dual-mode (HMAC or token) ---
    auth_ok = False
    if settings.webhook_secret and x_hub_signature:
        expected = hmac.new(
            settings.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        sig_value = x_hub_signature.removeprefix("sha256=")
        if hmac.compare_digest(expected, sig_value):
            auth_ok = True
    if (
        not auth_ok
        and settings.ado_webhook_token
        and x_ado_webhook_token
        and hmac.compare_digest(x_ado_webhook_token, settings.ado_webhook_token)
    ):
        auth_ok = True
    if not auth_ok:
        if not settings.webhook_secret and not settings.ado_webhook_token:
            # Neither secret configured — open access (local dev)
            auth_ok = True
        else:
            raise HTTPException(status_code=401, detail="Invalid ADO webhook auth")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Webhook payload must be a JSON object")

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
    # (PR merge ArtifactLinks, comments, field edits). See _check_trigger_edge.
    if not _check_trigger_edge(ticket.id, tag_present):
        _bump_webhook_counter(COUNTER_SKIPPED_NOT_EDGE)
        logger.info(
            "ado_webhook_not_edge",
            ticket_id=ticket.id,
            reason="trigger tag already present on previous webhook",
        )
        append_trace(
            ticket.id, generate_trace_id(), "webhook", "ado_webhook_skipped_not_edge",
            ticket_type=ticket.ticket_type, source="ado",
            ticket_title=ticket.title,
            reason="tag already present on previous webhook",
        )
        return {
            "status": "skipped",
            "ticket_id": ticket.id,
            "reason": "trigger tag already present on previous webhook (not a new edge)",
        }

    trace_id = generate_trace_id()
    logger.info("ado_webhook_received", ticket_id=ticket.id, ticket_type=ticket.ticket_type)
    append_trace(ticket.id, trace_id, "webhook", "ado_webhook_received",
                 ticket_type=ticket.ticket_type, source="ado",
                 ticket_title=ticket.title)
    _bump_webhook_counter(COUNTER_ACCEPTED_EDGE)

    dispatch = _enqueue_or_background(ticket, background_tasks, trace_id=trace_id)
    if dispatch == "duplicate":
        return {"status": "skipped", "ticket_id": ticket.id, "reason": "already processing"}
    return {"status": "accepted", "ticket_id": ticket.id, "dispatch": dispatch}


@app.post("/api/process-ticket", status_code=202, dependencies=[Depends(_require_api_key)])
async def manual_process_ticket(
    ticket: TicketPayload,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Manual trigger — accepts a TicketPayload directly (bypasses webhook).

    Essential for testing the pipeline without Jira/ADO webhooks configured.
    """
    logger.info(
        "manual_ticket_submitted", ticket_id=ticket.id, ticket_type=ticket.ticket_type
    )
    dispatch = _enqueue_or_background(ticket, background_tasks)
    return {"status": "accepted", "ticket_id": ticket.id, "dispatch": dispatch}


_TICKET_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+-[0-9]+$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9/_.-]+$")  # No shell metacharacters
_VALID_PHASES = {"qa", "e2e", "review"}


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
    if not _BRANCH_PATTERN.match(branch):
        raise HTTPException(
            status_code=400,
            detail="Invalid branch name (alphanumeric, slashes, dots, hyphens only)",
        )

    client_repo = settings.default_client_repo
    if not client_repo:
        return {"status": "error", "detail": "No default_client_repo configured"}

    worktree_dir = str(Path(client_repo).parent / "worktrees" / branch)
    # Ensure resolved path is under the expected worktrees parent (path traversal guard)
    worktree_resolved = Path(worktree_dir).resolve()
    worktrees_parent = (Path(client_repo).parent / "worktrees").resolve()
    if not str(worktree_resolved).startswith(str(worktrees_parent)):
        raise HTTPException(status_code=400, detail="Branch resolves outside worktree directory")
    if not Path(worktree_dir).exists():
        return {
            "status": "error",
            "detail": f"Worktree not found for branch '{branch}'. Run the ticket first.",
        }

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
            result: dict[str, str] = response.json()
            return result
        except httpx.ConnectError:
            logger.warning("l3_service_unavailable")
            return {"status": "l3_unavailable"}


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
    """
    body = await request.json()
    ticket_id = str(body.pop("ticket_id", ""))
    trace_id = str(body.pop("trace_id", ""))
    phase = str(body.pop("phase", ""))
    event = str(body.pop("event", ""))
    body.pop("timestamp", None)  # append_trace generates its own timestamp
    if ticket_id and event:
        append_trace(ticket_id, trace_id, phase, event, source="agent", **body)
    return {"status": "ok"}


@app.post("/api/agent-complete", status_code=200, dependencies=[Depends(_require_api_key)])
async def agent_complete(payload: CompletionPayload) -> dict[str, str]:
    """Called by the spawn script when the agent finishes.

    Updates the Jira/ADO ticket with the PR link and transitions to Done.
    """
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

    _BACKGROUND_TASKS.add(asyncio.ensure_future(_delayed_release(payload.ticket_id)))

    # Trace: record completion — reuse the trace_id from the spawn chain
    # so live-reported entries and completion entries share the same trace_id
    trace_id = payload.trace_id or generate_trace_id()
    append_trace(payload.ticket_id, trace_id, "completion", "agent_finished",
                 status=payload.status, pr_url=payload.pr_url, branch=payload.branch)

    # Consolidate worktree artifacts into the persistent trace
    worktree_path = f"{settings.default_client_repo}/../worktrees/{payload.branch}"
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

        if payload.status == "complete":
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
