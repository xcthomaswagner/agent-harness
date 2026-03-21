"""Tests for the Azure DevOps adapter — normalization and write-back operations."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.ado_adapter import AdoAdapter
from config import Settings
from models import TicketSource, TicketType

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        ado_org_url="https://dev.azure.com/acme",
        ado_pat="test-pat",
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock(spec=httpx.AsyncClient)
    dummy_request = httpx.Request("GET", "https://test")
    ok_response = httpx.Response(200, json={}, request=dummy_request)
    client.post.return_value = ok_response
    client.patch.return_value = ok_response
    client.get.return_value = httpx.Response(
        200,
        json={"fields": {"System.Tags": "existing-tag"}},
        request=dummy_request,
    )
    return client


@pytest.fixture
def adapter(settings: Settings, mock_client: AsyncMock) -> AdoAdapter:
    return AdoAdapter(settings=settings, client=mock_client)


def load_fixture(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


# --- Normalization ---


class TestNormalize:
    def test_story_webhook(self, adapter: AdoAdapter) -> None:
        payload = load_fixture("ado_webhook_story.json")
        ticket = adapter.normalize(payload)

        assert ticket.source == TicketSource.ADO
        assert ticket.id == "AcmeProject-42"
        assert ticket.ticket_type == TicketType.STORY
        assert ticket.title == "Add user profile page with avatar upload"
        assert "view and edit" in ticket.description
        assert ticket.priority == "P2"
        assert ticket.assignee == "dev@acme.com"
        assert "ai-implement" in ticket.labels
        assert "sprint-7" in ticket.labels

    def test_acceptance_criteria_parsed(self, adapter: AdoAdapter) -> None:
        payload = load_fixture("ado_webhook_story.json")
        ticket = adapter.normalize(payload)

        assert len(ticket.acceptance_criteria) == 3
        assert ticket.acceptance_criteria[0] == "User can view their current profile information"
        assert ticket.acceptance_criteria[2] == "Avatar must be resized to 256x256"

    def test_linked_items(self, adapter: AdoAdapter) -> None:
        payload = load_fixture("ado_webhook_story.json")
        ticket = adapter.normalize(payload)

        assert len(ticket.linked_items) == 1
        assert ticket.linked_items[0].id == "50"
        assert ticket.linked_items[0].relationship == "Successor"

    def test_attachments(self, adapter: AdoAdapter) -> None:
        payload = load_fixture("ado_webhook_story.json")
        ticket = adapter.normalize(payload)

        assert len(ticket.attachments) == 1
        assert ticket.attachments[0].filename == "profile-mockup.png"

    def test_callback_config(self, adapter: AdoAdapter) -> None:
        payload = load_fixture("ado_webhook_story.json")
        ticket = adapter.normalize(payload)

        assert ticket.callback is not None
        assert ticket.callback.source == TicketSource.ADO
        assert ticket.callback.ticket_id == "42"

    def test_description_html_stripped(self, adapter: AdoAdapter) -> None:
        payload = load_fixture("ado_webhook_story.json")
        ticket = adapter.normalize(payload)

        assert "<p>" not in ticket.description
        assert "<ul>" not in ticket.description
        assert "view and edit" in ticket.description

    def test_unknown_type_defaults_to_task(self, adapter: AdoAdapter) -> None:
        payload = {
            "resource": {
                "id": 1,
                "fields": {
                    "System.WorkItemType": "Feature",
                    "System.Title": "Unknown type",
                },
            }
        }
        ticket = adapter.normalize(payload)
        assert ticket.ticket_type == TicketType.TASK

    def test_bug_type_mapping(self, adapter: AdoAdapter) -> None:
        payload = {
            "resource": {
                "id": 2,
                "fields": {
                    "System.WorkItemType": "Bug",
                    "System.Title": "A bug",
                },
            }
        }
        ticket = adapter.normalize(payload)
        assert ticket.ticket_type == TicketType.BUG

    def test_minimal_payload(self, adapter: AdoAdapter) -> None:
        payload = {
            "resource": {
                "id": 99,
                "fields": {
                    "System.Title": "Minimal",
                    "System.WorkItemType": "Task",
                },
            }
        }
        ticket = adapter.normalize(payload)
        assert ticket.id == "99"
        assert ticket.title == "Minimal"
        assert ticket.acceptance_criteria == []
        assert ticket.labels == []

    def test_empty_tags(self, adapter: AdoAdapter) -> None:
        payload = {
            "resource": {
                "id": 5,
                "fields": {
                    "System.Title": "No tags",
                    "System.WorkItemType": "Task",
                    "System.Tags": "",
                },
            }
        }
        ticket = adapter.normalize(payload)
        assert ticket.labels == []


# --- HTML parsing ---


class TestParseHtmlCriteria:
    def test_li_items(self) -> None:
        html = "<ul><li>First</li><li>Second</li><li>Third</li></ul>"
        result = AdoAdapter._parse_html_criteria(html)
        assert result == ["First", "Second", "Third"]

    def test_br_separated(self) -> None:
        html = "First criterion<br/>Second criterion<br>Third"
        result = AdoAdapter._parse_html_criteria(html)
        assert result == ["First criterion", "Second criterion", "Third"]

    def test_empty_html(self) -> None:
        assert AdoAdapter._parse_html_criteria("") == []

    def test_nested_html_in_li(self) -> None:
        html = "<ul><li><b>Bold</b> item</li><li>Plain item</li></ul>"
        result = AdoAdapter._parse_html_criteria(html)
        assert result == ["Bold item", "Plain item"]


class TestStripHtml:
    def test_basic_strip(self) -> None:
        assert AdoAdapter._strip_html("<p>Hello</p>") == "Hello"

    def test_br_to_newline(self) -> None:
        assert "line1\nline2" in AdoAdapter._strip_html("line1<br/>line2")

    def test_entities(self) -> None:
        assert AdoAdapter._strip_html("A &amp; B &lt; C") == "A & B < C"

    def test_empty(self) -> None:
        assert AdoAdapter._strip_html("") == ""


# --- Write-back ---


class TestWriteBack:
    async def test_write_comment(self, adapter: AdoAdapter, mock_client: AsyncMock) -> None:
        await adapter.write_comment("AcmeProject-42", "Analysis complete")
        mock_client.post.assert_called_once()
        call_url = mock_client.post.call_args[0][0]
        assert "/AcmeProject/_apis/wit/workItems/42/comments" in call_url

    async def test_update_fields(self, adapter: AdoAdapter, mock_client: AsyncMock) -> None:
        await adapter.update_fields("AcmeProject-42", {"System.Title": "Updated"})
        mock_client.patch.assert_called_once()
        call_args = mock_client.patch.call_args
        assert "/AcmeProject/_apis/wit/workItems/42" in call_args[0][0]
        patch_ops = call_args[1]["json"]
        assert patch_ops[0]["op"] == "replace"
        assert patch_ops[0]["path"] == "/fields/System.Title"

    async def test_transition_status(self, adapter: AdoAdapter, mock_client: AsyncMock) -> None:
        await adapter.transition_status("AcmeProject-42", "Active")
        mock_client.patch.assert_called_once()

    async def test_add_label(self, adapter: AdoAdapter, mock_client: AsyncMock) -> None:
        await adapter.add_label("AcmeProject-42", "needs-splitting")
        # Should GET current tags, then PATCH with appended tag
        mock_client.get.assert_called_once()
        mock_client.patch.assert_called_once()
        patch_ops = mock_client.patch.call_args[1]["json"]
        # Should append to existing tags
        assert "existing-tag; needs-splitting" in str(patch_ops)
