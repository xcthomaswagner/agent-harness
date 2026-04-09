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

        # Normalize reviewer votes
        reviewers: list[dict[str, Any]] = pr.get("reviewers", [])
        approvals = sum(
            1 for r in reviewers if r.get("vote", 0) in (10, 5)
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
            "changes_requested_count": changes_requested,
            "title": pr.get("title", ""),
            "status": status,
            # Compatibility keys with GitHub normalized dict
            "mergeable": merge_status == "succeeded" or merge_status == "notSet",
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
