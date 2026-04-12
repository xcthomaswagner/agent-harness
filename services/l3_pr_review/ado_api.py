"""Azure DevOps REST API wrappers for PR state + completion."""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


def _ado_auth_headers(pat: str) -> dict[str, str]:
    """Build Basic auth headers for ADO using a Personal Access Token.

    ADO convention: empty username + PAT as password.
    """
    credentials = f":{pat}"
    token = base64.b64encode(credentials.encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


# ADO reviewer name/descriptor markers that identify non-human
# reviewers (groups, service principals, build identities). When any
# of these appear in uniqueName, descriptor, or displayName the
# reviewer is NOT counted toward ``human_approvals_count`` — the
# gate that auto-merge relies on for human-in-the-loop safety.
# ADO has no type=Bot flag like GitHub, so we match on the
# conventional service-account prefixes/substrings instead.
_ADO_SERVICE_MARKERS: tuple[str, ...] = (
    "svc.",
    "build\\",
    "agent pool",
    "project collection build",
    "[bot]",
)


def _is_ado_human_reviewer(reviewer: dict[str, Any]) -> bool:
    """Return True if the ADO reviewer looks like a human user.

    Filters out groups (``isContainer``) and service-principal /
    build identities whose uniqueName/descriptor/displayName match
    the conventional ADO service-account markers. Also filters the
    harness's own bot account if BOT_GITHUB_USERNAME is set (the
    same env var used on the GitHub side — assumes the ADO
    displayName for the harness identity follows the same
    convention or is added explicitly via L3_APPROVAL_BOT_DENYLIST).

    Prior to this defense, ADO branch policies' auto-added service
    reviewers could cast vote=10 and satisfy the ``has_approval``
    gate on their own, default-OPENing auto-merge on AI PRs.
    """
    if reviewer.get("isContainer"):
        return False
    unique = (reviewer.get("uniqueName") or "").lower()
    descriptor = (reviewer.get("descriptor") or "").lower()
    display = (reviewer.get("displayName") or "").lower()
    for marker in _ADO_SERVICE_MARKERS:
        if marker in unique or marker in descriptor or marker in display:
            return False
    # Denylist from env: both the harness bot + any org-specific
    # service accounts administrators want to exclude.
    denylist: set[str] = {
        name.strip().lower()
        for name in os.getenv("L3_APPROVAL_BOT_DENYLIST", "").split(",")
        if name.strip()
    }
    harness_bot = (os.getenv("BOT_GITHUB_USERNAME") or "").strip().lower()
    if harness_bot:
        denylist.add(harness_bot)
    return not any(name and name in denylist for name in (unique, display))


async def get_ado_pr_state(
    org_url: str,
    project: str,
    repo_id: str,
    pr_id: int,
    *,
    ado_pat: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Fetch ADO PR details and return a normalized dict, or None on failure.

    Returns keys: author, merged, head_sha, approvals_count,
    changes_requested_count, title, status, labels.
    """
    pat = ado_pat or os.getenv("ADO_PAT", "")
    if not pat:
        logger.warning("ado_api_no_pat")
        return None

    headers = _ado_auth_headers(pat)
    url = (
        f"{org_url.rstrip('/')}/{project}/_apis/git/repositories/{repo_id}"
        f"/pullrequests/{pr_id}?api-version=7.1"
    )

    owns_client = client is None
    c = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await c.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning(
                "ado_pr_fetch_failed", status=resp.status_code, pr_id=pr_id
            )
            return None
        pr = resp.json()

        # Normalize reviewer votes. ``human_approvals_count`` is the
        # gate-relevant field — ``approvals_count`` is kept for
        # backward-compat audit logging but must NOT be used by
        # policy gates, because ADO auto-adds service-principal and
        # build-identity reviewers that would otherwise bypass the
        # has_approval gate.
        reviewers: list[dict[str, Any]] = pr.get("reviewers", [])
        approvals = sum(
            1 for r in reviewers if r.get("vote", 0) in (10, 5)
        )
        human_approvals = sum(
            1
            for r in reviewers
            if r.get("vote", 0) in (10, 5) and _is_ado_human_reviewer(r)
        )
        changes_requested = sum(
            1 for r in reviewers if r.get("vote", 0) in (-10, -5)
        )

        status = pr.get("status", "")
        last_merge_source = pr.get("lastMergeSourceCommit") or {}

        # ADO mergeStatus: "succeeded", "conflicts", "notSet", etc.
        merge_status = pr.get("mergeStatus", "")

        return {
            "author": (pr.get("createdBy") or {}).get("displayName", ""),
            "merged": status == "completed",
            "head_sha": last_merge_source.get("commitId", ""),
            "approvals_count": approvals,
            "human_approvals_count": human_approvals,
            "changes_requested_count": changes_requested,
            "title": pr.get("title", ""),
            "status": status,
            # Compatibility keys with GitHub normalized dict.
            #
            # Only ``succeeded`` is treated as mergeable. Previously this
            # returned True for ``notSet`` as well, but ``notSet`` means
            # ADO has not yet computed the merge status — semantically
            # equivalent to GitHub's ``mergeable: null`` unknown state.
            # evaluate_policy_gates does ``bool(pr_state.get("mergeable"))``,
            # so the old code silently fail-OPENed the mergeable gate
            # (the most dangerous default-open location in the codebase)
            # for any ADO PR whose mergeStatus was still being computed
            # at webhook time. Other non-succeeded states (``conflicts``,
            # ``rejectedByPolicy``, ``failure``, ``queued``) all
            # correctly evaluate to False.
            "mergeable": merge_status == "succeeded",
            "mergeable_state": merge_status,
            "checks_passed": False,  # ADO Pipelines CI not yet integrated
            "labels": [label.get("name", "") for label in pr.get("labels", [])],
        }
    except httpx.RequestError:
        logger.exception("ado_api_request_error", pr_id=pr_id)
        return None
    finally:
        if owns_client:
            await c.aclose()


async def complete_ado_pr(
    org_url: str,
    project: str,
    repo_id: str,
    pr_id: int,
    last_merge_source_commit: str,
    *,
    merge_strategy: str = "noFastForward",
    ado_pat: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[bool, str]:
    """Complete (merge) an ADO pull request via PATCH.

    Sets the PR status to ``completed`` with the specified merge strategy
    and the last merge source commit for optimistic locking.

    Returns (success, message).
    """
    pat = ado_pat or os.getenv("ADO_PAT", "")
    if not pat:
        return False, "no_pat"

    headers = _ado_auth_headers(pat)
    url = (
        f"{org_url.rstrip('/')}/{project}/_apis/git/repositories/{repo_id}"
        f"/pullrequests/{pr_id}?api-version=7.1"
    )
    body = {
        "status": "completed",
        "lastMergeSourceCommit": {"commitId": last_merge_source_commit},
        "completionOptions": {
            "mergeStrategy": merge_strategy,
        },
    }

    owns_client = client is None
    c = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await c.patch(url, headers=headers, json=body)
        if resp.status_code == 200:
            return True, "completed"
        if resp.status_code == 409:
            return False, "conflict"
        return False, f"http_{resp.status_code}"
    except httpx.RequestError as exc:
        return False, f"request_error:{exc.__class__.__name__}"
    finally:
        if owns_client:
            await c.aclose()
