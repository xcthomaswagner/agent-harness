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
