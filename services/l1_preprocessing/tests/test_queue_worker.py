"""Tests for queue worker — Redis fallback, ticket processing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from models import TicketPayload, TicketSource, TicketType
from queue_worker import enqueue_ticket, process_ticket_sync


class TestEnqueueTicket:
    def _make_ticket(self) -> TicketPayload:
        return TicketPayload(
            source=TicketSource.JIRA,
            id="Q-1",
            ticket_type=TicketType.STORY,
            title="Test queue",
        )

    def test_returns_none_when_no_redis_url(self) -> None:
        """Falls back to None when redis_url is empty."""
        with patch("queue_worker.settings") as mock_settings:
            mock_settings.redis_url = ""
            result = enqueue_ticket(self._make_ticket())
        assert result is None

    def test_returns_none_when_redis_not_installed(self) -> None:
        """Falls back to None when redis package not available."""
        with (
            patch("queue_worker.settings") as mock_settings,
            patch.dict("sys.modules", {"redis": None, "rq": None}),
        ):
            mock_settings.redis_url = "redis://localhost:6379"
            result = enqueue_ticket(self._make_ticket())
        assert result is None

    def test_returns_none_on_connection_error(self) -> None:
        """Falls back to None when Redis connection fails."""
        with patch("queue_worker.settings") as mock_settings:
            mock_settings.redis_url = "redis://nonexistent:6379"
            result = enqueue_ticket(self._make_ticket())
        assert result is None

class TestProcessTicketSync:
    def test_reconstructs_ticket_and_runs_pipeline(self) -> None:
        """Verifies ticket deserialization and pipeline invocation."""
        ticket_data = {
            "source": "jira",
            "id": "Q-2",
            "ticket_type": "story",
            "title": "Queue test",
        }

        mock_pipeline = MagicMock()
        mock_result = {"status": "enriched", "ticket_id": "Q-2"}

        async def fake_process(ticket: TicketPayload) -> dict:
            return mock_result

        mock_pipeline.process = fake_process

        with patch("queue_worker.Pipeline", return_value=mock_pipeline):
            result = process_ticket_sync(ticket_data)

        assert result["status"] == "enriched"
        assert result["ticket_id"] == "Q-2"

    def test_returns_error_on_invalid_ticket_data(self) -> None:
        """Invalid ticket data returns error dict instead of crashing."""
        result = process_ticket_sync({"invalid": "data"})
        assert result["status"] == "failed"
        assert "error" in result
