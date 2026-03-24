"""L3 PR Review Service — GitHub webhook receiver for PR events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import structlog
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from event_classifier import EventType, classify_event
from spawner import SessionSpawner

load_dotenv()

logger = structlog.get_logger()

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
BOT_USERNAME = os.getenv("BOT_GITHUB_USERNAME", "github-actions[bot]")
# Hidden marker injected into all agent-posted comments for self-detection.
# This catches bot-loops even when the agent uses the same GitHub user as the human.
BOT_COMMENT_MARKER = "<!-- xcagent -->"

# Dedup: track recently processed GitHub delivery IDs to prevent double-processing
# on webhook retries or race conditions. Bounded to prevent memory growth.
_processed_deliveries: set[str] = set()
_MAX_DELIVERY_CACHE = 500

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


def _is_bot_comment(payload: dict[str, Any], user_path: list[str]) -> bool:
    """Check whether a comment was posted by the bot.

    Two detection methods (either triggers skip):
    1. Author's GitHub login matches BOT_USERNAME
    2. Comment body contains the hidden BOT_COMMENT_MARKER
    """
    # Check author login
    obj: Any = payload
    for key in user_path:
        obj = obj.get(key, {})
    login: str = obj.get("login", "") if isinstance(obj, dict) else ""
    if login == BOT_USERNAME:
        return True

    # Check comment body for hidden marker
    body = (
        payload.get("review", {}).get("body", "")
        or payload.get("comment", {}).get("body", "")
    )
    return BOT_COMMENT_MARKER in body


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


async def _fetch_ci_logs(repo: str, run_id: int) -> str:
    """Fetch CI failure logs from GitHub Actions API."""
    import httpx

    gh_token = os.getenv("GITHUB_TOKEN", "")
    if not gh_token or not run_id:
        return ""

    try:
        async with httpx.AsyncClient() as client:
            # Get failed jobs
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs",
                headers={
                    "Authorization": f"Bearer {gh_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=15.0,
            )
            if resp.status_code != 200:
                return f"Failed to fetch jobs: HTTP {resp.status_code}"

            jobs = resp.json().get("jobs", [])
            failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]

            logs: list[str] = []
            for job in failed_jobs[:3]:  # Limit to 3 failed jobs
                job_name = job.get("name", "unknown")
                steps = job.get("steps", [])
                failed_steps = [
                    s for s in steps if s.get("conclusion") == "failure"
                ]
                for step in failed_steps:
                    logs.append(
                        f"Job: {job_name} | Step: {step.get('name', '?')} "
                        f"| Status: {step.get('conclusion', '?')}"
                    )

            return "\n".join(logs) if logs else "CI failed but no step details available"
    except Exception as exc:
        logger.warning("ci_log_fetch_failed", error=str(exc))
        return f"Failed to fetch CI logs: {exc}"


async def _handle_ci_failed(payload: dict[str, Any]) -> None:
    """Handle CI failure — fetch logs and spawn fix agent."""
    check = payload.get("check_suite", payload.get("check_run", {}))
    pr_numbers = [
        pr.get("number", 0) for pr in check.get("pull_requests", [])
    ]
    branch = check.get("head_branch", "")
    conclusion = check.get("conclusion", "")
    repo = (
        check.get("repository", {}).get("full_name", "")
        or payload.get("repository", {}).get("full_name", "")
    )

    if not pr_numbers:
        logger.debug("ci_failure_no_pr", branch=branch)
        return

    log = logger.bind(pr_numbers=pr_numbers, branch=branch)
    log.info("handling_ci_failure")

    # Fetch actual failure logs from GitHub Actions
    run_id = check.get("id", 0)
    failure_logs = await _fetch_ci_logs(repo, run_id)
    if not failure_logs:
        failure_logs = f"CI {conclusion} on branch {branch}. Check the Actions tab for details."

    log.info("ci_logs_fetched", log_length=len(failure_logs))

    for pr_number in pr_numbers:
        _get_spawner().spawn_ci_fix(
            pr_number=pr_number,
            branch=branch,
            failure_logs=failure_logs,
        )


async def _handle_review_comment(payload: dict[str, Any]) -> None:
    """Handle human review comment — spawn response agent."""
    # From pull_request_review event
    review = payload.get("review", {})
    if review:
        if _is_bot_comment(payload, ["review", "user"]):
            logger.debug("ignoring_bot_review_comment")
            return
        pr_number = payload.get("pull_request", {}).get("number", 0)
        comment_body = review.get("body", "")
        comment_author = review.get("user", {}).get("login", "unknown")
    else:
        # From issue_comment event
        if _is_bot_comment(payload, ["comment", "user"]):
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
    if _is_bot_comment(payload, ["review", "user"]):
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


async def _handle_review_approved(payload: dict[str, Any]) -> None:
    """Handle PR approval — check if auto-merge is appropriate."""
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number", 0)
    branch = pr.get("head", {}).get("ref", "")
    reviewer = payload.get("review", {}).get("user", {}).get("login", "unknown")
    repo = pr.get("base", {}).get("repo", {}).get("full_name", "")

    log = logger.bind(pr_number=pr_number, reviewer=reviewer)
    log.info("handling_review_approved")

    # Extract ticket type from branch name or PR labels
    labels = [label.get("name", "") for label in pr.get("labels", [])]
    ticket_type = "story"  # Default
    for label in labels:
        if label in ("bug", "chore", "config", "dependency", "docs"):
            ticket_type = label
            break

    # Notify L1 of the approval for autonomy tracking
    try:
        import httpx

        await httpx.AsyncClient().post(
            "http://localhost:8000/api/agent-complete",
            json={
                "ticket_id": branch.replace("ai/", ""),
                "status": "complete",
                "pr_url": pr.get("html_url", ""),
                "branch": branch,
            },
            timeout=10.0,
        )
    except Exception:
        log.warning("l1_notification_failed")

    # TODO: Check autonomy.should_auto_merge() and merge if appropriate
    # For now, just log the approval
    log.info(
        "pr_approved",
        ticket_type=ticket_type,
        branch=branch,
        repo=repo,
    )


async def _handle_ci_passed(payload: dict[str, Any]) -> None:
    """Handle CI passing — check if PR is approved and ready for auto-merge."""
    check = payload.get("check_suite", payload.get("check_run", {}))
    pr_numbers = [pr.get("number", 0) for pr in check.get("pull_requests", [])]
    branch = check.get("head_branch", "")

    if not pr_numbers:
        return

    log = logger.bind(pr_numbers=pr_numbers, branch=branch)
    log.info("ci_passed", branch=branch)

    # TODO: If PR is already approved and autonomy allows, trigger auto-merge
    # This requires checking the PR's review state via GitHub API


# Route map
_HANDLERS: dict[EventType, Any] = {
    EventType.PR_OPENED: _handle_pr_opened,
    EventType.PR_READY_FOR_REVIEW: _handle_pr_opened,
    EventType.CI_FAILED: _handle_ci_failed,
    EventType.CI_PASSED: _handle_ci_passed,
    EventType.REVIEW_APPROVED: _handle_review_approved,
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
    x_github_delivery: str | None = Header(default=None, alias="x-github-delivery"),
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

    # Dedup: skip if this delivery was already processed (webhook retry)
    delivery_id = x_github_delivery or ""
    if delivery_id and delivery_id in _processed_deliveries:
        logger.debug("duplicate_delivery_skipped", delivery_id=delivery_id)
        return {"status": "skipped", "reason": "duplicate delivery"}
    if delivery_id:
        _processed_deliveries.add(delivery_id)
        # Bound the cache to prevent memory growth
        if len(_processed_deliveries) > _MAX_DELIVERY_CACHE:
            # Remove oldest entries (set is unordered, but this is fine for dedup)
            excess = len(_processed_deliveries) - _MAX_DELIVERY_CACHE
            for _ in range(excess):
                _processed_deliveries.pop()

    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc

    headers = {
        "x-github-event": x_github_event or "",
    }

    event_type = classify_event(headers, payload)
    logger.info(
        "github_webhook_received",
        event_type=event_type,
        github_event=x_github_event,
        delivery_id=delivery_id,
    )

    if event_type == EventType.IGNORED:
        return {"status": "ignored", "event_type": event_type}

    handler = _HANDLERS.get(event_type)
    if handler:
        background_tasks.add_task(handler, payload)
        return {"status": "accepted", "event_type": event_type}

    return {"status": "unhandled", "event_type": event_type}
