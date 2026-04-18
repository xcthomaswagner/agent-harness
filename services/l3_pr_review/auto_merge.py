"""Auto-merge orchestrator. Ties policy + GitHub/ADO API + L1 audit together."""
from __future__ import annotations

import asyncio
import contextlib
import os
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from ado_api import complete_ado_pr, get_ado_pr_state
from autonomy_policy import (
    AutoMergeContext,
    evaluate_policy_gates,
    fetch_auto_merge_enabled,
    fetch_profile_by_repo,
    fetch_recommended_mode,
)
from github_api import get_pr_state, merge_pr

logger = structlog.get_logger()

# Cache size for the dedup store. Kept module-level so tests can
# ``monkeypatch.setattr(auto_merge, "_MAX_RECENT", 3)`` to exercise
# eviction in small traces.
_MAX_RECENT = 1000


class MergeDedupStore:
    """Per-dedup-key outcome cache with serialization locks.

    Two concurrent webhooks on the same PR (``review_approved`` +
    ``ci_passed``) used to both read ``prior=None``, both write
    ``in_progress``, and both proceed to call the merger. GitHub's
    sha-locked ``PUT /merge`` deduplicates the second call (409) but
    ADO's ``complete_ado_pr`` has no sha guarantee — the second call
    is nondeterministic. Worse, ``in_progress`` was never downgraded
    on an unhandled exception, so a transient failure mid-merge
    wedged the PR until the process restarted.

    The store fixes this with two pieces:

    1. ``_outcomes`` is an ``OrderedDict`` with FIFO eviction past
       ``_MAX_RECENT`` — same semantics the module had before.
    2. ``_locks`` is a per-dedup ``asyncio.Lock`` dict, lazily created
       under ``_locks_mutex``. Callers use ``async with acquire(key):``
       to serialize all reads + merger calls for the same dedup key.
       ``asyncio.Lock`` is not thread-safe in general, but L3 is a
       single-event-loop FastAPI app so the mutex is belt-and-braces.
    """

    def __init__(self) -> None:
        self._outcomes: OrderedDict[str, str] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_mutex: asyncio.Lock = asyncio.Lock()

    def get_outcome(self, dedup: str) -> str | None:
        return self._outcomes.get(dedup)

    def record_outcome(self, dedup: str, outcome: str) -> None:
        """Record a decision and evict oldest entries past the cap.

        Only one ``popitem(last=False)`` per call is needed because the
        cache grows by one insertion at a time.
        """
        self._outcomes[dedup] = outcome
        self._outcomes.move_to_end(dedup)
        while len(self._outcomes) > _MAX_RECENT:
            self._outcomes.popitem(last=False)

    @contextlib.asynccontextmanager
    async def acquire(self, dedup: str) -> AsyncIterator[None]:
        """Acquire the per-dedup lock, creating it lazily under mutex."""
        async with self._locks_mutex:
            lock = self._locks.get(dedup)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[dedup] = lock
        async with lock:
            yield

    def _clear(self) -> None:
        """Test helper — wipe outcomes and locks."""
        self._outcomes.clear()
        self._locks.clear()


# Module-level singleton. Preserves the old ``_recent_merge_outcomes``
# attribute name so existing tests that reach into the OrderedDict
# still work without rewriting the fixture surface.
_dedup_store = MergeDedupStore()


def _dedup_key(repo: str, pr_number: int, head_sha: str) -> str:
    return f"{repo}#{pr_number}#{head_sha}"


# Keep these helpers for backward compatibility with existing tests
# and any non-lock callers.
_recent_merge_outcomes = _dedup_store._outcomes


def _record_outcome(dedup: str, decision: str) -> None:
    """Legacy helper kept for backward compatibility. Prefer
    ``_dedup_store.record_outcome`` in new code.
    """
    _dedup_store.record_outcome(dedup, decision)


def _clear_dedup() -> None:
    """Test hook — wipes both outcomes and locks."""
    _dedup_store._clear()
    # ``_recent_merge_outcomes`` references the same dict so it's
    # already cleared, but keep the rebind guard in case anyone ever
    # reassigns the attribute under the hood.
    global _recent_merge_outcomes
    _recent_merge_outcomes = _dedup_store._outcomes


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
    """Evaluate auto-merge policy for a GitHub PR. Executes the merge if all gates
    pass AND global kill switch is on. Otherwise records a dry_run decision.

    Idempotent: dedup by (repo, pr_number, head_sha).
    """
    async def _fetch() -> dict[str, Any] | None:
        return await get_pr_state(
            repo_full_name, pr_number, github_token=github_token
        )

    async def _merge(sha: str) -> tuple[bool, str]:
        return await merge_pr(
            repo_full_name, pr_number, sha, github_token=github_token
        )

    return await _evaluate_core(
        pr_fetcher=_fetch,
        merger=_merge,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        ticket_id=ticket_id,
        ticket_type=ticket_type,
        trigger_event=trigger_event,
        l1_url=l1_url,
        internal_token=internal_token,
    )


async def _evaluate_core(
    *,
    pr_fetcher: Callable[[], Awaitable[dict[str, Any] | None]],
    merger: Callable[[str], Awaitable[tuple[bool, str]]],
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    ticket_id: str,
    ticket_type: str,
    trigger_event: str,
    l1_url: str | None = None,
    internal_token: str | None = None,
) -> dict[str, Any]:
    """Shared policy evaluation core for both GitHub and ADO.

    ``pr_fetcher`` returns a normalized PR state dict (or None on failure).
    ``merger`` accepts a head_sha and returns (success, message).

    Concurrency: the entire evaluation (dedup check → in_progress mark →
    PR state fetch → merger call → final outcome record) runs under a
    per-dedup-key ``asyncio.Lock``. This serializes two concurrent
    webhooks (``review_approved`` + ``ci_passed``) on the same PR so
    only one merge call fires. The lock is released before returning,
    and a try/finally ensures a ``failed`` outcome is recorded if the
    merger raises — previously an unhandled exception left the dedup
    marker at ``in_progress``, wedging the PR until the process
    restarted.
    """
    l1_url_resolved: str = l1_url or os.getenv(
        "L1_SERVICE_URL"
    ) or "http://localhost:8000"
    token_resolved: str = (
        internal_token or os.getenv("L1_INTERNAL_API_TOKEN") or ""
    )
    global_enabled = os.getenv("AUTO_MERGE_ENABLED", "false").lower() == "true"
    bot_username = os.getenv("BOT_GITHUB_USERNAME", "xcagentrockwell")

    # The caller-supplied head_sha is what we lock under. If the caller
    # didn't know it (empty string), we'll lock under the empty-sha
    # placeholder for the full evaluation — swapping the lock key
    # mid-evaluation would introduce a check-then-act race. Instead we
    # record the final outcome under BOTH the placeholder AND the real
    # discovered sha at the end so future webhooks (with either key)
    # see the cached result.
    dedup = _dedup_key(repo_full_name, pr_number, head_sha)
    log = logger.bind(
        repo=repo_full_name, pr=pr_number, ticket=ticket_id, trigger=trigger_event
    )

    async with _dedup_store.acquire(dedup):
        prior = _dedup_store.get_outcome(dedup)
        if prior in ("merged", "in_progress"):
            logger.info("auto_merge_dedup_skipped", dedup=dedup, prior=prior)
            return {"status": "deduped"}

        _dedup_store.record_outcome(dedup, "in_progress")
        _outcome_recorded = False

        try:
            # Resolve profile
            profile_info = await fetch_profile_by_repo(
                repo_full_name, l1_url=l1_url_resolved, internal_token=token_resolved
            )
            client_profile = profile_info.get("client_profile") or ""
            if not client_profile:
                log.info("auto_merge_no_profile_skipped")
                _dedup_store.record_outcome(dedup, "skipped")
                _outcome_recorded = True
                return {"status": "skipped", "reason": "no_profile_for_repo"}

            # Fetch mode + toggle + PR state
            mode, dq = await fetch_recommended_mode(client_profile, l1_url=l1_url_resolved)
            profile_enabled = await fetch_auto_merge_enabled(
                client_profile, l1_url=l1_url_resolved
            )
            pr_state = await pr_fetcher()
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
                _dedup_store.record_outcome(dedup, "failed")
                _outcome_recorded = True
                return {"status": "failed", "reason": "pr_state_fetch_failed"}

            # Build-SHA staleness check (Task 3.2): if the caller
            # supplied a non-empty head_sha AND the PR's current head
            # doesn't match, skip — a force-push landed after CI started
            # and the build result is stale. ADO's ``complete_ado_pr``
            # has no sha lock, so completing on the stale sha would
            # race against whatever's actually on the PR head now.
            pr_state_sha_raw = pr_state.get("head_sha") or ""
            if head_sha and pr_state_sha_raw and head_sha != pr_state_sha_raw:
                log.warning(
                    "build_sha_stale",
                    caller_sha=head_sha,
                    pr_sha=pr_state_sha_raw,
                )
                _dedup_store.record_outcome(dedup, "skipped")
                _outcome_recorded = True
                return {
                    "status": "skipped",
                    "reason": "build_sha_stale",
                }

            # When the caller didn't know the head_sha (e.g., build.complete
            # webhook), record the final outcome under BOTH the placeholder
            # key and the real discovered-sha key. We keep ``dedup`` pinned
            # to the original key so the lock stays consistent; outcome
            # propagation happens at the end.
            pr_state_sha = pr_state_sha_raw
            real_sha = pr_state_sha or head_sha

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

            if should_merge:
                if global_enabled:
                    ok, msg = await merger(pr_state["head_sha"])
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
                head_sha=real_sha,
                ticket_id=ticket_id,
                client_profile=client_profile,
                recommended_mode=mode,
                ticket_type=ticket_type,
                decision=decision,
                reason=reason,
                gates=gates,
                dry_run=not global_enabled,
            )

            # Record under the locked (original) key plus the discovered
            # real-sha key. Future webhooks with either dedup value see
            # the cached outcome.
            _dedup_store.record_outcome(dedup, decision)
            if pr_state_sha and pr_state_sha != head_sha:
                _dedup_store.record_outcome(
                    _dedup_key(repo_full_name, pr_number, pr_state_sha), decision
                )
            _outcome_recorded = True

            return {"status": decision, "reason": reason, "dry_run": not global_enabled}
        finally:
            # If we exit without recording a terminal outcome (unhandled
            # exception inside any awaited call), downgrade the
            # ``in_progress`` marker to ``failed`` so the PR isn't
            # wedged until a process restart.
            if not _outcome_recorded:
                _dedup_store.record_outcome(dedup, "failed")


async def evaluate_and_maybe_merge_ado(
    *,
    org_url: str,
    project: str,
    repo_id: str,
    pr_id: int,
    head_sha: str,
    ticket_id: str,
    ticket_type: str,
    trigger_event: str,
    checks_passed: bool | None = None,
    l1_url: str | None = None,
    internal_token: str | None = None,
    ado_pat: str | None = None,
) -> dict[str, Any]:
    """Evaluate auto-merge policy for an ADO PR.

    Parallel to ``evaluate_and_maybe_merge`` but uses ADO APIs for
    PR state fetching and PR completion.
    """
    repo_full_name = f"{project}/{repo_id}"

    async def _fetch() -> dict[str, Any] | None:
        return await get_ado_pr_state(
            org_url, project, repo_id, pr_id,
            checks_passed=checks_passed, ado_pat=ado_pat,
        )

    async def _merge(sha: str) -> tuple[bool, str]:
        return await complete_ado_pr(
            org_url, project, repo_id, pr_id, sha, ado_pat=ado_pat,
        )

    return await _evaluate_core(
        pr_fetcher=_fetch,
        merger=_merge,
        repo_full_name=repo_full_name,
        pr_number=pr_id,
        head_sha=head_sha,
        ticket_id=ticket_id,
        ticket_type=ticket_type,
        trigger_event=trigger_event,
        l1_url=l1_url,
        internal_token=internal_token,
    )
