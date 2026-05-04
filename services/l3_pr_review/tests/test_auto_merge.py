"""Tests for auto_merge orchestrator."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

import auto_merge
import autonomy_policy as ap
from auto_merge import evaluate_and_maybe_merge, evaluate_and_maybe_merge_ado


@pytest.fixture(autouse=True)
def _clear_state(monkeypatch: pytest.MonkeyPatch) -> None:
    auto_merge._clear_dedup()
    ap._cache_clear()
    # Default env
    monkeypatch.setenv("L1_SERVICE_URL", "http://l1")
    monkeypatch.setenv("L1_INTERNAL_API_TOKEN", "secret")
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "false")
    monkeypatch.setenv("BOT_GITHUB_USERNAME", "xcagentrockwell")


def _good_pr_state() -> dict[str, Any]:
    return {
        "author": "xcagentrockwell",
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head_sha": "abc123",
        "approvals_count": 1,
        "human_approvals_count": 1,
        "changes_requested_count": 0,
        "checks_passed": True,
        "labels": ["bug"],
    }


def _patch_fetches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: dict[str, Any] | None = None,
    mode: tuple[str, str] = ("semi_autonomous", "good"),
    toggle: bool = True,
    pr_state: dict[str, Any] | None = None,
    merge_result: tuple[bool, str] = (True, "merged"),
) -> dict[str, AsyncMock]:
    """Patch all remote calls on the auto_merge module. Returns mock registry."""
    prof = profile if profile is not None else {
        "client_profile": "acme",
        "low_risk_ticket_types": ["bug", "chore"],
        "auto_merge_enabled_yaml": True,
    }
    mocks = {
        "fetch_profile_by_repo": AsyncMock(return_value=prof),
        "fetch_recommended_mode": AsyncMock(return_value=mode),
        "fetch_auto_merge_enabled": AsyncMock(return_value=toggle),
        "get_pr_state": AsyncMock(
            return_value=pr_state if pr_state is not None else _good_pr_state()
        ),
        "merge_pr": AsyncMock(return_value=merge_result),
        "_record_decision": AsyncMock(return_value=None),
    }
    for name, m in mocks.items():
        monkeypatch.setattr(auto_merge, name, m)
    return mocks


async def test_dedup_allows_reevaluation_after_non_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approval → dry_run should NOT block the ci_passed re-evaluation."""
    mocks = _patch_fetches(monkeypatch)
    r1 = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="abc123",
        ticket_id="T-1",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    r2 = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="abc123",
        ticket_id="T-1",
        ticket_type="bug",
        trigger_event="ci_passed",
    )
    assert r1["status"] == "dry_run"
    # Second call re-evaluates (not deduped) because first wasn't "merged"
    assert r2["status"] == "dry_run"
    assert mocks["fetch_profile_by_repo"].call_count == 2


async def test_dedup_blocks_after_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a successful merge, same sha should be deduped."""
    _patch_fetches(
        monkeypatch,
        merge_result=(True, "merged"),
    )
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    r1 = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="abc123",
        ticket_id="T-1",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    r2 = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="abc123",
        ticket_id="T-1",
        ticket_type="bug",
        trigger_event="ci_passed",
    )
    assert r1["status"] == "merged"
    assert r2["status"] == "deduped"


async def test_no_profile_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = _patch_fetches(
        monkeypatch,
        profile={
            "client_profile": "",
            "low_risk_ticket_types": [],
            "auto_merge_enabled_yaml": False,
        },
    )
    result = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="abc123",
        ticket_id="T-1",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "no_profile_for_repo"
    assert mocks["_record_decision"].call_count == 1
    _args, kwargs = mocks["_record_decision"].call_args
    assert kwargs["decision"] == "skipped"
    assert kwargs["reason"] == "no_profile_for_repo"
    assert kwargs["client_profile"] == ""
    assert kwargs["gates"] == {"profile_resolved": False}
    # get_pr_state should not be called since we bail early
    assert mocks["get_pr_state"].call_count == 0


async def test_pr_state_fetch_failure_records_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocks = _patch_fetches(monkeypatch, pr_state=None)
    # Override get_pr_state to return None
    mocks["get_pr_state"].return_value = None
    result = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="abc123",
        ticket_id="T-1",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    assert result["status"] == "failed"
    assert result["reason"] == "pr_state_fetch_failed"
    assert mocks["_record_decision"].call_count == 1
    _args, kwargs = mocks["_record_decision"].call_args
    assert kwargs["decision"] == "failed"
    assert kwargs["reason"] == "pr_state_fetch_failed"


async def test_dry_run_records_dry_run_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "false")
    # Default pr_state.head_sha is "abc123"; webhook sha must match to
    # survive the Task 3.2 build_sha_stale skip.
    mocks = _patch_fetches(monkeypatch)
    result = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=2,
        head_sha="abc123",
        ticket_id="T-2",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    assert result["status"] == "dry_run"
    assert result["dry_run"] is True
    assert mocks["merge_pr"].call_count == 0
    _args, kwargs = mocks["_record_decision"].call_args
    assert kwargs["decision"] == "dry_run"
    assert kwargs["dry_run"] is True


async def test_global_enabled_calls_merge_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    mocks = _patch_fetches(monkeypatch)
    result = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=3,
        head_sha="abc123",  # matches _good_pr_state default
        ticket_id="T-3",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    assert result["status"] == "merged"
    assert result["dry_run"] is False
    assert mocks["merge_pr"].call_count == 1
    _args, kwargs = mocks["_record_decision"].call_args
    assert kwargs["decision"] == "merged"


async def test_skipped_records_skipped_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    mocks = _patch_fetches(
        monkeypatch, mode=("conservative", "good")
    )
    result = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=4,
        head_sha="abc123",  # matches _good_pr_state default
        ticket_id="T-4",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    assert result["status"] == "skipped"
    assert mocks["merge_pr"].call_count == 0
    _args, kwargs = mocks["_record_decision"].call_args
    assert kwargs["decision"] == "skipped"
    assert kwargs["reason"] == "mode_conservative"


async def test_global_enabled_merge_failure_records_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    mocks = _patch_fetches(
        monkeypatch, merge_result=(False, "sha_mismatch")
    )
    result = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=5,
        head_sha="abc123",  # matches _good_pr_state default
        ticket_id="T-5",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    assert result["status"] == "failed"
    assert result["reason"] == "sha_mismatch"
    assert mocks["merge_pr"].call_count == 1


# --- Dedup eviction regression ---
#
# Bug: _recent_merge_outcomes used `.clear()` when it hit _MAX_RECENT.
# This wipes every "merged" marker wholesale, re-opening a re-merge
# window on any delayed webhook retry for a PR whose marker was
# evicted. The fix uses OrderedDict FIFO eviction so old markers are
# retired one at a time, preserving recent "merged" entries.


async def test_dedup_fifo_eviction_preserves_recent_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the cache fills, the oldest entries evict — recent 'merged' stays."""
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    monkeypatch.setattr(auto_merge, "_MAX_RECENT", 3)

    # pr_state returns whatever sha the webhook supplies, so there's
    # no second "force-push" key recorded per call.
    async def _fake_get_pr_state(repo: str, pr_number: int, **kw) -> dict:
        state = _good_pr_state()
        state["head_sha"] = _last_sha["v"]
        return state

    _last_sha: dict[str, str] = {"v": ""}
    _patch_fetches(monkeypatch, merge_result=(True, "merged"))
    monkeypatch.setattr(auto_merge, "get_pr_state", AsyncMock(side_effect=_fake_get_pr_state))

    async def _merge(sha: str) -> dict:
        _last_sha["v"] = sha
        return await evaluate_and_maybe_merge(
            repo_full_name="acme/repo",
            pr_number=1,
            head_sha=sha,
            ticket_id="T",
            ticket_type="bug",
            trigger_event="review_approved",
        )

    # Fill the cache to capacity with merges on 3 different shas.
    await _merge("sha1")
    await _merge("sha2")
    await _merge("sha3")
    assert len(auto_merge._recent_merge_outcomes) == 3

    # A 4th merge pushes the cache to cap+1; sha1 (oldest) must evict,
    # but sha2/sha3/sha4 stay. The old .clear() code would wipe all 3.
    await _merge("sha4")
    keys = list(auto_merge._recent_merge_outcomes)
    assert not any(k.endswith("#sha1") for k in keys), (
        "sha1 should have evicted (oldest)"
    )
    for recent in ("sha2", "sha3", "sha4"):
        assert any(k.endswith(f"#{recent}") for k in keys), (
            f"{recent} should still be marked merged"
        )


async def test_dedup_records_both_webhook_and_pr_state_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty head_sha webhook records outcome under BOTH the placeholder
    key AND the discovered pr_state sha key. A retry with either key
    must see the cached outcome.

    Rewritten from the pre-Phase-3 force-push test which let a webhook
    with a stale non-empty sha merge anyway. Task 3.2 added a
    ``build_sha_stale`` skip for non-empty webhooks that don't match
    the PR head, so the force-push tolerance case no longer exists —
    the use case now is ``head_sha=""`` from build.complete webhooks.
    """
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    pr_state = _good_pr_state()
    pr_state["head_sha"] = "real_sha"
    _patch_fetches(monkeypatch, pr_state=pr_state, merge_result=(True, "merged"))

    # First call: empty head_sha (build.complete). pr_state discovers
    # "real_sha". Merge proceeds and both dedup keys are recorded.
    r1 = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="",
        ticket_id="T",
        ticket_type="bug",
        trigger_event="ci_passed",
    )
    assert r1["status"] == "merged"

    # A retry with the same empty sha must be deduped (placeholder key).
    r2 = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="",
        ticket_id="T",
        ticket_type="bug",
        trigger_event="ci_passed",
    )
    assert r2["status"] == "deduped"

    # A NEW webhook delivered with the real pr_state sha must ALSO be
    # deduped — we recorded the outcome under the real-sha key too.
    r3 = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="real_sha",
        ticket_id="T",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    assert r3["status"] == "deduped"


async def test_build_sha_stale_records_skipped_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr_state = _good_pr_state()
    pr_state["head_sha"] = "new_sha"
    mocks = _patch_fetches(monkeypatch, pr_state=pr_state)

    result = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="old_sha",
        ticket_id="T",
        ticket_type="bug",
        trigger_event="ci_passed",
    )

    assert result == {"status": "skipped", "reason": "build_sha_stale"}
    assert mocks["merge_pr"].call_count == 0
    assert mocks["_record_decision"].call_count == 1
    _args, kwargs = mocks["_record_decision"].call_args
    assert kwargs["decision"] == "skipped"
    assert kwargs["reason"] == "build_sha_stale"
    assert kwargs["head_sha"] == "old_sha"
    assert kwargs["gates"] == {
        "build_sha_matches": False,
        "caller_sha": "old_sha",
        "pr_head_sha": "new_sha",
    }


# --- ADO auto-merge ---


def _patch_ado_fetches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: dict[str, Any] | None = None,
    mode: tuple[str, str] = ("semi_autonomous", "good"),
    toggle: bool = True,
    pr_state: dict[str, Any] | None = None,
    merge_result: tuple[bool, str] = (True, "merged"),
) -> dict[str, AsyncMock]:
    """Patch remote calls for ADO auto-merge tests."""
    prof = profile if profile is not None else {
        "client_profile": "acme",
        "low_risk_ticket_types": ["bug", "chore"],
        "auto_merge_enabled_yaml": True,
    }
    mocks = {
        "fetch_profile_by_repo": AsyncMock(return_value=prof),
        "fetch_recommended_mode": AsyncMock(return_value=mode),
        "fetch_auto_merge_enabled": AsyncMock(return_value=toggle),
        "get_ado_pr_state": AsyncMock(
            return_value=pr_state if pr_state is not None else _good_pr_state()
        ),
        "complete_ado_pr": AsyncMock(return_value=merge_result),
        "_record_decision": AsyncMock(return_value=None),
    }
    for name, m in mocks.items():
        monkeypatch.setattr(auto_merge, name, m)
    return mocks


async def test_evaluate_ado_calls_ado_apis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADO path calls get_ado_pr_state and complete_ado_pr, not GitHub equivalents."""
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    mocks = _patch_ado_fetches(monkeypatch)

    result = await evaluate_and_maybe_merge_ado(
        org_url="https://dev.azure.com/org",
        project="proj",
        repo_id="repo-id",
        pr_id=1,
        head_sha="abc123",
        ticket_id="T-1",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    assert result["status"] == "merged"
    assert mocks["get_ado_pr_state"].call_count == 1
    assert mocks["complete_ado_pr"].call_count == 1


async def test_evaluate_ado_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADO dry_run behavior matches GitHub path."""
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "false")
    mocks = _patch_ado_fetches(monkeypatch)

    result = await evaluate_and_maybe_merge_ado(
        org_url="https://dev.azure.com/org",
        project="proj",
        repo_id="repo-id",
        pr_id=2,
        head_sha="abc123",  # matches _good_pr_state default
        ticket_id="T-2",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    assert result["status"] == "dry_run"
    assert result["dry_run"] is True
    assert mocks["complete_ado_pr"].call_count == 0


async def test_evaluate_ado_empty_head_sha_upgrades_dedup_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When head_sha is empty (build.complete), dedup key upgrades to real sha
    after pr_state is fetched. Two successive calls with empty head_sha but
    different pr_state shas must NOT collide."""
    mocks = _patch_ado_fetches(monkeypatch)

    # First call: empty head_sha, pr_state returns "sha_from_pr_1"
    pr_state_1 = _good_pr_state()
    pr_state_1["head_sha"] = "sha_from_pr_1"
    mocks["get_ado_pr_state"].return_value = pr_state_1

    r1 = await evaluate_and_maybe_merge_ado(
        org_url="https://dev.azure.com/org",
        project="proj",
        repo_id="repo-id",
        pr_id=42,
        head_sha="",
        ticket_id="T-42",
        ticket_type="bug",
        trigger_event="ci_passed",
    )
    assert r1["status"] == "dry_run"

    # Second call: empty head_sha again, but pr_state returns a NEW sha
    # (simulating a force-push + rebuild). Must NOT be deduped.
    pr_state_2 = _good_pr_state()
    pr_state_2["head_sha"] = "sha_from_pr_2"
    mocks["get_ado_pr_state"].return_value = pr_state_2

    r2 = await evaluate_and_maybe_merge_ado(
        org_url="https://dev.azure.com/org",
        project="proj",
        repo_id="repo-id",
        pr_id=42,
        head_sha="",
        ticket_id="T-42",
        ticket_type="bug",
        trigger_event="ci_passed",
    )
    assert r2["status"] == "dry_run", (
        f"Expected dry_run (re-evaluation), got {r2['status']}. "
        "Empty head_sha dedup collision?"
    )


# --- Task 3.1: per-dedup asyncio.Lock serialization ---
#
# Two concurrent webhooks on the same PR (review_approved + ci_passed)
# used to race: both read prior=None, both set in_progress, both
# called the merger. GitHub's sha-locked PUT /merge deduplicates but
# ADO's complete_ado_pr has no sha guarantee → nondeterministic.
# Worse: unhandled exceptions mid-merge left in_progress set forever,
# wedging the PR until process restart.
#
# Fix: MergeDedupStore wraps the entire evaluation in a per-dedup
# asyncio.Lock, and a try/finally downgrades in_progress → failed on
# exceptions.


async def test_concurrent_evaluations_on_same_dedup_serialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent evaluations on the same dedup must serialize:
    only one merger call fires; the second sees the cached outcome."""
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    _patch_fetches(monkeypatch, merge_result=(True, "merged"))

    # Replace merge_pr with a merger that waits on an event to
    # prove the lock serializes concurrent entries. If the second
    # task sneaks past the dedup check, it will ALSO start waiting,
    # and our assertion on merger_calls==1 will catch it.
    merger_calls: list[str] = []
    ready_to_finish = asyncio.Event()

    async def _slow_merger(repo: str, pr_number: int, sha: str, **kw: Any) -> tuple[bool, str]:
        merger_calls.append(sha)
        # Wait for the test to release us, simulating a real merge
        # API call taking time. Windows of time where both tasks
        # could be in the merger simultaneously are what we're
        # guarding against.
        await ready_to_finish.wait()
        return (True, "merged")

    monkeypatch.setattr(auto_merge, "merge_pr", _slow_merger)

    async def _run() -> dict[str, Any]:
        return await evaluate_and_maybe_merge(
            repo_full_name="acme/repo",
            pr_number=1,
            head_sha="abc123",
            ticket_id="T-1",
            ticket_type="bug",
            trigger_event="review_approved",
        )

    task1 = asyncio.create_task(_run())
    task2 = asyncio.create_task(_run())

    # Give task1 a beat to acquire the lock and start the merger;
    # task2 will queue on the same lock. When we release, task1
    # finishes, records "merged", releases the lock, and task2 picks
    # it up — but sees prior="merged" and returns "deduped" instead
    # of calling the merger a second time.
    await asyncio.sleep(0.01)
    ready_to_finish.set()

    r1, r2 = await asyncio.gather(task1, task2)

    # One merger call exactly
    assert len(merger_calls) == 1, (
        f"expected serialization → 1 merger call, got {len(merger_calls)}. "
        "Lock not held across check-then-merge-then-record?"
    )
    # First finished "merged", second saw cache and "deduped"
    outcomes = {r1["status"], r2["status"]}
    assert outcomes == {"merged", "deduped"}, (
        f"expected {{merged, deduped}}, got {outcomes}"
    )


async def test_exception_during_merger_records_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the merger raises, the dedup outcome must downgrade to
    'failed' so the PR isn't wedged at 'in_progress' forever."""
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    _patch_fetches(monkeypatch)

    async def _raising_merger(repo: str, pr_number: int, sha: str, **kw: Any) -> tuple[bool, str]:
        raise RuntimeError("merge API hit a bug")

    monkeypatch.setattr(auto_merge, "merge_pr", _raising_merger)

    with pytest.raises(RuntimeError):
        await evaluate_and_maybe_merge(
            repo_full_name="acme/repo",
            pr_number=1,
            head_sha="abc123",
            ticket_id="T-1",
            ticket_type="bug",
            trigger_event="review_approved",
        )

    dedup = auto_merge._dedup_key("acme/repo", 1, "abc123")
    assert auto_merge._dedup_store.get_outcome(dedup) == "failed", (
        "in_progress leaked past exception — PR would wedge"
    )

    # A subsequent evaluation must be able to re-enter the lock
    # (it sees "failed" which is NOT in {merged, in_progress}, so it
    # proceeds). Swap the merger for a succeeding one and retry.
    _patch_fetches(monkeypatch, merge_result=(True, "merged"))
    r = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="abc123",
        ticket_id="T-1",
        ticket_type="bug",
        trigger_event="ci_passed",
    )
    assert r["status"] == "merged"


async def test_dedup_key_upgrade_records_both_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start with empty head_sha (build.complete), verify after success
    both placeholder-key and real-sha-key return 'merged'."""
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    pr_state = _good_pr_state()
    pr_state["head_sha"] = "sha123"
    _patch_fetches(monkeypatch, pr_state=pr_state, merge_result=(True, "merged"))

    r = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="",
        ticket_id="T",
        ticket_type="bug",
        trigger_event="ci_passed",
    )
    assert r["status"] == "merged"

    # Both dedup keys must return "merged"
    placeholder_key = auto_merge._dedup_key("acme/repo", 1, "")
    real_key = auto_merge._dedup_key("acme/repo", 1, "sha123")
    assert auto_merge._dedup_store.get_outcome(placeholder_key) == "merged"
    assert auto_merge._dedup_store.get_outcome(real_key) == "merged"
