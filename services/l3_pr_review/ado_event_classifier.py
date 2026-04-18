"""Classifies Azure DevOps Service Hook events into actionable categories."""

from __future__ import annotations

from typing import Any

import structlog

from event_classifier import EventType

logger = structlog.get_logger()


# ADO reviewer vote codes — see Azure DevOps REST API,
# GitPullRequestReviewer.vote:
#   https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-request-reviewers
#
#   10 = approved
#    5 = approved with suggestions
#    0 = no vote / reset
#   -5 = waiting for author
#  -10 = rejected
#
# Safety posture: when classifying for the auto-merge pipeline we
# COLLAPSE the whole reviewers array with "any rejection wins".
# Previously the classifier walked the reviewers list and returned the
# first non-zero vote under the assumption "ADO sends the changed
# reviewer first" — which is not guaranteed by the ADO webhook
# contract. On a PR where reviewer X approved and reviewer Y then
# rejected, the list order could place X first, masking Y's rejection
# and misclassifying the event as REVIEW_APPROVED. That would flow
# through _handle_review_approved -> evaluate_and_maybe_merge and
# default-OPEN the merge decision. Treating any -10/-5 as the
# dominant signal is the only safe order-independent rule.
_ADO_VOTE_REJECTED = {-10, -5}
_ADO_VOTE_APPROVED = {10}
_ADO_VOTE_SUGGESTED = {5}


def _classify_reviewer_votes(
    reviewers: list[dict[str, Any]],
) -> EventType | None:
    """Collapse all reviewer votes to a single EventType with rejection-wins.

    Returns None when the reviewers list is empty or all votes are 0.
    """
    if not reviewers:
        return None
    votes = {int(r.get("vote") or 0) for r in reviewers}
    if votes & _ADO_VOTE_REJECTED:
        return EventType.REVIEW_CHANGES_REQUESTED
    if votes & _ADO_VOTE_APPROVED:
        return EventType.REVIEW_APPROVED
    if votes & _ADO_VOTE_SUGGESTED:
        return EventType.REVIEW_COMMENT
    return None


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

    # --- Build completed ---
    if event_type == "build.complete":
        result = str(resource.get("result", "")).lower()
        if result == "succeeded":
            return EventType.CI_PASSED
        if result in ("failed", "partiallysucceeded"):
            return EventType.CI_FAILED
        # canceled, other → ignore
        return EventType.IGNORED

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

        reviewers: list[dict[str, Any]] = resource.get("reviewers", [])
        vote_event = _classify_reviewer_votes(reviewers)
        if vote_event is not None:
            return vote_event

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
