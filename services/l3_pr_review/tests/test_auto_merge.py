"""Tests for auto_merge orchestrator."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

import auto_merge
import autonomy_policy as ap
from auto_merge import evaluate_and_maybe_merge


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
