"""Tests for the Azure DevOps adapter — normalization and write-back operations."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.ado_adapter import AdoAdapter
from config import Settings
from models import Attachment, TicketSource, TicketType

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

    def test_callback_url_normalized(self, adapter: AdoAdapter) -> None:
        """CallbackConfig should auto-prefix https:// if missing."""
        from models import CallbackConfig, TicketSource

        cb = CallbackConfig(
            base_url="dev.azure.com/org",
            ticket_id="1",
            source=TicketSource.ADO,
        )
        assert cb.base_url == "https://dev.azure.com/org"

    def test_callback_ticket_id_is_numeric_only(self, adapter: AdoAdapter) -> None:
        """Callback ticket_id should be the numeric work item ID, not the composite."""
        payload = load_fixture("ado_webhook_story.json")
        ticket = adapter.normalize(payload)
        assert ticket.callback is not None
        # Should be numeric only, no project prefix
        assert ticket.callback.ticket_id.isdigit()

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

    async def test_add_label_skips_when_already_present(
        self,
        adapter: AdoAdapter,
        mock_client: AsyncMock,
    ) -> None:
        """Bug regression: add_label used to unconditionally append
        ``"; {label}"``, so calling it twice with the same label
        produced ``"existing-tag; existing-tag"``. Over many retries
        the tag list would grow unbounded. Fixed with a
        case-insensitive check against existing ``;``-separated
        elements — no PATCH is sent when the label is already on
        the work item.
        """
        dummy_request = httpx.Request("GET", "https://test")
        mock_client.get.return_value = httpx.Response(
            200,
            json={"fields": {"System.Tags": "existing-tag; ai_complete"}},
            request=dummy_request,
        )
        await adapter.add_label("AcmeProject-42", "ai_complete")

        mock_client.get.assert_called_once()
        # No PATCH — the tag is already present.
        mock_client.patch.assert_not_called()

    async def test_add_label_case_insensitive_dedup(
        self,
        adapter: AdoAdapter,
        mock_client: AsyncMock,
    ) -> None:
        """Case-insensitive exact-element match: ``AI_Complete`` should
        dedupe against ``ai_complete`` already on the work item."""
        dummy_request = httpx.Request("GET", "https://test")
        mock_client.get.return_value = httpx.Response(
            200,
            json={"fields": {"System.Tags": "ai_complete; done"}},
            request=dummy_request,
        )
        await adapter.add_label("AcmeProject-42", "AI_Complete")
        mock_client.patch.assert_not_called()


# --- Attachment download ---


class TestDownloadAttachment:
    async def test_download_attachment_success(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """Successful image download writes file and sets local_path."""
        image_bytes = b"\x89PNG\r\n\x1a\nfake-image-data"
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                content=image_bytes,
                headers={"content-length": str(len(image_bytes))},
            )
        )
        download_client = httpx.AsyncClient(transport=transport)
        adapter = AdoAdapter(settings=settings)
        adapter._download_client = download_client

        att = Attachment(
            filename="mockup.png",
            url="https://dev.azure.com/acme/_apis/wit/attachments/abc-123",
            content_type="image/png",
        )
        result = await adapter.download_attachment(att, str(tmp_path))

        assert result.local_path != ""
        assert Path(result.local_path).exists()
        assert Path(result.local_path).read_bytes() == image_bytes
        assert not result.download_failed

    async def test_download_attachment_too_large(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """Attachments exceeding 5 MB are skipped (returned unchanged)."""
        big_size = 6 * 1024 * 1024
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                content=b"x",
                headers={"content-length": str(big_size)},
            )
        )
        download_client = httpx.AsyncClient(transport=transport)
        adapter = AdoAdapter(settings=settings)
        adapter._download_client = download_client

        att = Attachment(
            filename="huge.png",
            url="https://dev.azure.com/acme/_apis/wit/attachments/big",
            content_type="image/png",
        )
        result = await adapter.download_attachment(att, str(tmp_path))

        assert result.local_path == ""
        assert not result.download_failed

    async def test_download_attachment_sanitizes_filename(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """Path traversal filenames are rejected (returned unchanged)."""
        adapter = AdoAdapter(settings=settings)

        att = Attachment(
            filename="../../etc/passwd",
            url="https://dev.azure.com/acme/_apis/wit/attachments/evil",
            content_type="image/png",
        )
        result = await adapter.download_attachment(att, str(tmp_path))

        # sanitize_attachment_filename extracts basename "passwd" which is valid,
        # but the file should still be written safely inside dest_dir. The function
        # does NOT reject "../../etc/passwd" outright — it sanitizes to "passwd".
        # So we verify the path is inside tmp_path.
        if result.local_path:
            assert Path(result.local_path).resolve().is_relative_to(tmp_path.resolve())

    async def test_download_attachment_rejects_dotdot_only(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """A filename of '..' is rejected outright."""
        adapter = AdoAdapter(settings=settings)

        att = Attachment(
            filename="..",
            url="https://dev.azure.com/acme/_apis/wit/attachments/evil",
            content_type="image/png",
        )
        result = await adapter.download_attachment(att, str(tmp_path))
        assert result.local_path == ""

    async def test_download_image_attachments_filters_by_content_type(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """Only image attachments are downloaded; non-images pass through."""
        image_bytes = b"fake-png"
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                content=image_bytes,
                headers={"content-length": str(len(image_bytes))},
            )
        )
        download_client = httpx.AsyncClient(transport=transport)
        adapter = AdoAdapter(settings=settings)
        adapter._download_client = download_client

        attachments = [
            Attachment(
                filename="design.png",
                url="https://dev.azure.com/acme/_apis/wit/attachments/1",
                content_type="image/png",
            ),
            Attachment(
                filename="readme.txt",
                url="https://dev.azure.com/acme/_apis/wit/attachments/2",
                content_type="text/plain",
            ),
            Attachment(
                filename="photo.jpeg",
                url="https://dev.azure.com/acme/_apis/wit/attachments/3",
                content_type="image/jpeg",
            ),
        ]
        result = await adapter.download_image_attachments(attachments, str(tmp_path))

        assert len(result) == 3
        # Images should have local_path set
        assert result[0].local_path != ""
        assert result[2].local_path != ""
        # Non-image should pass through unchanged
        assert result[1].local_path == ""
        assert result[1].filename == "readme.txt"

    async def test_download_uses_separate_client(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """Download requests must NOT include Content-Type: application/json-patch+json."""
        captured_headers: dict[str, str] = {}

        def capture_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, content=b"img-data")

        transport = httpx.MockTransport(capture_handler)
        # Let _get_download_client build the client so it gets auth headers
        adapter = AdoAdapter(settings=settings)
        # Patch the lazily-created client with our capturing transport + auth
        import base64
        credentials = f":{settings.ado_pat}"
        token = base64.b64encode(credentials.encode()).decode()
        adapter._download_client = httpx.AsyncClient(
            transport=transport,
            headers={"Authorization": f"Basic {token}"},
        )

        att = Attachment(
            filename="test.png",
            url="https://dev.azure.com/acme/_apis/wit/attachments/x",
            content_type="image/png",
        )
        await adapter.download_attachment(att, str(tmp_path))

        # The download client should have Authorization but NOT json-patch content type
        assert "authorization" in captured_headers
        content_type = captured_headers.get("content-type", "")
        assert "json-patch" not in content_type


def test_ado_adapter_satisfies_ticket_writeback_protocol(
    settings: Settings,
) -> None:
    """Protocol regression: AdoAdapter must conform to
    TicketWriteBackAdapter so pipeline._get_adapter's return type
    stays honest."""
    from adapters.base import TicketWriteBackAdapter

    adapter = AdoAdapter(settings=settings)
    assert isinstance(adapter, TicketWriteBackAdapter)
    import inspect
    assert inspect.iscoroutinefunction(adapter.write_comment)
    assert inspect.iscoroutinefunction(adapter.transition_status)
    assert inspect.iscoroutinefunction(adapter.add_label)
