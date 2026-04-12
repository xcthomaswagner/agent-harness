"""Tests for auto_merge orchestrator."""
from __future__ import annotations

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
    mocks = _patch_fetches(monkeypatch)
    result = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=2,
        head_sha="sha2",
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
        head_sha="sha3",
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
        head_sha="sha4",
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
        head_sha="sha5",
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
    """Force-push between webhook and get_pr_state: both shas must dedup."""
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    # pr_state returns a different (newer) head_sha than the webhook's.
    pr_state = _good_pr_state()
    pr_state["head_sha"] = "newer_sha"
    _patch_fetches(monkeypatch, pr_state=pr_state, merge_result=(True, "merged"))

    r1 = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="webhook_sha",  # stale sha from the delayed webhook
        ticket_id="T",
        ticket_type="bug",
        trigger_event="review_approved",
    )
    assert r1["status"] == "merged"

    # A retry of the same webhook (same stale sha) must be deduped.
    r2 = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="webhook_sha",
        ticket_id="T",
        ticket_type="bug",
        trigger_event="ci_passed",
    )
    assert r2["status"] == "deduped"

    # A NEW webhook delivered with the actual pr_state sha must ALSO
    # be deduped — we recorded against both keys.
    r3 = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="newer_sha",
        ticket_id="T",
        ticket_type="bug",
        trigger_event="ci_passed",
    )
    assert r3["status"] == "deduped"


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
        head_sha="sha2",
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
