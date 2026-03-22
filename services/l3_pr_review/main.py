"""L3 PR Review Service — GitHub webhook receiver for PR events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import structlog
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from event_classifier import EventType, classify_event
from spawner import SessionSpawner

from dotenv import load_dotenv

load_dotenv()

logger = structlog.get_logger()

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
BOT_USERNAME = os.getenv("BOT_GITHUB_USERNAME", "github-actions[bot]")

app = FastAPI(
    title="Agentic Harness L3 PR Review",
    description="Receives GitHub PR webhooks, classifies events, spawns review/fix sessions.",
    version="0.1.0",
)

_spawner: SessionSpawner | None = None


def _get_spawner() -> SessionSpawner:
    global _spawner
    if _spawner is None:
        _spawner = SessionSpawner(repo_path=os.getenv("CLIENT_REPO_PATH", ""))
    return _spawner


# --- Helpers ---


def _is_bot_author(payload: dict[str, Any], user_path: list[str]) -> bool:
    """Check whether the event author matches the bot's GitHub username."""
    obj: Any = payload
    for key in user_path:
        obj = obj.get(key, {})
    login: str = obj.get("login", "") if isinstance(obj, dict) else ""
    return login == BOT_USERNAME


# --- Event handlers ---


async def _handle_pr_opened(payload: dict[str, Any]) -> None:
    """Handle a new or ready-for-review PR — spawn AI review."""
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number", 0)
    pr_diff_url = pr.get("diff_url", "")
    pr_body = pr.get("body", "")

    log = logger.bind(pr_number=pr_number)
    log.info("handling_pr_opened")

    _get_spawner().spawn_pr_review(
        pr_number=pr_number,
        pr_diff=f"Diff available at: {pr_diff_url}",
        ticket_context=pr_body,
    )


async def _handle_ci_failed(payload: dict[str, Any]) -> None:
    """Handle CI failure — spawn fix agent."""
    check = payload.get("check_suite", payload.get("check_run", {}))
    pr_numbers = [
        pr.get("number", 0) for pr in check.get("pull_requests", [])
    ]
    branch = check.get("head_branch", "")
    conclusion = check.get("conclusion", "")

    if not pr_numbers:
        logger.debug("ci_failure_no_pr", branch=branch)
        return

    log = logger.bind(pr_numbers=pr_numbers, branch=branch)
    log.info("handling_ci_failure")

    # TODO: Fetch actual failure logs from GitHub Actions API
    failure_summary = f"CI {conclusion} on branch {branch}"

    for pr_number in pr_numbers:
        _get_spawner().spawn_ci_fix(
            pr_number=pr_number,
            branch=branch,
            failure_logs=failure_summary,
        )


async def _handle_review_comment(payload: dict[str, Any]) -> None:
    """Handle human review comment — spawn response agent."""
    # From pull_request_review event
    review = payload.get("review", {})
    if review:
        if _is_bot_author(payload, ["review", "user"]):
            logger.debug("ignoring_bot_review_comment")
            return
        pr_number = payload.get("pull_request", {}).get("number", 0)
        comment_body = review.get("body", "")
        comment_author = review.get("user", {}).get("login", "unknown")
    else:
        # From issue_comment event
        if _is_bot_author(payload, ["comment", "user"]):
            logger.debug("ignoring_bot_issue_comment")
            return
        issue = payload.get("issue", {})
        pr_number = issue.get("number", 0)
        comment = payload.get("comment", {})
        comment_body = comment.get("body", "")
        comment_author = comment.get("user", {}).get("login", "unknown")

    if not comment_body.strip():
        return

    log = logger.bind(pr_number=pr_number, author=comment_author)
    log.info("handling_review_comment")

    _get_spawner().spawn_comment_response(
        pr_number=pr_number,
        comment_body=comment_body,
        comment_author=comment_author,
    )


async def _handle_review_changes_requested(payload: dict[str, Any]) -> None:
    """Handle change requests — spawn targeted fix agent."""
    if _is_bot_author(payload, ["review", "user"]):
        logger.debug("ignoring_bot_changes_requested")
        return
    review = payload.get("review", {})
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number", 0)
    review_body = review.get("body", "")
    reviewer = review.get("user", {}).get("login", "unknown")

    log = logger.bind(pr_number=pr_number, reviewer=reviewer)
    log.info("handling_changes_requested")

    _get_spawner().spawn_comment_response(
        pr_number=pr_number,
        comment_body=f"Changes requested by @{reviewer}:\n\n{review_body}",
        comment_author=reviewer,
    )


# Route map
_HANDLERS: dict[EventType, Any] = {
    EventType.PR_OPENED: _handle_pr_opened,
    EventType.PR_READY_FOR_REVIEW: _handle_pr_opened,
    EventType.CI_FAILED: _handle_ci_failed,
    EventType.REVIEW_COMMENT: _handle_review_comment,
    EventType.REVIEW_CHANGES_REQUESTED: _handle_review_changes_requested,
}


# --- Endpoints ---


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None, alias="x-hub-signature-256"),
    x_github_event: str | None = Header(default=None, alias="x-github-event"),
) -> dict[str, str]:
    """Receive GitHub webhooks for PR events."""
    body = await request.body()

    # Validate signature — reject all requests when no secret is configured
    if not WEBHOOK_SECRET:
        logger.error("webhook_secret_not_configured")
        raise HTTPException(status_code=500, detail="Webhook secret not configured")
    if not x_hub_signature_256:
        raise HTTPException(status_code=401, detail="Missing webhook signature")
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc

    headers = {
        "x-github-event": x_github_event or "",
    }

    event_type = classify_event(headers, payload)
    logger.info("github_webhook_received", event_type=event_type, github_event=x_github_event)

    if event_type == EventType.IGNORED:
        return {"status": "ignored", "event_type": event_type}

    handler = _HANDLERS.get(event_type)
    if handler:
        background_tasks.add_task(handler, payload)
        return {"status": "accepted", "event_type": event_type}

    return {"status": "unhandled", "event_type": event_type}
