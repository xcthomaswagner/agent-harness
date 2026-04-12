"""GitHub REST API wrappers for PR state + merge."""
from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


def _is_bot_reviewer(user: dict[str, Any] | None) -> bool:
    """Return True if ``user`` looks like an automated GitHub reviewer.

    GitHub Apps install with ``type == "Bot"`` and their integration
    login typically ends in ``[bot]`` (e.g. ``dependabot[bot]``,
    ``github-actions[bot]``, ``copilot[bot]``). We treat anything
    matching either signal — plus the harness's own bot account —
    as a non-human reviewer whose APPROVED state must NOT satisfy
    the ``has_approval`` gate on auto-merge.

    Rationale: the gate is meant to require a human in the loop.
    Prior to this defense, a Dependabot auto-approve (or Copilot, or
    a security scanner with ``pull-requests: write``) landing an
    APPROVED review on an AI-authored branch would pass every gate
    and trigger merge_pr with zero human review — the worst
    default-OPEN hazard in the auto-merge pipeline.
    """
    if not user:
        return False
    if user.get("type") == "Bot":
        return True
    login = (user.get("login") or "").lower()
    if not login:
        return False
    if login.endswith("[bot]"):
        return True
    # Harness's own bot account + optional denylist for third-party
    # service accounts that don't carry the [bot] suffix.
    denylist_env = os.getenv("L3_APPROVAL_BOT_DENYLIST", "")
    denylist = {
        name.strip().lower()
        for name in denylist_env.split(",")
        if name.strip()
    }
    harness_bot = os.getenv("BOT_GITHUB_USERNAME", "").strip().lower()
    if harness_bot:
        denylist.add(harness_bot)
    return login in denylist


async def _paginate(
    c: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    envelope_key: str | None = None,
    max_pages: int = 20,
) -> list[Any] | None:
    """GET a paginated GitHub endpoint, following Link rel=next.

    Returns the concatenated items on success, or None if any page fails
    with a non-200. ``envelope_key`` selects the list from a wrapper
    object (e.g. ``check_suites`` in ``/check-suites`` responses); when
    None the page body itself must be a list.

    ``max_pages`` caps runaway follows in the unlikely event of a
    server loop (GitHub's own cap is ~10 for most endpoints at
    per_page=100).

    GitHub defaults to 30 items per page; without explicit pagination
    a human's CHANGES_REQUESTED review can silently drop off page 1 on
    PRs with many bot reviewers, making the auto-merge gate fail-open.
    """
    items: list[Any] = []
    next_url: str | None = url
    params: dict[str, str] | None = {"per_page": "100"}
    for _ in range(max_pages):
        if next_url is None:
            break
        resp = await c.get(next_url, headers=headers, params=params)
        if resp.status_code != 200:
            return None
        body = resp.json()
        page = body.get(envelope_key, []) if envelope_key else body
        if isinstance(page, list):
            items.extend(page)
        next_link = resp.links.get("next") if hasattr(resp, "links") else None
        next_url = next_link.get("url") if next_link else None
        params = None  # per_page already encoded in next_url
    return items


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

        # Reviews — need approvals + changes_requested counts (latest per user).
        # Must paginate: on PRs with many bot reviewers a human's
        # CHANGES_REQUESTED can otherwise fall off page 1 silently.
        reviews = await _paginate(
            c, f"{base}/pulls/{pr_number}/reviews", headers
        ) or []
        # Collapse to latest review per user (reviews come in chronological order).
        #
        # APPROVED reviews are ONLY counted when they target the
        # current head_sha. Otherwise, a human who approved commit A
        # followed by the agent force-pushing commit B would leave a
        # stale APPROVED review on the reviews endpoint, making
        # approvals_count=1 and the approval gate fail-OPEN on a
        # commit that was never actually reviewed. GitHub's native
        # "dismiss stale reviews" branch protection is per-repo and
        # not guaranteed, so L3 must enforce it defensively.
        #
        # CHANGES_REQUESTED reviews are KEPT regardless of commit —
        # a rejection shouldn't silently clear just because a new
        # commit landed. The reviewer re-APPROVING on the new commit
        # is the only thing that should unblock.
        #
        # We ALSO track human vs bot reviewers separately so the
        # ``has_approval`` gate can require at least one human
        # approval. Without that separation, a Dependabot/Copilot/
        # security-bot APPROVED review would satisfy the gate on its
        # own and auto-merge would execute with zero human review.
        latest_by_user: dict[str, str] = {}
        user_is_bot: dict[str, bool] = {}
        for r in reviews:
            if r.get("state") == "COMMENTED":
                continue  # Comments don't count as approval/change request
            reviewer = r.get("user") or {}
            user = reviewer.get("login", "")
            state = r.get("state", "")
            if not (user and state):
                continue
            if state == "APPROVED" and r.get("commit_id") != head_sha:
                # Stale approval against an older commit — drop.
                continue
            latest_by_user[user] = state
            user_is_bot[user] = _is_bot_reviewer(reviewer)
        approvals = sum(1 for s in latest_by_user.values() if s == "APPROVED")
        human_approvals = sum(
            1
            for user, s in latest_by_user.items()
            if s == "APPROVED" and not user_is_bot.get(user, False)
        )
        changes_req = sum(
            1 for s in latest_by_user.values() if s == "CHANGES_REQUESTED"
        )

        # Check suites for head_sha — paginated for the same reason as
        # reviews: a large repo can ship many CI suites per commit and
        # we must see every one before trusting ``checks_passed``.
        checks_passed = False
        if head_sha:
            suites = await _paginate(
                c,
                f"{base}/commits/{head_sha}/check-suites",
                headers,
                envelope_key="check_suites",
            )
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
            # ``approvals_count`` = all approvals (bots + humans),
            # kept for backward-compat logs and audit payloads.
            # ``human_approvals_count`` = the gate-relevant number.
            "approvals_count": approvals,
            "human_approvals_count": human_approvals,
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
