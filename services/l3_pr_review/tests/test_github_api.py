"""Tests for github_api: PR state fetching + merge."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from github_api import get_pr_state, merge_pr


def _mk_response(
    status: int,
    json_body: Any,
    *,
    next_url: str | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_body)
    # httpx.Response.links is a dict[str, dict[str, str]] keyed by rel.
    resp.links = {"next": {"url": next_url}} if next_url else {}
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
        {"user": {"login": "alice"}, "state": "APPROVED", "commit_id": "abc123"},
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
    # alice: CHANGES_REQUESTED then APPROVED — latest wins; comment ignored.
    # All reviews target the current head_sha "abc" so they count.
    reviews_json = [
        {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED", "commit_id": "abc"},
        {"user": {"login": "alice"}, "state": "COMMENTED", "commit_id": "abc"},
        {"user": {"login": "alice"}, "state": "APPROVED", "commit_id": "abc"},
        {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED", "commit_id": "abc"},
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


# --- get_pr_state pagination regression ---
#
# Bug: get_pr_state used to fetch /reviews and /check-suites without
# pagination. GitHub defaults to 30 items per page, so on PRs with many
# bot reviewers a human's CHANGES_REQUESTED could silently drop off
# page 1 → auto-merge gate fail-open. Fix: follow Link rel=next with
# per_page=100.


async def test_get_pr_state_paginates_reviews_across_pages() -> None:
    """Human CHANGES_REQUESTED on page 2 must be counted."""
    pr_json = {
        "user": {"login": "bot"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "abc"},
        "labels": [],
    }
    # Page 1: 2 bot approvals. Page 2: 1 human CHANGES_REQUESTED.
    # All target the current head_sha "abc".
    reviews_page1 = [
        {"user": {"login": "bot1"}, "state": "APPROVED", "commit_id": "abc"},
        {"user": {"login": "bot2"}, "state": "APPROVED", "commit_id": "abc"},
    ]
    reviews_page2 = [
        {"user": {"login": "human"}, "state": "CHANGES_REQUESTED", "commit_id": "abc"},
    ]
    suites_json = {
        "check_suites": [{"status": "completed", "conclusion": "success"}]
    }
    client = _mk_client(
        [
            _mk_response(200, pr_json),
            _mk_response(
                200, reviews_page1, next_url="https://api.github.com/reviews?page=2"
            ),
            _mk_response(200, reviews_page2),
            _mk_response(200, suites_json),
        ]
    )
    state = await get_pr_state(
        "acme/repo", 1, github_token="tok", client=client
    )
    assert state is not None
    assert state["approvals_count"] == 2
    assert state["changes_requested_count"] == 1, (
        "human review on page 2 must be included — regression for "
        "bug where missing pagination let auto-merge gate fail-open"
    )


async def test_get_pr_state_paginates_check_suites_across_pages() -> None:
    """A failing suite on page 2 must block checks_passed."""
    pr_json = {
        "user": {"login": "bot"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "abc"},
        "labels": [],
    }
    suites_page1 = {
        "check_suites": [{"status": "completed", "conclusion": "success"}]
    }
    suites_page2 = {
        "check_suites": [{"status": "completed", "conclusion": "failure"}]
    }
    client = _mk_client(
        [
            _mk_response(200, pr_json),
            _mk_response(200, []),
            _mk_response(
                200,
                suites_page1,
                next_url="https://api.github.com/check-suites?page=2",
            ),
            _mk_response(200, suites_page2),
        ]
    )
    state = await get_pr_state(
        "acme/repo", 1, github_token="tok", client=client
    )
    assert state is not None
    assert state["checks_passed"] is False, (
        "failing suite on page 2 must block checks_passed"
    )


# --- get_pr_state stale approval regression ---
#
# Bug: get_pr_state counted APPROVED reviews without checking their
# commit_id. A human approves commit A; agent force-pushes commit B;
# the old review still appears in /pulls/{n}/reviews with commit_id=A.
# approvals_count=1 → approval gate passes → evaluate_and_maybe_merge
# fires merge_pr on commit B which was never reviewed.
# Fix: drop APPROVED reviews whose commit_id != head_sha.
# CHANGES_REQUESTED reviews remain sticky across commits (a rejection
# shouldn't silently clear just because a new commit lands).


async def test_get_pr_state_drops_stale_approval_on_old_commit() -> None:
    """APPROVED review on commit A must NOT count when head_sha is B."""
    pr_json = {
        "user": {"login": "bot"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "B"},
        "labels": [],
    }
    reviews_json = [
        {
            "user": {"login": "human"},
            "state": "APPROVED",
            "commit_id": "A",  # stale — was approved on an older commit
        },
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
    assert state["approvals_count"] == 0, (
        "stale approval against an older commit must not be counted — "
        "regression for default-OPEN auto-merge bug"
    )


async def test_get_pr_state_keeps_changes_requested_across_commits() -> None:
    """CHANGES_REQUESTED on old commit must stay sticky."""
    pr_json = {
        "user": {"login": "bot"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "B"},
        "labels": [],
    }
    reviews_json = [
        {
            "user": {"login": "human"},
            "state": "CHANGES_REQUESTED",
            "commit_id": "A",  # old commit — but rejection stays
        },
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
    assert state["changes_requested_count"] == 1, (
        "rejection must be sticky across new commits; only a fresh "
        "APPROVED review should clear it"
    )


async def test_get_pr_state_fresh_approval_replaces_old_rejection() -> None:
    """Reviewer's new APPROVED on current commit clears their old CHANGES_REQUESTED."""
    pr_json = {
        "user": {"login": "bot"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "B"},
        "labels": [],
    }
    reviews_json = [
        {
            "user": {"login": "human"},
            "state": "CHANGES_REQUESTED",
            "commit_id": "A",
        },
        {
            "user": {"login": "human"},
            "state": "APPROVED",
            "commit_id": "B",  # fresh approval on current head
        },
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
    assert state["approvals_count"] == 1
    assert state["changes_requested_count"] == 0


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
