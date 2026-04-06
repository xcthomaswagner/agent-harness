"""GitHub REST API wrappers for PR state + merge."""
from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


async def get_pr_state(
    repo_full_name: str,
    pr_number: int,
    *,
    github_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Fetch PR + reviews + check-suites status, return normalized dict or None on failure.

    Returns keys: author, merged, mergeable, mergeable_state, head_sha,
    approvals_count, changes_requested_count, checks_passed, labels.
    """
    token = github_token or os.getenv("GITHUB_TOKEN") or os.getenv("AGENT_GH_TOKEN", "")
    if not token:
        logger.warning("github_api_no_token")
        return None
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    base = f"https://api.github.com/repos/{repo_full_name}"
    owns_client = client is None
    c = client or httpx.AsyncClient(timeout=10.0)
    try:
        # PR
        pr_resp = await c.get(f"{base}/pulls/{pr_number}", headers=headers)
        if pr_resp.status_code != 200:
            logger.warning(
                "github_pr_fetch_failed", status=pr_resp.status_code, pr=pr_number
            )
            return None
        pr = pr_resp.json()
        head_sha = (pr.get("head") or {}).get("sha", "")

        # Reviews — need approvals + changes_requested counts (latest per user)
        reviews_resp = await c.get(
            f"{base}/pulls/{pr_number}/reviews", headers=headers
        )
        reviews = reviews_resp.json() if reviews_resp.status_code == 200 else []
        # Collapse to latest review per user (reviews come in chronological order)
        latest_by_user: dict[str, str] = {}
        for r in reviews:
            if r.get("state") == "COMMENTED":
                continue  # Comments don't count as approval/change request
            user = (r.get("user") or {}).get("login", "")
            state = r.get("state", "")
            if user and state:
                latest_by_user[user] = state
        approvals = sum(1 for s in latest_by_user.values() if s == "APPROVED")
        changes_req = sum(
            1 for s in latest_by_user.values() if s == "CHANGES_REQUESTED"
        )

        # Check suites for head_sha
        checks_passed = False
        if head_sha:
            cs_resp = await c.get(
                f"{base}/commits/{head_sha}/check-suites", headers=headers
            )
            if cs_resp.status_code == 200:
                suites = cs_resp.json().get("check_suites", [])
                if suites:
                    # ALL suites must be completed — if any are still
                    # queued or in_progress, checks_passed stays False
                    # to prevent premature merge decisions.
                    all_done = all(
                        s.get("status") == "completed" for s in suites
                    )
                    if all_done:
                        checks_passed = all(
                            s.get("conclusion")
                            in ("success", "skipped", "neutral")
                            for s in suites
                        )

        return {
            "author": (pr.get("user") or {}).get("login", ""),
            "merged": bool(pr.get("merged")),
            "mergeable": pr.get("mergeable"),
            "mergeable_state": pr.get("mergeable_state", ""),
            "head_sha": head_sha,
            "approvals_count": approvals,
            "changes_requested_count": changes_req,
            "checks_passed": checks_passed,
            "labels": [label.get("name", "") for label in (pr.get("labels") or [])],
            "title": pr.get("title", ""),
        }
    except httpx.RequestError:
        logger.exception("github_api_request_error", pr=pr_number)
        return None
    finally:
        if owns_client:
            await c.aclose()


async def merge_pr(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    *,
    method: str = "squash",
    github_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[bool, str]:
    """PUT /repos/{owner}/{repo}/pulls/{n}/merge with sha for optimistic locking.

    Returns (success, message).
    """
    token = github_token or os.getenv("GITHUB_TOKEN") or os.getenv("AGENT_GH_TOKEN", "")
    if not token:
        return False, "no_token"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/merge"
    body = {"sha": head_sha, "merge_method": method}
    owns_client = client is None
    c = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await c.put(url, headers=headers, json=body)
        if resp.status_code == 200:
            return True, "merged"
        if resp.status_code == 405:
            return False, "not_mergeable"
        if resp.status_code == 409:
            return False, "sha_mismatch"
        return False, f"http_{resp.status_code}"
    except httpx.RequestError as exc:
        return False, f"request_error:{exc.__class__.__name__}"
    finally:
        if owns_client:
            await c.aclose()
