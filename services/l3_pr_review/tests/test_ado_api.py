"""Tests for ado_api: PR state fetching + completion + comment posting."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx

from ado_api import get_ado_pr_state, post_ado_pr_comment


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


# --- post_ado_pr_comment ---


async def test_post_ado_pr_comment_success() -> None:
    resp = MagicMock()
    resp.status_code = 200
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()

    ok, msg = await post_ado_pr_comment(
        "https://dev.azure.com/org", "proj", "repo-id", 42,
        "Hello from the harness!",
        ado_pat="test-pat", client=client,
    )
    assert ok is True
    assert msg == "posted"
    # Verify URL construction
    call_args = client.post.call_args
    url = call_args[0][0]
    assert "/proj/_apis/git/repositories/repo-id/pullrequests/42/threads" in url
    # Verify body shape
    body = call_args[1]["json"]
    assert len(body["comments"]) == 1
    assert body["comments"][0]["content"] == "Hello from the harness!"
    assert body["comments"][0]["commentType"] == 1
    assert body["status"] == 1


async def test_post_ado_pr_comment_no_pat() -> None:
    """Returns (False, 'no_pat') when PAT is empty."""
    ok, msg = await post_ado_pr_comment(
        "https://dev.azure.com/org", "proj", "repo", 1,
        "test", ado_pat="",
    )
    assert ok is False
    assert msg == "no_pat"


async def test_post_ado_pr_comment_http_error() -> None:
    resp = MagicMock()
    resp.status_code = 403
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()

    ok, msg = await post_ado_pr_comment(
        "https://dev.azure.com/org", "proj", "repo", 1,
        "test", ado_pat="pat", client=client,
    )
    assert ok is False
    assert msg == "http_403"


# --- get_ado_pr_state checks_passed parameter ---


async def test_get_ado_pr_state_checks_passed_from_param() -> None:
    """When checks_passed=True is passed, it appears in the result."""
    # Mock: the build status API should NOT be called when checks_passed is provided
    pr_resp = MagicMock()
    pr_resp.status_code = 200
    pr_resp.json = MagicMock(return_value={
        "createdBy": {"displayName": "bot"},
        "status": "active",
        "mergeStatus": "succeeded",
        "lastMergeSourceCommit": {"commitId": "abc123"},
        "reviewers": [],
        "title": "T",
        "labels": [],
    })
    client = MagicMock()
    client.get = AsyncMock(return_value=pr_resp)
    client.aclose = AsyncMock()

    state = await get_ado_pr_state(
        "https://dev.azure.com/org", "proj", "repo", 1,
        checks_passed=True, ado_pat="pat", client=client,
    )
    assert state is not None
    assert state["checks_passed"] is True
    # Only one GET call (for the PR itself), not two (no build status query)
    assert client.get.call_count == 1


async def test_get_ado_pr_state_checks_passed_from_build_api() -> None:
    """When checks_passed is None, queries the build status API."""
    pr_resp = MagicMock()
    pr_resp.status_code = 200
    pr_resp.json = MagicMock(return_value={
        "createdBy": {"displayName": "bot"},
        "status": "active",
        "mergeStatus": "succeeded",
        "lastMergeSourceCommit": {"commitId": "abc123"},
        "reviewers": [],
        "title": "T",
        "labels": [],
    })
    build_resp = MagicMock()
    build_resp.status_code = 200
    build_resp.json = MagicMock(return_value={
        "value": [{"result": "succeeded"}],
    })
    client = MagicMock()
    # First call: PR state, second call: build status
    client.get = AsyncMock(side_effect=[pr_resp, build_resp])
    client.aclose = AsyncMock()

    state = await get_ado_pr_state(
        "https://dev.azure.com/org", "proj", "repo", 1,
        ado_pat="pat", client=client,
    )
    assert state is not None
    assert state["checks_passed"] is True
    assert client.get.call_count == 2
