"""Tests for trace dashboard — HTML rendering, XSS, API endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from trace_dashboard import _escape


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
                "completed_at": "2026-01-01T00:10:00Z",
                "status": "Pipeline complete",
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
            "completed_at": "2026-01-01",
            "status": "complete",
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
        assert "<script>" not in resp.text
        assert "&lt;script&gt;" in resp.text

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
        with patch("trace_dashboard.list_traces", return_value=mock_traces):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/traces?pr=nonexistent")
        assert resp.status_code == 200
        assert "0 tickets" in resp.text


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


class TestTracesApiEndpoint:
    async def test_returns_json(self) -> None:
        traces = [{"ticket_id": "A-1", "entries": 3}]
        with patch("trace_dashboard.list_traces", return_value=traces):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/api/traces")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["ticket_id"] == "A-1"

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
