"""Tests for ADO webhook routing in main.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app


@pytest.fixture
def _no_ado_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable ADO webhook token validation for testing."""
    monkeypatch.setattr("main.ADO_WEBHOOK_TOKEN", "")


def _ado_pr_payload(vote: int = 0, status: str = "active") -> dict:
    return {
        "eventType": "git.pullrequest.updated",
        "resource": {
            "pullRequestId": 42,
            "status": status,
            "sourceRefName": "refs/heads/ai/TICKET-123",
            "title": "Test PR",
            "repository": {
                "id": "repo-id",
                "name": "my-repo",
                "project": {"name": "my-project"},
            },
            "reviewers": [{"vote": vote}] if vote else [],
            "labels": [],
            "lastMergeSourceCommit": {"commitId": "abc123"},
        },
        "resourceContainers": {
            "collection": {"baseUrl": "https://dev.azure.com/org"},
        },
    }


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_webhook_review_approved_routes() -> None:
    """POST to /webhooks/ado-pr with vote=10 returns 202 + accepted."""
    payload = _ado_pr_payload(vote=10)

    with patch("main._handle_ado_review_approved", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/webhooks/ado-pr", json=payload)

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_webhook_changes_requested_routes() -> None:
    """POST with vote=-10 returns 202 + accepted."""
    payload = _ado_pr_payload(vote=-10)

    with patch("main._handle_ado_review_changes_requested", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/webhooks/ado-pr", json=payload)

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_webhook_review_comment_routes() -> None:
    """POST with vote=5 (approved with suggestions) returns 202 + accepted."""
    payload = _ado_pr_payload(vote=5)

    with patch("main._handle_ado_review_comment", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/webhooks/ado-pr", json=payload)

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_build_webhook_succeeded() -> None:
    """POST to /webhooks/ado-build with succeeded build returns 202."""
    payload = {
        "eventType": "build.complete",
        "resource": {
            "id": 123,
            "result": "succeeded",
            "reason": "pullRequest",
            "triggerInfo": {"pr.number": "42"},
            "repository": {"id": "repo-id"},
            "project": {"name": "my-project"},
            "sourceBranch": "refs/heads/ai/TICKET-123",
        },
        "resourceContainers": {
            "collection": {"baseUrl": "https://dev.azure.com/org"},
        },
    }

    with patch("main._handle_ado_build_complete", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/webhooks/ado-build", json=payload)

        assert resp.status_code == 202
        body = resp.json()
        assert body["event_type"] == "ci_passed"


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_build_complete_extracts_pr_number_flat_key() -> None:
    """Regression: PR number extraction from flat 'pr.number' key in triggerInfo
    must work (operator precedence bug fix)."""
    payload = {
        "eventType": "build.complete",
        "resource": {
            "id": 999,
            "result": "succeeded",
            "reason": "pullRequest",
            "triggerInfo": {"pr.number": "77"},
            "repository": {"id": "repo-id"},
            "project": {"name": "my-project"},
            "sourceBranch": "refs/heads/ai/TICKET-55",
        },
        "resourceContainers": {
            "collection": {"baseUrl": "https://dev.azure.com/org"},
        },
    }

    with patch(
        "main.evaluate_and_maybe_merge_ado", new_callable=AsyncMock
    ) as mock_eval:
        mock_eval.return_value = {"status": "dry_run"}
        # Call _handle_ado_build_complete directly to test extraction logic
        from main import _handle_ado_build_complete
        await _handle_ado_build_complete(payload)

        assert mock_eval.call_count == 1
        call_kwargs = mock_eval.call_args.kwargs
        assert call_kwargs["pr_id"] == 77, (
            f"Expected pr_id=77 from flat 'pr.number' key, got {call_kwargs['pr_id']}"
        )
        assert call_kwargs["ticket_id"] == "TICKET-55"
        assert call_kwargs["checks_passed"] is True


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_build_webhook_failed() -> None:
    """POST to /webhooks/ado-build with failed build returns 202."""
    payload = {
        "eventType": "build.complete",
        "resource": {"id": 123, "result": "failed"},
        "resourceContainers": {},
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/webhooks/ado-build", json=payload)

    assert resp.status_code == 202
    body = resp.json()
    assert body["event_type"] == "ci_failed"
