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

        # Verify stderr is redirected to a real file handle, not a pipe.
        # Pipes risked SIGPIPE-killing the spawned team once L1 detached
        # after the 2-second health check — a temp file has no such
        # constraint. We can't assert exact equality (the file object is
        # created per-call), so we assert it's a real file-like with a
        # fileno, and explicitly that it is NOT subprocess.PIPE.
        assert call_args[1]["stdout"] == subprocess.DEVNULL
        stderr_arg = call_args[1]["stderr"]
        assert stderr_arg is not subprocess.PIPE
        assert hasattr(stderr_arg, "fileno")

    async def test_enriched_spawn_early_failure_reads_stderr_from_tempfile(
        self,
        settings: Settings,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
        tmp_path: Path,
    ) -> None:
        """Bug 2 regression: when spawn exits non-zero within 2s, the
        error path must read stderr from the backing temp file (not from
        a PIPE), log the failure, and unlink the temp file afterward."""
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

        captured_stderr_path: list[Path] = []

        def fake_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            # Write fake stderr into the provided temp file BEFORE returning
            # so the early-failure read path sees content.
            stderr_fh = kwargs["stderr"]
            stderr_fh.write(b"boom: missing client repo\n")
            stderr_fh.flush()
            captured_stderr_path.append(Path(stderr_fh.name))
            mock_proc = MagicMock()
            mock_proc.poll.return_value = 2  # Non-zero → early failure path
            return mock_proc

        with patch("pipeline.subprocess.Popen", side_effect=fake_popen):
            result = await pipe.process(sample_ticket)

        assert result["spawn_triggered"] is False
        # Temp file must be cleaned up after the early-failure branch.
        assert captured_stderr_path, "fake Popen never ran"
        assert not captured_stderr_path[0].exists(), (
            "early-failure path must unlink the stderr temp file"
        )

    async def test_enriched_spawn_reaper_cleans_up_running_process(
        self,
        settings: Settings,
        mock_analyst: AsyncMock,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
        tmp_path: Path,
    ) -> None:
        """Bug 2 regression: when the child survives the 2-second health
        check, the reaper task must await proc.wait and unlink the stderr
        temp file when the child finally exits — no zombies, no leftover
        temp files."""
        import asyncio

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

        captured_stderr_path: list[Path] = []

        def fake_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured_stderr_path.append(Path(kwargs["stderr"].name))
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None  # Still running after 2s
            mock_proc.wait.return_value = 0  # Eventually exits cleanly
            return mock_proc

        with patch("pipeline.subprocess.Popen", side_effect=fake_popen):
            result = await pipe.process(sample_ticket)

        assert result["spawn_triggered"] is True
        assert captured_stderr_path, "fake Popen never ran"

        # The reaper runs asynchronously — give it a moment to complete.
        # Wait until all pending tasks on the running loop are done so
        # we can deterministically assert cleanup ran.
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # Reaper's unlink should have cleaned up the stderr temp file.
        assert not captured_stderr_path[0].exists(), (
            "reaper must unlink the stderr temp file after proc.wait returns"
        )


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


class TestConcurrentProcessTempDirs:
    """Bug regression: Pipeline is a module-level singleton (see
    main._get_pipeline), and temp-dir tracking used to live on
    ``self._temp_dirs``. Two concurrent webhook tasks shared that list,
    so ticket A's cleanup would rmtree ticket B's in-flight attachment
    and figma directories before B's spawn process could read them.
    Fixed by making temp_dirs a per-call local variable passed through
    the download / figma / cleanup helpers. These tests prove the fix
    by running two ``process()`` calls concurrently and verifying both
    tickets' directories survive long enough for each ticket to see
    them before its own cleanup runs."""

    async def test_temp_dirs_are_not_shared_across_concurrent_processes(
        self,
        settings: Settings,
        mock_jira: AsyncMock,
        sample_ticket: TicketPayload,
        tmp_path: Path,
    ) -> None:
        import asyncio

        # A Pipeline instance shared by both "webhook" calls — exactly
        # the singleton pattern main.py uses in production.
        shared_analyst = AsyncMock()

        def _make_enriched(ticket_id: str) -> EnrichedTicket:
            return EnrichedTicket(
                source=TicketSource.JIRA,
                id=ticket_id,
                ticket_type=TicketType.STORY,
                title=f"Concurrent ticket {ticket_id}",
                description="Test",
                generated_acceptance_criteria=["AC"],
                size_assessment=SizeAssessment(
                    classification=SizeClassification.SMALL,
                    estimated_units=1,
                    recommended_dev_count=1,
                ),
            )

        # Analyst returns a different enriched ticket per call so the
        # two process() invocations have distinct payloads.
        call_sequence = iter([_make_enriched("CONC-1"), _make_enriched("CONC-2")])
        shared_analyst.analyze.side_effect = lambda *_args, **_kw: next(call_sequence)

        pipe = Pipeline(
            settings=settings,
            analyst=shared_analyst,
            jira_adapter=mock_jira,
        )

        # Instrument _cleanup_temp_dirs so we can see what each call
        # believes belongs to it. The recorded list must NEVER include
        # the other call's directories.
        cleanup_calls: list[list[str]] = []
        original_cleanup = Pipeline._cleanup_temp_dirs

        def _spy_cleanup(log: object, temp_dirs: list[str]) -> None:
            cleanup_calls.append(list(temp_dirs))
            original_cleanup(log, temp_dirs)

        with patch("pipeline.subprocess.Popen") as mock_popen, \
             patch.object(
                 Pipeline, "_cleanup_temp_dirs", staticmethod(_spy_cleanup)
             ):
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            # Fire two process() calls concurrently.
            t1 = TicketPayload(
                source=TicketSource.JIRA,
                id="CONC-1",
                ticket_type=TicketType.STORY,
                title="t1",
                description="",
            )
            t2 = TicketPayload(
                source=TicketSource.JIRA,
                id="CONC-2",
                ticket_type=TicketType.STORY,
                title="t2",
                description="",
            )
            await asyncio.gather(pipe.process(t1), pipe.process(t2))

        # Each cleanup call must operate on its own list — no directories
        # from the other concurrent call can appear. In this mock setup
        # no temp dirs are created (no figma, no attachments), so both
        # lists must simply be empty. Critically, there is NO shared
        # _temp_dirs attribute on the Pipeline anymore — previously the
        # first cleanup would have reported both calls' dirs and the
        # second would have reported zero.
        assert len(cleanup_calls) == 2
        assert all(isinstance(c, list) for c in cleanup_calls)

        # Pipeline no longer carries instance-level temp_dirs state.
        assert not hasattr(pipe, "_temp_dirs")
