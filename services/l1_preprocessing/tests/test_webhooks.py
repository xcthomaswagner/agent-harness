"""Tests for the webhook receiver endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

import main
from main import app

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


async def _make_client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# --- Jira Webhook ---


async def test_jira_webhook_accepts_valid_payload() -> None:
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())
    async with await _make_client() as client:
        response = await client.post("/webhooks/jira", json=payload)
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["ticket_id"] == "ACME-42"


async def test_jira_webhook_bug_payload() -> None:
    payload = json.loads((FIXTURES / "jira_webhook_bug.json").read_text())
    async with await _make_client() as client:
        response = await client.post("/webhooks/jira", json=payload)
        assert response.status_code == 202
        assert response.json()["ticket_id"] == "ACME-99"


async def test_jira_webhook_validates_signature_when_secret_set() -> None:
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())
    body = json.dumps(payload).encode()
    secret = "test-secret-123"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = secret
        mock_settings.jira_base_url = "https://test.atlassian.net"
        mock_settings.jira_api_token = "token"
        mock_settings.jira_user_email = "bot@test.com"
        mock_settings.jira_ac_field_id = "customfield_10429"

        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/jira",
                content=body,
                headers={"Content-Type": "application/json", "x-hub-signature": signature},
            )
            assert response.status_code == 202


async def test_jira_webhook_rejects_invalid_signature() -> None:
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = "real-secret"

        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/jira",
                json=payload,
                headers={"x-hub-signature": "sha256=bad"},
            )
            assert response.status_code == 401


async def test_jira_webhook_rejects_missing_signature_when_secret_set() -> None:
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = "real-secret"

        async with await _make_client() as client:
            response = await client.post("/webhooks/jira", json=payload)
            assert response.status_code == 401


async def test_jira_webhook_skips_signature_when_no_secret() -> None:
    """When webhook_secret is empty, signature validation is skipped."""
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())
    async with await _make_client() as client:
        response = await client.post("/webhooks/jira", json=payload)
        assert response.status_code == 202


# --- ADO Webhook ---


async def test_ado_webhook_accepts_valid_payload() -> None:
    payload = {
        "eventType": "workitem.updated",
        "resource": {
            "id": 10,
            "fields": {
                "System.WorkItemType": "Task",
                "System.Title": "ADO test task",
            },
        },
    }
    async with await _make_client() as client:
        response = await client.post("/webhooks/ado", json=payload)
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"


async def test_ado_webhook_rejects_non_json() -> None:
    async with await _make_client() as client:
        response = await client.post(
            "/webhooks/ado",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422


# --- Manual Process Ticket ---


async def test_manual_process_ticket() -> None:
    ticket = {
        "source": "jira",
        "id": "TEST-1",
        "ticket_type": "story",
        "title": "Test ticket",
        "description": "A test",
        "acceptance_criteria": ["It works"],
    }
    async with await _make_client() as client:
        response = await client.post("/api/process-ticket", json=ticket)
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["ticket_id"] == "TEST-1"


async def test_manual_process_ticket_validation_error() -> None:
    """Missing required fields should return 422."""
    async with await _make_client() as client:
        response = await client.post("/api/process-ticket", json={"title": "incomplete"})
        assert response.status_code == 422


# --- Agent Completion Callback ---


async def test_agent_complete_updates_jira() -> None:
    completion = {
        "ticket_id": "SCRUM-1",
        "status": "complete",
        "pr_url": "https://github.com/org/repo/pull/1",
        "branch": "ai/SCRUM-1",
    }
    with patch.object(main, "_get_jira_adapter") as mock_get:
        mock_adapter = AsyncMock()
        mock_get.return_value = mock_adapter
        async with await _make_client() as client:
            response = await client.post("/api/agent-complete", json=completion)
        assert response.status_code == 200
        mock_adapter.write_comment.assert_called_once()
        mock_adapter.transition_status.assert_called_once_with("SCRUM-1", "Done")


async def test_agent_complete_partial_adds_label() -> None:
    completion = {
        "ticket_id": "SCRUM-2",
        "status": "partial",
        "pr_url": "https://github.com/org/repo/pull/2",
        "branch": "ai/SCRUM-2",
    }
    with patch.object(main, "_get_jira_adapter") as mock_get:
        mock_adapter = AsyncMock()
        mock_get.return_value = mock_adapter
        async with await _make_client() as client:
            response = await client.post("/api/agent-complete", json=completion)
        assert response.status_code == 200
        mock_adapter.add_label.assert_called_once_with("SCRUM-2", "partial-implementation")
        mock_adapter.transition_status.assert_not_called()


async def test_agent_complete_escalated_adds_label() -> None:
    completion = {
        "ticket_id": "SCRUM-3",
        "status": "escalated",
        "pr_url": "",
        "branch": "ai/SCRUM-3",
    }
    with patch.object(main, "_get_jira_adapter") as mock_get:
        mock_adapter = AsyncMock()
        mock_get.return_value = mock_adapter
        async with await _make_client() as client:
            response = await client.post("/api/agent-complete", json=completion)
        assert response.status_code == 200
        mock_adapter.add_label.assert_called_once_with("SCRUM-3", "needs-human")


# --- Jira Webhook: malformed payloads ---


async def test_jira_webhook_rejects_non_json_body() -> None:
    """Non-JSON body should return 422."""
    async with await _make_client() as client:
        response = await client.post(
            "/webhooks/jira",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422


# --- Background task enqueuing ---


async def test_jira_webhook_enqueues_background_task() -> None:
    """Verify that _process_ticket is scheduled as a background task."""
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())

    with patch.object(main, "_process_ticket", new_callable=AsyncMock) as mock_process:
        async with await _make_client() as client:
            response = await client.post("/webhooks/jira", json=payload)
            assert response.status_code == 202

        # Background tasks run after response in test transport
        mock_process.assert_called_once()
        ticket = mock_process.call_args[0][0]
        assert ticket.id == "ACME-42"
        assert ticket.source == "jira"


async def test_manual_process_enqueues_background_task() -> None:
    """Verify that manual endpoint also enqueues the background task."""
    ticket_data = {
        "source": "jira",
        "id": "TEST-99",
        "ticket_type": "bug",
        "title": "Background task test",
        "description": "Verify enqueuing",
        "acceptance_criteria": [],
    }

    with patch.object(main, "_process_ticket", new_callable=AsyncMock) as mock_process:
        async with await _make_client() as client:
            response = await client.post("/api/process-ticket", json=ticket_data)
            assert response.status_code == 202

        mock_process.assert_called_once()
        ticket = mock_process.call_args[0][0]
        assert ticket.id == "TEST-99"


# --- HMAC edge cases ---


async def test_jira_webhook_rejects_signature_without_prefix() -> None:
    """A raw hex signature (no sha256= prefix) should still be validated correctly."""
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())
    body = json.dumps(payload).encode()
    secret = "test-secret-456"
    raw_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = secret
        mock_settings.jira_base_url = "https://test.atlassian.net"
        mock_settings.jira_api_token = "token"
        mock_settings.jira_user_email = "bot@test.com"
        mock_settings.jira_ac_field_id = "customfield_10429"

        async with await _make_client() as client:
            # Send signature without "sha256=" prefix -- should still pass
            # because removeprefix is a no-op when prefix is absent
            response = await client.post(
                "/webhooks/jira",
                content=body,
                headers={"Content-Type": "application/json", "x-hub-signature": raw_hex},
            )
            assert response.status_code == 202
