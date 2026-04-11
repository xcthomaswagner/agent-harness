"""Tests for tracer — trace generation, reading, listing, consolidation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tracer import (
    append_trace,
    build_span_tree,
    build_trace_list_row,
    compute_phase_durations,
    consolidate_worktree_logs,
    extract_diagnostic_info,
    extract_escalation_reason,
    generate_trace_id,
    list_traces,
    read_trace,
    trace_path,
)


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    return logs


class TestGenerateTraceId:
    def test_returns_12_char_hex(self) -> None:
        tid = generate_trace_id()
        assert len(tid) == 12
        int(tid, 16)  # Should not raise

    def test_unique(self) -> None:
        ids = {generate_trace_id() for _ in range(100)}
        assert len(ids) == 100


class TestTracePath:
    def test_returns_jsonl_path(self) -> None:
        with patch("tracer.LOGS_DIR", Path("/tmp/test-logs")):
            p = trace_path("SCRUM-1")
        assert p == Path("/tmp/test-logs/SCRUM-1.jsonl")


class TestAppendTrace:
    def test_appends_json_line(self, trace_dir: Path) -> None:
        with patch("tracer.LOGS_DIR", trace_dir):
            append_trace("T-1", "abc123", "webhook", "received", source="jira")

        path = trace_dir / "T-1.jsonl"
        assert path.exists()
        entry = json.loads(path.read_text().strip())
        assert entry["ticket_id"] == "T-1"
        assert entry["trace_id"] == "abc123"
        assert entry["phase"] == "webhook"
        assert entry["event"] == "received"
        assert entry["source"] == "jira"
        assert "timestamp" in entry

    def test_appends_multiple_entries(self, trace_dir: Path) -> None:
        with patch("tracer.LOGS_DIR", trace_dir):
            append_trace("T-2", "aaa", "webhook", "event1")
            append_trace("T-2", "aaa", "pipeline", "event2")

        path = trace_dir / "T-2.jsonl"
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_extra_kwargs_included(self, trace_dir: Path) -> None:
        with patch("tracer.LOGS_DIR", trace_dir):
            append_trace("T-3", "bbb", "review", "done", verdict="APPROVED")

        entry = json.loads((trace_dir / "T-3.jsonl").read_text().strip())
        assert entry["verdict"] == "APPROVED"


class TestReadTrace:
    def test_reads_entries(self, trace_dir: Path) -> None:
        path = trace_dir / "T-4.jsonl"
        path.write_text(
            json.dumps({"phase": "a", "event": "e1"}) + "\n"
            + json.dumps({"phase": "b", "event": "e2"}) + "\n"
        )
        with patch("tracer.LOGS_DIR", trace_dir):
            entries = read_trace("T-4")
        assert len(entries) == 2
        assert entries[0]["phase"] == "a"

    def test_returns_empty_for_missing(self, trace_dir: Path) -> None:
        with patch("tracer.LOGS_DIR", trace_dir):
            entries = read_trace("NONEXISTENT")
        assert entries == []

    def test_skips_corrupt_lines(self, trace_dir: Path) -> None:
        path = trace_dir / "T-5.jsonl"
        path.write_text(
            json.dumps({"phase": "ok"}) + "\n"
            "not json\n"
            + json.dumps({"phase": "also_ok"}) + "\n"
        )
        with patch("tracer.LOGS_DIR", trace_dir):
            entries = read_trace("T-5")
        assert len(entries) == 2

    def test_skips_empty_lines(self, trace_dir: Path) -> None:
        path = trace_dir / "T-6.jsonl"
        path.write_text(
            json.dumps({"phase": "a"}) + "\n\n\n"
            + json.dumps({"phase": "b"}) + "\n"
        )
        with patch("tracer.LOGS_DIR", trace_dir):
            entries = read_trace("T-6")
        assert len(entries) == 2


class TestListTraces:
    def test_lists_all_traces(self, trace_dir: Path) -> None:
        for tid in ["A-1", "B-2"]:
            (trace_dir / f"{tid}.jsonl").write_text(
                json.dumps({
                    "trace_id": "x", "ticket_id": tid,
                    "timestamp": "2026-01-01T00:00:00Z",
                    "phase": "webhook", "event": "received",
                }) + "\n"
            )
        with patch("tracer.LOGS_DIR", trace_dir):
            traces = list_traces()
        assert len(traces) == 2
        ids = {t["ticket_id"] for t in traces}
        assert ids == {"A-1", "B-2"}

    def test_skips_empty_trace_files(self, trace_dir: Path) -> None:
        (trace_dir / "EMPTY.jsonl").write_text("")
        with patch("tracer.LOGS_DIR", trace_dir):
            traces = list_traces()
        assert len(traces) == 0

    def test_extracts_pr_url(self, trace_dir: Path) -> None:
        (trace_dir / "P-1.jsonl").write_text(
            json.dumps({
                "trace_id": "x", "timestamp": "2026-01-01",
                "phase": "complete", "event": "Pipeline complete",
                "pr_url": "https://github.com/test/pr/1",
                "review_verdict": "APPROVED",
                "qa_result": "PASS",
            }) + "\n"
        )
        with patch("tracer.LOGS_DIR", trace_dir):
            traces = list_traces()
        assert traces[0]["pr_url"] == "https://github.com/test/pr/1"
        assert traces[0]["review_verdict"] == "APPROVED"
        assert traces[0]["qa_result"] == "PASS"


class TestConsolidateWorktreeLogs:
    def test_imports_pipeline_jsonl(self, trace_dir: Path, tmp_path: Path) -> None:
        # Create a fake worktree with pipeline log
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "pipeline.jsonl").write_text(
            json.dumps({"phase": "impl", "event": "done"}) + "\n"
        )

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-1", "trace-abc", str(wt))

        entries = json.loads(
            (trace_dir / "C-1.jsonl").read_text().strip().split("\n")[-1]
        )
        assert entries["phase"] == "impl"
        assert entries["trace_id"] == "trace-abc"
        assert entries["source"] == "agent"

    def test_imports_code_review(self, trace_dir: Path, tmp_path: Path) -> None:
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "code-review.md").write_text("## Code Review\nAPPROVED")

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-2", "trace-def", str(wt))
            entries = read_trace("C-2")
        assert any(e["event"] == "code_review_artifact" for e in entries)

    def test_imports_qa_matrix(self, trace_dir: Path, tmp_path: Path) -> None:
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "qa-matrix.md").write_text("## QA Matrix\nPASS")

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-3", "trace-ghi", str(wt))
            entries = read_trace("C-3")
        assert any(e["event"] == "qa_matrix_artifact" for e in entries)

    def test_handles_missing_worktree(self, trace_dir: Path) -> None:
        """Should not crash if worktree path doesn't exist."""
        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-4", "trace-jkl", "/nonexistent")
        # No file created (no pipeline.jsonl to import)

    def test_handles_corrupt_pipeline_jsonl(
        self, trace_dir: Path, tmp_path: Path
    ) -> None:
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "pipeline.jsonl").write_text("not json\n")

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-5", "trace-mno", str(wt))

        # Should not crash; corrupt line skipped
        path = trace_dir / "C-5.jsonl"
        assert not path.exists() or path.read_text().strip() == ""

    def test_truncates_long_content(
        self, trace_dir: Path, tmp_path: Path
    ) -> None:
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "code-review.md").write_text("x" * 10000)

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-6", "trace-pqr", str(wt))
            entries = read_trace("C-6")
        content_entry = [e for e in entries if e.get("content")]
        assert len(content_entry[0]["content"]) <= 5000

    def _make_worktree(self, tmp_path: Path, ticket_id: str) -> Path:
        """Create a fake worktree matching the real spawn_team layout.

        Real layout (scripts/spawn_team.py:243):
            <client_repo.parent>/worktrees/<branch>
        So the worktree's parent is the worktrees dir, and its grandparent
        is the client_repo.parent — where trace-archive lives.
        """
        wt = tmp_path / "worktrees" / f"ai-{ticket_id}"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)

        (wt / "CLAUDE.md").write_text("# Effective harness instructions\n")
        (logs / "session.log").write_text("[spawn] starting\n[spawn] done\n")
        (logs / "pipeline.jsonl").write_text(
            json.dumps({"phase": "impl", "event": "done"}) + "\n"
        )

        stream_events = [
            {
                "type": "system",
                "subtype": "init",
                "mcp_servers": [
                    {"name": "salesforce", "status": "connected"},
                    {"name": "playwright", "status": "connected"},
                ],
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "id": "t1"}
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__salesforce__sf_deploy",
                            "id": "t2",
                        }
                    ]
                },
            },
        ]
        (logs / "session-stream.jsonl").write_text(
            "\n".join(json.dumps(e) for e in stream_events) + "\n"
        )
        return wt

    def test_imports_session_stream_and_tool_index_archive_exists(
        self, trace_dir: Path, tmp_path: Path
    ) -> None:
        """Archive path exists — should be preferred over live path."""
        wt = self._make_worktree(tmp_path, "C-7")

        # Pre-populate the archive file at the canonical location — this is
        # what spawn_team.py:561 does on successful runs. The archive lives
        # at <client_repo.parent>/trace-archive/<ticket>/, which equals
        # wt.parent.parent/trace-archive/<ticket>/ because:
        #   wt = <tmp_path>/worktrees/ai-C-7
        #   wt.parent = <tmp_path>/worktrees
        #   wt.parent.parent = <tmp_path> (== client_repo.parent)
        archive_dir = tmp_path / "trace-archive" / "C-7"
        archive_dir.mkdir(parents=True)
        archive_stream = archive_dir / "session-stream.jsonl"
        archive_stream.write_text(
            (wt / ".harness" / "logs" / "session-stream.jsonl").read_text()
        )

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-7", "trace-xyz", str(wt))
            entries = read_trace("C-7")

        events_by_name = {e.get("event"): e for e in entries}
        assert "session_log_artifact" in events_by_name
        assert "effective_claude_md_artifact" in events_by_name
        assert "Effective harness" in events_by_name[
            "effective_claude_md_artifact"
        ]["content"]

        stream_entry = events_by_name["session_stream_artifact"]
        # Exact equality — do NOT use endswith, which masks path bugs.
        expected_archive = str(archive_stream)
        assert stream_entry["artifact_path"] == expected_archive
        assert stream_entry["size_bytes"] > 0
        assert stream_entry["line_count"] == 3

        tool_index_entry = events_by_name["tool_index"]
        idx = tool_index_entry["index"]
        assert idx["tool_counts"] == {"Bash": 1, "mcp__salesforce__sf_deploy": 1}
        assert idx["tool_call_count"] == 2
        assert idx["mcp_servers_used"] == ["salesforce"]
        assert idx["mcp_servers_unused"] == ["playwright"]

    def test_session_stream_falls_back_to_live_path_when_no_archive(
        self, trace_dir: Path, tmp_path: Path
    ) -> None:
        """Failed/escalated runs never archive — fall back to live worktree path."""
        wt = self._make_worktree(tmp_path, "C-8")
        # Do NOT create the archive dir — simulates status != "complete".

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-8", "trace-fail", str(wt))
            entries = read_trace("C-8")

        events_by_name = {e.get("event"): e for e in entries}
        stream_entry = events_by_name["session_stream_artifact"]

        expected_live = str(wt / ".harness" / "logs" / "session-stream.jsonl")
        assert stream_entry["artifact_path"] == expected_live
        # Sanity: we didn't accidentally point to a (non-existent) archive path
        assert "trace-archive" not in stream_entry["artifact_path"]

    def test_session_stream_artifact_skipped_when_missing(
        self, trace_dir: Path, tmp_path: Path
    ) -> None:
        """If session-stream.jsonl doesn't exist at all, no artifact entry is written."""
        wt = tmp_path / "worktrees" / "ai-C-9"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "pipeline.jsonl").write_text(
            json.dumps({"phase": "impl", "event": "done"}) + "\n"
        )
        # No session-stream.jsonl at all.

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-9", "trace-none", str(wt))
            entries = read_trace("C-9")

        event_names = {e.get("event") for e in entries}
        assert "session_stream_artifact" not in event_names
        assert "tool_index" not in event_names


class TestComputePhaseDurations:
    """Tests for compute_phase_durations — per-phase timing from agent entries."""

    def test_simple_pipeline(self) -> None:
        entries = [
            {"phase": "webhook", "event": "jira_webhook_received",
             "timestamp": "2026-03-23T17:27:30Z"},
            {"phase": "ticket_read", "event": "Pipeline started, simple mode",
             "timestamp": "2026-03-23T17:28:23Z", "source": "agent"},
            {"phase": "implementation", "event": "Implementation complete",
             "timestamp": "2026-03-23T17:35:27Z", "source": "agent"},
            {"phase": "code_review", "event": "Review complete",
             "timestamp": "2026-03-23T17:36:54Z", "source": "agent"},
            {"phase": "qa_validation", "event": "QA complete",
             "timestamp": "2026-03-23T17:40:50Z", "source": "agent"},
            {"phase": "complete", "event": "Pipeline complete",
             "timestamp": "2026-03-23T17:41:22Z", "source": "agent"},
        ]
        durations = compute_phase_durations(entries)
        assert len(durations) == 4
        assert durations[0]["phase"] == "ticket_read"
        assert durations[0]["duration_seconds"] == 424.0
        assert durations[1]["phase"] == "implementation"
        assert durations[2]["phase"] == "code_review"
        assert durations[3]["phase"] == "qa_validation"

    def test_empty_entries(self) -> None:
        assert compute_phase_durations([]) == []

    def test_no_agent_entries(self) -> None:
        entries = [
            {"phase": "webhook", "event": "received", "timestamp": "2026-01-01T00:00:00Z"},
        ]
        assert compute_phase_durations(entries) == []

    def test_single_agent_entry(self) -> None:
        entries = [
            {"phase": "ticket_read", "event": "started",
             "timestamp": "2026-01-01T00:00:00Z", "source": "agent"},
        ]
        assert compute_phase_durations(entries) == []

    def test_multi_run_uses_last_run(self) -> None:
        """Should only compute durations from the last run."""
        entries = [
            # Old run
            {"phase": "webhook", "event": "jira_webhook_received",
             "timestamp": "2026-03-20T10:00:00Z"},
            {"phase": "ticket_read", "event": "Pipeline started, simple mode",
             "timestamp": "2026-03-20T10:01:00Z", "source": "agent"},
            {"phase": "implementation", "event": "Implementation complete",
             "timestamp": "2026-03-20T10:10:00Z", "source": "agent"},
            # New run (re-processed)
            {"phase": "webhook", "event": "jira_webhook_received",
             "timestamp": "2026-03-23T17:27:30Z"},
            {"phase": "ticket_read", "event": "Pipeline started, simple mode",
             "timestamp": "2026-03-23T17:28:23Z", "source": "agent"},
            {"phase": "implementation", "event": "Implementation complete",
             "timestamp": "2026-03-23T17:35:27Z", "source": "agent"},
            {"phase": "complete", "event": "Pipeline complete",
             "timestamp": "2026-03-23T17:41:22Z", "source": "agent"},
        ]
        durations = compute_phase_durations(entries)
        # Should only get durations from the second run
        assert len(durations) == 2
        assert durations[0]["phase"] == "ticket_read"
        # ~7 minutes from 17:28:23 to 17:35:27
        assert 420 <= durations[0]["duration_seconds"] <= 425


class TestExtractEscalationReason:
    """Tests for extract_escalation_reason — human-readable failure reasons."""

    def test_from_escalation_artifact(self) -> None:
        entries = [
            {"event": "escalation_artifact",
             "content": "## Escalation Report\nQA failed: 5 of 8 criteria failed\nDetails below"},
        ]
        reason = extract_escalation_reason(entries)
        assert reason == "QA failed: 5 of 8 criteria failed"

    def test_from_escalated_event(self) -> None:
        entries = [
            {"event": "Escalated"},
        ]
        reason = extract_escalation_reason(entries)
        assert reason == "Escalated"

    def test_clean_completion(self) -> None:
        entries = [
            {"event": "Pipeline complete", "timestamp": "2026-03-23T17:41:22Z"},
        ]
        reason = extract_escalation_reason(entries)
        assert reason == ""

    def test_empty_entries(self) -> None:
        assert extract_escalation_reason([]) == ""

    def test_artifact_with_only_headings(self) -> None:
        entries = [
            {"event": "escalation_artifact",
             "content": "## Escalation\n### Details\n"},
        ]
        # Falls through to check for Escalated event, then staleness
        reason = extract_escalation_reason(entries)
        assert reason == ""

    def test_staleness_detection(self) -> None:
        """Tickets with no terminal event and old timestamps should report staleness."""
        entries = [
            {"event": "l2_dispatched",
             "timestamp": "2025-01-01T00:00:00+00:00"},
        ]
        reason = extract_escalation_reason(entries)
        assert reason.startswith("No progress since")


class TestListTracesRunStartedAt:
    """Tests for run_started_at field in list_traces output."""

    def test_includes_run_started_at(self, trace_dir: Path) -> None:
        (trace_dir / "R-1.jsonl").write_text(
            json.dumps({
                "trace_id": "x", "ticket_id": "R-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "phase": "webhook", "event": "jira_webhook_received",
            }) + "\n"
            + json.dumps({
                "trace_id": "x", "ticket_id": "R-1",
                "timestamp": "2026-01-01T00:05:00Z",
                "phase": "complete", "event": "Pipeline complete",
            }) + "\n"
        )
        with patch("tracer.LOGS_DIR", trace_dir):
            traces = list_traces()
        assert "run_started_at" in traces[0]

    def test_multi_run_uses_last_boundary(self, trace_dir: Path) -> None:
        """run_started_at should be from the last run, not the first-ever event."""
        (trace_dir / "R-2.jsonl").write_text(
            # First run
            json.dumps({
                "trace_id": "a", "ticket_id": "R-2",
                "timestamp": "2026-01-01T00:00:00Z",
                "phase": "webhook", "event": "jira_webhook_received",
            }) + "\n"
            # Second run
            + json.dumps({
                "trace_id": "b", "ticket_id": "R-2",
                "timestamp": "2026-03-23T17:27:30Z",
                "phase": "webhook", "event": "jira_webhook_received",
            }) + "\n"
            + json.dumps({
                "trace_id": "b", "ticket_id": "R-2",
                "timestamp": "2026-03-23T17:41:22Z",
                "phase": "complete", "event": "Pipeline complete",
            }) + "\n"
        )
        with patch("tracer.LOGS_DIR", trace_dir):
            traces = list_traces()
        assert traces[0]["run_started_at"] == "2026-03-23T17:27:30Z"
        assert traces[0]["started_at"] == "2026-01-01T00:00:00Z"


class TestExtractDiagnosticInfo:
    """Tests for extract_diagnostic_info — structured error diagnostics."""

    def test_error_at_processing(self) -> None:
        entries = [
            {"event": "processing_started", "timestamp": "2026-01-01T10:00:00Z"},
            {"event": "error", "error_type": "RuntimeError",
             "error_message": "Analyst API rate limited after 3 retries",
             "timestamp": "2026-01-01T10:00:05Z", "phase": "pipeline"},
        ]
        diag = extract_diagnostic_info(entries)
        assert len(diag["errors"]) == 1
        assert diag["errors"][0]["error_type"] == "RuntimeError"
        assert "rate limit" in diag["hint"].lower()

    def test_spawn_failed(self) -> None:
        entries = [
            {"event": "processing_started", "timestamp": "2026-01-01T10:00:00Z"},
            {"event": "l2_dispatched", "timestamp": "2026-01-01T10:00:05Z"},
            {"event": "error", "error_type": "SpawnFailed",
             "error_message": "spawn_team.py exited 1",
             "error_context": {"stderr": "fatal: not a git repo"},
             "timestamp": "2026-01-01T10:00:07Z", "phase": "spawn"},
        ]
        diag = extract_diagnostic_info(entries)
        assert len(diag["errors"]) == 1
        assert "not a git repo" in diag["hint"]

    def test_no_error_at_dispatched(self) -> None:
        entries = [
            {"event": "processing_started", "timestamp": "2026-01-01T10:00:00Z"},
            {"event": "l2_dispatched", "timestamp": "2026-01-01T10:00:05Z"},
        ]
        diag = extract_diagnostic_info(entries)
        assert len(diag["errors"]) == 0
        assert "never reported back" in diag["hint"]

    def test_completed_ticket(self) -> None:
        entries = [
            {"event": "processing_started", "timestamp": "2026-01-01T10:00:00Z"},
            {"event": "Pipeline complete", "timestamp": "2026-01-01T10:10:00Z"},
        ]
        diag = extract_diagnostic_info(entries)
        assert len(diag["errors"]) == 0
        assert diag["hint"] == ""

    def test_empty_entries(self) -> None:
        diag = extract_diagnostic_info([])
        assert diag["errors"] == []
        assert diag["hint"] == ""

    def test_multiple_errors(self) -> None:
        entries = [
            {"event": "processing_started", "timestamp": "2026-01-01T10:00:00Z"},
            {"event": "error", "error_type": "JiraTransitionFailed",
             "error_message": "403 Forbidden",
             "timestamp": "2026-01-01T10:00:02Z", "phase": "pipeline"},
            {"event": "error", "error_type": "RuntimeError",
             "error_message": "Connection refused",
             "timestamp": "2026-01-01T10:00:05Z", "phase": "pipeline"},
        ]
        diag = extract_diagnostic_info(entries)
        assert len(diag["errors"]) == 2

    def test_connection_error_hint(self) -> None:
        entries = [
            {"event": "processing_started", "timestamp": "2026-01-01T10:00:00Z"},
            {"event": "error", "error_type": "RuntimeError",
             "error_message": "Analyst API connection failed after 3 retries",
             "timestamp": "2026-01-01T10:00:05Z", "phase": "pipeline"},
        ]
        diag = extract_diagnostic_info(entries)
        assert "network" in diag["hint"].lower()


class TestBuildSpanTree:
    """Tests for build_span_tree — L1/L2/L3 grouping with artifact linking."""

    def _scrum10_entries(self) -> list[dict]:
        """Realistic SCRUM-10 trace entries."""
        return [
            {"phase": "webhook", "event": "jira_webhook_received",
             "timestamp": "2026-03-23T17:27:30Z", "source": "jira",
             "ticket_type": "story"},
            {"phase": "pipeline", "event": "processing_completed",
             "timestamp": "2026-03-23T17:28:05Z", "source": "l1",
             "status": "enriched"},
            {"phase": "ticket_read",
             "event": "Pipeline started, simple mode",
             "timestamp": "2026-03-23T17:28:23Z", "source": "agent"},
            {"phase": "implementation",
             "event": "Implementation complete",
             "timestamp": "2026-03-23T17:35:27Z", "source": "agent",
             "commit": "86499d1"},
            {"phase": "code_review", "event": "Review complete",
             "timestamp": "2026-03-23T17:36:54Z", "source": "agent",
             "verdict": "APPROVED", "issues": 6},
            {"phase": "qa_validation", "event": "QA complete",
             "timestamp": "2026-03-23T17:40:50Z", "source": "agent",
             "overall": "PASS", "criteria_passed": 8,
             "criteria_total": 8},
            {"phase": "complete", "event": "Pipeline complete",
             "timestamp": "2026-03-23T17:41:22Z", "source": "agent",
             "pr_url": "https://github.com/test/pr/10",
             "review_verdict": "APPROVED", "qa_result": "PASS",
             "pipeline_mode": "simple", "units": 1},
            {"phase": "artifact", "event": "code_review_artifact",
             "timestamp": "2026-03-23T17:41:33Z",
             "content": "## Code Review\nAPPROVED"},
            {"phase": "artifact", "event": "qa_matrix_artifact",
             "timestamp": "2026-03-23T17:41:33Z",
             "content": "## QA Matrix\nPASS 8/8"},
        ]

    def test_groups_into_layers(self) -> None:
        tree = build_span_tree(self._scrum10_entries())
        assert len(tree["l1"]) == 2  # webhook + processing_completed
        assert len(tree["l2"]) >= 4  # ticket_read, impl, review, qa, complete
        assert len(tree["l3"]) == 0
        assert len(tree["errors"]) == 0

    def test_artifacts_linked_to_phases(self) -> None:
        tree = build_span_tree(self._scrum10_entries())
        review_node = next(
            n for n in tree["l2"]
            if n["entry"].get("phase") == "code_review"
        )
        assert len(review_node["artifacts"]) == 1
        assert "Code Review" in review_node["artifacts"][0].get("content", "")

        qa_node = next(
            n for n in tree["l2"]
            if n["entry"].get("phase") == "qa_validation"
        )
        assert len(qa_node["artifacts"]) == 1

    def test_summary_extracted(self) -> None:
        tree = build_span_tree(self._scrum10_entries())
        s = tree["summary"]
        assert s["status"] == "Complete"
        assert s["review_verdict"] == "APPROVED"
        assert s["qa_result"] == "PASS"
        assert s["pr_url"] == "https://github.com/test/pr/10"
        assert s["pipeline_mode"] == "simple"
        assert "implementation" in s["phases_completed"]

    def test_durations_populated(self) -> None:
        tree = build_span_tree(self._scrum10_entries())
        impl_node = next(
            n for n in tree["l2"]
            if n["entry"].get("phase") == "implementation"
        )
        assert impl_node["duration_seconds"] is not None
        assert impl_node["duration_seconds"] > 0

    def test_empty_entries(self) -> None:
        tree = build_span_tree([])
        assert tree["l1"] == []
        assert tree["l2"] == []
        assert tree["summary"] == {}

    def test_l3_events_separated(self) -> None:
        entries = [
            {"phase": "webhook", "event": "jira_webhook_received",
             "timestamp": "2026-01-01T00:00:00Z", "source": "jira"},
            {"phase": "l3_pr_review", "event": "pr_review_spawned",
             "timestamp": "2026-01-01T01:00:00Z", "source": "l1",
             "pr_number": 10},
        ]
        tree = build_span_tree(entries)
        assert len(tree["l3"]) == 1
        assert tree["l3"][0]["entry"]["event"] == "pr_review_spawned"

    def test_errors_collected(self) -> None:
        entries = [
            {"phase": "pipeline", "event": "processing_started",
             "timestamp": "2026-01-01T00:00:00Z"},
            {"phase": "pipeline", "event": "error",
             "error_type": "RuntimeError",
             "error_message": "API failed",
             "timestamp": "2026-01-01T00:00:05Z"},
        ]
        tree = build_span_tree(entries)
        assert len(tree["errors"]) == 1

    def test_phase_started_events_captured(self) -> None:
        entries = [
            {"phase": "webhook", "event": "jira_webhook_received",
             "timestamp": "2026-01-01T00:00:00Z", "source": "jira"},
            {"phase": "implementation", "event": "phase_started",
             "timestamp": "2026-01-01T00:01:00Z", "source": "agent"},
            {"phase": "implementation",
             "event": "Implementation complete",
             "timestamp": "2026-01-01T00:08:00Z", "source": "agent",
             "commit": "abc123"},
        ]
        tree = build_span_tree(entries)
        impl = next(
            n for n in tree["l2"]
            if n["entry"].get("phase") == "implementation"
        )
        assert impl["started_entry"] is not None
        assert impl["started_entry"]["event"] == "phase_started"


class TestBuildTraceListRow:
    """Tests for build_trace_list_row — phase dots and duration percentage."""

    def test_phase_dots_from_agent_entries(self) -> None:
        entries = [
            {"phase": "webhook", "event": "received",
             "timestamp": "2026-01-01T00:00:00Z", "source": "jira"},
            {"phase": "implementation",
             "event": "Implementation complete",
             "timestamp": "2026-01-01T00:05:00Z", "source": "agent"},
            {"phase": "code_review", "event": "Review complete",
             "timestamp": "2026-01-01T00:06:00Z", "source": "agent"},
            {"phase": "complete", "event": "Pipeline complete",
             "timestamp": "2026-01-01T00:07:00Z", "source": "agent"},
        ]
        summary = {"duration": "7m 0s", "status": "Complete"}
        row = build_trace_list_row(summary, entries)
        assert len(row["phase_dots"]) == 3
        assert row["phase_dots"][0]["phase"] == "implementation"
        assert row["duration_pct"] > 0

    def test_duration_color_green_for_short(self) -> None:
        summary = {"duration": "5m 0s"}
        row = build_trace_list_row(summary, [])
        assert row["duration_color"] == "#124D49"

    def test_duration_seconds_only_not_inflated(self) -> None:
        """'30s' should be 30 seconds (1.7%), not 30 minutes (100%)."""
        summary = {"duration": "30s"}
        row = build_trace_list_row(summary, [])
        assert row["duration_pct"] < 5  # 30s / 1800s = 1.7%

    def test_duration_color_red_for_long(self) -> None:
        summary = {"duration": ">24h (multi-run)"}
        row = build_trace_list_row(summary, [])
        assert row["duration_color"] == "#DB2626"
        assert row["duration_pct"] == 100
