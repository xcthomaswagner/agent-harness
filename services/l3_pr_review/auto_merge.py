"""Auto-merge orchestrator. Ties policy + GitHub API + L1 audit together."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from autonomy_policy import (
    AutoMergeContext,
    evaluate_policy_gates,
    fetch_auto_merge_enabled,
    fetch_profile_by_repo,
    fetch_recommended_mode,
)
from github_api import get_pr_state, merge_pr

logger = structlog.get_logger()

# Track recent evaluations to dedup (CI-passed + review-approved firing together)
_recent_evaluations: set[str] = set()
_MAX_RECENT = 200


def _dedup_key(repo: str, pr_number: int, head_sha: str) -> str:
    return f"{repo}#{pr_number}#{head_sha}"


def _clear_dedup() -> None:
    """Test hook."""
    _recent_evaluations.clear()


async def _record_decision(
    l1_url: str,
    internal_token: str,
    *,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    ticket_id: str,
    client_profile: str,
    recommended_mode: str,
    ticket_type: str,
    decision: str,
    reason: str,
    gates: dict[str, bool],
    dry_run: bool,
) -> None:
    """POST decision to L1 audit endpoint. Fire-and-forget-ish; log on failure."""
    if not internal_token:
        return
    url = f"{l1_url.rstrip('/')}/api/internal/autonomy/auto-merge-decisions"
    body = {
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "ticket_id": ticket_id,
        "client_profile": client_profile,
        "recommended_mode": recommended_mode,
        "ticket_type": ticket_type,
        "decision": decision,
        "reason": reason,
        "gates": gates,
        "dry_run": dry_run,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            resp = await c.post(
                url, json=body, headers={"X-Internal-Api-Token": internal_token}
            )
            if resp.status_code != 200:
                logger.warning("auto_merge_audit_failed", status=resp.status_code)
    except httpx.RequestError:
        logger.exception("auto_merge_audit_error")


async def evaluate_and_maybe_merge(
    *,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    ticket_id: str,
    ticket_type: str,
    trigger_event: str,
    l1_url: str | None = None,
    internal_token: str | None = None,
    github_token: str | None = None,
) -> dict[str, Any]:
    """Evaluate auto-merge policy for a PR. Executes the merge if all gates
    pass AND global kill switch is on. Otherwise records a dry_run decision.

    Idempotent: dedup by (repo, pr_number, head_sha).
    """
    l1_url_resolved: str = l1_url or os.getenv(
        "L1_SERVICE_URL"
    ) or "http://localhost:8000"
    token_resolved: str = (
        internal_token or os.getenv("L1_INTERNAL_API_TOKEN") or ""
    )
    global_enabled = os.getenv("AUTO_MERGE_ENABLED", "false").lower() == "true"
    bot_username = os.getenv("BOT_GITHUB_USERNAME", "xcagentrockwell")

    dedup = _dedup_key(repo_full_name, pr_number, head_sha)
    if dedup in _recent_evaluations:
        logger.info("auto_merge_dedup_skipped", dedup=dedup)
        return {"status": "deduped"}
    # Simple LRU-ish trim
    if len(_recent_evaluations) >= _MAX_RECENT:
        _recent_evaluations.clear()
    _recent_evaluations.add(dedup)

    log = logger.bind(
        repo=repo_full_name, pr=pr_number, ticket=ticket_id, trigger=trigger_event
    )

    # Resolve profile
    profile_info = await fetch_profile_by_repo(
        repo_full_name, l1_url=l1_url_resolved, internal_token=token_resolved
    )
    client_profile = profile_info.get("client_profile") or ""
    if not client_profile:
        log.info("auto_merge_no_profile_skipped")
        return {"status": "skipped", "reason": "no_profile_for_repo"}

    # Fetch mode + toggle + PR state
    mode, dq = await fetch_recommended_mode(client_profile, l1_url=l1_url_resolved)
    profile_enabled = await fetch_auto_merge_enabled(client_profile, l1_url=l1_url_resolved)
    pr_state = await get_pr_state(
        repo_full_name, pr_number, github_token=github_token
    )
    if pr_state is None:
        log.warning("auto_merge_pr_state_fetch_failed")
        await _record_decision(
            l1_url_resolved,
            token_resolved,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            ticket_id=ticket_id,
            client_profile=client_profile,
            recommended_mode=mode,
            ticket_type=ticket_type,
            decision="failed",
            reason="pr_state_fetch_failed",
            gates={},
            dry_run=not global_enabled,
        )
        return {"status": "failed", "reason": "pr_state_fetch_failed"}

    ctx = AutoMergeContext(
        recommended_mode=mode,
        data_quality_status=dq,
        ticket_type=ticket_type,
        low_risk_types=profile_info.get("low_risk_ticket_types") or [],
        profile_enabled=profile_enabled,
        global_enabled=global_enabled,
        bot_github_username=bot_username,
        dry_run=not global_enabled,
    )

    should_merge, reason, gates = evaluate_policy_gates(ctx, pr_state)

    # Execute (or dry-run)
    if should_merge:
        if global_enabled:
            ok, msg = await merge_pr(
                repo_full_name,
                pr_number,
                pr_state["head_sha"],
                github_token=github_token,
            )
            decision = "merged" if ok else "failed"
            reason = msg if not ok else reason
        else:
            decision = "dry_run"
    else:
        decision = "skipped"

    log.info(
        "auto_merge_evaluated",
        decision=decision,
        reason=reason,
        mode=mode,
        dry_run=not global_enabled,
    )

    await _record_decision(
        l1_url_resolved,
        token_resolved,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        ticket_id=ticket_id,
        client_profile=client_profile,
        recommended_mode=mode,
        ticket_type=ticket_type,
        decision=decision,
        reason=reason,
        gates=gates,
        dry_run=not global_enabled,
    )

    return {"status": decision, "reason": reason, "dry_run": not global_enabled}
