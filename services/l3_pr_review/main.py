"""L3 PR Review Service — GitHub webhook receiver for PR events."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

# Add L1 to path for shared tracer access (single-machine deployment)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "l1_preprocessing"))
from tracer import append_trace, generate_trace_id, read_trace

from ado_event_classifier import classify_ado_event
from auto_merge import evaluate_and_maybe_merge
from backlog import append_backlog, backlog_status, drain_backlog
from event_classifier import EventType, classify_event
from github_api import get_pr_state
from spawner import SessionSpawner

load_dotenv()

logger = structlog.get_logger()

# Hold references to fire-and-forget startup tasks so they aren't GC'd.
_startup_tasks: set[asyncio.Task[None]] = set()

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
L1_SERVICE_URL = os.getenv("L1_SERVICE_URL", "http://localhost:8000")
L1_INTERNAL_API_TOKEN = os.getenv("L1_INTERNAL_API_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_GITHUB_USERNAME", "github-actions[bot]")
# Hidden marker injected into all agent-posted comments for self-detection.
# This catches bot-loops even when the agent uses the same GitHub user as the human.
BOT_COMMENT_MARKER = "<!-- xcagent -->"

# Dedup: track recently processed GitHub delivery IDs to prevent double-processing
# on webhook retries or race conditions. OrderedDict gives FIFO eviction so recent
# entries are never evicted before old ones.
_processed_deliveries: OrderedDict[str, None] = OrderedDict()
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


def _require_internal_api_token(x_internal_api_token: str | None) -> None:
    """Validate the admin API token in constant time.

    Plain ``!=`` leaks timing on the first differing byte, so an
    attacker can byte-by-byte recover the secret. Use
    ``hmac.compare_digest`` and raise the generic 401 only after the
    compare, so missing tokens and wrong tokens take the same path.

    Fails with 503 when the admin API isn't configured (empty env
    var) so we don't accidentally accept an empty-string token.
    """
    expected = os.getenv("L1_INTERNAL_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Admin API not configured")
    provided = x_internal_api_token or ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token")


_AI_BRANCH_PATTERN = re.compile(r"^ai/([A-Za-z0-9]+-\d+)")


_TICKET_TYPE_LABELS: tuple[str, ...] = (
    "bug",
    "chore",
    "config",
    "dependency",
    "docs",
    "story",
)


def _ticket_type_from_labels(
    labels: list[str], *, default: str = "story"
) -> str:
    """Return the first label that matches a known ticket type, or ``default``.

    Consolidates the copy-pasted label scan that appeared in both
    ``_handle_review_approved`` and ``_handle_ci_passed``. The two
    call sites previously used *different* label sets (one omitted
    "story"), so a new ticket type would have required touching both.
    """
    for label in labels:
        if label in _TICKET_TYPE_LABELS:
            return label
    return default


def _ticket_id_from_payload(payload: dict[str, Any]) -> str:
    """Extract ticket ID from the PR branch name (e.g., ai/SCRUM-16 → SCRUM-16)."""
    pr = payload.get("pull_request", {})
    branch = pr.get("head", {}).get("ref", "")
    match = _AI_BRANCH_PATTERN.match(branch)
    return match.group(1) if match else ""


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


def _lookup_trace_id(ticket_id: str) -> str:
    """Find the L2 run's trace ID for this ticket.

    Looks for the ``agent_finished`` or ``Pipeline complete`` event's trace_id
    (the L2 run's ID) rather than just taking the last entry, which could be
    from any source.
    """
    entries = read_trace(ticket_id)
    for entry in reversed(entries):
        ev = entry.get("event", "")
        if "agent_finished" in ev or "Pipeline complete" in ev:
            return str(entry.get("trace_id", generate_trace_id()))
    if entries:
        return str(entries[-1].get("trace_id", generate_trace_id()))
    # No trace exists — L3 event will start a new trace chain
    logger.warning("trace_id_lookup_miss", ticket_id=ticket_id,
                   hint="No existing trace found; L3 event will have a new trace ID")
    return generate_trace_id()


# --- Autonomy event forwarding (L3 → L1) ---


_AUTONOMY_EVENTS_PATH = "/api/internal/autonomy/events"
_HUMAN_ISSUES_PATH = "/api/internal/autonomy/human-issues"


async def _post_to_l1_with_retry(
    path: str,
    payload: dict[str, Any],
    *,
    log_event: str,
    log_context: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> bool:
    """POST ``payload`` to L1 with retry-once on transient failure.

    Returns True on 2xx, False on non-retryable 4xx or double-fail 5xx.
    Shared path for every L1 forwarder — both the internal autonomy
    pipelines (which use ``X-Internal-Api-Token``, the default) and
    the ``/api/agent-complete`` caller (which today sends no auth
    header because L1 runs with ``API_KEY`` unset in local dev).

    ``log_context`` is merged into every failure log.
    """
    url = f"{L1_SERVICE_URL.rstrip('/')}{path}"
    if headers is None:
        headers = {"X-Internal-Api-Token": L1_INTERNAL_API_TOKEN}

    last_error: str = ""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                return False
            if resp.status_code >= 400:
                logger.error(
                    log_event,
                    status_code=resp.status_code,
                    body=resp.text[:500],
                    **log_context,
                )
                return False
            return True
        except httpx.RequestError as exc:
            last_error = f"RequestError: {exc}"
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return False

    logger.error(log_event, error=last_error, url=url, **log_context)
    return False


async def _forward_autonomy_event_once(event: dict[str, Any]) -> bool:
    """POST event to L1 with retry-once on transient failure."""
    return await _post_to_l1_with_retry(
        _AUTONOMY_EVENTS_PATH,
        event,
        log_event="l1_autonomy_event_forward_failed",
        log_context={
            "event_type": event.get("event_type"),
            "ticket_id": event.get("ticket_id"),
        },
    )


async def _forward_autonomy_event(event: dict[str, Any]) -> None:
    """Forward autonomy event to L1; persist to backlog on final failure.

    Short-circuits (without backlog) if L1_INTERNAL_API_TOKEN is unset.
    """
    if not L1_INTERNAL_API_TOKEN:
        logger.warning(
            "l1_autonomy_event_forward_skipped",
            reason="L1_INTERNAL_API_TOKEN unset — autonomy events are being dropped. "
            "Set L1_INTERNAL_API_TOKEN to enable forwarding.",
            event_type=event.get("event_type"),
        )
        return
    ok = await _forward_autonomy_event_once(event)
    if not ok:
        logger.error(
            "l1_autonomy_event_forward_failed",
            event_type=event.get("event_type"),
            ticket_id=event.get("ticket_id"),
            backlogged=True,
        )
        await append_backlog("autonomy_event", event)


async def _forward_human_issue_once(payload: dict[str, Any]) -> bool:
    """POST human issue to L1 with retry-once on transient failure."""
    return await _post_to_l1_with_retry(
        _HUMAN_ISSUES_PATH,
        payload,
        log_event="l1_human_issue_forward_failed",
        log_context={
            "event_type": payload.get("event_type"),
            "ticket_id": payload.get("ticket_id"),
        },
    )


async def _forward_human_issue(payload: dict[str, Any]) -> None:
    """Forward human issue to L1; persist to backlog on final failure.

    Short-circuits (without backlog) if L1_INTERNAL_API_TOKEN is unset.
    """
    if not L1_INTERNAL_API_TOKEN:
        logger.warning(
            "l1_human_issue_forward_skipped",
            reason="L1_INTERNAL_API_TOKEN unset — human review issues are being dropped. "
            "Set L1_INTERNAL_API_TOKEN to enable forwarding.",
            event_type=payload.get("event_type"),
        )
        return
    ok = await _forward_human_issue_once(payload)
    if not ok:
        logger.error(
            "l1_human_issue_forward_failed",
            event_type=payload.get("event_type"),
            ticket_id=payload.get("ticket_id"),
            backlogged=True,
        )
        await append_backlog("human_issue", payload)


_GITHUB_DEFECT_LINK_PATH = "/api/internal/autonomy/github-defect-link"

# Match (in order): full PR URL, owner/repo#N, bare #N (same-repo).
_PR_REF_URL_PATTERN = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)"
)
_PR_REF_OWNER_REPO_PATTERN = re.compile(
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)"
)
_PR_REF_BARE_PATTERN = re.compile(r"(?<![A-Za-z0-9/#])#(\d+)(?![A-Za-z0-9/])")


def _extract_pr_ref(body: str, same_repo: str) -> tuple[str, int] | None:
    """Return (repo_full_name, pr_number) from first PR reference in body, else None.

    same_repo is used when the body contains a bare same-repo '#N' reference.
    """
    if not body:
        return None
    m = _PR_REF_URL_PATTERN.search(body)
    if m:
        return m.group(1), int(m.group(2))
    m = _PR_REF_OWNER_REPO_PATTERN.search(body)
    if m:
        return m.group(1), int(m.group(2))
    m = _PR_REF_BARE_PATTERN.search(body)
    if m and same_repo:
        return same_repo, int(m.group(1))
    return None


def _category_from_labels(labels: list[str]) -> str:
    """Map GitHub issue labels to defect_links.category."""
    lower = {(label_name or "").lower() for label_name in labels}
    if "pre-existing" in lower or "pre_existing" in lower:
        return "pre_existing"
    if "infra" in lower or "infrastructure" in lower:
        return "infra"
    if "feature-request" in lower or "feature_request" in lower or "enhancement" in lower:
        return "feature_request"
    return "escaped"


async def _forward_github_defect_once(payload: dict[str, Any]) -> bool:
    """POST GitHub defect-link to L1 with retry-once on transient failure."""
    return await _post_to_l1_with_retry(
        _GITHUB_DEFECT_LINK_PATH,
        payload,
        log_event="l1_github_defect_forward_failed",
        log_context={
            "issue_number": payload.get("issue_number"),
            "pr_number": payload.get("pr_number"),
        },
    )


async def _forward_github_defect(payload: dict[str, Any]) -> None:
    """Forward GitHub defect-link to L1; persist to backlog on final failure.

    Short-circuits (without backlog) if L1_INTERNAL_API_TOKEN is unset.
    """
    if not L1_INTERNAL_API_TOKEN:
        logger.warning(
            "l1_github_defect_forward_skipped",
            reason="L1_INTERNAL_API_TOKEN unset — GitHub defect links are being dropped. "
            "Set L1_INTERNAL_API_TOKEN to enable forwarding.",
            issue_number=payload.get("issue_number"),
        )
        return
    ok = await _forward_github_defect_once(payload)
    if not ok:
        logger.error(
            "l1_github_defect_forward_failed",
            issue_number=payload.get("issue_number"),
            backlogged=True,
        )
        await append_backlog("github_defect", payload)


def _is_bot_user(user: dict[str, Any]) -> bool:
    """Return True if the GitHub user looks like a bot."""
    if not isinstance(user, dict):
        return False
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _truncate(value: str | None, limit: int = 2000) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit]


def _build_autonomy_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_at: str | None = None,
) -> dict[str, Any] | None:
    """Build a normalized AutonomyEventIn payload from a GitHub webhook payload.

    Returns None if required fields are missing (no ticket_id, no PR number, etc.).
    """
    pr = payload.get("pull_request", {}) or {}
    repo = payload.get("repository", {}) or {}
    repo_full_name = repo.get("full_name", "") or (
        pr.get("base", {}).get("repo", {}).get("full_name", "")
    )
    pr_number = pr.get("number", 0)
    head = pr.get("head", {}) or {}
    base = pr.get("base", {}) or {}
    head_sha = head.get("sha", "")
    ticket_id = _ticket_id_from_payload(payload)

    if not (repo_full_name and pr_number and head_sha and ticket_id):
        logger.debug(
            "autonomy_event_missing_required_fields",
            event_type=event_type,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            ticket_id=ticket_id,
        )
        return None

    event: dict[str, Any] = {
        "event_type": event_type,
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "ticket_id": ticket_id,
        "event_at": event_at or datetime.now(UTC).isoformat(),
        "pr_url": pr.get("html_url", "") or None,
        "head_ref": head.get("ref", "") or None,
        "base_sha": base.get("sha", "") or None,
    }

    if event_type == "pr_merged":
        merged_at = pr.get("merged_at")
        if merged_at:
            event["merged_at"] = merged_at

    review = payload.get("review") or {}
    if review:
        user = review.get("user") or {}
        reviewer_login = user.get("login")
        if reviewer_login:
            event["reviewer_login"] = reviewer_login
        review_id = review.get("id")
        if review_id is not None:
            event["review_id"] = str(review_id)
        body = _truncate(review.get("body"))
        if body is not None:
            event["review_body"] = body
        review_url = review.get("html_url")
        if review_url:
            event["comment_url"] = review_url

    comment = payload.get("comment") or {}
    if comment:
        comment_id = comment.get("id")
        if comment_id is not None:
            event["comment_id"] = str(comment_id)
        comment_url = comment.get("html_url")
        if comment_url and "comment_url" not in event:
            event["comment_url"] = comment_url

    # Strip empty-string optionals for a cleaner payload
    return {k: v for k, v in event.items() if v is not None and v != ""}


async def _forward_review_body_human_issue(
    event_type: str, payload: dict[str, Any]
) -> None:
    """Forward the top-level review body as a human issue, if present and non-bot."""
    review = payload.get("review") or {}
    body = review.get("body") or ""
    if not body.strip():
        return
    user = review.get("user") or {}
    if _is_bot_user(user):
        return

    ticket_id = _ticket_id_from_payload(payload)
    if not ticket_id:
        logger.info("review_body_no_ticket_id", event_type=event_type)
        return

    pr = payload.get("pull_request", {}) or {}
    repo = payload.get("repository", {}) or {}
    repo_full_name = repo.get("full_name", "") or (
        pr.get("base", {}).get("repo", {}).get("full_name", "")
    )
    human_issue = {
        "repo_full_name": repo_full_name,
        "pr_number": pr.get("number", 0),
        "head_sha": pr.get("head", {}).get("sha", ""),
        "ticket_id": ticket_id,
        "external_id": str(review.get("id", "")),
        "event_type": event_type,
        "file_path": "",
        "line_start": 0,
        "line_end": 0,
        "summary": _truncate(body, 500) or "",
        "details": _truncate(body, 4000) or "",
        "reviewer_login": user.get("login", ""),
        "event_at": review.get("submitted_at") or datetime.now(UTC).isoformat(),
        "comment_url": review.get("html_url", ""),
    }
    await _forward_human_issue(human_issue)


# --- Event handlers ---


async def _handle_pr_opened(payload: dict[str, Any]) -> None:
    """Handle a new or ready-for-review PR — spawn AI review."""
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number", 0)
    pr_diff_url = pr.get("diff_url", "")
    pr_body = pr.get("body", "")

    log = logger.bind(pr_number=pr_number)
    log.info("handling_pr_opened")

    ticket_id = _ticket_id_from_payload(payload)
    if ticket_id:
        append_trace(ticket_id, _lookup_trace_id(ticket_id), "l3_pr_review",
                     "pr_review_spawned", pr_number=pr_number)

    # Forward autonomy event to L1 (pr_opened or pr_synchronized based on action)
    action = payload.get("action", "")
    autonomy_event_type = "pr_synchronized" if action == "synchronize" else "pr_opened"
    autonomy_event = _build_autonomy_event(autonomy_event_type, payload)
    if autonomy_event:
        await _forward_autonomy_event(autonomy_event)

    branch = pr.get("head", {}).get("ref", "")
    repo = payload.get("repository", {}).get("full_name", "")
    _get_spawner().spawn_pr_review(
        pr_number=pr_number,
        pr_diff=f"Diff available at: {pr_diff_url}",
        ticket_context=pr_body,
        branch=branch,
        repo=repo,
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

    # Trace CI failure — extract ticket ID from branch name
    match = _AI_BRANCH_PATTERN.match(branch)
    if match:
        ci_ticket_id = match.group(1)
        append_trace(ci_ticket_id, _lookup_trace_id(ci_ticket_id), "l3_ci_fix",
                     "ci_fix_spawned", branch=branch, pr_numbers=pr_numbers)

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
            repo=repo,
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

    # Forward autonomy event to L1
    autonomy_event = _build_autonomy_event("review_comment", payload)
    if autonomy_event:
        await _forward_autonomy_event(autonomy_event)

    # Forward the top-level review body as a human issue, if present.
    # (issue_comment events have no 'review' key, so this no-ops there.)
    if review:
        await _forward_review_body_human_issue("review_comment", payload)

    ticket_id = _ticket_id_from_payload(payload)
    if ticket_id:
        append_trace(ticket_id, _lookup_trace_id(ticket_id), "l3_comment",
                     "comment_response_spawned", pr_number=pr_number,
                     author=comment_author)

    pr = payload.get("pull_request", {})
    branch = pr.get("head", {}).get("ref", "")
    repo = payload.get("repository", {}).get("full_name", "")
    _get_spawner().spawn_comment_response(
        pr_number=pr_number,
        comment_body=comment_body,
        comment_author=comment_author,
        branch=branch,
        repo=repo,
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

    # Forward autonomy event to L1
    autonomy_event = _build_autonomy_event("review_changes_requested", payload)
    if autonomy_event:
        await _forward_autonomy_event(autonomy_event)

    # Forward top-level review body as a human issue
    await _forward_review_body_human_issue("review_changes_requested", payload)

    ticket_id = _ticket_id_from_payload(payload)
    if ticket_id:
        append_trace(ticket_id, _lookup_trace_id(ticket_id), "l3_changes_requested",
                     "changes_requested_spawned", pr_number=pr_number,
                     reviewer=reviewer)

    branch = pr.get("head", {}).get("ref", "")
    repo = payload.get("repository", {}).get("full_name", "")
    _get_spawner().spawn_comment_response(
        pr_number=pr_number,
        comment_body=f"Changes requested by @{reviewer}:\n\n{review_body}",
        comment_author=reviewer,
        branch=branch,
        repo=repo,
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

    # Forward autonomy event to L1
    autonomy_event = _build_autonomy_event("review_approved", payload)
    if autonomy_event:
        await _forward_autonomy_event(autonomy_event)

    # Forward top-level review body as a human issue (only if body non-empty)
    await _forward_review_body_human_issue("review_approved", payload)

    # Extract the AI ticket id from the branch. Approvals on non-AI
    # branches (main, develop, a human PR that happens to be on this
    # repo) must not trigger L1's /api/agent-complete — silently
    # marking an unrelated branch as a "completed ticket". The
    # previous ``branch.replace("ai/", "")`` replaced every
    # occurrence of the substring anywhere in the branch name AND
    # passed non-AI branches through unchanged, both of which
    # produced wrong ticket ids.
    ticket_id = _ticket_id_from_payload(payload)
    if not ticket_id:
        log.info(
            "pr_approved_skipped_non_ai_branch",
            branch=branch,
            reason="branch does not match ai/<TICKET>-<N> pattern",
        )
        return

    # Extract ticket type from PR labels
    labels = [label.get("name", "") for label in pr.get("labels", [])]
    ticket_type = _ticket_type_from_labels(labels)

    # Notify L1 of the approval for autonomy tracking. Routes through
    # the shared retry helper — previously this was the only L1 caller
    # with a bespoke inline 2-attempt retry loop. ``headers={}`` keeps
    # the current no-auth-header behavior (L1 runs with API_KEY unset
    # in dev); swap to ``{"X-API-Key": ...}`` once the production
    # deployment enforces it.
    await _post_to_l1_with_retry(
        "/api/agent-complete",
        {
            "ticket_id": ticket_id,
            "status": "complete",
            "pr_url": pr.get("html_url", ""),
            "branch": branch,
        },
        log_event="l1_agent_complete_failed",
        log_context={"ticket_id": ticket_id, "branch": branch},
        headers={},
    )

    log.info(
        "pr_approved",
        ticket_type=ticket_type,
        branch=branch,
        repo=repo,
    )

    # Phase 4: evaluate auto-merge policy. Reuses ``ticket_id``
    # extracted at the top of the function via the canonical regex —
    # we would have already returned if it were empty.
    try:
        await evaluate_and_maybe_merge(
            repo_full_name=repo,
            pr_number=pr_number,
            head_sha=pr.get("head", {}).get("sha", ""),
            ticket_id=ticket_id,
            ticket_type=ticket_type,
            trigger_event="review_approved",
        )
    except Exception:
        log.exception("auto_merge_evaluation_failed")


async def _handle_review_comment_created(payload: dict[str, Any]) -> None:
    """Handle line-anchored PR review comment — forward as a human issue to L1."""
    comment = payload.get("comment") or {}
    if not comment:
        return
    action = payload.get("action", "")
    if action not in ("created", "edited"):
        return
    body = comment.get("body") or ""
    if not body.strip():
        return
    user = comment.get("user") or {}
    if _is_bot_user(user):
        logger.debug("ignoring_bot_review_comment_created")
        return
    # Also honor the hidden marker guard used elsewhere
    if BOT_COMMENT_MARKER and BOT_COMMENT_MARKER in body:
        logger.debug("ignoring_marker_review_comment_created")
        return

    ticket_id = _ticket_id_from_payload(payload)
    if not ticket_id:
        logger.info("review_comment_no_ticket_id")
        return

    pr = payload.get("pull_request", {}) or {}
    repo = payload.get("repository", {}) or {}
    repo_full_name = repo.get("full_name", "") or (
        pr.get("base", {}).get("repo", {}).get("full_name", "")
    )
    line_start = comment.get("line") or comment.get("original_line") or 0
    line_end = line_start

    human_issue = {
        "repo_full_name": repo_full_name,
        "pr_number": pr.get("number", 0),
        "head_sha": pr.get("head", {}).get("sha", ""),
        "ticket_id": ticket_id,
        "external_id": str(comment.get("id", "")),
        "event_type": "review_comment",
        "file_path": comment.get("path", ""),
        "line_start": int(line_start) if line_start else 0,
        "line_end": int(line_end) if line_end else 0,
        "summary": _truncate(body, 500) or "",
        "details": _truncate(body, 4000) or "",
        "reviewer_login": user.get("login", ""),
        "event_at": comment.get("created_at") or datetime.now(UTC).isoformat(),
        "comment_url": comment.get("html_url", ""),
    }
    await _forward_human_issue(human_issue)


async def _handle_pr_merged(payload: dict[str, Any]) -> None:
    """Handle PR merged — forward autonomy event to L1."""
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number", 0)
    merged_at = pr.get("merged_at")

    log = logger.bind(pr_number=pr_number, merged_at=merged_at)
    log.info("handling_pr_merged")

    autonomy_event = _build_autonomy_event(
        "pr_merged", payload, event_at=merged_at or None
    )
    if autonomy_event:
        await _forward_autonomy_event(autonomy_event)


async def _handle_ci_passed(payload: dict[str, Any]) -> None:
    """Handle CI passing — check if PR is approved and ready for auto-merge."""
    check = payload.get("check_suite", payload.get("check_run", {}))
    pr_entries = check.get("pull_requests", []) or []
    pr_numbers = [pr.get("number", 0) for pr in pr_entries]
    branch = check.get("head_branch", "")
    head_sha = check.get("head_sha", "") or check.get("head_commit", {}).get("id", "")
    repo_full_name = (
        payload.get("repository", {}).get("full_name", "")
        or check.get("repository", {}).get("full_name", "")
    )

    if not pr_numbers:
        return

    log = logger.bind(pr_numbers=pr_numbers, branch=branch)
    log.info("ci_passed", branch=branch)

    # Derive ticket_id from branch (ai/TICKET-123)
    match = _AI_BRANCH_PATTERN.match(branch or "")
    ticket_id = match.group(1) if match else ""

    # Phase 4: evaluate auto-merge for each PR (check suite may be attached to multiple)
    for pr_number in pr_numbers:
        try:
            # We need ticket_type; fetch labels via PR state
            pr_state = await get_pr_state(repo_full_name, pr_number)
            ticket_type = _ticket_type_from_labels(
                pr_state.get("labels", []) if pr_state else [],
                default="",
            )
            await evaluate_and_maybe_merge(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                ticket_id=ticket_id,
                ticket_type=ticket_type,
                trigger_event="ci_passed",
            )
        except Exception:
            log.exception("auto_merge_evaluation_failed", pr_number=pr_number)


async def _handle_issue_labeled(payload: dict[str, Any]) -> None:
    """Handle GitHub issues.labeled event — forward as defect-link if applicable.

    Checks if any label matches the configured defect labels
    (GITHUB_DEFECT_LABELS env var, default: defect,bug,regression).
    If so, parses the issue body for a PR reference and forwards the
    normalized payload to L1.
    """
    if payload.get("action", "") != "labeled":
        return
    issue = payload.get("issue") or {}
    if not issue:
        return

    defect_labels_env = os.getenv(
        "GITHUB_DEFECT_LABELS", "defect,bug,regression"
    )
    defect_labels = [
        label.strip().lower()
        for label in defect_labels_env.split(",")
        if label.strip()
    ]
    labels = [
        (label_obj or {}).get("name", "")
        for label_obj in (issue.get("labels") or [])
    ]
    lower_labels = {label_name.lower() for label_name in labels}
    if not any(defect_label in lower_labels for defect_label in defect_labels):
        logger.debug(
            "github_issue_labeled_not_defect",
            issue_number=issue.get("number"),
            labels=labels,
        )
        return

    repo_full_name = (payload.get("repository") or {}).get("full_name", "")
    body = issue.get("body") or ""
    pr_ref = _extract_pr_ref(body, repo_full_name)
    if not pr_ref:
        logger.info(
            "github_defect_no_pr_ref",
            issue_number=issue.get("number"),
            repo=repo_full_name,
        )
        return
    pr_repo, pr_number = pr_ref

    forward_payload = {
        "issue_number": int(issue.get("number") or 0),
        "issue_url": issue.get("html_url", "") or "",
        "issue_title": (issue.get("title") or "")[:500],
        "issue_body": body[:2000],
        "labels": labels,
        "reported_at": issue.get("created_at", "") or "",
        "reporter_login": (issue.get("user") or {}).get("login", "") or "",
        "pr_repo_full_name": pr_repo,
        "pr_number": pr_number,
        "category": _category_from_labels(labels),
        "severity": "",
    }
    logger.info(
        "github_defect_forwarding",
        issue_number=forward_payload["issue_number"],
        pr_repo=pr_repo,
        pr_number=pr_number,
        category=forward_payload["category"],
    )
    await _forward_github_defect(forward_payload)


# Route map
_HANDLERS: dict[EventType, Any] = {
    EventType.PR_OPENED: _handle_pr_opened,
    EventType.PR_SYNCHRONIZE: _handle_pr_opened,  # Re-review on new commits
    EventType.PR_READY_FOR_REVIEW: _handle_pr_opened,
    EventType.PR_MERGED: _handle_pr_merged,
    EventType.CI_FAILED: _handle_ci_failed,
    EventType.CI_PASSED: _handle_ci_passed,
    EventType.REVIEW_APPROVED: _handle_review_approved,
    EventType.REVIEW_COMMENT: _handle_review_comment,
    EventType.REVIEW_CHANGES_REQUESTED: _handle_review_changes_requested,
    EventType.REVIEW_COMMENT_CREATED: _handle_review_comment_created,
    EventType.ISSUE_LABELED: _handle_issue_labeled,
}


# --- Endpoints ---


@app.on_event("startup")
async def _drain_backlog_on_startup() -> None:
    async def _drain() -> None:
        forwarders: dict[str, Callable[[dict[str, Any]], Awaitable[bool]]] = {
            "autonomy_event": _forward_autonomy_event_once,
            "human_issue": _forward_human_issue_once,
            "github_defect": _forward_github_defect_once,
        }
        try:
            await drain_backlog(forwarders)
        except Exception:
            logger.exception("l3_backlog_startup_drain_failed")

    # Fire-and-forget so startup isn't blocked on L1 being down.
    # Store reference so asyncio doesn't GC the task mid-flight.
    _startup_tasks.add(asyncio.create_task(_drain()))


@app.post("/admin/backlog/drain")
async def post_drain_backlog(
    x_internal_api_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_internal_api_token(x_internal_api_token)
    forwarders: dict[str, Callable[[dict[str, Any]], Awaitable[bool]]] = {
        "autonomy_event": _forward_autonomy_event_once,
        "human_issue": _forward_human_issue_once,
        "github_defect": _forward_github_defect_once,
    }
    result = await drain_backlog(forwarders)
    return {"status": "ok", **result}


@app.get("/admin/backlog/status")
async def get_backlog_status(
    x_internal_api_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_internal_api_token(x_internal_api_token)
    return backlog_status()


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

    # Validate signature — skip validation in dev mode (no secret configured)
    if WEBHOOK_SECRET:
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
        _processed_deliveries[delivery_id] = None
        while len(_processed_deliveries) > _MAX_DELIVERY_CACHE:
            _processed_deliveries.popitem(last=False)  # FIFO: evict oldest

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


# --- ADO Service Hook webhook ---

ADO_WEBHOOK_TOKEN = os.getenv("ADO_WEBHOOK_TOKEN", "")

_ADO_BRANCH_PATTERN = re.compile(r"^refs/heads/ai/([A-Za-z0-9]+-\d+)")


def _ticket_id_from_ado_payload(payload: dict[str, Any]) -> str:
    """Extract ticket ID from ADO PR source branch (e.g., refs/heads/ai/SCRUM-16 -> SCRUM-16)."""
    resource = payload.get("resource", {})
    source_ref = resource.get("sourceRefName", "")
    match = _ADO_BRANCH_PATTERN.match(source_ref)
    return match.group(1) if match else ""


async def _handle_ado_pr_opened(payload: dict[str, Any]) -> None:
    """Handle a new ADO pull request -- log and prepare for spawner integration."""
    resource = payload.get("resource", {})
    pr_id = resource.get("pullRequestId", 0)
    repo_name = resource.get("repository", {}).get("name", "")
    project = resource.get("repository", {}).get("project", {}).get("name", "")
    source_ref = resource.get("sourceRefName", "")
    title = resource.get("title", "")
    ticket_id = _ticket_id_from_ado_payload(payload)

    log = logger.bind(
        pr_id=pr_id,
        repo=repo_name,
        project=project,
        ticket_id=ticket_id,
        source_control_type="azure-repos",
    )
    log.info(
        "handling_ado_pr_opened",
        title=title,
        source_ref=source_ref,
    )

    if ticket_id:
        append_trace(
            ticket_id,
            _lookup_trace_id(ticket_id),
            "l3_pr_review",
            "ado_pr_review_spawned",
            pr_id=pr_id,
            source_control_type="azure-repos",
        )

    # TODO: Wire into spawner with source_control_type="azure-repos"
    # once SessionSpawner supports ADO PR review sessions.
    log.info("ado_pr_opened_logged", note="spawner integration pending")


@app.post("/webhooks/ado-pr", status_code=202)
async def ado_pr_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_ado_webhook_token: str | None = Header(
        default=None, alias="x-ado-webhook-token"
    ),
) -> dict[str, str]:
    """Receive Azure DevOps Service Hook webhooks for PR events."""
    # Validate token if configured (constant-time comparison)
    if ADO_WEBHOOK_TOKEN and (
        not x_ado_webhook_token
        or not hmac.compare_digest(x_ado_webhook_token, ADO_WEBHOOK_TOKEN)
    ):
        raise HTTPException(status_code=401, detail="Invalid ADO webhook token")

    body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc

    event_type = classify_ado_event(payload)

    resource = payload.get("resource", {})
    pr_id = resource.get("pullRequestId", 0)
    repo_id = resource.get("repository", {}).get("id", "")
    project = resource.get("repository", {}).get("project", {}).get("name", "")

    logger.info(
        "ado_webhook_received",
        event_type=event_type,
        ado_event_type=payload.get("eventType", ""),
        pr_id=pr_id,
        project=project,
        repo_id=repo_id,
    )

    if event_type == EventType.IGNORED:
        return {"status": "ignored", "event_type": event_type}

    if event_type == EventType.PR_OPENED:
        background_tasks.add_task(_handle_ado_pr_opened, payload)
        return {"status": "accepted", "event_type": event_type}

    # Other ADO event types — log but don't act yet
    logger.info("ado_event_not_yet_handled", event_type=event_type, pr_id=pr_id)
    return {"status": "accepted", "event_type": event_type}
