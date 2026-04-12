"""Tests for ado_api: PR state fetching + completion."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx

from ado_api import get_ado_pr_state


def _mk_response(status: int, json_body: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_body)
    return resp


def _mk_client(response: MagicMock) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    return client


# --- get_ado_pr_state: mergeable default-open guard ---
#
# Bug regression: ``get_ado_pr_state`` used to return
# ``mergeable=True`` when ``mergeStatus`` was either ``"succeeded"`` OR
# ``"notSet"``. ``notSet`` means ADO hasn't computed the merge status
# yet — semantically equivalent to GitHub's ``mergeable: null``
# unknown state. ``evaluate_policy_gates`` does
# ``bool(pr_state.get("mergeable"))`` so the old code silently
# fail-OPENed the mergeable gate on any in-flight ADO PR whose
# mergeStatus was still being computed. Only ``succeeded`` should
# pass.


async def test_ado_pr_state_mergeable_only_on_succeeded() -> None:
    client = _mk_client(_mk_response(200, {
        "createdBy": {"displayName": "bot"},
        "status": "active",
        "mergeStatus": "succeeded",
        "lastMergeSourceCommit": {"commitId": "abc123"},
        "reviewers": [],
        "title": "T",
        "labels": [],
    }))
    state = await get_ado_pr_state(
        "https://dev.azure.com/org",
        "proj",
        "repo",
        1,
        ado_pat="test-pat",
        client=client,
    )
    assert state is not None
    assert state["mergeable"] is True


async def test_ado_pr_state_mergeable_false_on_notset() -> None:
    """Regression: notSet used to return True (default-open)."""
    client = _mk_client(_mk_response(200, {
        "createdBy": {"displayName": "bot"},
        "status": "active",
        "mergeStatus": "notSet",
        "lastMergeSourceCommit": {"commitId": "abc123"},
        "reviewers": [],
        "title": "T",
        "labels": [],
    }))
    state = await get_ado_pr_state(
        "https://dev.azure.com/org",
        "proj",
        "repo",
        1,
        ado_pat="test-pat",
        client=client,
    )
    assert state is not None
    assert state["mergeable"] is False, (
        "notSet means ADO hasn't computed merge status yet — must not "
        "be treated as mergeable"
    )


async def test_ado_pr_state_mergeable_false_on_conflicts() -> None:
    client = _mk_client(_mk_response(200, {
        "createdBy": {"displayName": "bot"},
        "status": "active",
        "mergeStatus": "conflicts",
        "lastMergeSourceCommit": {"commitId": "abc123"},
        "reviewers": [],
        "title": "T",
        "labels": [],
    }))
    state = await get_ado_pr_state(
        "https://dev.azure.com/org",
        "proj",
        "repo",
        1,
        ado_pat="test-pat",
        client=client,
    )
    assert state is not None
    assert state["mergeable"] is False


async def test_ado_pr_state_mergeable_false_on_rejected_by_policy() -> None:
    client = _mk_client(_mk_response(200, {
        "createdBy": {"displayName": "bot"},
        "status": "active",
        "mergeStatus": "rejectedByPolicy",
        "lastMergeSourceCommit": {"commitId": "abc123"},
        "reviewers": [],
        "title": "T",
        "labels": [],
    }))
    state = await get_ado_pr_state(
        "https://dev.azure.com/org",
        "proj",
        "repo",
        1,
        ado_pat="test-pat",
        client=client,
    )
    assert state is not None
    assert state["mergeable"] is False


async def test_ado_pr_state_returns_none_on_404() -> None:
    resp = MagicMock()
    resp.status_code = 404
    resp.json = MagicMock(return_value={})
    client = _mk_client(resp)
    state = await get_ado_pr_state(
        "https://dev.azure.com/org", "proj", "repo", 999, client=client
    )
    assert state is None


async def test_ado_pr_state_handles_request_error() -> None:
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.RequestError("boom"))
    client.aclose = AsyncMock()
    state = await get_ado_pr_state(
        "https://dev.azure.com/org",
        "proj",
        "repo",
        1,
        ado_pat="test-pat",
        client=client,
    )
    assert state is None


# --- human_approvals_count regression (default-OPEN bypass) ---
#
# Bug: iter 19 introduced human_approvals_count on the GitHub side
# but ado_api never emitted it. The has_approval gate fell back to
# approvals_count, so an ADO service-principal or build-identity
# auto-reviewer casting vote=10 would satisfy the gate on its own
# and default-OPEN auto-merge on AI-authored PRs. Iter 20 fix:
# emit human_approvals_count from get_ado_pr_state using the
# _is_ado_human_reviewer filter; the gate no longer falls back.


def _base_pr(reviewers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "createdBy": {"displayName": "xcagentrockwell"},
        "status": "active",
        "mergeStatus": "succeeded",
        "lastMergeSourceCommit": {"commitId": "abc"},
        "reviewers": reviewers,
        "title": "T",
        "labels": [],
    }


async def test_ado_human_approval_counted() -> None:
    """A normal human reviewer with vote=10 counts as a human approval."""
    client = _mk_client(_mk_response(200, _base_pr([
        {
            "vote": 10,
            "displayName": "Alice",
            "uniqueName": "alice@example.com",
        },
    ])))
    state = await get_ado_pr_state(
        "https://dev.azure.com/org", "proj", "repo", 1,
        ado_pat="test-pat", client=client,
    )
    assert state is not None
    assert state["approvals_count"] == 1
    assert state["human_approvals_count"] == 1


async def test_ado_service_principal_approval_not_counted() -> None:
    """Regression: ADO service-principal reviewers (uniqueName starting
    with svc.) must NOT count toward human_approvals_count — default-OPEN
    on AI PRs otherwise."""
    client = _mk_client(_mk_response(200, _base_pr([
        {
            "vote": 10,
            "displayName": "Build Service",
            "uniqueName": "svc.build.example.com",
        },
    ])))
    state = await get_ado_pr_state(
        "https://dev.azure.com/org", "proj", "repo", 1,
        ado_pat="test-pat", client=client,
    )
    assert state is not None
    assert state["approvals_count"] == 1
    assert state["human_approvals_count"] == 0, (
        "service-principal reviewer must not satisfy has_approval gate"
    )


async def test_ado_container_group_approval_not_counted() -> None:
    """isContainer=True reviewers (AAD groups) don't count."""
    client = _mk_client(_mk_response(200, _base_pr([
        {
            "vote": 10,
            "displayName": "Engineering Team",
            "uniqueName": "[Proj]\\Engineering",
            "isContainer": True,
        },
    ])))
    state = await get_ado_pr_state(
        "https://dev.azure.com/org", "proj", "repo", 1,
        ado_pat="test-pat", client=client,
    )
    assert state is not None
    assert state["human_approvals_count"] == 0


async def test_ado_build_identity_approval_not_counted() -> None:
    """Build\\ and Project Collection Build reviewers don't count."""
    client = _mk_client(_mk_response(200, _base_pr([
        {
            "vote": 10,
            "displayName": "Project Collection Build Service (Org)",
            "uniqueName": "Build\\abc-def",
        },
    ])))
    state = await get_ado_pr_state(
        "https://dev.azure.com/org", "proj", "repo", 1,
        ado_pat="test-pat", client=client,
    )
    assert state is not None
    assert state["human_approvals_count"] == 0


async def test_ado_mixed_reviewers_counts_only_humans() -> None:
    """Human + service principal + group: only the human counts."""
    client = _mk_client(_mk_response(200, _base_pr([
        {"vote": 10, "displayName": "Alice", "uniqueName": "alice@example.com"},
        {"vote": 10, "displayName": "svc-build", "uniqueName": "svc.build"},
        {"vote": 10, "displayName": "Team", "isContainer": True},
    ])))
    state = await get_ado_pr_state(
        "https://dev.azure.com/org", "proj", "repo", 1,
        ado_pat="test-pat", client=client,
    )
    assert state is not None
    assert state["approvals_count"] == 3
    assert state["human_approvals_count"] == 1
