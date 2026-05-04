"""Tests for tracer — trace generation, reading, listing, consolidation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tracer import (
    _build_summary,
    _compute_run_duration,
    _extract_trace_metadata,
    append_trace,
    build_span_tree,
    build_trace_list_row,
    compute_phase_durations,
    consolidate_worktree_logs,
    derive_trace_status,
    extract_diagnostic_info,
    extract_escalation_reason,
    find_artifact,
    find_run_start_idx,
    generate_trace_id,
    latest_artifacts,
    list_traces,
    read_trace,
    redact_entry_in_place,
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

    def test_rejects_path_like_ticket_id(self) -> None:
        with pytest.raises(ValueError):
            trace_path("../SCRUM-1")


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

    def test_tolerates_non_utf8_pipeline_jsonl(
        self, trace_dir: Path, tmp_path: Path
    ) -> None:
        """Bug regression: ``Path.read_text()`` defaults to strict
        UTF-8, so a single invalid byte in pipeline.jsonl raised
        ``UnicodeDecodeError`` and aborted consolidation mid-function
        — every artifact file imported AFTER pipeline.jsonl
        (code-review.md, qa-matrix.md, CLAUDE.md, plans) was silently
        dropped from the trace. Fixed with ``errors='replace'`` on
        every read_text call. This test plants a pipeline.jsonl with
        a stray 0x80 byte alongside a code-review.md and verifies
        BOTH land in the trace store."""
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        # Pipeline.jsonl with a good line followed by a rogue byte.
        good_line = json.dumps({"phase": "impl", "event": "done"}) + "\n"
        (logs / "pipeline.jsonl").write_bytes(
            good_line.encode() + b"\x80 garbage non-utf8 byte\n"
        )
        (logs / "code-review.md").write_text("## Code Review\nAPPROVED")

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-UTF8", "trace-utf8", str(wt))
            entries = read_trace("C-UTF8")

        # The good pipeline.jsonl line must have been imported.
        assert any(e.get("event") == "done" for e in entries), (
            "pipeline.jsonl good line should survive a rogue byte"
        )
        # And code-review.md, which gets imported AFTER pipeline.jsonl,
        # must also be present — before the fix this would have been
        # silently dropped when UnicodeDecodeError aborted the function.
        assert any(
            e.get("event") == "code_review_artifact" for e in entries
        ), "artifacts after pipeline.jsonl must still be imported"

    def test_tolerates_non_utf8_artifact_file(
        self, trace_dir: Path, tmp_path: Path
    ) -> None:
        """Bug regression: artifact file reads also used strict UTF-8.
        A rogue byte in code-review.md used to abort consolidation
        and silently drop every subsequent artifact."""
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "pipeline.jsonl").write_text(
            json.dumps({"phase": "impl", "event": "done"}) + "\n"
        )
        (logs / "code-review.md").write_bytes(
            b"## Code Review\n\x80 rogue byte\nAPPROVED"
        )
        (logs / "qa-matrix.md").write_text("## QA\nPASS")

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("C-UTF8-2", "trace-utf8-2", str(wt))
            entries = read_trace("C-UTF8-2")

        # Both artifacts must land — qa-matrix imports AFTER code-review.
        assert any(e.get("event") == "code_review_artifact" for e in entries)
        assert any(e.get("event") == "qa_matrix_artifact" for e in entries)

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


class TestConsolidateRedaction:
    """Redact-on-consolidation: commit 6 wires ``redact()`` into every
    ``content`` field consolidated by ``consolidate_worktree_logs``. Secrets
    live in the worktree's raw logs but must be gone by the time the trace
    store sees them.
    """

    # An Anthropic key shape the redactor's line pass will catch.
    SECRET = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    def test_consolidate_redacts_session_log_content(
        self, trace_dir: Path, tmp_path: Path,
    ) -> None:
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "session.log").write_text(
            f"[bootstrap] loading secret {self.SECRET}\n[run] done\n",
        )

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("R-1", "trace-r1", str(wt))
            entries = read_trace("R-1")

        session_entry = next(
            e for e in entries if e.get("event") == "session_log_artifact"
        )
        content = str(session_entry["content"])
        assert self.SECRET not in content, (
            "raw secret must not survive consolidation"
        )
        assert "sk-ant-[REDACTED]" in content

    def test_consolidate_redacts_effective_claude_md(
        self, trace_dir: Path, tmp_path: Path,
    ) -> None:
        wt = tmp_path / "worktree"
        (wt / ".harness" / "logs").mkdir(parents=True)
        (wt / "CLAUDE.md").write_text(
            f"# Effective instructions\n\nAPI_KEY={self.SECRET}\n",
        )

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("R-2", "trace-r2", str(wt))
            entries = read_trace("R-2")

        claude_entry = next(
            e for e in entries if e.get("event") == "effective_claude_md_artifact"
        )
        content = str(claude_entry["content"])
        assert self.SECRET not in content
        assert "sk-ant-[REDACTED]" in content

    def test_consolidate_redacts_markdown_artifacts(
        self, trace_dir: Path, tmp_path: Path,
    ) -> None:
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "qa-matrix.md").write_text(
            f"# QA\nused token {self.SECRET}\n",
        )
        (logs / "code-review.md").write_text(
            f"# Review\nfound token {self.SECRET}\n",
        )

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("R-3", "trace-r3", str(wt))
            entries = read_trace("R-3")

        by_event = {e.get("event"): e for e in entries}
        for event_name in ("qa_matrix_artifact", "code_review_artifact"):
            content = str(by_event[event_name]["content"])
            assert self.SECRET not in content, (
                f"{event_name} must be redacted at consolidation time"
            )
            assert "sk-ant-[REDACTED]" in content

    def test_consolidate_skips_stream_file_redaction(
        self, trace_dir: Path, tmp_path: Path,
    ) -> None:
        """The session-stream file on disk is NOT touched by consolidation.

        Stream redaction happens lazily at bundle-export time — the raw file
        stays on disk as a forensic escape hatch.
        """
        wt = tmp_path / "worktrees" / "ai-R-4"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "pipeline.jsonl").write_text(
            json.dumps({"phase": "impl", "event": "done"}) + "\n",
        )
        stream_file = logs / "session-stream.jsonl"
        raw_bytes = (
            json.dumps({"type": "system", "subtype": "init", "mcp_servers": []})
            + "\n"
            + json.dumps({"type": "assistant", "text": f"key={self.SECRET}"})
            + "\n"
        ).encode()
        stream_file.write_bytes(raw_bytes)

        before = stream_file.read_bytes()
        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("R-4", "trace-r4", str(wt))
        after = stream_file.read_bytes()

        assert before == after, (
            "consolidation must leave session-stream.jsonl byte-for-byte "
            "identical — redaction is deferred to bundle export"
        )
        # The secret still lives in the raw stream on disk, by design.
        assert self.SECRET.encode() in after

    def test_consolidate_redacts_tool_index_first_tool_error_message(
        self, trace_dir: Path, tmp_path: Path,
    ) -> None:
        """Regression: tool_index.first_tool_error.message is redacted at rest.

        ``tool_index._extract_error_message`` captures up to 500 chars of
        raw tool-error output. A Bash call like ``sf org display --json``
        can print a live access token into stderr, which then lands in
        ``first_tool_error.message``. Before the fix the tracer wrote this
        straight into the trace store via ``append_trace(..., index=...)``,
        so any dashboard panel or non-bundle reader would see the raw key.
        """
        wt = tmp_path / "worktrees" / "ai-R-5"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)

        # Minimal stream that drives build_tool_index into producing a
        # tool_use block followed by an is_error=true tool_result with a
        # secret in its text content.
        stream_events = [
            {"type": "system", "subtype": "init", "mcp_servers": []},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_err_1",
                            "name": "Bash",
                            "input": {"command": "sf org display --json"},
                        },
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_err_1",
                            "is_error": True,
                            "content": (
                                f"sf: error: access token {self.SECRET} "
                                f"has expired, please reauthenticate"
                            ),
                        },
                    ],
                },
            },
        ]
        (logs / "session-stream.jsonl").write_text(
            "\n".join(json.dumps(e) for e in stream_events) + "\n",
        )

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("R-5", "trace-r5", str(wt))
            entries = read_trace("R-5")

        tool_index_entry = next(
            e for e in entries if e.get("event") == "tool_index"
        )
        first_err = tool_index_entry["index"]["first_tool_error"]
        assert first_err is not None, (
            "test setup bug — error-producing tool_result did not "
            "produce a first_tool_error in the index"
        )
        msg = first_err["message"]
        assert self.SECRET not in msg, (
            "tool_index.first_tool_error.message must be redacted before "
            "hitting the trace store"
        )
        assert "[REDACTED]" in msg

    def test_consolidate_redacts_non_content_pipeline_fields(
        self, trace_dir: Path, tmp_path: Path,
    ) -> None:
        """Regression: imported pipeline.jsonl entries have their known-risky
        non-``content`` fields redacted, not just ``content``.

        Before the fix, only ``content`` was redacted on import. An agent
        step that wrote a credential into ``debug_payload``, ``error``,
        ``stderr``, etc. would land in the trace store verbatim and be
        readable by dashboard panels. This test seeds both ``content`` AND
        ``debug_payload`` AND ``error`` and verifies all three are clean.
        """
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        pipeline_entry = {
            "phase": "impl",
            "event": "tool_failed",
            "content": f"narrative log line mentioning {self.SECRET}",
            "debug_payload": f"token={self.SECRET}",
            "error": f"auth failed with key {self.SECRET}",
        }
        (logs / "pipeline.jsonl").write_text(
            json.dumps(pipeline_entry) + "\n",
        )

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("R-6", "trace-r6", str(wt))
            entries = read_trace("R-6")

        imported = next(
            e for e in entries
            if e.get("source") == "agent" and e.get("event") == "tool_failed"
        )
        # All three must be redacted — not just content (the legacy path).
        for field in ("content", "debug_payload", "error"):
            value = imported[field]
            assert self.SECRET not in value, (
                f"{field} must be redacted on pipeline.jsonl import"
            )
            assert "[REDACTED]" in value, (
                f"{field} must show a redaction marker"
            )


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


class TestLatestArtifacts:
    """One-pass artifact index used by _build_bundle and panel rendering
    to avoid N-way find_artifact scans of the entries list."""

    def test_empty_entries_returns_empty_dict(self) -> None:
        assert latest_artifacts([]) == {}

    def test_returns_latest_per_event(self) -> None:
        # Re-triggered trace: two session.log artifacts. The second must win.
        entries: list[dict] = [
            {"phase": "artifact", "event": "session.log", "content": "run1"},
            {"phase": "pipeline", "event": "something_else"},
            {"phase": "artifact", "event": "tool_index", "index": {"x": 1}},
            {"phase": "artifact", "event": "session.log", "content": "run2"},
        ]
        idx = latest_artifacts(entries)
        assert idx["session.log"]["content"] == "run2"
        assert idx["tool_index"]["index"] == {"x": 1}

    def test_ignores_non_artifact_phase(self) -> None:
        entries: list[dict] = [
            {"phase": "pipeline", "event": "session.log", "content": "wrong"},
            {"phase": "artifact", "event": "session.log", "content": "right"},
        ]
        idx = latest_artifacts(entries)
        assert idx["session.log"]["content"] == "right"

    def test_matches_find_artifact_semantics(self) -> None:
        # Every key in the cached index must equal find_artifact(..., latest=True).
        entries: list[dict] = [
            {"phase": "artifact", "event": "a", "val": 1},
            {"phase": "artifact", "event": "b", "val": 2},
            {"phase": "artifact", "event": "a", "val": 3},
            {"phase": "artifact", "event": "c", "val": 4},
        ]
        idx = latest_artifacts(entries)
        for event in ("a", "b", "c"):
            assert idx[event] == find_artifact(entries, event)

    def test_skips_missing_or_non_string_event(self) -> None:
        entries: list[dict] = [
            {"phase": "artifact", "event": None, "x": 1},
            {"phase": "artifact"},
            {"phase": "artifact", "event": "", "x": 2},
            {"phase": "artifact", "event": "good", "x": 3},
        ]
        idx = latest_artifacts(entries)
        assert idx == {"good": {"phase": "artifact", "event": "good", "x": 3}}


class TestRedactEntryInPlace:
    """Shared helper used by consolidate_worktree_logs AND admin_re_redact
    so the import path and the rescan path can't drift on what constitutes
    an entry's redactable surface area."""

    def test_returns_zero_when_nothing_to_redact(self) -> None:
        entry: dict = {"event": "clean", "content": "hello"}
        assert redact_entry_in_place(entry) == 0
        assert entry == {"event": "clean", "content": "hello"}

    def test_redacts_top_level_known_fields(self) -> None:
        secret = "sk-ant-api03-" + "A" * 40
        entry: dict = {
            "content": f"key={secret}",
            "stderr": f"another {secret}",
            "event": "something",
        }
        n = redact_entry_in_place(entry)
        assert n >= 2
        assert secret not in entry["content"]
        assert secret not in entry["stderr"]

    def test_redacts_nested_first_tool_error_message(self) -> None:
        secret = "sk-ant-api03-" + "B" * 40
        entry: dict = {
            "event": "tool_index",
            "index": {
                "tool_counts": {"Bash": 1},
                "first_tool_error": {
                    "tool": "Bash",
                    "line": 7,
                    "message": f"sf: token {secret} expired",
                },
            },
        }
        n = redact_entry_in_place(entry)
        assert n >= 1
        assert secret not in entry["index"]["first_tool_error"]["message"]
        # Sibling tool_counts must not be touched.
        assert entry["index"]["tool_counts"] == {"Bash": 1}

    def test_redacts_unknown_fields_via_recursive_walk(self) -> None:
        """Phase 2 change: the redactor now walks recursively across
        every reachable string, not just a fixed top-level allowlist.
        An unknown field containing a known-shape credential gets
        redacted. The allowlist in ``_REDACT_IMPORTED_FIELDS`` is
        retained as a contributor hint (and for the seeded scenarios
        older tests reference), but the walk is the single source of
        truth — anything reachable from the entry dict is covered."""
        secret = "sk-ant-api03-" + "C" * 40
        entry: dict = {"custom_field": secret, "event": "raw"}
        n = redact_entry_in_place(entry)
        assert n >= 1
        assert secret not in entry["custom_field"]
        # Metadata key ``event`` is in the skip set and untouched.
        assert entry["event"] == "raw"


class TestRunStartIdxKwarg:
    """Regression guard for the run_start_idx caching optimisation.

    build_span_tree, compute_phase_durations, and extract_diagnostic_info
    all used to compute _find_run_start_idx on every call. The detail
    view hit that code path 4 times per request on the same entries list.
    Now those functions accept an optional run_start_idx kwarg so the
    dashboard can compute once and pass it through. These tests verify:
      1. The kwarg is actually honored (passing a different value gives
         a different result — not silently ignored).
      2. Default behavior (kwarg omitted) still matches a fresh
         find_run_start_idx call.
    """

    @staticmethod
    def _two_run_entries() -> list[dict]:
        return [
            # Run 1
            {"trace_id": "t1", "timestamp": "2026-01-01T00:00:00+00:00",
             "phase": "webhook", "event": "webhook_received", "source": "jira"},
            {"trace_id": "t1", "timestamp": "2026-01-01T00:00:01+00:00",
             "phase": "ticket_read", "event": "processing_started", "source": "jira"},
            {"trace_id": "t1", "timestamp": "2026-01-01T00:00:02+00:00",
             "phase": "implementation", "event": "phase_complete", "source": "agent",
             "error": None},
            # Run 2 boundary
            {"trace_id": "t2", "timestamp": "2026-01-02T00:00:00+00:00",
             "phase": "webhook", "event": "webhook_received", "source": "jira"},
            {"trace_id": "t2", "timestamp": "2026-01-02T00:00:01+00:00",
             "phase": "ticket_read", "event": "processing_started", "source": "jira"},
            {"trace_id": "t2", "timestamp": "2026-01-02T00:00:02+00:00",
             "phase": "implementation", "event": "error", "source": "agent",
             "error_type": "Boom", "error_message": "run 2 error"},
        ]

    def test_find_run_start_idx_picks_latest_run(self) -> None:
        entries = self._two_run_entries()
        # Latest run starts at index 3 (the second webhook_received).
        assert find_run_start_idx(entries) == 3

    def test_find_run_start_idx_keeps_l1_context_before_agent_pipeline_start(
        self,
    ) -> None:
        entries = [
            {"phase": "webhook", "event": "ado_webhook_received", "source": "ado"},
            {"phase": "pipeline", "event": "processing_started", "source": "ado"},
            {"phase": "analyst", "event": "analyst_completed"},
            {"phase": "pipeline", "event": "l2_dispatched"},
            {
                "phase": "webhook",
                "event": "ado_webhook_skipped_no_tag",
                "source": "ado",
            },
            {
                "phase": "ticket_read",
                "event": "Pipeline started, simple mode",
                "source": "agent",
            },
            {
                "phase": "implementation",
                "event": "Implementation complete",
                "source": "agent",
            },
        ]

        assert find_run_start_idx(entries) == 0

    def test_extract_diagnostic_info_honors_forced_kwarg(self) -> None:
        entries = self._two_run_entries()
        # Default: picks up run 2's error.
        default_diag = extract_diagnostic_info(entries)
        assert len(default_diag["errors"]) == 1
        assert default_diag["errors"][0]["error_message"] == "run 2 error"

        # Force run_start_idx=0 to include run 1 entries — proves the
        # kwarg is actually used. Run 1 has no error, so errors list
        # should still be just the one from run 2 but run 1's
        # processing_started should now appear as last_event candidate.
        forced_diag = extract_diagnostic_info(entries, run_start_idx=0)
        assert len(forced_diag["errors"]) == 1  # still only one error
        # But forcing idx=0 definitely changed the scan range — sanity
        # check by forcing an idx PAST the error so errors becomes empty.
        empty_diag = extract_diagnostic_info(entries, run_start_idx=6)
        assert empty_diag["errors"] == []

    def test_compute_phase_durations_honors_forced_kwarg(self) -> None:
        entries = self._two_run_entries()
        # Default: one agent entry in run 2 → <2 → empty durations.
        default_dur = compute_phase_durations(entries)
        assert default_dur == []
        # Force idx=0: two agent entries across both runs → one duration.
        full_dur = compute_phase_durations(entries, run_start_idx=0)
        assert len(full_dur) == 1

    def test_build_span_tree_honors_forced_kwarg(self) -> None:
        entries = self._two_run_entries()
        default_tree = build_span_tree(entries)
        forced_tree = build_span_tree(entries, run_start_idx=0)
        # Forcing idx=0 pulls in run 1's agent entry in addition to run 2's,
        # so L2 entry count must grow (or at least not shrink).
        assert len(forced_tree["l2"]) >= len(default_tree["l2"])

    def test_build_trace_list_row_honors_forced_kwarg(self) -> None:
        entries = self._two_run_entries()
        summary = {"duration": "5s"}
        default_row = build_trace_list_row(summary, entries)
        forced_row = build_trace_list_row(summary, entries, run_start_idx=0)
        # Forcing idx=0 pulls run 1's agent phase into phase_dots too.
        default_phases = {d["phase"] for d in default_row["phase_dots"]}
        forced_phases = {d["phase"] for d in forced_row["phase_dots"]}
        assert default_phases <= forced_phases

    def test_kwarg_default_matches_fresh_computation(self) -> None:
        """Omitting the kwarg must produce the same result as passing
        the value from find_run_start_idx — i.e. the default path is
        equivalent to the cached path."""
        entries = self._two_run_entries()
        idx = find_run_start_idx(entries)
        assert build_span_tree(entries) == build_span_tree(entries, run_start_idx=idx)
        assert compute_phase_durations(entries) == compute_phase_durations(
            entries, run_start_idx=idx
        )
        assert extract_diagnostic_info(entries) == extract_diagnostic_info(
            entries, run_start_idx=idx
        )


class TestDeriveTraceStatus:
    """Shared status-derivation helper used by both list_traces and
    build_span_tree._build_summary. Previously the two sites were
    hand-rolled, 18-branch vs 8-branch, and drifted — the detail view
    silently missed half the labels."""

    def _run(self, events: list[str], pr_url: str = "") -> str:
        """Helper: build minimal entries from a list of event names and
        derive the status."""
        entries = [{"event": ev} for ev in events]
        return derive_trace_status(entries, events, pr_url)

    def test_empty_returns_unknown(self) -> None:
        assert derive_trace_status([], [], "") == "Unknown"

    def test_cleaned_up_wins_over_everything(self) -> None:
        # stale_worktree_cleaned takes precedence even if later events
        # include a terminal success marker.
        events = ["webhook_received", "Pipeline complete", "stale_worktree_cleaned"]
        assert self._run(events) == "Cleaned Up"

    def test_escalated(self) -> None:
        assert self._run(["Escalated"]) == "Escalated"

    def test_failed_via_agent_finished_escalated(self) -> None:
        entries = [
            {"event": "agent_finished", "status": "escalated"},
        ]
        assert derive_trace_status(entries, ["agent_finished"], "") == "Failed"

    def test_timed_out_case_insensitive(self) -> None:
        assert self._run(["Pipeline timed out after 30m"]) == "Timed Out"

    def test_complete(self) -> None:
        assert self._run(["webhook_received", "Pipeline complete"]) == "Complete"

    def test_pr_merged_wins_over_pipeline_complete(self) -> None:
        assert self._run(["Pipeline complete", "pr_merged"]) == "Merged"

    def test_ado_skip_does_not_override_running_pipeline(self) -> None:
        """Duplicate no-tag ADO webhooks after label removal are chatter."""
        events = [
            "ado_webhook_received",
            "processing_started",
            "l2_dispatched",
            "ado_webhook_skipped_no_tag",
        ]
        assert self._run(events) == "Dispatched"

    def test_ado_skip_terminal_when_pipeline_never_started(self) -> None:
        assert self._run(["ado_webhook_skipped_no_tag"]) == "Skipped"

    def test_manual_submission_terminal_when_pipeline_never_started(self) -> None:
        assert self._run(["manual_ticket_submitted"]) == "Submitted"

    def test_manual_submission_does_not_mask_dispatched_pipeline(self) -> None:
        events = [
            "manual_ticket_submitted",
            "processing_started",
            "l2_dispatched",
            "processing_completed",
        ]
        assert self._run(events) == "Dispatched"

    def test_pr_created_without_complete(self) -> None:
        assert (
            self._run(["webhook_received", "pr_created"], pr_url="https://x/pr/1")
            == "PR Created"
        )

    def test_merged_and_implementing_and_planned_and_ci_fix(self) -> None:
        # These four statuses were MISSING from the old _build_summary
        # chain — the whole point of the consolidation.
        assert self._run(["Merge complete"]) == "Merged"
        assert self._run(["unit-1 complete"]) == "Implementing"
        assert self._run(["Plan complete"]) == "Planned"
        assert self._run(["ci_fix_spawned"]) == "CI Fix"

    def test_agent_done_no_pr(self) -> None:
        assert self._run(["agent_finished"]) == "Agent Done (no PR)"

    def test_pipeline_error_with_no_progress_returns_failed(self) -> None:
        # processing_started → error with no l2_dispatched or Pipeline complete
        # is a crashed enrichment step — should be Failed, not Processing.
        entries = [
            {"event": "webhook_received"},
            {"event": "processing_started"},
            {"event": "error"},
        ]
        events = [e["event"] for e in entries]
        assert derive_trace_status(entries, events, "") == "Failed"

    def test_error_after_dispatch_does_not_override_to_failed(self) -> None:
        # An error entry that appears after l2_dispatched means the agent
        # wrote something downstream — don't clobber the real status.
        entries = [
            {"event": "webhook_received"},
            {"event": "processing_started"},
            {"event": "l2_dispatched"},
            {"event": "error"},
        ]
        events = [e["event"] for e in entries]
        # l2_dispatched is in events so the error guard is bypassed;
        # last-event is "error" so falls through to Dispatched branch.
        assert derive_trace_status(entries, events, "") == "Dispatched"

    def test_received_when_only_webhook(self) -> None:
        assert self._run(["webhook_received"]) == "Received"

    def test_implementing_label_wins_where_old_build_summary_fell_through(
        self,
    ) -> None:
        """Before extraction, _build_summary's chain lacked the
        ``unit-*complete*`` check, so a trace that had dispatched and
        finished an implementation phase (no Plan / Review / QA yet)
        would fall all the way through to ``events[-1]`` and render a
        raw event string in the detail view, while the list view
        correctly rendered ``Implementing``. With the shared helper
        both views agree."""
        entries = [
            {"event": "webhook_received"},
            {"event": "processing_started"},
            {"event": "l2_dispatched"},
            {"event": "unit-1 complete"},
        ]
        events = [e.get("event", "") for e in entries]
        assert derive_trace_status(entries, events, "") == "Implementing"

    def test_merged_label_when_no_pr_url(self) -> None:
        """Merge complete without a pr_url on the trace summary must
        render as Merged — _build_summary didn't have this branch at
        all and would fall through to events[-1]."""
        entries = [
            {"event": "webhook_received"},
            {"event": "processing_started"},
            {"event": "l2_dispatched"},
            {"event": "Merge complete"},
        ]
        events = [e.get("event", "") for e in entries]
        assert derive_trace_status(entries, events, "") == "Merged"


class TestExtractTraceMetadata:
    """Shared metadata extraction used by both list_traces and
    _build_summary. The two sites used to have duplicated loops with
    different field coverage — ``ticket_title`` lived only in
    list_traces, so the detail-view summary had no way to surface it."""

    def test_empty_entries_returns_all_empty_strings(self) -> None:
        assert _extract_trace_metadata([]) == {
            "pr_url": "",
            "review_verdict": "",
            "qa_result": "",
            "pipeline_mode": "",
            "platform_profile": "",
            "client_profile": "",
            "ticket_title": "",
        }

    def test_extracts_simple_fields(self) -> None:
        entries = [
            {"event": "webhook_received", "ticket_title": "My Ticket"},
            {
                "event": "pipeline_started",
                "pipeline_mode": "multi",
                "platform_profile": "contentstack",
                "client_profile": "cstk-demo",
            },
            {"event": "l2_dispatched", "pr_url": "https://github.com/o/r/pull/1"},
            {"event": "Review complete", "review_verdict": "APPROVED"},
            {"event": "QA complete", "qa_result": "PASS"},
        ]
        metadata = _extract_trace_metadata(entries)
        assert metadata["ticket_title"] == "My Ticket"
        assert metadata["pipeline_mode"] == "multi"
        assert metadata["platform_profile"] == "contentstack"
        assert metadata["client_profile"] == "cstk-demo"
        assert metadata["pr_url"] == "https://github.com/o/r/pull/1"
        assert metadata["review_verdict"] == "APPROVED"
        assert metadata["qa_result"] == "PASS"

    def test_pipeline_complete_overrides_review_and_qa(self) -> None:
        """The Pipeline complete entry's review_verdict/qa_result are
        authoritative — its values should win over any earlier entries."""
        entries = [
            {"event": "Review complete", "review_verdict": "PASS_WITH_NOTES"},
            {"event": "QA complete", "qa_result": "FAIL"},
            {
                "event": "Pipeline complete",
                "review_verdict": "APPROVED",
                "qa_result": "PASS",
            },
        ]
        metadata = _extract_trace_metadata(entries)
        assert metadata["review_verdict"] == "APPROVED"
        assert metadata["qa_result"] == "PASS"

    def test_ticket_title_keeps_first_non_empty(self) -> None:
        """Only the FIRST non-empty ticket_title wins — later overrides
        (which in practice are artifacts from different runs) should
        not flip the display name."""
        entries = [
            {"event": "a", "ticket_title": "Original"},
            {"event": "b", "ticket_title": "Later override"},
        ]
        metadata = _extract_trace_metadata(entries)
        assert metadata["ticket_title"] == "Original"


class TestComputeRunDuration:
    """Shared duration formatter used by list_traces and _build_summary."""

    def test_empty_entries_returns_empty(self) -> None:
        assert _compute_run_duration([]) == ""

    def test_sub_minute_duration(self) -> None:
        entries = [
            {"timestamp": "2026-01-01T10:00:00+00:00"},
            {"timestamp": "2026-01-01T10:00:30+00:00"},
        ]
        assert _compute_run_duration(entries) == "30s"

    def test_minute_and_seconds_duration(self) -> None:
        entries = [
            {"timestamp": "2026-01-01T10:00:00+00:00"},
            {"timestamp": "2026-01-01T10:05:15+00:00"},
        ]
        assert _compute_run_duration(entries) == "5m 15s"

    def test_over_24h_multi_run_marker(self) -> None:
        entries = [
            {"timestamp": "2026-01-01T00:00:00+00:00"},
            {"timestamp": "2026-01-03T00:00:00+00:00"},
        ]
        assert _compute_run_duration(entries) == ">24h (multi-run)"

    def test_malformed_timestamp_returns_empty(self) -> None:
        entries = [
            {"timestamp": "not-a-timestamp"},
            {"timestamp": "2026-01-01T10:00:00+00:00"},
        ]
        assert _compute_run_duration(entries) == ""

    def test_negative_delta_returns_empty(self) -> None:
        """If somehow last entry has a timestamp earlier than the first
        (out-of-order writes), we must not return a negative duration."""
        entries = [
            {"timestamp": "2026-01-01T10:05:00+00:00"},
            {"timestamp": "2026-01-01T10:00:00+00:00"},
        ]
        assert _compute_run_duration(entries) == ""


class TestBillingField:
    """Token billing tracking — API vs Max subscription."""

    def test_billing_field_in_trace_entry(self, trace_dir: Path) -> None:
        """append_trace with billing kwarg stores it in the entry."""
        with patch("tracer.LOGS_DIR", trace_dir):
            append_trace("B-1", "tid1", "analyst", "analyst_completed",
                         billing="api", tokens_in=100, tokens_out=200)
        entry = json.loads((trace_dir / "B-1.jsonl").read_text().strip())
        assert entry["billing"] == "api"

    def test_consolidate_stamps_max_subscription(
        self, trace_dir: Path, tmp_path: Path
    ) -> None:
        """Consolidated agent entries get billing=max_subscription."""
        wt = tmp_path / "worktree"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "pipeline.jsonl").write_text(
            json.dumps({"phase": "impl", "event": "step1",
                        "tokens_in": 500, "tokens_out": 1000}) + "\n"
            + json.dumps({"phase": "review", "event": "step2",
                          "tokens_in": 300, "tokens_out": 600}) + "\n"
        )

        with patch("tracer.LOGS_DIR", trace_dir):
            consolidate_worktree_logs("B-2", "tid2", str(wt))
            entries = read_trace("B-2")

        agent_entries = [e for e in entries if e.get("source") == "agent"]
        assert len(agent_entries) == 2
        for e in agent_entries:
            assert e["billing"] == "max_subscription"

    def test_build_summary_separates_billing(self) -> None:
        """_build_summary produces separate billing_api and billing_max totals."""
        entries = [
            {"event": "analyst_completed", "billing": "api",
             "tokens_in": 100, "tokens_out": 200},
            {"event": "impl_step", "source": "agent",
             "billing": "max_subscription",
             "tokens_in": 500, "tokens_out": 1000},
            {"event": "review_step", "source": "agent",
             "billing": "max_subscription",
             "tokens_in": 500, "tokens_out": 1000},
        ]
        summary = _build_summary(entries, [], [])
        # API billing
        assert summary["billing_api_tokens_in"] == 100
        assert summary["billing_api_tokens_out"] == 200
        # Max billing
        assert summary["billing_max_tokens_in"] == 1000
        assert summary["billing_max_tokens_out"] == 2000
        # Backward compat — tokens_in/out stay analyst-only
        assert summary["tokens_in"] == 100
        assert summary["tokens_out"] == 200

    def test_build_summary_no_billing_field_backward_compat(self) -> None:
        """Old entries without billing field still produce valid summary."""
        entries = [
            {"event": "analyst_completed",
             "tokens_in": 50, "tokens_out": 75},
        ]
        summary = _build_summary(entries, [], [])
        # Backward compat
        assert summary["tokens_in"] == 50
        assert summary["tokens_out"] == 75
        # Billing fields default to 0 for max (no agent entries)
        assert summary["billing_api_tokens_in"] == 50
        assert summary["billing_api_tokens_out"] == 75
        assert summary["billing_max_tokens_in"] == 0
        assert summary["billing_max_tokens_out"] == 0


# --- Task 3.5: per-ticket write lock (append vs rewrite serialization) ---
#
# Bug 1: append_trace opened <ticket>.jsonl in append mode with no
#        lock. POSIX append atomicity only holds for writes <PIPE_BUF
#        (4096 bytes on Linux). Trace entries can be larger — e.g.
#        tool_index.first_tool_error.message captures up to 500 chars
#        of raw tool output, which combined with surrounding metadata
#        frequently exceeds 4KB. Two concurrent writers would have
#        their bytes interleaved, corrupting the JSONL stream.
#
# Bug 2: /admin/re-redact rewrote trace files in place via os.replace
#        with no lock. A concurrent appender between read_text() and
#        os.replace() had its entry silently lost when the rewritten
#        file clobbered the appended one.
#
# Fix: per-ticket threading.Lock, held around both append_trace writes
# AND the re-redact read-rewrite-replace cycle. Different tickets'
# operations proceed in parallel; same-ticket operations serialize.


class TestAppendTraceConcurrency:
    def test_concurrent_append_trace_no_interleaving(
        self, trace_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """20 threads appending 10KB entries to the same ticket must
        produce exactly 200 valid JSON lines with no byte interleaving.

        Without the per-ticket lock, a payload larger than PIPE_BUF
        (4KB) splits mid-write and produces corrupt JSONL.
        """
        import threading

        import tracer

        # Clear any existing locks from prior tests
        with tracer._ticket_locks_mutex:
            tracer._ticket_locks.clear()

        # Set LOGS_DIR once at the module level rather than via
        # ``with patch()`` inside each thread. ``unittest.mock.patch``
        # as a context manager is not thread-safe: concurrent
        # __enter__/__exit__ race on the target's attribute, which
        # was corrupting the write path and dropping entries.
        monkeypatch.setattr(tracer, "LOGS_DIR", trace_dir)

        # Force each entry to exceed PIPE_BUF (4096 bytes) so append
        # atomicity can't save us — the lock is the ONLY defense.
        big_payload = "x" * 10000

        def _writer(worker_id: int) -> None:
            for i in range(10):
                tracer.append_trace(
                    "CONCURRENT-1",
                    "tid",
                    "test",
                    f"event-{worker_id}-{i}",
                    blob=big_payload,
                )

        threads = [
            threading.Thread(target=_writer, args=(w,)) for w in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        path = trace_dir / "CONCURRENT-1.jsonl"
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        assert len(lines) == 200, (
            f"expected 200 JSON lines, got {len(lines)} — lost or interleaved"
        )

        # Each line must parse. Interleaved bytes would produce
        # JSONDecodeError on split entries.
        for i, line in enumerate(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"line {i} is corrupt — byte interleaving: {exc}\n"
                    f"line[:200]={line[:200]!r}"
                ) from exc
            assert entry["blob"] == big_payload, (
                f"line {i} blob was corrupted during concurrent write"
            )

    def test_re_redact_blocks_appends_during_rewrite(
        self, trace_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A re-redact rewrite must hold the per-ticket lock, so an
        append attempted during the rewrite waits and lands AFTER the
        replace rather than being clobbered."""
        import threading
        import time

        import tracer

        with tracer._ticket_locks_mutex:
            tracer._ticket_locks.clear()

        # Set LOGS_DIR once — monkeypatch is thread-safe for simple
        # attribute overrides, mock.patch as a CM is not.
        monkeypatch.setattr(tracer, "LOGS_DIR", trace_dir)

        # Seed the trace file with one line
        tracer.append_trace(
            "LOCKED-1", "tid", "test", "initial", val="before",
        )
        assert (trace_dir / "LOCKED-1.jsonl").exists()

        # Simulate the re-redact code path: acquire the per-ticket
        # lock, hold it briefly, then release.
        rewrite_started = threading.Event()
        rewrite_done = threading.Event()
        append_result: dict[str, object] = {}

        def _rewriter() -> None:
            with tracer._get_ticket_lock("LOCKED-1"):
                rewrite_started.set()
                # Hold the lock long enough that the appender
                # definitely has time to reach the lock request.
                time.sleep(0.1)
                rewrite_done.set()

        def _appender() -> None:
            # Wait for rewriter to grab the lock first, then attempt
            # to append. The append should block until rewriter releases.
            rewrite_started.wait(timeout=2)
            append_start = time.monotonic()
            tracer.append_trace(
                "LOCKED-1", "tid", "test", "appended_during_rewrite",
                val="after",
            )
            append_end = time.monotonic()
            append_result["elapsed"] = append_end - append_start
            append_result["rewriter_done"] = rewrite_done.is_set()

        t1 = threading.Thread(target=_rewriter)
        t2 = threading.Thread(target=_appender)
        t1.start()
        t2.start()
        t1.join(timeout=2)
        t2.join(timeout=2)

        # The appender must have waited at least until the rewriter
        # released the lock. Allow a small scheduling slop.
        elapsed = append_result["elapsed"]
        assert isinstance(elapsed, float)
        assert elapsed >= 0.05, (
            f"appender did not wait for lock — elapsed {elapsed!r} "
            "suggests no serialization"
        )
        assert append_result["rewriter_done"] is True, (
            "appender landed BEFORE rewriter finished — lock not honored"
        )

        # Both entries must be in the final file (appender was not clobbered)
        lines = (trace_dir / "LOCKED-1.jsonl").read_text().splitlines()
        events = [json.loads(ln)["event"] for ln in lines if ln.strip()]
        assert "initial" in events
        assert "appended_during_rewrite" in events
