"""Follow-up-commit attribution for human review comments.

A human comment counts as a "code-change-triggering" issue only if at least
one code-changing commit lands in the window between that comment and the
NEXT human comment (or, for the last comment, the approval time). This rule
implements the concurrent-comment mitigation described in the autonomy
metrics spec section 7.1 / 12: if several reviewers comment in rapid
succession and a single commit follows, only the latest comment before the
commit is attributed.
"""

from __future__ import annotations

from typing import Any


def attribute_human_issues_to_commits(
    human_issues: list[dict[str, Any]],
    commits: list[dict[str, Any]],
    approval_at: str,
) -> list[int]:
    """Return ids of human issues that are code-change-triggering.

    Args:
        human_issues: list of {"id": int, "created_at": str} dicts, any order.
        commits: list of {"sha": str, "committed_at": str} dicts, any order.
        approval_at: ISO-8601 timestamp of the approval event.

    Rule (per-comment view): for each human comment H sorted ascending by
    created_at, define a window [H.created_at, next_event_time) where
    next_event_time is the next comment's created_at if one exists, else
    approval_at. If any commit's committed_at falls in that half-open
    window, H is code-change-triggering.

    This is equivalent to: attribute each commit to the LATEST human
    comment with created_at <= commit.committed_at (and before approval).
    """
    if not human_issues:
        return []

    sorted_humans = sorted(
        human_issues, key=lambda h: (str(h["created_at"]), int(h["id"]))
    )
    # Drop comments at/after approval — they post-date the decision window.
    if approval_at:
        sorted_humans = [
            h for h in sorted_humans if str(h["created_at"]) < approval_at
        ]
    if not sorted_humans:
        return []

    triggering: list[int] = []
    for idx, human in enumerate(sorted_humans):
        start = str(human["created_at"])
        if idx + 1 < len(sorted_humans):
            end = str(sorted_humans[idx + 1]["created_at"])
        else:
            end = approval_at
        # Cap window at approval_at — commits after approval are never
        # attributed.
        if approval_at and end > approval_at:
            end = approval_at
        if start >= end:
            # Empty or inverted window — no attribution possible.
            continue
        for commit in commits:
            committed_at = str(commit["committed_at"])
            if start <= committed_at < end:
                triggering.append(int(human["id"]))
                break
    return triggering
