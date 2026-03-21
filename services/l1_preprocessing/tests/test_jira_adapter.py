"""Tests for the Jira adapter — normalization and write-back operations."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.jira_adapter import JiraAdapter
from config import Settings
from models import TicketSource, TicketType

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        jira_base_url="https://acme.atlassian.net",
        jira_api_token="test-token",
        jira_user_email="bot@acme.com",
        jira_ac_field_id="customfield_10429",
    )


@pytest.fixture
def mock_client() -> httpx.AsyncClient:
    """A mock httpx client that returns success for all requests."""
    client = AsyncMock(spec=httpx.AsyncClient)
    # httpx.Response needs a request object for raise_for_status() to work
    dummy_request = httpx.Request("GET", "https://test")
    ok_response = httpx.Response(200, json={}, request=dummy_request)
    client.post.return_value = ok_response
    client.put.return_value = ok_response
    client.get.return_value = httpx.Response(
        200,
        json={"transitions": [{"id": "31", "name": "Needs Clarification"}]},
        request=dummy_request,
    )
    return client


@pytest.fixture
def adapter(settings: Settings, mock_client: httpx.AsyncClient) -> JiraAdapter:
    return JiraAdapter(settings=settings, client=mock_client)


def load_fixture(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


# --- Normalization tests ---


class TestNormalize:
    def test_story_webhook(self, adapter: JiraAdapter) -> None:
        payload = load_fixture("jira_webhook_story.json")
        ticket = adapter.normalize(payload)

        assert ticket.source == TicketSource.JIRA
        assert ticket.id == "ACME-42"
        assert ticket.ticket_type == TicketType.STORY
        assert ticket.title == "Add user profile page with avatar upload"
        assert "avatar" in ticket.description.lower()
        assert ticket.priority == "High"
        assert ticket.assignee == "dev@acme.com"
        assert "ai-implement" in ticket.labels
        assert len(ticket.acceptance_criteria) == 5
        assert ticket.acceptance_criteria[0] == "User can view their current profile information"
        assert ticket.acceptance_criteria[2] == "Avatar must be resized to 256x256"

    def test_story_attachments(self, adapter: JiraAdapter) -> None:
        payload = load_fixture("jira_webhook_story.json")
        ticket = adapter.normalize(payload)

        assert len(ticket.attachments) == 1
        assert ticket.attachments[0].filename == "profile-mockup.png"
        assert ticket.attachments[0].content_type == "image/png"

    def test_story_linked_items(self, adapter: JiraAdapter) -> None:
        payload = load_fixture("jira_webhook_story.json")
        ticket = adapter.normalize(payload)

        assert len(ticket.linked_items) == 1
        assert ticket.linked_items[0].id == "ACME-50"
        assert ticket.linked_items[0].relationship == "Blocks"
        assert ticket.linked_items[0].title == "Release 2.1"

    def test_bug_webhook(self, adapter: JiraAdapter) -> None:
        payload = load_fixture("jira_webhook_bug.json")
        ticket = adapter.normalize(payload)

        assert ticket.id == "ACME-99"
        assert ticket.ticket_type == TicketType.BUG
        assert ticket.priority == "Critical"
        assert ticket.assignee == ""  # null assignee
        assert ticket.acceptance_criteria == []  # empty AC field
        assert ticket.attachments == []
        assert ticket.linked_items == []

    def test_callback_config(self, adapter: JiraAdapter) -> None:
        payload = load_fixture("jira_webhook_story.json")
        ticket = adapter.normalize(payload)

        assert ticket.callback is not None
        assert ticket.callback.base_url == "https://acme.atlassian.net"
        assert ticket.callback.ticket_id == "ACME-42"
        assert ticket.callback.source == TicketSource.JIRA

    def test_raw_payload_preserved(self, adapter: JiraAdapter) -> None:
        payload = load_fixture("jira_webhook_story.json")
        ticket = adapter.normalize(payload)
        assert ticket.raw_payload == payload

    def test_unknown_issue_type_defaults_to_task(self, adapter: JiraAdapter) -> None:
        payload = {
            "issue": {
                "key": "X-1",
                "fields": {
                    "summary": "Unknown type",
                    "issuetype": {"name": "Epic"},
                },
            }
        }
        ticket = adapter.normalize(payload)
        assert ticket.ticket_type == TicketType.TASK

    def test_minimal_payload(self, adapter: JiraAdapter) -> None:
        """Adapter handles a payload with missing optional fields."""
        payload = {
            "issue": {
                "key": "MIN-1",
                "fields": {
                    "summary": "Minimal ticket",
                    "issuetype": {"name": "Task"},
                },
            }
        }
        ticket = adapter.normalize(payload)
        assert ticket.id == "MIN-1"
        assert ticket.title == "Minimal ticket"
        assert ticket.acceptance_criteria == []
        assert ticket.attachments == []
        assert ticket.labels == []


# --- Acceptance criteria parsing ---


class TestParseAcceptanceCriteria:
    def test_bullet_dashes(self) -> None:
        raw = "- First item\n- Second item\n- Third item"
        result = JiraAdapter._parse_acceptance_criteria(raw)
        assert result == ["First item", "Second item", "Third item"]

    def test_bullet_asterisks(self) -> None:
        raw = "* Alpha\n* Beta"
        result = JiraAdapter._parse_acceptance_criteria(raw)
        assert result == ["Alpha", "Beta"]

    def test_numbered(self) -> None:
        raw = "1. First\n2. Second\n3. Third"
        result = JiraAdapter._parse_acceptance_criteria(raw)
        assert result == ["First", "Second", "Third"]

    def test_plain_lines(self) -> None:
        raw = "Login works\nLogout works"
        result = JiraAdapter._parse_acceptance_criteria(raw)
        assert result == ["Login works", "Logout works"]

    def test_empty_string(self) -> None:
        assert JiraAdapter._parse_acceptance_criteria("") == []

    def test_whitespace_only(self) -> None:
        assert JiraAdapter._parse_acceptance_criteria("   \n  \n ") == []

    def test_mixed_with_blank_lines(self) -> None:
        raw = "- Item one\n\n- Item two\n\n"
        result = JiraAdapter._parse_acceptance_criteria(raw)
        assert result == ["Item one", "Item two"]

    def test_double_digit_numbered(self) -> None:
        raw = "10. Tenth item\n11. Eleventh item"
        result = JiraAdapter._parse_acceptance_criteria(raw)
        assert result == ["Tenth item", "Eleventh item"]


# --- ADF (Atlassian Document Format) handling ---


class TestAdfHandling:
    def test_plain_string_passes_through(self) -> None:
        assert JiraAdapter._extract_text("hello world") == "hello world"

    def test_empty_string(self) -> None:
        assert JiraAdapter._extract_text("") == ""

    def test_none_returns_empty(self) -> None:
        assert JiraAdapter._extract_text(None) == ""

    def test_adf_paragraph(self) -> None:
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello world"}],
                }
            ],
        }
        result = JiraAdapter._extract_text(adf)
        assert "Hello world" in result

    def test_adf_bullet_list(self) -> None:
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item one"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item two"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        result = JiraAdapter._extract_text(adf)
        assert "- Item one" in result
        assert "- Item two" in result

    def test_adf_mixed_content(self) -> None:
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Description text"}],
                },
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Bullet"}],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        result = JiraAdapter._extract_text(adf)
        assert "Description text" in result
        assert "- Bullet" in result


# --- Write-back operations ---


class TestWriteBack:
    async def test_write_comment(self, adapter: JiraAdapter, mock_client: AsyncMock) -> None:
        await adapter.write_comment("ACME-42", "AI analysis complete")
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "/rest/api/3/issue/ACME-42/comment" in call_args[0][0]

    async def test_update_fields(self, adapter: JiraAdapter, mock_client: AsyncMock) -> None:
        await adapter.update_fields("ACME-42", {"summary": "Updated title"})
        mock_client.put.assert_called_once()
        call_args = mock_client.put.call_args
        assert "/rest/api/3/issue/ACME-42" in call_args[0][0]
        assert call_args[1]["json"] == {"fields": {"summary": "Updated title"}}

    async def test_transition_status(self, adapter: JiraAdapter, mock_client: AsyncMock) -> None:
        await adapter.transition_status("ACME-42", "Needs Clarification")
        # Should have called GET (to list transitions) then POST (to execute)
        mock_client.get.assert_called_once()
        assert mock_client.post.call_count == 1
        post_args = mock_client.post.call_args
        assert post_args[1]["json"] == {"transition": {"id": "31"}}

    async def test_transition_not_found(
        self, adapter: JiraAdapter, mock_client: AsyncMock
    ) -> None:
        """When the target status doesn't exist, log warning and don't crash."""
        await adapter.transition_status("ACME-42", "Nonexistent Status")
        # GET was called to fetch transitions, but POST was NOT called
        mock_client.get.assert_called_once()
        mock_client.post.assert_not_called()

    async def test_add_label(self, adapter: JiraAdapter, mock_client: AsyncMock) -> None:
        await adapter.add_label("ACME-42", "needs-splitting")
        mock_client.put.assert_called_once()
        call_args = mock_client.put.call_args
        assert call_args[1]["json"] == {"update": {"labels": [{"add": "needs-splitting"}]}}
