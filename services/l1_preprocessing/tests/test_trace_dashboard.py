"""Tests for trace dashboard — Langfuse-style views, XSS, filtering, span tree."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from trace_dashboard import _classify_traces
from trace_dashboard import _e as _escape


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
        return [{
            "ticket_id": "T-1", "trace_id": "abc",
            "started_at": "2026-01-01T00:00:00Z",
            "run_started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:10:00Z",
            "duration": "10m 0s", "status": "Complete",
            "pr_url": "https://github.com/test/pr/1",
            "review_verdict": "APPROVED", "qa_result": "PASS",
            "pipeline_mode": "simple", "phases": 5, "entries": 8,
        }]

    async def test_returns_html(self, mock_traces: list) -> None:
        with (
            patch("trace_dashboard.list_traces", return_value=mock_traces),
            patch("trace_dashboard.count_traces", return_value=1),
            patch("trace_dashboard.read_trace", return_value=[]),
        ):
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
            "duration": "", "status": "Complete",
            "pr_url": "", "review_verdict": "", "qa_result": "",
            "pipeline_mode": "", "phases": 1, "entries": 1,
        }]
        with (
            patch("trace_dashboard.list_traces", return_value=evil_traces),
            patch("trace_dashboard.count_traces", return_value=1),
            patch("trace_dashboard.read_trace", return_value=[]),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert "&lt;script&gt;alert" in resp.text
        assert 'alert("xss")' not in resp.text

    async def test_empty_traces(self) -> None:
        with (
            patch("trace_dashboard.list_traces", return_value=[]),
            patch("trace_dashboard.count_traces", return_value=0),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert resp.status_code == 200
        # Stats bar shows 0 total
        assert ">0<" in resp.text

    async def test_table_is_default_view(self, mock_traces: list) -> None:
        with (
            patch("trace_dashboard.list_traces", return_value=mock_traces),
            patch("trace_dashboard.count_traces", return_value=1),
            patch("trace_dashboard.read_trace", return_value=[]),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert "<table>" in resp.text
        assert "Traces" in resp.text

    async def test_board_view(self, mock_traces: list) -> None:
        with patch("trace_dashboard.list_traces", return_value=mock_traces):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces?view=board")
        assert "Status Board" in resp.text
        assert "In-Flight" in resp.text or "Completed" in resp.text

    async def test_auto_refresh(self, mock_traces: list) -> None:
        with (
            patch("trace_dashboard.list_traces", return_value=mock_traces),
            patch("trace_dashboard.count_traces", return_value=1),
            patch("trace_dashboard.read_trace", return_value=[]),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        # Table view uses JS setInterval for soft refresh (preserves scroll/filters)
        assert "setInterval" in resp.text

    async def test_filter_bar_present(self, mock_traces: list) -> None:
        with (
            patch("trace_dashboard.list_traces", return_value=mock_traces),
            patch("trace_dashboard.count_traces", return_value=1),
            patch("trace_dashboard.read_trace", return_value=[]),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces")
        assert "filterTable" in resp.text
        assert "f-status" in resp.text

    async def test_pr_filter(self, mock_traces: list) -> None:
        with (
            patch("trace_dashboard.list_traces", return_value=mock_traces),
            patch("trace_dashboard.count_traces", return_value=1),
            patch("trace_dashboard.read_trace", return_value=[]),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces?pr=nonexistent")
        assert resp.status_code == 200


class TestStatusBoardBucketing:
    """Tests for trace classification into in-flight/completed/stuck."""

    def _trace(self, status: str, ts: str = "2026-01-01") -> dict:
        return {"status": status, "started_at": ts, "run_started_at": ts}

    def test_completed_bucket(self) -> None:
        _, comp, _ = _classify_traces([self._trace("Complete")])
        assert len(comp) == 1

    def test_escalated_goes_to_stuck(self) -> None:
        _, _, stuck = _classify_traces([self._trace("Escalated")])
        assert len(stuck) == 1

    def test_dispatched_recent_is_in_flight(self) -> None:
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()
        in_f, _, _ = _classify_traces([self._trace("Dispatched", now)])
        assert len(in_f) == 1

    def test_dispatched_old_is_stuck(self) -> None:
        _, _, stuck = _classify_traces(
            [self._trace("Dispatched", "2025-01-01T00:00:00Z")]
        )
        assert len(stuck) == 1

    def test_implementing_recent_is_in_flight(self) -> None:
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()
        in_f, _, _ = _classify_traces([self._trace("Implementing", now)])
        assert len(in_f) == 1

    def test_implementing_old_is_stuck(self) -> None:
        _, _, stuck = _classify_traces(
            [self._trace("Implementing", "2025-01-01T00:00:00Z")]
        )
        assert len(stuck) == 1


class TestTraceDetailEndpoint:
    async def test_returns_span_tree(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01T10:00:00Z",
             "phase": "webhook", "event": "jira_webhook_received",
             "source": "jira"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:05:00Z",
             "phase": "implementation", "event": "Implementation complete",
             "source": "agent", "commit": "abc123"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:06:00Z",
             "phase": "complete", "event": "Pipeline complete",
             "source": "agent", "pr_url": "https://github.com/test/pr/1",
             "review_verdict": "APPROVED", "qa_result": "PASS",
             "pipeline_mode": "simple", "units": 1},
        ]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/T-1")
        assert resp.status_code == 200
        # Span tree sections
        assert "L1: Ticket Intake" in resp.text
        assert "L2: Agent Pipeline" in resp.text
        # Summary bar
        assert "APPROVED" in resp.text
        # Raw events section
        assert "Raw Events" in resp.text

    async def test_missing_ticket(self) -> None:
        with patch("trace_dashboard.read_trace", return_value=[]):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/MISSING-99")
        assert resp.status_code == 200
        assert "No trace found" in resp.text

    async def test_xss_in_event(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01",
             "phase": "test", "event": '<img onerror="alert(1)">',
             "source": "agent"},
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
             "phase": "webhook", "event": "jira_webhook_received",
             "source": "jira"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:01:00Z",
             "phase": "ticket_read",
             "event": "Pipeline started, simple mode",
             "source": "agent"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:06:00Z",
             "phase": "implementation",
             "event": "Implementation complete",
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
        assert "5m 0s" in resp.text
        assert "ticket read" in resp.text

    async def test_artifact_expansion(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01T10:00:00Z",
             "phase": "code_review", "event": "Review complete",
             "source": "agent", "verdict": "APPROVED", "issues": 2},
            {"trace_id": "x", "timestamp": "2026-01-01T10:01:00Z",
             "phase": "artifact", "event": "code_review_artifact",
             "content": "## Code Review\nAPPROVED\n- Issue 1\n- Issue 2"},
        ]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/ART-1")
        assert "View code review" in resp.text
        assert "Issue 1" in resp.text

    async def test_token_display(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01T10:00:00Z",
             "phase": "analyst", "event": "analyst_completed",
             "source": "l1", "tokens_in": 1500, "tokens_out": 500},
        ]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/TOK-1")
        assert "1,500 in" in resp.text

    async def test_error_box_displayed(self) -> None:
        entries = [
            {"trace_id": "x", "timestamp": "2026-01-01T10:00:00Z",
             "phase": "pipeline", "event": "processing_started",
             "source": "l1"},
            {"trace_id": "x", "timestamp": "2026-01-01T10:00:05Z",
             "phase": "pipeline", "event": "error",
             "error_type": "RuntimeError",
             "error_message": "API rate limited"},
        ]
        with patch("trace_dashboard.read_trace", return_value=entries):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces/ERR-1")
        assert "RuntimeError" in resp.text
        assert "rate limited" in resp.text


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
        assert resp.json()["total"] == 1

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
