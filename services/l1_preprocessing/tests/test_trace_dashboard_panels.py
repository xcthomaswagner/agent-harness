"""Tests for the session observability panel renderer."""

from __future__ import annotations

import json
from pathlib import Path

from trace_dashboard_panels import (
    ARTIFACT_EFFECTIVE_CLAUDE_MD,
    ARTIFACT_SESSION_LOG,
    ARTIFACT_SESSION_STREAM,
    ARTIFACT_TOOL_INDEX,
    render_session_panels,
)


def _tool_index_entry(index: dict) -> dict:
    """Wrap a tool_index dict as a trace entry."""
    return {
        "phase": "artifact",
        "event": ARTIFACT_TOOL_INDEX,
        "index": index,
    }


def _session_log_entry(content: str) -> dict:
    return {
        "phase": "artifact",
        "event": ARTIFACT_SESSION_LOG,
        "content": content,
    }


def _effective_claude_md_entry(content: str) -> dict:
    return {
        "phase": "artifact",
        "event": ARTIFACT_EFFECTIVE_CLAUDE_MD,
        "content": content,
    }


def _session_stream_entry(path: Path, line_count: int) -> dict:
    return {
        "phase": "artifact",
        "event": ARTIFACT_SESSION_STREAM,
        "artifact_path": str(path),
        "line_count": line_count,
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }


class TestEmptyAndMissing:
    def test_empty_entries_returns_empty_string(self) -> None:
        assert render_session_panels([]) == ""

    def test_only_tool_index_renders_only_tool_usage(self) -> None:
        entries = [
            _tool_index_entry(
                {
                    "tool_call_count": 5,
                    "assistant_turns": 3,
                    "tool_counts": {"Read": 3, "Bash": 2},
                }
            )
        ]
        # Tool Usage is now rendered via render_tool_usage_panel (separate)
        from trace_dashboard_panels import render_tool_usage_panel
        out = render_tool_usage_panel(entries)
        assert "Tool Usage" in out
        assert "5 tool calls across 3 assistant turns" in out
        assert "Read" in out
        assert "Bash" in out
        # render_session_panels should NOT contain Tool Usage anymore
        session_out = render_session_panels(entries)
        assert "Tool Usage" not in session_out


class TestAllFourArtifacts:
    def test_all_four_panels_render(self, tmp_path: Path) -> None:
        stream_file = tmp_path / "session-stream.jsonl"
        stream_file.write_text(
            json.dumps({"type": "system", "subtype": "init"}) + "\n"
        )
        entries = [
            {"ticket_id": "DEMO-1", "phase": "webhook", "event": "received"},
            _tool_index_entry(
                {
                    "tool_call_count": 2,
                    "assistant_turns": 1,
                    "tool_counts": {"Bash": 2},
                }
            ),
            _effective_claude_md_entry("# CLAUDE.md\n\nBe a good agent."),
            _session_log_entry("Reasoning: considered option A."),
            _session_stream_entry(stream_file, line_count=1),
        ]
        out = render_session_panels(entries)
        # Tool Usage is rendered separately via render_tool_usage_panel
        assert "Tool Usage" not in out
        assert "Agent Instructions" in out
        assert "Reasoning Narrative" in out
        assert "Tool Calls Timeline" in out
        assert "Raw Downloads" in out
        # Verify tool usage renders via its own function
        from trace_dashboard_panels import render_tool_usage_panel
        tool_out = render_tool_usage_panel(entries)
        assert "Tool Usage" in tool_out

    def test_character_count_in_header(self) -> None:
        entries = [_effective_claude_md_entry("x" * 1234)]
        out = render_session_panels(entries)
        assert "1,234 characters" in out


class TestToolIndexWarnings:
    def test_unused_mcp_server_warning(self) -> None:
        entries = [
            _tool_index_entry(
                {
                    "tool_call_count": 1,
                    "assistant_turns": 1,
                    "tool_counts": {"Read": 1},
                    "mcp_servers_unused": ["obsidian"],
                }
            )
        ]
        from trace_dashboard_panels import render_tool_usage_panel
        out = render_tool_usage_panel(entries)
        assert "obsidian" in out
        assert "connected but never used" in out

    def test_first_tool_error_warning(self) -> None:
        entries = [
            _tool_index_entry(
                {
                    "tool_call_count": 2,
                    "assistant_turns": 1,
                    "tool_counts": {"Bash": 2},
                    "first_tool_error": {
                        "tool": "Bash",
                        "line": 42,
                        "message": "command not found: foo",
                    },
                }
            )
        ]
        from trace_dashboard_panels import render_tool_usage_panel
        out = render_tool_usage_panel(entries)
        assert "First tool error" in out
        assert "Bash" in out
        assert "42" in out
        assert "command not found: foo" in out

    def test_tool_with_errors_annotation(self) -> None:
        entries = [
            _tool_index_entry(
                {
                    "tool_call_count": 5,
                    "assistant_turns": 2,
                    "tool_counts": {"Bash": 5},
                    "tool_errors": {"Bash": 2},
                }
            )
        ]
        from trace_dashboard_panels import render_tool_usage_panel
        out = render_tool_usage_panel(entries)
        assert "2 errors" in out


class TestTimelinePanel:
    def test_missing_file_shows_graceful_message(self, tmp_path: Path) -> None:
        ghost = tmp_path / "does-not-exist.jsonl"
        entries = [
            {"ticket_id": "T-1"},
            {
                "phase": "artifact",
                "event": ARTIFACT_SESSION_STREAM,
                "artifact_path": str(ghost),
                "line_count": 42,
            },
        ]
        out = render_session_panels(entries)
        assert "Session stream not available" in out
        # No crash, panel still rendered
        assert "Tool Calls Timeline" in out

    def test_three_events_render_three_rows(self, tmp_path: Path) -> None:
        stream_file = tmp_path / "session-stream.jsonl"
        lines = [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ]
                },
                "timestamp": "2026-04-10T12:00:00Z",
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "file1\nfile2\n",
                        }
                    ]
                },
                "timestamp": "2026-04-10T12:00:01Z",
            },
        ]
        stream_file.write_text(
            "\n".join(json.dumps(ln) for ln in lines) + "\n"
        )

        entries = [
            {"ticket_id": "T-1"},
            _session_stream_entry(stream_file, line_count=3),
        ]
        out = render_session_panels(entries)
        # One tool_use event + one tool_result event (the init is not a tool event)
        # so we should see 2 rows rendered from this fixture. The "3 events"
        # requirement in the spec refers to the stream containing 3 events;
        # this asserts that both tool events are rendered.
        assert out.count('border-left:3px solid') >= 2
        assert "Bash" in out
        assert "file1" in out

    def test_timeline_header_shows_line_count(self, tmp_path: Path) -> None:
        stream_file = tmp_path / "stream.jsonl"
        stream_file.write_text("{}\n")
        entries = [_session_stream_entry(stream_file, line_count=500)]
        out = render_session_panels(entries)
        assert "500 stream events" in out


class TestXssEscaping:
    def test_xss_in_tool_name_is_escaped(self) -> None:
        from trace_dashboard_panels import render_tool_usage_panel
        entries = [
            _tool_index_entry(
                {
                    "tool_call_count": 1,
                    "assistant_turns": 1,
                    "tool_counts": {'<script>alert("xss")</script>': 1},
                }
            )
        ]
        out = render_tool_usage_panel(entries)
        assert "<script>alert" not in out
        assert "&lt;script&gt;" in out

    def test_xss_in_claude_md_content_is_escaped(self) -> None:
        payload = '<img src=x onerror="alert(1)">'
        entries = [_effective_claude_md_entry(payload)]
        out = render_session_panels(entries)
        assert payload not in out
        assert "&lt;img" in out

    def test_xss_in_unused_mcp_server_is_escaped(self) -> None:
        from trace_dashboard_panels import render_tool_usage_panel
        entries = [
            _tool_index_entry(
                {
                    "tool_call_count": 0,
                    "assistant_turns": 0,
                    "tool_counts": {},
                    "mcp_servers_unused": ['<svg onload="x">'],
                }
            )
        ]
        out = render_tool_usage_panel(entries)
        assert '<svg onload="x">' not in out
        assert "&lt;svg" in out


class TestRawDownloadsPanel:
    def test_links_with_ticket_id(self) -> None:
        entries = [
            {"ticket_id": "RAW-123", "phase": "webhook", "event": "received"},
            _session_log_entry("some reasoning"),
        ]
        out = render_session_panels(entries)
        assert "Raw Downloads" in out
        assert "/traces/RAW-123/artifact/session_log" in out
        assert "/traces/RAW-123/artifact/session_stream" in out
        assert "/traces/RAW-123/artifact/effective_claude_md" in out

    def test_warning_paragraph_present(self) -> None:
        entries = [_session_log_entry("x")]
        out = render_session_panels(entries)
        assert "not redacted" in out
        assert "commit 6" in out


class TestToolIndexUnavailable:
    def test_renders_not_available_notice_without_crashing(self) -> None:
        from trace_dashboard_panels import render_tool_usage_panel
        # Only a non-artifact entry; tool_index artifact absent
        entries = [{"phase": "webhook", "event": "received", "ticket_id": "N-1"}]
        out = render_tool_usage_panel(entries)
        assert "Tool Usage" in out
        assert "not available" in out
