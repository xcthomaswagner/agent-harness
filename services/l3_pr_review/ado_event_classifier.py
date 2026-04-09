"""Classifies Azure DevOps Service Hook events into actionable categories."""

from __future__ import annotations

from typing import Any

import structlog

from event_classifier import EventType

logger = structlog.get_logger()


def classify_ado_event(payload: dict[str, Any]) -> EventType:
    """Classify an ADO Service Hook event into an actionable EventType.

    ADO PR webhooks carry the event type in the payload body (``eventType``),
    not in HTTP headers like GitHub.  The ``resource`` object contains the PR
    data including status, merge info, and reviewer votes.

    Args:
        payload: Parsed JSON body of the ADO Service Hook.

    Returns:
        The classified EventType.
    """
    event_type = payload.get("eventType", "")
    resource: dict[str, Any] = payload.get("resource", {})

    # --- PR created ---
    if event_type == "git.pullrequest.created":
        return EventType.PR_OPENED

    # --- PR merged (dedicated event type, if configured) ---
    if event_type == "git.pullrequest.merged":
        return EventType.PR_MERGED

    # --- PR updated (covers multiple sub-events) ---
    if event_type == "git.pullrequest.updated":
        status = resource.get("status", "")

        # PR completed (merged or closed-as-completed)
        if status == "completed":
            return EventType.PR_MERGED

        # PR abandoned (closed without merge) — ignore
        if status == "abandoned":
            return EventType.IGNORED

        # Check for reviewer vote changes
        reviewers: list[dict[str, Any]] = resource.get("reviewers", [])
        if reviewers:
            vote = _extract_latest_vote(reviewers)
            if vote is not None:
                if vote == 10:
                    return EventType.REVIEW_APPROVED
                if vote in (-10, -5):
                    return EventType.REVIEW_CHANGES_REQUESTED
                if vote in (0, 5):
                    return EventType.REVIEW_COMMENT

        # Check for new commits (source branch updated)
        # ADO includes lastMergeSourceCommit when source is updated.
        # We detect this by checking if the update message references commits
        # or if lastMergeSourceCommit is present in the resource.
        if resource.get("lastMergeSourceCommit"):
            return EventType.PR_SYNCHRONIZE

        logger.debug(
            "ado_pr_updated_unhandled",
            status=status,
            has_reviewers=bool(reviewers),
        )
        return EventType.IGNORED

    logger.debug("unhandled_ado_event", event_type=event_type)
    return EventType.IGNORED


def _extract_latest_vote(reviewers: list[dict[str, Any]]) -> int | None:
    """Extract the most recent non-zero vote from the reviewers list.

    ADO PR payloads include all reviewers.  When a webhook fires for a
    vote change, the updated reviewer entry reflects the new vote.
    We return the first non-zero vote found (ADO sends the changed
    reviewer first in update payloads), or 0 if all votes are zero,
    or None if the list is empty.
    """
    if not reviewers:
        return None
    for reviewer in reviewers:
        vote = reviewer.get("vote", 0)
        if vote != 0:
            return int(vote)
    # All reviewers have vote=0 (no vote / reset)
    return 0
