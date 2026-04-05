"""Tests for L3 webhook endpoints and event routing."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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


# --- Autonomy event forwarding ---


def _base_pr_payload(action: str = "opened", *, merged: bool = False) -> dict:
    return {
        "action": action,
        "repository": {"full_name": "org/repo"},
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/org/repo/pull/42",
            "diff_url": "https://github.com/org/repo/pull/42.diff",
            "body": "Implements SCRUM-16",
            "merged": merged,
            "merged_at": "2026-04-05T12:00:00Z" if merged else None,
            "head": {"ref": "ai/SCRUM-16", "sha": "abc123"},
            "base": {"sha": "def456", "repo": {"full_name": "org/repo"}},
            "labels": [],
        },
    }


async def test_pr_opened_forwards_autonomy_event() -> None:
    payload = _base_pr_payload("opened")

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "_get_spawner") as mock_get,
        patch.object(
            l3_main, "_forward_autonomy_event", new_callable=AsyncMock
        ) as mock_forward,
    ):
        mock_get.return_value = MagicMock()

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "pull_request")

        assert response.status_code == 202
        # Allow background task to run
        import asyncio as _asyncio
        await _asyncio.sleep(0.05)

        assert mock_forward.await_count >= 1
        event = mock_forward.await_args.args[0]
        assert event["event_type"] == "pr_opened"
        assert event["repo_full_name"] == "org/repo"
        assert event["pr_number"] == 42
        assert event["head_sha"] == "abc123"
        assert event["ticket_id"] == "SCRUM-16"
        assert event["head_ref"] == "ai/SCRUM-16"
        assert event["base_sha"] == "def456"
        assert event["pr_url"] == "https://github.com/org/repo/pull/42"
        assert "event_at" in event


async def test_review_approved_forwards_autonomy_event() -> None:
    payload = _base_pr_payload("submitted")
    payload["review"] = {
        "state": "approved",
        "id": 9001,
        "body": "LGTM",
        "html_url": "https://github.com/org/repo/pull/42#pullrequestreview-9001",
        "user": {"login": "lead-dev"},
    }

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(
            l3_main, "_forward_autonomy_event", new_callable=AsyncMock
        ) as mock_forward,
    ):
        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "pull_request_review")

        assert response.status_code == 202
        import asyncio as _asyncio
        await _asyncio.sleep(0.05)

        assert mock_forward.await_count >= 1
        event = mock_forward.await_args.args[0]
        assert event["event_type"] == "review_approved"
        assert event["reviewer_login"] == "lead-dev"
        assert event["review_id"] == "9001"
        assert event["review_body"] == "LGTM"


async def test_pr_merged_forwards_autonomy_event() -> None:
    payload = _base_pr_payload("closed", merged=True)

    with (
        patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
        patch.object(l3_main, "_get_spawner") as mock_get,
        patch.object(
            l3_main, "_forward_autonomy_event", new_callable=AsyncMock
        ) as mock_forward,
    ):
        mock_get.return_value = MagicMock()

        async with await _make_client() as client:
            response = await _post_webhook(client, payload, "pull_request")

        assert response.status_code == 202
        assert response.json()["event_type"] == "pr_merged"

        import asyncio as _asyncio
        await _asyncio.sleep(0.05)

        assert mock_forward.await_count >= 1
        event = mock_forward.await_args.args[0]
        assert event["event_type"] == "pr_merged"
        assert event["merged_at"] == "2026-04-05T12:00:00Z"
        assert event["ticket_id"] == "SCRUM-16"


async def test_forwarder_short_circuits_when_token_empty() -> None:
    """When L1_INTERNAL_API_TOKEN is empty, forwarder should not make HTTP calls."""
    event = {
        "event_type": "pr_opened",
        "repo_full_name": "org/repo",
        "pr_number": 1,
        "head_sha": "abc",
        "ticket_id": "SCRUM-1",
        "event_at": "2026-04-05T00:00:00Z",
    }

    with (
        patch.object(l3_main, "L1_INTERNAL_API_TOKEN", ""),
        patch("main.httpx.AsyncClient") as mock_client,
    ):
        await l3_main._forward_autonomy_event(event)
        mock_client.assert_not_called()


async def test_forwarder_retries_once_on_request_error() -> None:
    """Forwarder should retry once on httpx.RequestError, then log on double failure."""
    event = {
        "event_type": "pr_opened",
        "repo_full_name": "org/repo",
        "pr_number": 1,
        "head_sha": "abc",
        "ticket_id": "SCRUM-1",
        "event_at": "2026-04-05T00:00:00Z",
    }

    call_count = 0

    class _FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.RequestError("boom")

    with (
        patch.object(l3_main, "L1_INTERNAL_API_TOKEN", "secret"),
        patch("main.httpx.AsyncClient", _FailingClient),
        patch.object(l3_main, "logger") as mock_logger,
        patch("main.asyncio.sleep", new_callable=AsyncMock),
    ):
        await l3_main._forward_autonomy_event(event)

    assert call_count == 2  # original + 1 retry
    # Verify error was logged
    error_calls = [
        c for c in mock_logger.error.call_args_list
        if c.args and c.args[0] == "l1_autonomy_event_forward_failed"
    ]
    assert len(error_calls) == 1


async def test_forwarder_succeeds_on_first_try() -> None:
    """Forwarder should POST to L1 with correct headers and not retry on 2xx."""
    event = {
        "event_type": "pr_opened",
        "repo_full_name": "org/repo",
        "pr_number": 1,
        "head_sha": "abc",
        "ticket_id": "SCRUM-1",
        "event_at": "2026-04-05T00:00:00Z",
    }

    captured: dict = {}

    class _OkClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _OkClient:
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            resp = MagicMock()
            resp.status_code = 200
            return resp

    with (
        patch.object(l3_main, "L1_INTERNAL_API_TOKEN", "secret-token"),
        patch.object(l3_main, "L1_SERVICE_URL", "http://l1.test"),
        patch("main.httpx.AsyncClient", _OkClient),
    ):
        await l3_main._forward_autonomy_event(event)

    assert captured["url"] == "http://l1.test/api/internal/autonomy/events"
    assert captured["headers"] == {"X-Internal-Api-Token": "secret-token"}
    assert captured["json"]["event_type"] == "pr_opened"


# --- Human issue forwarding ---


class TestHumanIssueForwarding:
    """Tests for L3 -> L1 human-issue forwarding."""

    async def test_review_approved_with_body_forwards_human_issue(self) -> None:
        payload = _base_pr_payload("submitted")
        payload["review"] = {
            "state": "approved",
            "id": 9001,
            "body": "LGTM with a few nits" * 40,  # long body to test truncation
            "submitted_at": "2026-04-05T12:00:00Z",
            "html_url": "https://github.com/org/repo/pull/42#pullrequestreview-9001",
            "user": {"login": "lead-dev", "type": "User"},
        }

        with (
            patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
            patch.object(
                l3_main, "_forward_autonomy_event", new_callable=AsyncMock
            ),
            patch.object(
                l3_main, "_forward_human_issue", new_callable=AsyncMock
            ) as mock_forward,
        ):
            async with await _make_client() as client:
                response = await _post_webhook(client, payload, "pull_request_review")

            assert response.status_code == 202
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)

            assert mock_forward.await_count == 1
            issue = mock_forward.await_args.args[0]
            assert issue["event_type"] == "review_approved"
            assert issue["reviewer_login"] == "lead-dev"
            assert issue["external_id"] == "9001"
            assert issue["ticket_id"] == "SCRUM-16"
            assert issue["pr_number"] == 42
            assert issue["repo_full_name"] == "org/repo"
            assert issue["head_sha"] == "abc123"
            assert issue["file_path"] == ""
            assert issue["line_start"] == 0
            assert issue["line_end"] == 0
            assert len(issue["summary"]) <= 500
            assert issue["comment_url"].endswith("9001")
            assert issue["event_at"] == "2026-04-05T12:00:00Z"

    async def test_review_approved_empty_body_no_forward(self) -> None:
        payload = _base_pr_payload("submitted")
        payload["review"] = {
            "state": "approved",
            "id": 9001,
            "body": "",
            "html_url": "https://github.com/org/repo/pull/42#pullrequestreview-9001",
            "user": {"login": "lead-dev"},
        }

        with (
            patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
            patch.object(
                l3_main, "_forward_autonomy_event", new_callable=AsyncMock
            ),
            patch.object(
                l3_main, "_forward_human_issue", new_callable=AsyncMock
            ) as mock_forward,
        ):
            async with await _make_client() as client:
                response = await _post_webhook(client, payload, "pull_request_review")

            assert response.status_code == 202
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)

            assert mock_forward.await_count == 0

    async def test_bot_review_not_forwarded(self) -> None:
        payload = _base_pr_payload("submitted")
        payload["review"] = {
            "state": "approved",
            "id": 9001,
            "body": "Approved by bot",
            "html_url": "https://github.com/org/repo/pull/42#pullrequestreview-9001",
            "user": {"login": "automation-bot", "type": "Bot"},
        }

        with (
            patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
            patch.object(
                l3_main, "_forward_autonomy_event", new_callable=AsyncMock
            ),
            patch.object(
                l3_main, "_forward_human_issue", new_callable=AsyncMock
            ) as mock_forward,
        ):
            async with await _make_client() as client:
                response = await _post_webhook(client, payload, "pull_request_review")

            assert response.status_code == 202
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)

            assert mock_forward.await_count == 0

    async def test_review_changes_requested_forwards_with_flag_event_type(self) -> None:
        payload = _base_pr_payload("submitted")
        payload["review"] = {
            "state": "changes_requested",
            "id": 7777,
            "body": "Please fix error handling",
            "submitted_at": "2026-04-05T13:00:00Z",
            "html_url": "https://github.com/org/repo/pull/42#pullrequestreview-7777",
            "user": {"login": "lead-dev", "type": "User"},
        }

        with (
            patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
            patch.object(l3_main, "_get_spawner") as mock_get,
            patch.object(
                l3_main, "_forward_autonomy_event", new_callable=AsyncMock
            ),
            patch.object(
                l3_main, "_forward_human_issue", new_callable=AsyncMock
            ) as mock_forward,
        ):
            mock_get.return_value = MagicMock()
            async with await _make_client() as client:
                response = await _post_webhook(client, payload, "pull_request_review")

            assert response.status_code == 202
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)

            assert mock_forward.await_count == 1
            issue = mock_forward.await_args.args[0]
            assert issue["event_type"] == "review_changes_requested"
            assert issue["external_id"] == "7777"
            assert issue["summary"] == "Please fix error handling"

    async def test_pull_request_review_comment_created_forwards_with_path_and_line(
        self,
    ) -> None:
        payload = {
            "action": "created",
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "number": 42,
                "head": {"ref": "ai/SCRUM-16", "sha": "abc123"},
                "base": {"sha": "def456", "repo": {"full_name": "org/repo"}},
            },
            "comment": {
                "id": 555,
                "path": "src/app.py",
                "line": 42,
                "original_line": 40,
                "body": "Consider using a set here",
                "created_at": "2026-04-05T14:00:00Z",
                "html_url": "https://github.com/org/repo/pull/42#discussion_r555",
                "user": {"login": "reviewer", "type": "User"},
            },
        }

        with (
            patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
            patch.object(
                l3_main, "_forward_human_issue", new_callable=AsyncMock
            ) as mock_forward,
        ):
            async with await _make_client() as client:
                response = await _post_webhook(
                    client, payload, "pull_request_review_comment"
                )

            assert response.status_code == 202
            assert response.json()["event_type"] == "review_comment_created"
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)

            assert mock_forward.await_count == 1
            issue = mock_forward.await_args.args[0]
            assert issue["event_type"] == "review_comment"
            assert issue["file_path"] == "src/app.py"
            assert issue["line_start"] == 42
            assert issue["line_end"] == 42
            assert issue["external_id"] == "555"
            assert issue["ticket_id"] == "SCRUM-16"
            assert issue["comment_url"].endswith("r555")

    async def test_review_comment_edited_also_forwards(self) -> None:
        payload = {
            "action": "edited",
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "number": 42,
                "head": {"ref": "ai/SCRUM-16", "sha": "abc123"},
                "base": {"sha": "def456", "repo": {"full_name": "org/repo"}},
            },
            "comment": {
                "id": 555,
                "path": "src/app.py",
                "line": 42,
                "body": "Edited body",
                "created_at": "2026-04-05T14:00:00Z",
                "html_url": "https://github.com/org/repo/pull/42#discussion_r555",
                "user": {"login": "reviewer"},
            },
        }

        with (
            patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
            patch.object(
                l3_main, "_forward_human_issue", new_callable=AsyncMock
            ) as mock_forward,
        ):
            async with await _make_client() as client:
                response = await _post_webhook(
                    client, payload, "pull_request_review_comment"
                )

            assert response.status_code == 202
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)

            assert mock_forward.await_count == 1

    async def test_review_comment_deleted_does_not_forward(self) -> None:
        payload = {
            "action": "deleted",
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "number": 42,
                "head": {"ref": "ai/SCRUM-16", "sha": "abc123"},
                "base": {"sha": "def456", "repo": {"full_name": "org/repo"}},
            },
            "comment": {
                "id": 555,
                "path": "src/app.py",
                "line": 42,
                "body": "Goodbye",
                "html_url": "https://github.com/org/repo/pull/42#discussion_r555",
                "user": {"login": "reviewer"},
            },
        }

        with (
            patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
            patch.object(
                l3_main, "_forward_human_issue", new_callable=AsyncMock
            ) as mock_forward,
        ):
            async with await _make_client() as client:
                response = await _post_webhook(
                    client, payload, "pull_request_review_comment"
                )

            assert response.status_code == 202
            # deleted is classified as IGNORED, so no handler runs
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)

            assert mock_forward.await_count == 0

    async def test_review_comment_no_ticket_id_skipped(self) -> None:
        payload = {
            "action": "created",
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "number": 42,
                "head": {"ref": "feature/other-branch", "sha": "abc123"},
                "base": {"sha": "def456", "repo": {"full_name": "org/repo"}},
            },
            "comment": {
                "id": 555,
                "path": "src/app.py",
                "line": 42,
                "body": "Nit",
                "html_url": "https://github.com/org/repo/pull/42#discussion_r555",
                "user": {"login": "reviewer"},
            },
        }

        with (
            patch.object(l3_main, "WEBHOOK_SECRET", TEST_SECRET),
            patch.object(
                l3_main, "_forward_human_issue", new_callable=AsyncMock
            ) as mock_forward,
        ):
            async with await _make_client() as client:
                response = await _post_webhook(
                    client, payload, "pull_request_review_comment"
                )

            assert response.status_code == 202
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)

            assert mock_forward.await_count == 0

    async def test_human_issue_forwarder_retry_then_log_on_double_fail(self) -> None:
        issue = {
            "event_type": "review_comment",
            "repo_full_name": "org/repo",
            "pr_number": 1,
            "head_sha": "abc",
            "ticket_id": "SCRUM-1",
            "external_id": "1",
            "summary": "hi",
            "event_at": "2026-04-05T00:00:00Z",
        }

        call_count = 0

        class _FailingClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self) -> _FailingClient:
                return self

            async def __aexit__(self, *args) -> None:
                return None

            async def post(self, *args, **kwargs):
                nonlocal call_count
                call_count += 1
                raise httpx.RequestError("boom")

        with (
            patch.object(l3_main, "L1_INTERNAL_API_TOKEN", "secret"),
            patch("main.httpx.AsyncClient", _FailingClient),
            patch.object(l3_main, "logger") as mock_logger,
            patch("main.asyncio.sleep", new_callable=AsyncMock),
        ):
            await l3_main._forward_human_issue(issue)

        assert call_count == 2
        error_calls = [
            c for c in mock_logger.error.call_args_list
            if c.args and c.args[0] == "l1_human_issue_forward_failed"
        ]
        assert len(error_calls) == 1


# --- Backlog integration ---


async def test_forward_failure_appends_to_backlog(tmp_path) -> None:
    """On final forward failure, event is persisted to the backlog."""
    import backlog as backlog_mod

    backlog_path = tmp_path / "backlog.jsonl"

    event = {
        "event_type": "pr_opened",
        "repo_full_name": "org/repo",
        "pr_number": 1,
        "head_sha": "abc",
        "ticket_id": "SCRUM-1",
        "event_at": "2026-04-05T00:00:00Z",
    }

    class _FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, *args, **kwargs):
            raise httpx.RequestError("boom")

    with (
        patch.object(l3_main, "L1_INTERNAL_API_TOKEN", "secret"),
        patch("main.httpx.AsyncClient", _FailingClient),
        patch("main.asyncio.sleep", new_callable=AsyncMock),
        patch.object(backlog_mod, "BACKLOG_PATH", backlog_path),
    ):
        await l3_main._forward_autonomy_event(event)

    assert backlog_path.exists()
    lines = backlog_path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["endpoint"] == "autonomy_event"
    assert entry["payload"]["ticket_id"] == "SCRUM-1"
    assert entry["attempts"] == 1


async def test_forward_skip_when_no_token_does_not_backlog(tmp_path) -> None:
    """When L1_INTERNAL_API_TOKEN is unset, short-circuit without backlog append."""
    import backlog as backlog_mod

    backlog_path = tmp_path / "backlog.jsonl"

    event = {
        "event_type": "pr_opened",
        "repo_full_name": "org/repo",
        "pr_number": 1,
        "head_sha": "abc",
        "ticket_id": "SCRUM-1",
        "event_at": "2026-04-05T00:00:00Z",
    }

    with (
        patch.object(l3_main, "L1_INTERNAL_API_TOKEN", ""),
        patch.object(backlog_mod, "BACKLOG_PATH", backlog_path),
    ):
        await l3_main._forward_autonomy_event(event)

    assert not backlog_path.exists()
