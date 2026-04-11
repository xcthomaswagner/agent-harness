"""Tool-call index — declarative summary of a Claude Code session-stream.jsonl.

Parses NDJSON stream output once and produces a structured dict capturing
tool usage, error counts, and MCP server availability. Used by the L1 trace
consolidation step to support post-mortem analysis without re-reading the
(potentially megabyte-scale) stream file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


def _mcp_server_name(tool_name: str) -> str | None:
    """Extract the server name from an MCP-style tool name.

    MCP tools are named ``mcp__<server>__<tool>``.
    """
    if not tool_name.startswith("mcp__"):
        return None
    rest = tool_name[len("mcp__"):]
    sep = rest.find("__")
    if sep <= 0:
        return None
    return rest[:sep]


def build_tool_index(stream_path: Path) -> dict[str, Any]:
    """Parse a session-stream.jsonl file and return a tool-call summary.

    See module docstring for the shape of the returned dict. Missing or empty
    files return a valid but empty index. Malformed lines are skipped.
    """
    tool_counts: dict[str, int] = {}
    tool_errors: dict[str, int] = {}
    mcp_servers_used: set[str] = set()
    mcp_servers_available: list[str] = []
    first_tool_error: dict[str, Any] | None = None
    assistant_turns = 0
    tool_call_count = 0

    # Map tool_use_id -> (tool_name, line_number) so we can attribute errors.
    tool_use_by_id: dict[str, tuple[str, int]] = {}

    if not stream_path.exists():
        return _empty_index(mcp_servers_available)

    try:
        raw_lines = stream_path.read_text().splitlines()
    except OSError as exc:
        logger.debug("tool_index_read_failed", path=str(stream_path), error=str(exc))
        return _empty_index(mcp_servers_available)

    for idx, raw in enumerate(raw_lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.debug(
                "tool_index_skip_malformed_line",
                path=str(stream_path),
                line=idx,
                error=str(exc),
            )
            continue

        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            servers = event.get("mcp_servers") or []
            for s in servers:
                if isinstance(s, dict) and s.get("status") == "connected":
                    name = s.get("name")
                    if isinstance(name, str):
                        # Canonicalize to the same form MCP tool prefixes use
                        # (underscore-safe). This makes mcp_servers_used and
                        # mcp_servers_available directly comparable.
                        canonical = _canonical_server(name)
                        if canonical not in mcp_servers_available:
                            mcp_servers_available.append(canonical)
            continue

        if etype == "assistant":
            assistant_turns += 1
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                if not isinstance(name, str):
                    continue
                tool_call_count += 1
                tool_counts[name] = tool_counts.get(name, 0) + 1
                tool_use_id = block.get("id")
                if isinstance(tool_use_id, str):
                    tool_use_by_id[tool_use_id] = (name, idx)
                server = _mcp_server_name(name)
                if server:
                    # Canonicalize to match mcp_servers_available (which
                    # was canonicalized above from the init event). MCP
                    # tool names preserve the original server form
                    # (e.g. ``mcp__browser-bridge__browser_click`` ->
                    # ``browser-bridge``), so without this both sides
                    # drift and every hyphenated server that IS used
                    # still shows up in mcp_servers_unused.
                    mcp_servers_used.add(_canonical_server(server))
            continue

        if etype == "user":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                if not block.get("is_error"):
                    continue
                tool_use_id = block.get("tool_use_id")
                if not isinstance(tool_use_id, str):
                    # Orphan tool_result with no id — can't attribute to a tool.
                    logger.debug(
                        "tool_index_orphan_tool_result",
                        path=str(stream_path),
                        line=idx,
                    )
                    continue
                lookup = tool_use_by_id.get(tool_use_id)
                if lookup is None:
                    # Orphan: tool_use_id didn't match any prior tool_use.
                    # Don't pollute tool_errors with an "unknown" bucket — this
                    # counter is keyed by tool name, not by event id.
                    logger.debug(
                        "tool_index_unmatched_tool_use_id",
                        path=str(stream_path),
                        line=idx,
                        tool_use_id=tool_use_id,
                    )
                    continue
                tool_name, origin_line = lookup
                tool_errors[tool_name] = tool_errors.get(tool_name, 0) + 1
                if first_tool_error is None:
                    first_tool_error = {
                        "tool": tool_name,
                        "line": origin_line,
                        "message": _extract_error_message(block.get("content")),
                    }

    # mcp_servers_available is already canonicalized above; use direct set
    # difference now that both lists share the same representation.
    used_set = set(mcp_servers_used)
    mcp_servers_unused = [s for s in mcp_servers_available if s not in used_set]

    return {
        "tool_counts": tool_counts,
        "tool_errors": tool_errors,
        "mcp_servers_used": sorted(mcp_servers_used),
        "mcp_servers_available": list(mcp_servers_available),
        "mcp_servers_unused": mcp_servers_unused,
        "first_tool_error": first_tool_error,
        "assistant_turns": assistant_turns,
        "tool_call_count": tool_call_count,
    }


def _empty_index(mcp_servers_available: list[str]) -> dict[str, Any]:
    return {
        "tool_counts": {},
        "tool_errors": {},
        "mcp_servers_used": [],
        "mcp_servers_available": list(mcp_servers_available),
        "mcp_servers_unused": list(mcp_servers_available),
        "first_tool_error": None,
        "assistant_turns": 0,
        "tool_call_count": 0,
    }


def _canonical_server(name: str) -> str:
    """Normalize server names so init-event names match MCP tool-name prefixes.

    MCP tool names use ``mcp__<server>__<tool>`` where ``<server>`` is the
    underscore-safe form. Init events sometimes report server names with
    spaces, dots, or colons. Normalize both sides for comparison.
    """
    return name.replace(" ", "_").replace(".", "_").replace(":", "_").replace("-", "_")


def _extract_error_message(content: Any) -> str:
    """Best-effort text extraction from a tool_result content payload."""
    if isinstance(content, str):
        return content[:500]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text[:500]
    return ""
