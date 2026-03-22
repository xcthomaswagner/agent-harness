"""L1 Pre-Processing Service — Webhook receiver and ticket processing pipeline."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import structlog
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from adapters.ado_adapter import AdoAdapter
from adapters.jira_adapter import JiraAdapter
from config import settings
from models import TicketPayload
from pipeline import Pipeline
from trace_dashboard import router as trace_router
from tracer import append_trace, consolidate_worktree_logs, generate_trace_id

logger = structlog.get_logger()

app = FastAPI(
    title="Agentic Harness L1 Pre-Processing",
    description="Receives Jira/ADO webhooks, enriches tickets, dispatches to Agent Teams.",
    version="0.1.0",
)
app.include_router(trace_router)

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


# --- Pipeline processing (background) ---


def _enqueue_or_background(
    ticket: TicketPayload, background_tasks: BackgroundTasks
) -> str:
    """Try to enqueue via Redis, fall back to FastAPI BackgroundTasks.

    Returns "queued" or "background".
    """
    from queue_worker import enqueue_ticket

    job_id = enqueue_ticket(ticket)
    if job_id:
        logger.info("ticket_queued", ticket_id=ticket.id, job_id=job_id)
        return "queued"

    background_tasks.add_task(_process_ticket, ticket)
    return "background"


async def _process_ticket(ticket: TicketPayload) -> None:
    """Process a normalized ticket through the L1 pipeline.

    Steps:
    1. Run ticket analyst (Claude Opus API call) to enrich
    2. Route based on analyst output:
       - Enriched: hand off to L2 (spawn Agent Team)
       - Info request: write comment to Jira/ADO, set status
       - Decomposition: flag for manual PM splitting
    """
    log = logger.bind(ticket_id=ticket.id, source=ticket.source)
    log.info("processing_ticket_started")
    trace_id = generate_trace_id()
    append_trace(ticket.id, trace_id, "pipeline", "processing_started",
                 ticket_type=ticket.ticket_type, source=ticket.source)

    try:
        result = await _get_pipeline().process(ticket)
        log.info("processing_ticket_completed", **result)
        # Remove ticket_id from result to avoid collision with positional arg
        trace_data = {k: v for k, v in result.items() if k != "ticket_id"}
        append_trace(ticket.id, trace_id, "pipeline", "processing_completed", **trace_data)
    except Exception:
        log.exception("processing_ticket_failed")


# --- Endpoints ---


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/webhooks/jira", status_code=202)
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
) -> dict[str, str]:
    """Receive a Jira automation webhook and enqueue for processing.

    Jira automation rules fire this webhook when a ticket transitions to
    "Ready for AI" or receives the `ai-implement` label.
    """
    body = await request.body()

    # Validate webhook signature if secret is configured
    if settings.webhook_secret:
        if not x_hub_signature:
            raise HTTPException(status_code=401, detail="Missing webhook signature")
        expected = hmac.new(
            settings.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        signature = x_hub_signature.removeprefix("sha256=")
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc

    ticket = _get_jira_adapter().normalize(payload)
    trace_id = generate_trace_id()

    logger.info("jira_webhook_received", ticket_id=ticket.id, ticket_type=ticket.ticket_type)
    append_trace(ticket.id, trace_id, "webhook", "jira_webhook_received",
                 ticket_type=ticket.ticket_type, source="jira")

    dispatch = _enqueue_or_background(ticket, background_tasks)
    return {"status": "accepted", "ticket_id": ticket.id, "dispatch": dispatch}


@app.post("/webhooks/ado", status_code=202)
async def ado_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Receive an Azure DevOps Service Hook webhook and enqueue for processing."""
    body = await request.body()

    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc

    ticket = _get_ado_adapter().normalize(payload)

    logger.info("ado_webhook_received", ticket_id=ticket.id, ticket_type=ticket.ticket_type)

    dispatch = _enqueue_or_background(ticket, background_tasks)
    return {"status": "accepted", "ticket_id": ticket.id, "dispatch": dispatch}


@app.post("/api/process-ticket", status_code=202)
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


@app.post("/api/agent-complete", status_code=200)
async def agent_complete(payload: CompletionPayload) -> dict[str, str]:
    """Called by the spawn script when the agent finishes.

    Updates the Jira/ADO ticket with the PR link and transitions to Done.
    """
    log = logger.bind(ticket_id=payload.ticket_id, status=payload.status)
    log.info("agent_completion_received", pr_url=payload.pr_url)

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

        # Unregister from conflict detection
        from conflict_detector import ConflictDetector

        ConflictDetector().unregister(payload.ticket_id)

    except Exception:
        log.exception("completion_update_failed")

    return {"status": "ok", "ticket_id": payload.ticket_id}
