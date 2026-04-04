"""L1 Pre-Processing Service — Webhook receiver and ticket processing pipeline."""

from __future__ import annotations

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
from config import settings
from models import TicketPayload
from pipeline import Pipeline
from trace_dashboard import router as trace_router
from tracer import append_trace, consolidate_worktree_logs, generate_trace_id

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
app.include_router(trace_router)


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
    except Exception as exc:
        log.exception("processing_ticket_failed")
        append_trace(
            ticket.id, tid, "pipeline", "error",
            error_type=type(exc).__name__,
            error_message=str(exc)[:500],
        )
    finally:
        _release_ticket(ticket.id)


# --- Endpoints ---


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


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
                 ticket_type=ticket.ticket_type, source="jira")

    dispatch = _enqueue_or_background(ticket, background_tasks, trace_id=trace_id)
    if dispatch == "duplicate":
        return {"status": "skipped", "ticket_id": ticket.id, "reason": "already processing"}
    return {"status": "accepted", "ticket_id": ticket.id, "dispatch": dispatch}


@app.post("/webhooks/ado", status_code=202)
async def ado_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
) -> dict[str, str]:
    """Receive an Azure DevOps Service Hook webhook and enqueue for processing."""
    payload = await _validate_and_parse_webhook(request, x_hub_signature)
    ticket = _get_ado_adapter().normalize(payload)

    trace_id = generate_trace_id()
    logger.info("ado_webhook_received", ticket_id=ticket.id, ticket_type=ticket.ticket_type)
    append_trace(ticket.id, trace_id, "webhook", "ado_webhook_received",
                 ticket_type=ticket.ticket_type, source="ado")

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
    status: str  # "complete", "partial", "escalated"
    pr_url: str = ""
    branch: str = ""
    failed_units: list[FailedUnit] = []


@app.post("/api/agent-complete", status_code=200, dependencies=[Depends(_require_api_key)])
async def agent_complete(payload: CompletionPayload) -> dict[str, str]:
    """Called by the spawn script when the agent finishes.

    Updates the Jira/ADO ticket with the PR link and transitions to Done.
    """
    log = logger.bind(ticket_id=payload.ticket_id, status=payload.status)
    log.info("agent_completion_received", pr_url=payload.pr_url)

    # Clear idempotency guard so ticket can be reprocessed if needed
    _release_ticket(payload.ticket_id)

    # Trace: record completion and consolidate worktree logs
    trace_id = generate_trace_id()
    append_trace(payload.ticket_id, trace_id, "completion", "agent_finished",
                 status=payload.status, pr_url=payload.pr_url, branch=payload.branch)

    # Consolidate worktree artifacts into the persistent trace
    worktree_path = f"{settings.default_client_repo}/../worktrees/{payload.branch}"
    consolidate_worktree_logs(payload.ticket_id, trace_id, worktree_path)

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

        # Upload final screenshot if it exists in the worktree
        screenshot_path = Path(worktree_path) / ".harness" / "screenshots" / "final.png"
        if screenshot_path.exists():
            await adapter.upload_attachment(
                payload.ticket_id,
                str(screenshot_path),
                filename=f"{payload.ticket_id}-implementation.png",
            )
            log.info("screenshot_uploaded_to_jira", path=str(screenshot_path))

        if payload.status == "complete":
            await adapter.transition_status(payload.ticket_id, "Done")
            log.info("ticket_transitioned_to_done")
        elif payload.status in ("partial", "escalated"):
            label = "needs-human" if payload.status == "escalated" else "partial-implementation"
            await adapter.add_label(payload.ticket_id, label)

            # Auto-file failed units as Jira sub-tasks
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
