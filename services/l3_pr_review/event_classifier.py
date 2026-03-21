"""Classifies GitHub webhook events into actionable categories."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger()


class EventType(StrEnum):
    """Classified PR event types."""

    PR_OPENED = "pr_opened"
    PR_READY_FOR_REVIEW = "pr_ready_for_review"
    CI_FAILED = "ci_failed"
    CI_PASSED = "ci_passed"
    REVIEW_APPROVED = "review_approved"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"
    REVIEW_COMMENT = "review_comment"
    IGNORED = "ignored"


def classify_event(headers: dict[str, str], payload: dict[str, Any]) -> EventType:
    """Classify a GitHub webhook event into an actionable EventType.

    Args:
        headers: HTTP headers from the webhook (notably X-GitHub-Event).
        payload: Parsed JSON body of the webhook.

    Returns:
        The classified EventType.
    """
    github_event = headers.get("x-github-event", "")
    action = payload.get("action", "")

    # Pull request events
    if github_event == "pull_request":
        if action == "opened":
            return EventType.PR_OPENED
        if action == "ready_for_review":
            return EventType.PR_READY_FOR_REVIEW
        return EventType.IGNORED

    # Check suite / check run events (CI)
    if github_event in ("check_suite", "check_run"):
        conclusion = payload.get("check_suite", payload.get("check_run", {})).get(
            "conclusion", ""
        )
        if conclusion == "failure":
            return EventType.CI_FAILED
        if conclusion == "success":
            return EventType.CI_PASSED
        return EventType.IGNORED

    # Pull request review events
    if github_event == "pull_request_review":
        state = payload.get("review", {}).get("state", "")
        if state == "approved":
            return EventType.REVIEW_APPROVED
        if state == "changes_requested":
            return EventType.REVIEW_CHANGES_REQUESTED
        if state == "commented":
            return EventType.REVIEW_COMMENT
        return EventType.IGNORED

    # Issue comment on a PR (only new comments, not edits or deletions)
    if github_event == "issue_comment":
        if action == "created" and "pull_request" in payload.get("issue", {}):
            return EventType.REVIEW_COMMENT
        return EventType.IGNORED

    logger.debug("unhandled_github_event", github_event=github_event, action=action)
    return EventType.IGNORED
