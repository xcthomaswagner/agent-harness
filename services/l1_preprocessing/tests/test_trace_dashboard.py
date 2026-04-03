"""Tests for trace dashboard — HTML rendering, XSS, API endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from trace_dashboard import _classify_traces, _escape


class TestEscape:
    def test_escapes_html_tags(self) -> None:
        assert "<script>" not in _escape("<script>alert('xss')</script>")
        assert "&lt;script&gt;" in _escape("<script>alert('xss')</script>")

    def test_escapes_quotes(self) -> None:
        assert "&quot;" in _escape('onclick="evil()"')

    def test_escapes_ampersand(self) -> None:
        assert "&amp;" in _escape("foo&bar")

    def test_plain_text_unchanged(self) -> None:
        assert _escape("hello world") == "hello world"

    def test_empty_string(self) -> None:
        assert _escape("") == ""


class TestTracesListEndpoint:
    @pytest.fixture
    def mock_traces(self) -> list[dict]:
        return [
            {
                "ticket_id": "T-1",
                "trace_id": "abc",
                "started_at": "2026-01-01T00:00:00Z",
                "run_started_at": "2026-01-01T00:00:00Z",
                "completed_at": "2026-01-01T00:10:00Z",
                "duration": "10m 0s",
                "status": "Complete",
                "pr_url": "https://github.com/test/pr/1",
                "review_verdict": "APPROVED",
                "qa_result": "PASS",
                "pipeline_mode": "simple",
                "phases": 5,
                "entries": 8,
            }
        ]

    async def test_returns_html(self, mock_traces: list) -> None:
        with patch("trace_dashboard.list_traces", return_value=mock_traces):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "T-1" in resp.text

    async def test_xss_in_ticket_id(self) -> None:
        evil_traces = [{
            "ticket_id": '<script>alert("xss")</script>',
            "trace_id": "x",
            "started_at": "2026-01-01",
            "run_started_at": "2026-01-01",
            "completed_at": "2026-01-01",
            "duration": "",
            "status": "Complete",
            "pr_url": "",
            "review_verdict": "",
            "qa_result": "",
            "pipeline_mode": "",
            "phases": 1,
            "entries": 1,
        }]
        with patch("trace_dashboard.list_traces", return_value=evil_traces):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        # The ticket ID should be escaped in the rendered HTML
        assert "&lt;script&gt;alert" in resp.text
        # The raw XSS payload should not appear unescaped (excluding our own JS)
        assert 'alert("xss")' not in resp.text

    async def test_empty_traces(self) -> None:
        with patch("trace_dashboard.list_traces", return_value=[]):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert resp.status_code == 200
        assert "0 tickets" in resp.text

    async def test_pr_filter(self, mock_traces: list) -> None:
        with (
            patch("trace_dashboard.list_traces", return_value=mock_traces),
            patch("trace_dashboard.count_traces", return_value=len(mock_traces)),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces?pr=nonexistent")
        assert resp.status_code == 200
        assert "0 tickets" in resp.text

    async def test_table_view(self, mock_traces: list) -> None:
        with (
            patch("trace_dashboard.list_traces", return_value=mock_traces),
            patch("trace_dashboard.count_traces", return_value=len(mock_traces)),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces?view=table")
        assert resp.status_code == 200
        assert "<table>" in resp.text
        assert "Board view" in resp.text

    async def test_board_view_default(self, mock_traces: list) -> None:
        with patch("trace_dashboard.list_traces", return_value=mock_traces):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert resp.status_code == 200
        assert "Status Board" in resp.text
        assert "Table view" in resp.text

    async def test_board_auto_refresh(self, mock_traces: list) -> None:
        with patch("trace_dashboard.list_traces", return_value=mock_traces):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert 'http-equiv="refresh"' in resp.text


class TestStatusBoardBucketing:
    """Tests for trace classification into in-flight/completed/stuck buckets."""

    def _trace(self, status: str, ts: str = "2026-01-01") -> dict:
        return {
            "status": status,
            "started_at": ts,
            "run_started_at": ts,
        }

    def test_completed_bucket(self) -> None:
        in_flight, completed, stuck = _classify_traces([self._trace("Complete")])
        assert len(completed) == 1
        assert len(in_flight) == 0
        assert len(stuck) == 0

    def test_escalated_goes_to_stuck(self) -> None:
        in_flight, completed, stuck = _classify_traces([self._trace("Escalated")])
        assert len(stuck) == 1
        assert len(in_flight) == 0

    def test_agent_done_no_pr_goes_to_stuck(self) -> None:
        _, _, stuck = _classify_traces([self._trace("Agent Done (no PR)")])
        assert len(stuck) == 1

    def test_dispatched_recent_is_in_flight(self) -> None:
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()
        in_flight, _, stuck = _classify_traces([self._trace("Dispatched", now)])
        assert len(in_flight) == 1
        assert len(stuck) == 0

    def test_dispatched_old_is_stuck(self) -> None:
        old = "2025-01-01T00:00:00Z"
        in_flight, _, stuck = _classify_traces([self._trace("Dispatched", old)])
        assert len(stuck) == 1
        assert len(in_flight) == 0

    def test_implementing_stays_in_flight(self) -> None:
        old = "2025-01-01T00:00:00Z"
        in_flight, _, stuck = _classify_traces([self._trace("Implementing", old)])
        assert len(in_flight) == 1
        assert len(stuck) == 0

    def test_mixed_traces(self) -> None:
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()
        traces = [
            {"status": "Complete", "started_at": "2026-01-01", "run_started_at": "2026-01-01"},
            {"status": "Dispatched", "started_at": now, "run_started_at": now},
            {"status": "Escalated", "started_at": "2026-01-01", "run_started_at": "2026-01-01"},
        ]
        in_flight, completed, stuck = _classify_traces(traces)
        assert len(completed) == 1
        assert len(in_flight) == 1
        assert len(stuck) == 1


class TestTraceDetailEndpoint:
    async def test_returns_timeline(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01T10:00:00Z",
             "phase": "webhook", "event": "received"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:05:00Z",
             "phase": "complete", "event": "Pipeline complete",
             "pr_url": "https://github.com/test/pr/1"},
        ]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/T-1")
        assert resp.status_code == 200
        assert "webhook" in resp.text
        assert "Pipeline complete" in resp.text

    async def test_missing_ticket_returns_404_html(self) -> None:
        with patch("trace_dashboard.read_trace", return_value=[]):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/MISSING-99")
        assert resp.status_code == 200  # Returns HTML 200 with "not found" message
        assert "No trace found" in resp.text

    async def test_xss_in_event_content(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01",
             "phase": "test", "event": '<img onerror="alert(1)">'},
        ]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/XSS-1")
        assert 'onerror="alert' not in resp.text

    async def test_phase_duration_bar(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01T10:00:00Z",
             "phase": "webhook", "event": "jira_webhook_received"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:01:00Z",
             "phase": "ticket_read", "event": "Pipeline started, simple mode",
             "source": "agent"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:06:00Z",
             "phase": "implementation", "event": "Implementation complete",
             "source": "agent"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:08:00Z",
             "phase": "complete", "event": "Pipeline complete",
             "source": "agent"},
        ]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/DUR-1")
        assert resp.status_code == 200
        assert "ticket read" in resp.text
        assert "5m 0s" in resp.text

    async def test_token_display(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01T10:00:00Z",
             "phase": "analyst", "event": "analyst_completed",
             "tokens_in": 1500, "tokens_out": 500},
        ]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/TOK-1")
        assert "1,500 in" in resp.text
        assert "500 out" in resp.text

    async def test_token_na_for_old_data(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01T10:00:00Z",
             "phase": "analyst", "event": "analyst_completed",
             "output_type": "enriched"},
        ]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/OLD-1")
        assert "N/A" in resp.text

    async def test_failure_box_for_escalated(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01T10:00:00Z",
             "phase": "webhook", "event": "received"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:05:00Z",
             "phase": "artifact", "event": "escalation_artifact",
             "content": "## Escalation\nQA failed: 6 of 8 criteria failed"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:05:01Z",
             "phase": "complete", "event": "Escalated"},
        ]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/ESC-1")
        assert "QA failed" in resp.text
        assert "Failure" in resp.text


class TestTracesApiEndpoint:
    async def test_returns_json(self) -> None:
        traces = [{"ticket_id": "A-1", "entries": 3}]
        with (
            patch("trace_dashboard.list_traces", return_value=traces),
            patch("trace_dashboard.count_traces", return_value=1),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/api/traces")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["traces"][0]["ticket_id"] == "A-1"

    async def test_trace_detail_api(self) -> None:
        entries = [{"phase": "webhook", "event": "got it"}]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/api/traces/T-1")
        assert resp.status_code == 200
        assert resp.json()[0]["phase"] == "webhook"


class TestExpandableStuckCards:
    """Tests for expandable diagnostics on stuck cards."""

    async def test_stuck_card_with_error_shows_diagnostics(self) -> None:
        stuck_traces = [{
            "ticket_id": "ERR-1",
            "trace_id": "x",
            "started_at": "2025-01-01T00:00:00Z",
            "run_started_at": "2025-01-01T00:00:00Z",
            "completed_at": "2025-01-01T00:00:05Z",
            "duration": "5s",
            "status": "Escalated",
            "pr_url": "",
            "review_verdict": "",
            "qa_result": "",
            "pipeline_mode": "",
            "phases": 1,
            "entries": 2,
        }]
        error_entries = [
            {"event": "processing_started",
             "timestamp": "2025-01-01T00:00:00Z"},
            {"event": "error", "error_type": "RuntimeError",
             "error_message": "Analyst API rate limited after 3 retries",
             "timestamp": "2025-01-01T00:00:05Z", "phase": "pipeline"},
        ]
        with (
            patch("trace_dashboard.list_traces", return_value=stuck_traces),
            patch("trace_dashboard.read_trace", return_value=error_entries),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert "Diagnostics" in resp.text
        assert "RuntimeError" in resp.text
        assert "rate limit" in resp.text.lower()

    async def test_stuck_card_without_errors_shows_hint(self) -> None:
        stuck_traces = [{
            "ticket_id": "STUCK-1",
            "trace_id": "x",
            "started_at": "2025-01-01T00:00:00Z",
            "run_started_at": "2025-01-01T00:00:00Z",
            "completed_at": "2025-01-01T00:00:05Z",
            "duration": "5s",
            "status": "Dispatched",
            "pr_url": "",
            "review_verdict": "",
            "qa_result": "",
            "pipeline_mode": "",
            "phases": 1,
            "entries": 2,
        }]
        entries = [
            {"event": "processing_started",
             "timestamp": "2025-01-01T00:00:00Z"},
            {"event": "l2_dispatched",
             "timestamp": "2025-01-01T00:00:05Z"},
        ]
        with (
            patch("trace_dashboard.list_traces", return_value=stuck_traces),
            patch("trace_dashboard.read_trace", return_value=entries),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert "Diagnostics" in resp.text
        assert "never reported back" in resp.text

    async def test_error_message_xss_escaped(self) -> None:
        stuck_traces = [{
            "ticket_id": "XSS-ERR",
            "trace_id": "x",
            "started_at": "2025-01-01T00:00:00Z",
            "run_started_at": "2025-01-01T00:00:00Z",
            "completed_at": "2025-01-01T00:00:05Z",
            "duration": "",
            "status": "Escalated",
            "pr_url": "",
            "review_verdict": "",
            "qa_result": "",
            "pipeline_mode": "",
            "phases": 1,
            "entries": 2,
        }]
        entries = [
            {"event": "processing_started",
             "timestamp": "2025-01-01T00:00:00Z"},
            {"event": "error",
             "error_type": '<img onerror="alert(1)">',
             "error_message": '<script>evil()</script>',
             "timestamp": "2025-01-01T00:00:05Z",
             "phase": "pipeline"},
        ]
        with (
            patch("trace_dashboard.list_traces", return_value=stuck_traces),
            patch("trace_dashboard.read_trace", return_value=entries),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert 'onerror="alert' not in resp.text
        assert "&lt;script&gt;evil" in resp.text
