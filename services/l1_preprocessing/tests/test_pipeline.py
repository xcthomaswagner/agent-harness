"""Integration tests for the L1 pipeline — analyst → routing → actions."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import Settings
from models import (
    Attachment,
    CallbackConfig,
    DecompositionPlan,
    EnrichedTicket,
    InfoRequest,
    SizeAssessment,
    SizeClassification,
    SubTicketSpec,
    TestScenario,
    TestType,
    TicketPayload,
    TicketSource,
    TicketType,
)
from pipeline import Pipeline


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="test-key",
        jira_base_url="https://test.atlassian.net",
        jira_api_token="test-token",
        jira_user_email="bot@test.com",
        default_client_repo="",
    )


@pytest.fixture
def mock_analyst() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_jira() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def pipeline(
    settings: Settings, mock_analyst: AsyncMock, mock_jira: AsyncMock
) -> Pipeline:
    return Pipeline(
        settings=settings, analyst=mock_analyst, jira_adapter=mock_jira
    )


@pytest.fixture
def sample_ticket() -> TicketPayload:
    return TicketPayload(
        source=TicketSource.JIRA,
        id="PIPE-10",
        ticket_type=TicketType.STORY,
        title="Pipeline test ticket",
        description="Test the pipeline routing",
        callback=CallbackConfig(
            base_url="https://test.atlassian.net",
            ticket_id="PIPE-10",
            source=TicketSource.JIRA,
            auth_token="token",
        ),
    )


# --- Route: Enriched ---


class TestRouteEnriched:
    async def test_enriched_writes_comment_to_jira(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        enriched = EnrichedTicket(
            **sample_ticket.model_dump(),
            generated_acceptance_criteria=["New AC 1", "New AC 2"],
            test_scenarios=[
                TestScenario(
                    name="test_1",
                    test_type=TestType.UNIT,
                    description="Test something",
                )
            ],
            edge_cases=["Edge case 1"],
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
            analyst_notes="Simple change",
        )
        mock_analyst.analyze.return_value = enriched

        result = await pipeline.process(sample_ticket)

        assert result["status"] == "enriched"
        assert result["generated_ac_count"] == 2
        assert result["test_scenario_count"] == 1

        # Verify Jira comment was posted with generated AC
        mock_jira.write_comment.assert_called_once()
        comment = mock_jira.write_comment.call_args[0][1]
        assert "New AC 1" in comment
        assert "New AC 2" in comment
        assert "Edge case 1" in comment

    async def test_enriched_skips_jira_when_no_generated_ac(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        enriched = EnrichedTicket(
            **sample_ticket.model_dump(),
            generated_acceptance_criteria=[],  # No generated AC
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
        )
        mock_analyst.analyze.return_value = enriched

        result = await pipeline.process(sample_ticket)

        assert result["status"] == "enriched"
        mock_jira.write_comment.assert_not_called()

    async def test_enriched_writes_edge_cases_without_ac(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        """Edge cases alone (no generated AC) should still trigger a comment."""
        enriched = EnrichedTicket(
            **sample_ticket.model_dump(),
            generated_acceptance_criteria=[],
            edge_cases=["Empty input crashes", "Unicode in name field"],
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
        )
        mock_analyst.analyze.return_value = enriched

        with patch("pipeline.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc
            await pipeline.process(sample_ticket)

        mock_jira.write_comment.assert_called_once()
        comment = mock_jira.write_comment.call_args[0][1]
        assert "Edge Cases" in comment
        assert "Empty input crashes" in comment
        # Should NOT have "Acceptance Criteria" section
        assert "Acceptance Criteria" not in comment

    async def test_enriched_skips_jira_when_no_callback(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
    ) -> None:
        """Even with generated AC, no Jira write if callback is None."""
        ticket = TicketPayload(
            source=TicketSource.JIRA,
            id="PIPE-11",
            ticket_type=TicketType.STORY,
            title="No callback ticket",
            description="Missing callback",
            callback=None,
        )
        enriched = EnrichedTicket(
            **ticket.model_dump(),
            generated_acceptance_criteria=["AC 1"],
            edge_cases=["Edge 1"],
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
        )
        mock_analyst.analyze.return_value = enriched

        result = await pipeline.process(ticket)

        assert result["status"] == "enriched"
        mock_jira.write_comment.assert_not_called()

    async def test_enriched_spawn_skipped_when_no_client_repo(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        enriched = EnrichedTicket(
            **sample_ticket.model_dump(),
            generated_acceptance_criteria=["AC"],
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
        )
        mock_analyst.analyze.return_value = enriched

        result = await pipeline.process(sample_ticket)

        assert result["spawn_triggered"] is False

    async def test_enriched_triggers_spawn_when_client_repo_set(
        self,
        settings: Settings,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
        tmp_path: Path,
    ) -> None:
        # Create a fake client repo with .git
        fake_repo = tmp_path / "client-repo"
        fake_repo.mkdir()
        (fake_repo / ".git").mkdir()

        settings.default_client_repo = str(fake_repo)
        pipe = Pipeline(
            settings=settings, analyst=mock_analyst, jira_adapter=mock_jira
        )

        enriched = EnrichedTicket(
            **sample_ticket.model_dump(),
            generated_acceptance_criteria=["AC"],
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
        )
        mock_analyst.analyze.return_value = enriched

        with patch("pipeline.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None  # Process still running
            mock_popen.return_value = mock_proc
            result = await pipe.process(sample_ticket)

        assert result["spawn_triggered"] is True
        mock_popen.assert_called_once()

        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert "--client-repo" in cmd
        assert "--branch-name" in cmd
        assert "ai/PIPE-10" in cmd

        # Verify stderr is captured for error detection
        assert call_args[1]["stdout"] == subprocess.DEVNULL
        assert call_args[1]["stderr"] == subprocess.PIPE


# --- Route: Info Request ---


class TestRouteInfoRequest:
    async def test_info_request_posts_comment_and_transitions(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        info_req = InfoRequest(
            ticket_id="PIPE-10",
            source=TicketSource.JIRA,
            questions=["What file size limit?", "Which formats?"],
            context="Upload constraints not specified",
            callback=sample_ticket.callback,
        )
        mock_analyst.analyze.return_value = info_req

        result = await pipeline.process(sample_ticket)

        assert result["status"] == "info_request"
        assert result["question_count"] == 2

        # Verify comment posted
        mock_jira.write_comment.assert_called_once()
        comment = mock_jira.write_comment.call_args[0][1]
        assert "What file size limit?" in comment
        assert "Which formats?" in comment

        # Verify status transition
        mock_jira.transition_status.assert_called_once_with(
            "PIPE-10", "Needs Clarification"
        )

    async def test_info_request_skips_jira_when_no_callback(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        info_req = InfoRequest(
            ticket_id="PIPE-10",
            source=TicketSource.JIRA,
            questions=["What file size limit?"],
            context="Upload constraints not specified",
            callback=None,
        )
        mock_analyst.analyze.return_value = info_req

        result = await pipeline.process(sample_ticket)

        assert result["status"] == "info_request"
        mock_jira.write_comment.assert_not_called()
        mock_jira.transition_status.assert_not_called()


    async def test_info_request_continues_on_write_failure(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        """Write-back failures should not crash the pipeline."""
        info_req = InfoRequest(
            ticket_id="PIPE-10",
            source=TicketSource.JIRA,
            questions=["What?"],
            context="Need clarification",
            callback=sample_ticket.callback,
        )
        mock_analyst.analyze.return_value = info_req
        mock_jira.write_comment.side_effect = RuntimeError("Jira 403")

        result = await pipeline.process(sample_ticket)
        assert result["status"] == "info_request"  # Doesn't crash


# --- Route: Decomposition ---


class TestRouteDecomposition:
    async def test_decomposition_posts_comment_and_labels(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        decomp = DecompositionPlan(
            ticket_id="PIPE-10",
            source=TicketSource.JIRA,
            reason="6 independent units",
            sub_tickets=[
                SubTicketSpec(
                    title="Auth API",
                    description="Build auth endpoints",
                    ticket_type=TicketType.TASK,
                ),
                SubTicketSpec(
                    title="Auth UI",
                    description="Build login form",
                    ticket_type=TicketType.STORY,
                    depends_on=["Auth API"],
                ),
            ],
            dependency_order=["Auth API", "Auth UI"],
            callback=sample_ticket.callback,
        )
        mock_analyst.analyze.return_value = decomp

        result = await pipeline.process(sample_ticket)

        assert result["status"] == "decomposition"
        assert result["sub_ticket_count"] == 2

        # Verify comment posted with decomposition details
        mock_jira.write_comment.assert_called_once()
        comment = mock_jira.write_comment.call_args[0][1]
        assert "Auth API" in comment
        assert "Auth UI" in comment
        assert "6 independent units" in comment

        # Verify needs-splitting label added
        mock_jira.add_label.assert_called_once_with("PIPE-10", "needs-splitting")

    async def test_decomposition_skips_jira_when_no_callback(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        decomp = DecompositionPlan(
            ticket_id="PIPE-10",
            source=TicketSource.JIRA,
            reason="Too large",
            sub_tickets=[
                SubTicketSpec(
                    title="Part A",
                    description="First half",
                    ticket_type=TicketType.TASK,
                ),
            ],
            dependency_order=["Part A"],
            callback=None,
        )
        mock_analyst.analyze.return_value = decomp

        result = await pipeline.process(sample_ticket)

        assert result["status"] == "decomposition"
        mock_jira.write_comment.assert_not_called()
        mock_jira.add_label.assert_not_called()


# --- Error handling ---


class TestPipelineErrors:
    async def test_analyst_error_propagates(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        mock_analyst.analyze.side_effect = RuntimeError("API connection failed")

        with pytest.raises(RuntimeError, match="API connection failed"):
            await pipeline.process(sample_ticket)

    async def test_jira_write_error_does_not_crash(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        """Jira adapter failures during enrichment write-back are caught gracefully."""
        enriched = EnrichedTicket(
            **sample_ticket.model_dump(),
            generated_acceptance_criteria=["AC 1"],
            edge_cases=["Edge 1"],
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
        )
        mock_analyst.analyze.return_value = enriched
        mock_jira.write_comment.side_effect = RuntimeError("Jira unavailable")

        # Should NOT raise — error is caught and logged
        result = await pipeline.process(sample_ticket)
        assert result["status"] == "enriched"


# --- Ticket JSON writing ---


class TestWriteTicketJson:
    def test_writes_valid_json(self, sample_ticket: TicketPayload) -> None:
        import json

        enriched = EnrichedTicket(
            **sample_ticket.model_dump(),
            generated_acceptance_criteria=["AC1"],
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
        )
        path = Pipeline._write_ticket_json(enriched)
        try:
            content = path.read_text()
            parsed = json.loads(content)
            assert parsed["id"] == "PIPE-10"
            assert parsed["generated_acceptance_criteria"] == ["AC1"]
        finally:
            path.unlink()

    def test_filename_contains_ticket_id(self, sample_ticket: TicketPayload) -> None:
        enriched = EnrichedTicket(
            **sample_ticket.model_dump(),
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
        )
        path = Pipeline._write_ticket_json(enriched)
        try:
            assert "PIPE-10" in path.name
        finally:
            path.unlink()


# --- Image attachment download ---


class TestImageAttachmentDownload:
    async def test_downloads_images_before_analyst(
        self,
        settings: Settings,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        """Pipeline downloads image attachments before calling analyst."""
        sample_ticket.attachments = [
            Attachment(
                filename="design.png",
                url="https://jira/att/1",
                content_type="image/png",
            )
        ]

        # Mock download_image_attachments to set local_path
        async def fake_download(attachments, dest_dir):
            return [
                att.model_copy(update={"local_path": f"{dest_dir}/{att.filename}"})
                for att in attachments
            ]

        mock_jira.download_image_attachments = AsyncMock(side_effect=fake_download)

        enriched = EnrichedTicket(
            **sample_ticket.model_dump(),
            generated_acceptance_criteria=["AC"],
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
        )
        mock_analyst.analyze.return_value = enriched

        pipe = Pipeline(
            settings=settings, analyst=mock_analyst, jira_adapter=mock_jira
        )
        result = await pipe.process(sample_ticket)

        assert result["status"] == "enriched"
        mock_jira.download_image_attachments.assert_called_once()

    async def test_skips_download_when_no_images(
        self,
        pipeline: Pipeline,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
    ) -> None:
        """Pipeline skips download when no image attachments."""
        enriched = EnrichedTicket(
            **sample_ticket.model_dump(),
            size_assessment=SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=1,
                recommended_dev_count=1,
            ),
        )
        mock_analyst.analyze.return_value = enriched

        await pipeline.process(sample_ticket)

        # download_image_attachments should not be called
        mock_jira.download_image_attachments.assert_not_called()
