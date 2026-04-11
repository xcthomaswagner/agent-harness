"""Tests for tool_index — parsing session-stream.jsonl into a tool-call summary."""

from __future__ import annotations

import json
from pathlib import Path

from tool_index import build_tool_index


def _write_stream(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _init_event(servers: list[tuple[str, str]]) -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "mcp_servers": [{"name": n, "status": s} for n, s in servers],
    }


def _assistant_tool_use(name: str, tool_id: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": name, "id": tool_id, "input": {}}
            ]
        },
    }


def _user_tool_result(tool_id: str, *, is_error: bool, text: str = "") -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": is_error,
                    "content": text,
                }
            ]
        },
    }


def test_empty_stream_file(tmp_path: Path) -> None:
    stream = tmp_path / "session-stream.jsonl"
    stream.write_text("")
    idx = build_tool_index(stream)
    assert idx["tool_counts"] == {}
    assert idx["tool_errors"] == {}
    assert idx["mcp_servers_used"] == []
    assert idx["mcp_servers_available"] == []
    assert idx["mcp_servers_unused"] == []
    assert idx["first_tool_error"] is None
    assert idx["assistant_turns"] == 0
    assert idx["tool_call_count"] == 0


def test_nonexistent_stream_file(tmp_path: Path) -> None:
    idx = build_tool_index(tmp_path / "nope.jsonl")
    assert idx["tool_call_count"] == 0
    assert idx["mcp_servers_available"] == []


def test_init_event_only_populates_servers(tmp_path: Path) -> None:
    stream = tmp_path / "session-stream.jsonl"
    _write_stream(
        stream,
        [
            _init_event(
                [
                    ("salesforce", "connected"),
                    ("github", "connected"),
                    ("playwright", "failed"),
                ]
            )
        ],
    )
    idx = build_tool_index(stream)
    assert idx["tool_call_count"] == 0
    assert idx["assistant_turns"] == 0
    # Only connected servers count as available. Both lists use the
    # canonical (underscore-safe) form that matches MCP tool prefixes.
    assert idx["mcp_servers_available"] == ["salesforce", "github"]
    assert idx["mcp_servers_used"] == []
    assert idx["mcp_servers_unused"] == ["salesforce", "github"]


def test_init_event_canonicalizes_server_names(tmp_path: Path) -> None:
    """Server names with spaces/dots/colons are canonicalized to match tool prefixes."""
    stream = tmp_path / "session-stream.jsonl"
    _write_stream(
        stream,
        [
            _init_event(
                [
                    ("claude.ai Gmail", "connected"),
                    ("plugin:context7:context7", "connected"),
                ]
            ),
            _assistant_tool_use("mcp__claude_ai_Gmail__gmail_search", "t1"),
        ],
    )
    idx = build_tool_index(stream)
    # Available list uses canonical form — same key shape as used list
    assert idx["mcp_servers_available"] == [
        "claude_ai_Gmail",
        "plugin_context7_context7",
    ]
    assert idx["mcp_servers_used"] == ["claude_ai_Gmail"]
    # Direct set comparison works now that both lists are canonicalized
    assert idx["mcp_servers_unused"] == ["plugin_context7_context7"]


def test_orphan_tool_result_does_not_create_unknown_bucket(tmp_path: Path) -> None:
    """tool_result with unmatched tool_use_id must not pollute tool_errors."""
    stream = tmp_path / "session-stream.jsonl"
    _write_stream(
        stream,
        [
            _init_event([]),
            # Error result for a tool_use_id that was never seen
            _user_tool_result("ghost_id", is_error=True, text="orphan"),
        ],
    )
    idx = build_tool_index(stream)
    assert idx["tool_errors"] == {}
    assert "unknown" not in idx["tool_errors"]
    assert idx["first_tool_error"] is None


def test_counts_mixed_tool_uses(tmp_path: Path) -> None:
    stream = tmp_path / "session-stream.jsonl"
    _write_stream(
        stream,
        [
            _init_event([("salesforce", "connected")]),
            _assistant_tool_use("Bash", "t1"),
            _assistant_tool_use("Read", "t2"),
            _assistant_tool_use("mcp__salesforce__sf_deploy", "t3"),
        ],
    )
    idx = build_tool_index(stream)
    assert idx["tool_counts"] == {
        "Bash": 1,
        "Read": 1,
        "mcp__salesforce__sf_deploy": 1,
    }
    assert idx["tool_call_count"] == 3
    assert idx["assistant_turns"] == 3
    assert idx["mcp_servers_used"] == ["salesforce"]
    assert idx["mcp_servers_unused"] == []


def test_tool_error_captures_first_and_line(tmp_path: Path) -> None:
    stream = tmp_path / "session-stream.jsonl"
    _write_stream(
        stream,
        [
            _init_event([]),
            _assistant_tool_use("Bash", "t1"),  # line 2
            _user_tool_result("t1", is_error=True, text="command failed"),  # line 3
            _assistant_tool_use("Read", "t2"),
            _user_tool_result("t2", is_error=False),
        ],
    )
    idx = build_tool_index(stream)
    assert idx["tool_errors"] == {"Bash": 1}
    assert idx["first_tool_error"] is not None
    assert idx["first_tool_error"]["tool"] == "Bash"
    assert idx["first_tool_error"]["line"] == 2
    assert "command failed" in idx["first_tool_error"]["message"]


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    stream = tmp_path / "session-stream.jsonl"
    good_lines = [
        json.dumps(_init_event([])),
        "this is not json at all {{{",
        json.dumps(_assistant_tool_use("Bash", "t1")),
    ]
    stream.write_text("\n".join(good_lines) + "\n")
    idx = build_tool_index(stream)
    assert idx["tool_counts"] == {"Bash": 1}
    assert idx["tool_call_count"] == 1


def test_mcp_server_connected_but_unused(tmp_path: Path) -> None:
    stream = tmp_path / "session-stream.jsonl"
    _write_stream(
        stream,
        [
            _init_event(
                [
                    ("salesforce", "connected"),
                    ("playwright", "connected"),
                    ("github", "connected"),
                ]
            ),
            _assistant_tool_use("mcp__salesforce__sf_deploy", "t1"),
        ],
    )
    idx = build_tool_index(stream)
    assert "salesforce" in idx["mcp_servers_used"]
    assert "playwright" in idx["mcp_servers_unused"]
    assert "github" in idx["mcp_servers_unused"]
    assert "salesforce" not in idx["mcp_servers_unused"]
