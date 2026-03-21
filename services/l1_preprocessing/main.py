"""L1 Pre-Processing Service — Webhook receiver and ticket processing pipeline."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import structlog
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from adapters.jira_adapter import JiraAdapter
from config import settings
from models import TicketPayload
from pipeline import Pipeline

logger = structlog.get_logger()

app = FastAPI(
    title="Agentic Harness L1 Pre-Processing",
    description="Receives Jira/ADO webhooks, enriches tickets, dispatches to Agent Teams.",
    version="0.1.0",
)

_jira_adapter: JiraAdapter | None = None
_pipeline: Pipeline | None = None


def _get_jira_adapter() -> JiraAdapter:
    """Return the Jira adapter, creating it lazily on first use."""
    global _jira_adapter
    if _jira_adapter is None:
        _jira_adapter = JiraAdapter(settings=settings)
    return _jira_adapter


def _get_pipeline() -> Pipeline:
    """Return the pipeline, creating it lazily on first use."""
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline(settings=settings)
    return _pipeline


# --- Pipeline processing (background) ---


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

    try:
        result = await _get_pipeline().process(ticket)
        log.info("processing_ticket_completed", **result)
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

    logger.info("jira_webhook_received", ticket_id=ticket.id, ticket_type=ticket.ticket_type)

    background_tasks.add_task(_process_ticket, ticket)
    return {"status": "accepted", "ticket_id": ticket.id}


@app.post("/webhooks/ado", status_code=501)
async def ado_webhook() -> dict[str, str]:
    """Azure DevOps webhook — stub for Phase 2."""
    raise HTTPException(status_code=501, detail="ADO webhook not yet implemented")


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
    background_tasks.add_task(_process_ticket, ticket)
    return {"status": "accepted", "ticket_id": ticket.id}
