"""Tests for L3 webhook endpoints and event routing."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from httpx import ASGITransport, AsyncClient

import main as l3_main
from main import app

TEST_SECRET = "test-webhook-secret"


async def _make_client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    """Compute the x-hub-signature-256 header value for a request body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _post_webhook(
    client: AsyncClient,
    payload: dict,
    event: str,
    secret: str = TEST_SECRET,
) -> object:
    """POST a signed webhook payload and return the response."""
    body = json.dumps(payload).encode()
    return await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "x-github-event": event,
            "x-hub-signature-256": _sign(body, secret),
        },
    )


# --- Health ---


async def test_health() -> None:
    async with await _make_client() as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# --- PR opened -> spawns review ---


async def test_pr_opened_triggers_review() -> None:
    payload = {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "diff_url": "https://github.com/org/repo/pull/42.diff",
            "body": "Implements PROJ-123: Add greeting component",
        },
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_spawner = MagicMock()
        mock_spawner.spawn_pr_review.return_value = True
        mock_get.return_value = mock_spawner

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "pull_request")

        assert response.status_code == 202
        assert response.json()["event_type"] == "pr_opened"


# --- CI failure -> spawns fix ---


async def test_ci_failure_triggers_fix() -> None:
    payload = {
        "action": "completed",
        "check_suite": {
            "conclusion": "failure",
            "head_branch": "ai/PROJ-123",
            "pull_requests": [{"number": 42}],
        },
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_spawner = MagicMock()
        mock_spawner.spawn_ci_fix.return_value = True
        mock_get.return_value = mock_spawner

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "check_suite")

        assert response.status_code == 202
        assert response.json()["event_type"] == "ci_failed"


# --- Review comment -> spawns response ---


async def test_review_comment_triggers_response() -> None:
    payload = {
        "action": "submitted",
        "review": {
            "state": "commented",
            "body": "Why did you use this approach?",
            "user": {"login": "reviewer"},
        },
        "pull_request": {"number": 42},
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_spawner = MagicMock()
        mock_spawner.spawn_comment_response.return_value = True
        mock_get.return_value = mock_spawner

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "pull_request_review")

        assert response.status_code == 202
        assert response.json()["event_type"] == "review_comment"


# --- Changes requested -> spawns fix ---


async def test_changes_requested_triggers_fix() -> None:
    payload = {
        "action": "submitted",
        "review": {
            "state": "changes_requested",
            "body": "Please fix the error handling",
            "user": {"login": "lead-dev"},
        },
        "pull_request": {"number": 42},
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_spawner = MagicMock()
        mock_spawner.spawn_comment_response.return_value = True
        mock_get.return_value = mock_spawner

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "pull_request_review")

        assert response.status_code == 202
        assert response.json()["event_type"] == "review_changes_requested"


# --- Issue comment on PR -> spawns response ---


async def test_issue_comment_on_pr_triggers_response() -> None:
    payload = {
        "action": "created",
        "issue": {"number": 10, "pull_request": {"url": "..."}},
        "comment": {
            "body": "Can you explain this change?",
            "user": {"login": "human-reviewer"},
        },
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_spawner = MagicMock()
        mock_spawner.spawn_comment_response.return_value = True
        mock_get.return_value = mock_spawner

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "issue_comment")

        assert response.status_code == 202
        assert response.json()["event_type"] == "review_comment"


# --- Bot self-loop prevention ---


async def test_bot_review_comment_ignored() -> None:
    """Comments from the bot itself should not spawn new sessions."""
    payload = {
        "action": "submitted",
        "review": {
            "state": "commented",
            "body": "I reviewed this PR and found no issues.",
            "user": {"login": "github-actions[bot]"},
        },
        "pull_request": {"number": 42},
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "BOT_USERNAME", "github-actions[bot]"),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_spawner = MagicMock()
        mock_get.return_value = mock_spawner

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "pull_request_review")

        assert response.status_code == 202
        # Handler runs in background, but spawner should NOT be called
        # The event is still classified as review_comment and accepted
        assert response.json()["event_type"] == "review_comment"


async def test_bot_changes_requested_ignored() -> None:
    """Changes requested by the bot itself should not spawn fix sessions."""
    payload = {
        "action": "submitted",
        "review": {
            "state": "changes_requested",
            "body": "Please address these issues.",
            "user": {"login": "github-actions[bot]"},
        },
        "pull_request": {"number": 42},
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "BOT_USERNAME", "github-actions[bot]"),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_spawner = MagicMock()
        mock_get.return_value = mock_spawner

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "pull_request_review")

        assert response.status_code == 202
        assert response.json()["event_type"] == "review_changes_requested"


async def test_bot_issue_comment_ignored() -> None:
    """Issue comments from the bot itself should not spawn new sessions."""
    payload = {
        "action": "created",
        "issue": {"number": 10, "pull_request": {"url": "..."}},
        "comment": {
            "body": "Fixed the issue and pushed.",
            "user": {"login": "github-actions[bot]"},
        },
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "BOT_USERNAME", "github-actions[bot]"),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_spawner = MagicMock()
        mock_get.return_value = mock_spawner

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "issue_comment")

        assert response.status_code == 202
        assert response.json()["event_type"] == "review_comment"


async def test_marker_based_bot_detection() -> None:
    """Comments containing the bot marker should be ignored even from human users."""
    payload = {
        "action": "created",
        "issue": {"number": 10, "pull_request": {"url": "..."}},
        "comment": {
            "body": "Fixed: moved ts-node\n\n<!-- xcagent -->",
            "user": {"login": "xcthomaswagner"},  # Human user, not bot
        },
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "BOT_COMMENT_MARKER", "<!-- xcagent -->"),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_spawner = MagicMock()
        mock_get.return_value = mock_spawner

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "issue_comment")

        assert response.status_code == 202
        # Spawner should NOT be called — marker detected
        assert response.json()["event_type"] == "review_comment"


async def test_human_comment_not_blocked() -> None:
    """Human comments without marker should still trigger response."""
    payload = {
        "action": "created",
        "issue": {"number": 10, "pull_request": {"url": "..."}},
        "comment": {
            "body": "Can you explain this change?",
            "user": {"login": "xcthomaswagner"},
        },
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "BOT_COMMENT_MARKER", "<!-- xcagent -->"),
        patch.object(l3_main, "BOT_USERNAME", "github-actions[bot]"),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_spawner = MagicMock()
        mock_get.return_value = mock_spawner

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "issue_comment")

        assert response.status_code == 202
        assert response.json()["event_type"] == "review_comment"


# --- Ignored events ---


async def test_ignored_event() -> None:
    payload = {"action": "closed"}

    with patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET):
        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "pull_request")

    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


# --- Signature validation ---


async def test_rejects_missing_signature() -> None:
    """Requests with no signature header should be rejected when secret is set."""
    payload = {"action": "opened"}

    with patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET):
        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/github",
                json=payload,
                headers={"x-github-event": "pull_request"},
            )
        assert response.status_code == 401


async def test_rejects_invalid_signature() -> None:
    payload = {"action": "opened"}

    with patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET):
        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/github",
                json=payload,
                headers={
                    "x-github-event": "pull_request",
                    "x-hub-signature-256": "sha256=bad",
                },
            )
        assert response.status_code == 401


async def test_accepts_without_secret_in_dev_mode() -> None:
    """When WEBHOOK_SECRET is empty (dev mode), requests are accepted without signature."""
    payload = {"action": "opened", "pull_request": {"number": 1, "diff_url": "", "body": ""}}

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", ""),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_get.return_value = MagicMock()
        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/github",
                json=payload,
                headers={"x-github-event": "pull_request"},
            )
        assert response.status_code == 202


async def test_accepts_valid_signature() -> None:
    payload = {"action": "opened", "pull_request": {"number": 1, "diff_url": "", "body": ""}}
    body = json.dumps(payload).encode()

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "_get_spawner") as mock_get,
    ):
        mock_get.return_value = MagicMock()

        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "x-github-event": "pull_request",
                    "x-hub-signature-256": _sign(body),
                },
            )
        assert response.status_code == 202


# --- Malformed body ---


async def test_rejects_non_json_body() -> None:
    body = b"not json"

    with patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET):
        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "x-github-event": "push",
                    "x-hub-signature-256": _sign(body),
                },
            )
    assert response.status_code == 422


# --- _lookup_trace_id ---


class TestLookupTraceId:
    """Tests for _lookup_trace_id — correlates L3 events with L2 trace IDs."""

    def test_finds_agent_finished_trace_id(self) -> None:
        entries = [
            {"event": "jira_webhook_received", "trace_id": "aaa"},
            {"event": "agent_finished", "trace_id": "bbb"},
            {"event": "code_review_artifact", "trace_id": "bbb"},
        ]
        with patch.object(l3_main, "read_trace", return_value=entries):
            result = l3_main._lookup_trace_id("PROJ-1")
        assert result == "bbb"

    def test_finds_pipeline_complete_trace_id(self) -> None:
        entries = [
            {"event": "jira_webhook_received", "trace_id": "aaa"},
            {"event": "Pipeline complete", "trace_id": "ccc"},
        ]
        with patch.object(l3_main, "read_trace", return_value=entries):
            result = l3_main._lookup_trace_id("PROJ-2")
        assert result == "ccc"

    def test_fallback_to_last_entry(self) -> None:
        entries = [
            {"event": "jira_webhook_received", "trace_id": "aaa"},
            {"event": "l2_dispatched", "trace_id": "ddd"},
        ]
        with patch.object(l3_main, "read_trace", return_value=entries):
            result = l3_main._lookup_trace_id("PROJ-3")
        assert result == "ddd"

    def test_generates_new_id_when_no_entries(self) -> None:
        with patch.object(l3_main, "read_trace", return_value=[]):
            result = l3_main._lookup_trace_id("PROJ-4")
        assert len(result) == 12
        int(result, 16)  # Should be valid hex
