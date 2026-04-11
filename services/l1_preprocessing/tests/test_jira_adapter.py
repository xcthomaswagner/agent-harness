"""Tests for the Jira adapter — normalization, write-back, and attachment download."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from adapters.jira_adapter import JiraAdapter
from config import Settings
from models import Attachment, TicketSource, TicketType

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

    def test_linked_items_tolerate_explicit_null_outward_side(
        self, adapter: JiraAdapter
    ) -> None:
        """Bug regression: Jira can emit ``"outwardIssue": null`` for
        some link shapes, which made ``link.get("outwardIssue",
        link.get("inwardIssue", {}))`` return None — then ``.get(
        "key")`` crashed with AttributeError. The filter at the end
        of the comprehension used truthiness (``or``) and passed the
        link through to the extractor, which exploded on the
        explicit null side. Fix uses ``or`` to resolve the side
        correctly and coerces type/fields through ``or {}``."""
        payload = load_fixture("jira_webhook_story.json")
        # Overwrite the issuelinks with a shape where outwardIssue is
        # explicitly null and only inwardIssue is populated.
        payload["issue"]["fields"]["issuelinks"] = [
            {
                "type": {"name": "Blocks"},
                "outwardIssue": None,
                "inwardIssue": {
                    "key": "ACME-50",
                    "fields": {"summary": "Release 2.1"},
                },
            }
        ]
        ticket = adapter.normalize(payload)
        assert len(ticket.linked_items) == 1
        assert ticket.linked_items[0].id == "ACME-50"
        assert ticket.linked_items[0].title == "Release 2.1"

    def test_linked_items_tolerate_null_type(
        self, adapter: JiraAdapter
    ) -> None:
        """Belt-and-braces: an explicit null ``type`` must not crash."""
        payload = load_fixture("jira_webhook_story.json")
        payload["issue"]["fields"]["issuelinks"] = [
            {
                "type": None,
                "outwardIssue": {
                    "key": "ACME-99",
                    "fields": {"summary": "linked"},
                },
            }
        ]
        ticket = adapter.normalize(payload)
        assert len(ticket.linked_items) == 1
        assert ticket.linked_items[0].id == "ACME-99"
        assert ticket.linked_items[0].relationship == ""

    def test_linked_items_skip_entry_with_both_sides_null(
        self, adapter: JiraAdapter
    ) -> None:
        """A link with both sides null is skipped entirely instead of
        raising — the previous truthiness filter would also skip but
        the new code does it explicitly via the helper."""
        payload = load_fixture("jira_webhook_story.json")
        payload["issue"]["fields"]["issuelinks"] = [
            {
                "type": {"name": "Blocks"},
                "outwardIssue": None,
                "inwardIssue": None,
            }
        ]
        ticket = adapter.normalize(payload)
        assert ticket.linked_items == []

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


    def test_adf_code_block(self) -> None:
        adf = {
            "type": "doc", "version": 1,
            "content": [{
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [{"type": "text", "text": "print('hello')"}],
            }],
        }
        result = JiraAdapter._extract_text(adf)
        assert "```python" in result
        assert "print('hello')" in result

    def test_adf_blockquote(self) -> None:
        adf = {
            "type": "doc", "version": 1,
            "content": [{
                "type": "blockquote",
                "content": [{
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Quoted text"}],
                }],
            }],
        }
        result = JiraAdapter._extract_text(adf)
        assert "> " in result
        assert "Quoted text" in result

    def test_adf_mention(self) -> None:
        adf = {
            "type": "doc", "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Assigned to "},
                    {"type": "mention", "attrs": {"text": "jdoe"}},
                ],
            }],
        }
        result = JiraAdapter._extract_text(adf)
        assert "@jdoe" in result

    def test_adf_rule(self) -> None:
        adf = {
            "type": "doc", "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Above"}]},
                {"type": "rule"},
                {"type": "paragraph", "content": [{"type": "text", "text": "Below"}]},
            ],
        }
        result = JiraAdapter._extract_text(adf)
        assert "---" in result


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


# --- Attachment download ---


class TestUploadAttachment:
    async def test_uploads_file(
        self, adapter: JiraAdapter, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        img = tmp_path / "screenshot.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 50)

        # Mock the upload client context
        with patch("adapters.jira_adapter.httpx.AsyncClient") as mock_cls:
            upload_client = AsyncMock()
            dummy_req = httpx.Request("POST", "https://test")
            upload_client.post.return_value = httpx.Response(
                200, json=[{"id": "1"}], request=dummy_req,
            )
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = upload_client
            mock_ctx.__aexit__.return_value = None
            mock_cls.return_value = mock_ctx

            await adapter.upload_attachment("ACME-42", str(img))

            upload_client.post.assert_called_once()
            call_args = upload_client.post.call_args
            assert "/attachments" in call_args[0][0]
            assert "file" in call_args[1]["files"]

    async def test_skips_missing_file(
        self, adapter: JiraAdapter
    ) -> None:
        # Should not raise, just log warning
        await adapter.upload_attachment("ACME-42", "/nonexistent/file.png")

    async def test_custom_filename(
        self, adapter: JiraAdapter, tmp_path: Path
    ) -> None:
        img = tmp_path / "raw.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 10)

        with patch("adapters.jira_adapter.httpx.AsyncClient") as mock_cls:
            upload_client = AsyncMock()
            dummy_req = httpx.Request("POST", "https://test")
            upload_client.post.return_value = httpx.Response(
                200, json=[{"id": "1"}], request=dummy_req,
            )
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = upload_client
            mock_ctx.__aexit__.return_value = None
            mock_cls.return_value = mock_ctx

            await adapter.upload_attachment(
                "ACME-42", str(img), filename="ACME-42-implementation.png"
            )

            call_args = upload_client.post.call_args
            file_tuple = call_args[1]["files"]["file"]
            assert file_tuple[0] == "ACME-42-implementation.png"


class TestDownloadAttachment:
    @pytest.fixture
    def stream_client(self) -> AsyncMock:
        """Mock httpx client with streaming support."""
        client = AsyncMock(spec=httpx.AsyncClient)
        dummy_request = httpx.Request("GET", "https://test")
        ok_response = httpx.Response(200, json={}, request=dummy_request)
        client.post.return_value = ok_response
        client.put.return_value = ok_response
        client.get.return_value = httpx.Response(
            200,
            json={"transitions": []},
            request=dummy_request,
        )
        return client

    @pytest.fixture
    def stream_adapter(self, settings: Settings, stream_client: AsyncMock) -> JiraAdapter:
        return JiraAdapter(settings=settings, client=stream_client)

    def _setup_stream(
        self, client: AsyncMock, data: bytes, content_length: str | None = None
    ) -> None:
        """Configure the mock client to stream given data."""
        stream_ctx = AsyncMock()
        stream_response = AsyncMock()
        headers = {}
        if content_length is not None:
            headers["content-length"] = content_length
        stream_response.headers = headers
        stream_response.raise_for_status = AsyncMock()

        async def aiter_bytes():
            yield data

        stream_response.aiter_bytes = aiter_bytes
        stream_ctx.__aenter__.return_value = stream_response
        stream_ctx.__aexit__.return_value = None
        client.stream.return_value = stream_ctx

    async def test_downloads_image(
        self, stream_adapter: JiraAdapter, stream_client: AsyncMock, tmp_path: Path
    ) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        self._setup_stream(stream_client, image_bytes, str(len(image_bytes)))

        att = Attachment(
            filename="mockup.png",
            url="https://acme.atlassian.net/secure/attachment/123/mockup.png",
            content_type="image/png",
        )
        result = await stream_adapter.download_attachment(att, str(tmp_path))

        assert result.local_path != ""
        assert Path(result.local_path).exists()
        assert Path(result.local_path).read_bytes() == image_bytes

    async def test_skips_empty_url(
        self, stream_adapter: JiraAdapter, tmp_path: Path
    ) -> None:
        att = Attachment(filename="empty.png", url="", content_type="image/png")
        result = await stream_adapter.download_attachment(att, str(tmp_path))
        assert result.local_path == ""

    async def test_skips_too_large_content_length(
        self, stream_adapter: JiraAdapter, stream_client: AsyncMock, tmp_path: Path
    ) -> None:
        self._setup_stream(stream_client, b"x", str(10 * 1024 * 1024))

        att = Attachment(
            filename="huge.png",
            url="https://acme.atlassian.net/secure/attachment/999/huge.png",
            content_type="image/png",
        )
        result = await stream_adapter.download_attachment(att, str(tmp_path))
        assert result.local_path == ""

    async def test_skips_too_large_streaming(
        self, stream_adapter: JiraAdapter, stream_client: AsyncMock, tmp_path: Path
    ) -> None:
        # No content-length header, but data exceeds limit during streaming
        big_data = b"\x00" * (6 * 1024 * 1024)
        self._setup_stream(stream_client, big_data, None)

        att = Attachment(
            filename="big.png",
            url="https://acme.atlassian.net/secure/attachment/999/big.png",
            content_type="image/png",
        )
        result = await stream_adapter.download_attachment(att, str(tmp_path))
        assert result.local_path == ""

    async def test_handles_http_error(
        self, stream_adapter: JiraAdapter, stream_client: AsyncMock, tmp_path: Path
    ) -> None:
        stream_ctx = AsyncMock()
        stream_ctx.__aenter__.side_effect = httpx.HTTPError("connection failed")
        stream_ctx.__aexit__.return_value = None
        stream_client.stream.return_value = stream_ctx

        att = Attachment(
            filename="fail.png",
            url="https://acme.atlassian.net/secure/attachment/404/fail.png",
            content_type="image/png",
        )
        result = await stream_adapter.download_attachment(att, str(tmp_path))
        assert result.local_path == ""

    @pytest.mark.parametrize(
        "evil_filename",
        [
            "..",
            ".",
            "",
            "has\x00nulbyte.png",
        ],
    )
    async def test_rejects_unnormalizable_filename(
        self,
        stream_adapter: JiraAdapter,
        stream_client: AsyncMock,
        tmp_path: Path,
        evil_filename: str,
    ) -> None:
        """Bug regression: filenames that can't be sanitized to a
        legitimate basename (empty, ``.``, ``..``, NUL-bearing) must
        be rejected outright — no file is written, the returned
        Attachment has no local_path, and nothing escapes tmp_path."""
        image_bytes = b"\x89PNG" + b"\x00" * 50
        self._setup_stream(stream_client, image_bytes, str(len(image_bytes)))

        # Plant a file in the parent directory that a successful traversal
        # would overwrite. It must stay intact after the call.
        parent_canary = tmp_path.parent / "canary.txt"
        parent_canary.write_bytes(b"original")

        att = Attachment(
            filename=evil_filename,
            url="https://acme.atlassian.net/secure/attachment/1/x.png",
            content_type="image/png",
        )
        result = await stream_adapter.download_attachment(att, str(tmp_path))

        assert result.local_path == "", (
            f"evil filename {evil_filename!r} must be rejected"
        )
        # Nothing written outside tmp_path.
        assert parent_canary.read_bytes() == b"original"
        # No files created inside tmp_path either.
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "traversal_input,expected_basename",
        [
            ("../../tmp/pwn.sh", "pwn.sh"),
            ("../../../etc/cron.d/payload", "payload"),
            ("/etc/passwd", "passwd"),
            ("/tmp/absolute-path", "absolute-path"),
            ("subdir/legit.png", "legit.png"),
        ],
    )
    async def test_strips_path_components_to_basename(
        self,
        stream_adapter: JiraAdapter,
        stream_client: AsyncMock,
        tmp_path: Path,
        traversal_input: str,
        expected_basename: str,
    ) -> None:
        """Bug regression: untrusted ``filename`` values with path
        components (``../foo``, absolute paths, subdirs) used to land
        directly in ``dest / filename`` so the write escaped tmp_path.
        Fixed by taking the basename — the write lands SAFELY inside
        dest with just the last path component, and nothing is ever
        written outside tmp_path. This is the correct safe behavior:
        the legitimate case of ``subdir/legit.png`` still succeeds,
        the traversal case of ``../../tmp/pwn.sh`` becomes
        ``tmp_path/pwn.sh`` instead of ``/tmp/pwn.sh``."""
        image_bytes = b"\x89PNG" + b"\x00" * 50
        self._setup_stream(stream_client, image_bytes, str(len(image_bytes)))

        # Plant a canary outside tmp_path — must stay intact.
        parent_canary = tmp_path.parent / expected_basename
        parent_canary.write_bytes(b"original-canary")

        att = Attachment(
            filename=traversal_input,
            url="https://acme.atlassian.net/secure/attachment/1/x.png",
            content_type="image/png",
        )
        result = await stream_adapter.download_attachment(att, str(tmp_path))

        # The write succeeded — but INSIDE tmp_path with the basename.
        assert result.local_path != ""
        written = Path(result.local_path)
        assert written.name == expected_basename
        assert written.parent == tmp_path
        # Canary outside tmp_path was NOT touched.
        assert parent_canary.read_bytes() == b"original-canary"


class TestDownloadImageAttachments:
    async def test_downloads_only_images(self, settings: Settings, tmp_path: Path) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)

        # Set up stream for image downloads
        image_bytes = b"\x89PNG" + b"\x00" * 50
        stream_ctx = AsyncMock()
        stream_response = AsyncMock()
        stream_response.headers = {"content-length": str(len(image_bytes))}
        stream_response.raise_for_status = AsyncMock()

        async def aiter_bytes():
            yield image_bytes

        stream_response.aiter_bytes = aiter_bytes
        stream_ctx.__aenter__.return_value = stream_response
        stream_ctx.__aexit__.return_value = None
        client.stream.return_value = stream_ctx

        adapter = JiraAdapter(settings=settings, client=client)

        attachments = [
            Attachment(filename="design.png", url="https://jira/att/1", content_type="image/png"),
            Attachment(
                filename="spec.pdf", url="https://jira/att/2",
                content_type="application/pdf",
            ),
            Attachment(filename="photo.jpg", url="https://jira/att/3", content_type="image/jpeg"),
        ]

        result = await adapter.download_image_attachments(attachments, str(tmp_path))

        assert len(result) == 3
        assert result[0].local_path != ""  # PNG downloaded
        assert result[1].local_path == ""  # PDF skipped
        assert result[2].local_path != ""  # JPEG downloaded


def test_jira_adapter_satisfies_ticket_writeback_protocol(
    settings: Settings,
) -> None:
    """Protocol regression: JiraAdapter must conform to
    TicketWriteBackAdapter so pipeline._get_adapter's return type
    stays honest. The @runtime_checkable protocol lets us verify this
    with isinstance without touching the adapter class definition."""
    from adapters.base import TicketWriteBackAdapter

    adapter = JiraAdapter(settings=settings)
    assert isinstance(adapter, TicketWriteBackAdapter)
    # And the three required methods are async coroutines.
    import inspect
    assert inspect.iscoroutinefunction(adapter.write_comment)
    assert inspect.iscoroutinefunction(adapter.transition_status)
    assert inspect.iscoroutinefunction(adapter.add_label)
