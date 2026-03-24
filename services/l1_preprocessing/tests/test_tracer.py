"""Tests for tracer — trace generation, reading, listing, consolidation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tracer import (
    append_trace,
    consolidate_worktree_logs,
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
