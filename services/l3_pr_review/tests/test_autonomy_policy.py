"""Tests for autonomy_policy — pure gates + L1 client with caching."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import autonomy_policy as ap
from autonomy_policy import (
    REASON_ALREADY_MERGED,
    REASON_CHANGES_REQUESTED,
    REASON_CI_NOT_PASSED,
    REASON_DATA_QUALITY_DEGRADED,
    REASON_HUMAN_AUTHORED,
    REASON_KILL_SWITCH_OFF,
    REASON_MODE_CONSERVATIVE,
    REASON_NO_APPROVAL,
    REASON_NOT_LOW_RISK_IN_SEMI,
    REASON_NOT_MERGEABLE,
    REASON_OK,
    AutoMergeContext,
    evaluate_policy_gates,
    fetch_auto_merge_enabled,
    fetch_profile_by_repo,
    fetch_recommended_mode,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    ap._cache_clear()


def _ctx(**overrides: Any) -> AutoMergeContext:
    defaults: dict[str, Any] = dict(
        recommended_mode="semi_autonomous",
        data_quality_status="good",
        ticket_type="bug",
        low_risk_types=["bug", "chore", "config", "dependency", "docs"],
        profile_enabled=True,
        global_enabled=True,
        bot_github_username="xcagentrockwell",
        dry_run=False,
    )
    defaults.update(overrides)
    return AutoMergeContext(**defaults)


def _pr(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = dict(
        author="xcagentrockwell",
        merged=False,
        mergeable=True,
        mergeable_state="clean",
        approvals_count=1,
        human_approvals_count=1,
        changes_requested_count=0,
        checks_passed=True,
        head_sha="abc123",
    )
    defaults.update(overrides)
    return defaults


# --- Gate tests ---


def test_global_disabled_does_not_block_policy() -> None:
    # global_enabled is recorded in gates for audit but the orchestrator,
    # not the policy, decides whether to execute merge vs. dry-run.
    ok, reason, gates = evaluate_policy_gates(_ctx(global_enabled=False), _pr())
    assert ok is True
    assert reason == REASON_OK
    assert gates["global_enabled"] is False


def test_profile_toggle_off_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(_ctx(profile_enabled=False), _pr())
    assert ok is False
    assert reason == REASON_KILL_SWITCH_OFF


def test_data_quality_degraded_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(
        _ctx(data_quality_status="degraded"), _pr()
    )
    assert ok is False
    assert reason == REASON_DATA_QUALITY_DEGRADED


def test_conservative_mode_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(
        _ctx(recommended_mode="conservative"), _pr()
    )
    assert ok is False
    assert reason == REASON_MODE_CONSERVATIVE


def test_semi_autonomous_non_low_risk_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(
        _ctx(recommended_mode="semi_autonomous", ticket_type="story"), _pr()
    )
    assert ok is False
    assert reason == REASON_NOT_LOW_RISK_IN_SEMI


def test_semi_autonomous_low_risk_passes() -> None:
    ok, reason, _ = evaluate_policy_gates(
        _ctx(recommended_mode="semi_autonomous", ticket_type="bug"), _pr()
    )
    assert ok is True
    assert reason == REASON_OK


def test_full_autonomous_any_type_passes() -> None:
    ok, reason, gates = evaluate_policy_gates(
        _ctx(recommended_mode="full_autonomous", ticket_type="story"), _pr()
    )
    assert ok is True
    assert reason == REASON_OK
    assert gates["low_risk_ticket_type"] is True


def test_human_pr_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(_ctx(), _pr(author="alice"))
    assert ok is False
    assert reason == REASON_HUMAN_AUTHORED


def test_already_merged_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(_ctx(), _pr(merged=True))
    assert ok is False
    assert reason == REASON_ALREADY_MERGED


def test_no_approval_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(
        _ctx(), _pr(approvals_count=0, human_approvals_count=0)
    )
    assert ok is False
    assert reason == REASON_NO_APPROVAL


def test_bot_only_approval_blocks() -> None:
    """Regression: a PR with 1 bot approval and 0 human approvals must
    fail the has_approval gate. Previously the gate used approvals_count
    which includes bots, so a Dependabot approval would pass the gate."""
    ok, reason, _ = evaluate_policy_gates(
        _ctx(),
        _pr(approvals_count=1, human_approvals_count=0),
    )
    assert ok is False
    assert reason == REASON_NO_APPROVAL, (
        "bot-only approvals must NOT satisfy has_approval — "
        "regression for default-OPEN bot-auto-merge bug"
    )


def test_human_approval_passes_even_with_bot_approval() -> None:
    """1 human + 1 bot approval still satisfies the gate."""
    ok, reason, _ = evaluate_policy_gates(
        _ctx(),
        _pr(approvals_count=2, human_approvals_count=1),
    )
    assert ok is True
    assert reason == REASON_OK


def test_missing_human_approvals_fails_closed() -> None:
    """Regression: iter 19 had a fallback to approvals_count when
    human_approvals_count was absent, which let ADO PRs (which
    didn't yet emit the human field) bypass the bot-approval
    defense — an ADO service-principal vote=10 became
    approvals_count=1 and the gate passed. Iter 20 removes the
    fallback: any pr_state missing human_approvals_count now fails
    closed. Both github_api and ado_api now emit the field."""
    pr_state = _pr(approvals_count=1)
    pr_state.pop("human_approvals_count", None)
    ok, reason, _ = evaluate_policy_gates(_ctx(), pr_state)
    assert ok is False
    assert reason == REASON_NO_APPROVAL, (
        "Missing human_approvals_count must fail-CLOSED — the "
        "old fallback to approvals_count was a default-OPEN bypass "
        "on ADO service-principal auto-approvals."
    )


def test_changes_requested_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(
        _ctx(), _pr(changes_requested_count=1)
    )
    assert ok is False
    assert reason == REASON_CHANGES_REQUESTED


def test_ci_not_passed_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(_ctx(), _pr(checks_passed=False))
    assert ok is False
    assert reason == REASON_CI_NOT_PASSED


def test_not_mergeable_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(_ctx(), _pr(mergeable=False))
    assert ok is False
    assert reason == REASON_NOT_MERGEABLE


def test_dirty_mergeable_state_blocks() -> None:
    ok, reason, _ = evaluate_policy_gates(_ctx(), _pr(mergeable_state="dirty"))
    assert ok is False
    assert reason == REASON_NOT_MERGEABLE


def test_happy_path_semi_autonomous_returns_ok() -> None:
    ok, reason, gates = evaluate_policy_gates(
        _ctx(recommended_mode="semi_autonomous", ticket_type="bug"), _pr()
    )
    assert ok is True
    assert reason == REASON_OK
    assert all(gates.values())


def test_happy_path_full_autonomous_returns_ok() -> None:
    ok, reason, gates = evaluate_policy_gates(
        _ctx(recommended_mode="full_autonomous", ticket_type="story"), _pr()
    )
    assert ok is True
    assert reason == REASON_OK
    assert all(gates.values())


# --- L1 client tests ---


def _mock_httpx_client(resp_status: int = 200, resp_json: Any = None) -> MagicMock:
    """Return a MagicMock simulating httpx.AsyncClient as async context or direct instance."""
    mock_resp = MagicMock()
    mock_resp.status_code = resp_status
    mock_resp.json = MagicMock(return_value=resp_json or {})
    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.post = AsyncMock(return_value=mock_resp)
    client.aclose = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_fetch_recommended_mode_caches() -> None:
    client = _mock_httpx_client(
        200,
        {"recommended_mode": "full_autonomous", "data_quality": {"status": "good"}},
    )
    result1 = await fetch_recommended_mode(
        "profile1", l1_url="http://l1", client=client
    )
    assert result1 == ("full_autonomous", "good")
    assert client.get.call_count == 1
    # Second call should hit cache
    result2 = await fetch_recommended_mode(
        "profile1", l1_url="http://l1", client=client
    )
    assert result2 == ("full_autonomous", "good")
    assert client.get.call_count == 1  # no new call


@pytest.mark.asyncio
async def test_fetch_recommended_mode_fail_closed() -> None:
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.RequestError("boom"))
    result = await fetch_recommended_mode(
        "profile1", l1_url="http://l1", client=client
    )
    assert result == ("conservative", "unknown")


@pytest.mark.asyncio
async def test_fetch_recommended_mode_non_200_fail_closed() -> None:
    client = _mock_httpx_client(500, {})
    result = await fetch_recommended_mode(
        "profile1", l1_url="http://l1", client=client
    )
    assert result == ("conservative", "unknown")


@pytest.mark.asyncio
async def test_fetch_auto_merge_enabled_fail_closed() -> None:
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.RequestError("boom"))
    result = await fetch_auto_merge_enabled(
        "profile1", l1_url="http://l1", client=client
    )
    assert result is False


@pytest.mark.asyncio
async def test_fetch_auto_merge_enabled_happy() -> None:
    client = _mock_httpx_client(200, {"enabled": True})
    result = await fetch_auto_merge_enabled(
        "profile1", l1_url="http://l1", client=client
    )
    assert result is True


@pytest.mark.asyncio
async def test_fetch_profile_by_repo_auth_header_sent() -> None:
    client = _mock_httpx_client(
        200,
        {
            "client_profile": "acme",
            "auto_merge_enabled_yaml": True,
            "low_risk_ticket_types": ["bug"],
        },
    )
    result = await fetch_profile_by_repo(
        "acme/repo",
        l1_url="http://l1",
        internal_token="secret-token",
        client=client,
    )
    assert result["client_profile"] == "acme"
    # Verify token in headers
    _args, kwargs = client.get.call_args
    assert kwargs["headers"]["X-Internal-Api-Token"] == "secret-token"


@pytest.mark.asyncio
async def test_fetch_profile_by_repo_no_token_returns_empty() -> None:
    client = _mock_httpx_client(200, {"client_profile": "x"})
    result = await fetch_profile_by_repo(
        "acme/repo", l1_url="http://l1", internal_token="", client=client
    )
    assert result["client_profile"] == ""


@pytest.mark.asyncio
async def test_cache_clear_resets() -> None:
    client = _mock_httpx_client(
        200,
        {"recommended_mode": "full_autonomous", "data_quality": {"status": "good"}},
    )
    await fetch_recommended_mode("profile1", l1_url="http://l1", client=client)
    ap._cache_clear()
    await fetch_recommended_mode("profile1", l1_url="http://l1", client=client)
    assert client.get.call_count == 2
