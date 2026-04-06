"""Tests for github_api: PR state fetching + merge."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from github_api import get_pr_state, merge_pr


def _mk_response(status: int, json_body: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_body)
    return resp


def _mk_client(responses: list[MagicMock]) -> MagicMock:
    """Mock client returning successive responses for .get() calls."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=responses)
    client.put = AsyncMock(side_effect=responses)
    client.aclose = AsyncMock()
    return client


# --- get_pr_state ---


async def test_get_pr_state_happy_path() -> None:
    pr_json = {
        "user": {"login": "xcagentrockwell"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "abc123"},
        "labels": [{"name": "bug"}],
        "title": "Fix X",
    }
    reviews_json = [
        {"user": {"login": "alice"}, "state": "APPROVED"},
    ]
    suites_json = {
        "check_suites": [{"status": "completed", "conclusion": "success"}]
    }
    client = _mk_client(
        [
            _mk_response(200, pr_json),
            _mk_response(200, reviews_json),
            _mk_response(200, suites_json),
        ]
    )
    state = await get_pr_state(
        "acme/repo", 1, github_token="tok", client=client
    )
    assert state is not None
    assert state["author"] == "xcagentrockwell"
    assert state["approvals_count"] == 1
    assert state["changes_requested_count"] == 0
    assert state["checks_passed"] is True
    assert state["head_sha"] == "abc123"
    assert state["labels"] == ["bug"]
    assert state["mergeable_state"] == "clean"


async def test_get_pr_state_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_GH_TOKEN", raising=False)
    state = await get_pr_state("acme/repo", 1, github_token="")
    assert state is None


async def test_get_pr_state_404() -> None:
    client = _mk_client([_mk_response(404, {})])
    state = await get_pr_state("acme/repo", 1, github_token="tok", client=client)
    assert state is None


async def test_get_pr_state_collapses_reviews_per_user() -> None:
    pr_json = {
        "user": {"login": "bot"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "abc"},
        "labels": [],
    }
    # alice: CHANGES_REQUESTED then APPROVED — latest wins; comment ignored
    reviews_json = [
        {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "alice"}, "state": "COMMENTED"},  # ignored
        {"user": {"login": "alice"}, "state": "APPROVED"},
        {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"},
    ]
    suites_json = {
        "check_suites": [{"status": "completed", "conclusion": "success"}]
    }
    client = _mk_client(
        [
            _mk_response(200, pr_json),
            _mk_response(200, reviews_json),
            _mk_response(200, suites_json),
        ]
    )
    state = await get_pr_state(
        "acme/repo", 1, github_token="tok", client=client
    )
    assert state is not None
    assert state["approvals_count"] == 1  # alice
    assert state["changes_requested_count"] == 1  # bob


async def test_get_pr_state_checks_passed_true_all_success() -> None:
    pr_json = {
        "user": {"login": "bot"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "abc"},
        "labels": [],
    }
    suites_json = {
        "check_suites": [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "skipped"},
            {"status": "completed", "conclusion": "neutral"},
        ]
    }
    client = _mk_client(
        [
            _mk_response(200, pr_json),
            _mk_response(200, []),
            _mk_response(200, suites_json),
        ]
    )
    state = await get_pr_state(
        "acme/repo", 1, github_token="tok", client=client
    )
    assert state is not None
    assert state["checks_passed"] is True


async def test_get_pr_state_checks_passed_false_if_any_failure() -> None:
    pr_json = {
        "user": {"login": "bot"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "abc"},
        "labels": [],
    }
    suites_json = {
        "check_suites": [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "failure"},
        ]
    }
    client = _mk_client(
        [
            _mk_response(200, pr_json),
            _mk_response(200, []),
            _mk_response(200, suites_json),
        ]
    )
    state = await get_pr_state(
        "acme/repo", 1, github_token="tok", client=client
    )
    assert state is not None
    assert state["checks_passed"] is False


async def test_get_pr_state_checks_passed_false_if_suite_still_running() -> None:
    """One suite success + one still in_progress must NOT produce checks_passed=True."""
    pr_json = {
        "user": {"login": "bot"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "abc"},
        "labels": [],
    }
    suites_json = {
        "check_suites": [
            {"status": "completed", "conclusion": "success"},
            {"status": "in_progress", "conclusion": None},
        ]
    }
    client = _mk_client(
        [
            _mk_response(200, pr_json),
            _mk_response(200, []),
            _mk_response(200, suites_json),
        ]
    )
    state = await get_pr_state(
        "acme/repo", 1, github_token="tok", client=client
    )
    assert state is not None
    assert state["checks_passed"] is False


async def test_get_pr_state_checks_passed_false_if_suite_queued() -> None:
    """Queued suite must block checks_passed."""
    pr_json = {
        "user": {"login": "bot"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "abc"},
        "labels": [],
    }
    suites_json = {
        "check_suites": [
            {"status": "completed", "conclusion": "success"},
            {"status": "queued", "conclusion": None},
        ]
    }
    client = _mk_client(
        [
            _mk_response(200, pr_json),
            _mk_response(200, []),
            _mk_response(200, suites_json),
        ]
    )
    state = await get_pr_state(
        "acme/repo", 1, github_token="tok", client=client
    )
    assert state is not None
    assert state["checks_passed"] is False


# --- merge_pr ---


async def test_merge_pr_200_returns_merged() -> None:
    client = MagicMock()
    client.put = AsyncMock(return_value=_mk_response(200, {"merged": True}))
    client.aclose = AsyncMock()
    ok, msg = await merge_pr(
        "acme/repo", 1, "abc", github_token="tok", client=client
    )
    assert ok is True
    assert msg == "merged"


async def test_merge_pr_405_not_mergeable() -> None:
    client = MagicMock()
    client.put = AsyncMock(return_value=_mk_response(405, {}))
    client.aclose = AsyncMock()
    ok, msg = await merge_pr(
        "acme/repo", 1, "abc", github_token="tok", client=client
    )
    assert ok is False
    assert msg == "not_mergeable"


async def test_merge_pr_409_sha_mismatch() -> None:
    client = MagicMock()
    client.put = AsyncMock(return_value=_mk_response(409, {}))
    client.aclose = AsyncMock()
    ok, msg = await merge_pr(
        "acme/repo", 1, "abc", github_token="tok", client=client
    )
    assert ok is False
    assert msg == "sha_mismatch"


async def test_merge_pr_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_GH_TOKEN", raising=False)
    ok, msg = await merge_pr("acme/repo", 1, "abc", github_token="")
    assert ok is False
    assert msg == "no_token"


async def test_merge_pr_request_error() -> None:
    client = MagicMock()
    client.put = AsyncMock(side_effect=httpx.RequestError("boom"))
    client.aclose = AsyncMock()
    ok, msg = await merge_pr(
        "acme/repo", 1, "abc", github_token="tok", client=client
    )
    assert ok is False
    assert "request_error" in msg
