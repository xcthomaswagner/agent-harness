"""Tool-call index — declarative summary of a Claude Code session-stream.jsonl.

Parses NDJSON stream output once and produces a structured dict capturing
tool usage, error counts, and MCP server availability. Used by the L1 trace
consolidation step to support post-mortem analysis without re-reading the
(potentially megabyte-scale) stream file.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


# Wrappers that front the real command verb. Order-independent; all
# stripped in a loop until the command settles.
_BASH_WRAPPER_VERBS: set[str] = {
    "env", "sudo", "nohup", "time", "exec", "timeout",
}

_BASH_SPLIT_RE = re.compile(r"&&|\|\||;")

# ``timeout`` accepts ``30``, ``5s``, ``5m``, ``2h``, ``1d`` — a bare
# integer/float, optionally suffixed with a unit letter. Anything
# outside that shape is a real command token we shouldn't eat.
_TIMEOUT_DURATION_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$")

# Short flags that DO take a separate value per wrapper. Everything
# else is a toggle — we must NOT consume the next token as a value,
# or ``sudo -E sf deploy`` returns ``deploy`` instead of ``sf``.
# Long-form ``--flag=value`` always self-contains; only short-form
# with a separate value needs the extra hop.
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-p", "-h", "-U", "-C", "-t", "-r"}),
    "env": frozenset({"-u", "-C", "-S", "-L", "-P"}),
    "timeout": frozenset({"-s", "-k"}),
    "nohup": frozenset(),
    "time": frozenset(),
    "exec": frozenset({"-a", "-c"}),
}


def extract_bash_verb(command: str) -> str:
    """Return the first meaningful verb from a Bash command string, or ''.

    Design notes:
    - Chained commands (``cd /path && sf deploy``, ``source venv && pytest``)
      split on ``&&``/``||``/``;`` and we take the LAST chunk's first verb
      — that's the "real" command; the earlier chunks are setup.
    - Wrapper prefixes (``env X=y``, ``sudo``, ``nohup``, ``timeout N``,
      ``time``, ``exec``) are stripped recursively so ``sudo timeout 30
      sf pull`` resolves to ``sf``.
    - ``bash -c "..."`` / ``sh -c "..."`` unwraps the quoted payload once
      (bounded — we don't recurse arbitrarily deep, just one hop).
    - Leading variable assignments (``FOO=bar BAZ=qux cmd``) are skipped.
    - Empty / unparseable commands return ''. Callers treat empty as
      "not attributable" and skip the count.
    """
    if not command or not isinstance(command, str):
        return ""
    chunks = _BASH_SPLIT_RE.split(command)
    last = chunks[-1].strip() if chunks else command.strip()
    verb = _first_verb_after_preamble(last)
    if verb in {"bash", "sh", "/bin/bash", "/bin/sh"}:
        # Try to unwrap ``bash -c "..."`` — if that fails, the bare
        # ``bash`` invocation is itself the signal we want to record.
        unwrapped = _unwrap_bash_c(last)
        if unwrapped:
            return _first_verb_after_preamble(unwrapped) or verb
    return verb


def _first_verb_after_preamble(command: str) -> str:
    """Strip wrappers + var-assignments, return the first token."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # shlex can't parse unbalanced quotes — fall back to whitespace.
        tokens = command.split()
    while tokens:
        head = tokens[0]
        if not head:
            tokens = tokens[1:]
            continue
        if "=" in head and head.split("=", 1)[0].isidentifier():
            tokens = tokens[1:]
            continue
        base = head.rsplit("/", 1)[-1]
        if base in _BASH_WRAPPER_VERBS:
            # Strip the wrapper itself, its numeric duration (timeout),
            # and any leading -flags including the value that follows a
            # separate-token option like ``sudo -u user``. A short flag
            # only consumes the next token as a value when it is in the
            # wrapper's known value-taking set — otherwise ``sudo -E sf
            # deploy`` would eat ``sf`` and return ``deploy``.
            value_flags = _WRAPPER_VALUE_FLAGS.get(base, frozenset())
            tokens = tokens[1:]
            while tokens:
                nxt = tokens[0]
                if base == "timeout" and _TIMEOUT_DURATION_RE.fullmatch(nxt):
                    tokens = tokens[1:]
                    continue
                if nxt.startswith("-"):
                    tokens = tokens[1:]
                    if (
                        nxt in value_flags
                        and tokens
                        and not tokens[0].startswith("-")
                    ):
                        tokens = tokens[1:]
                    continue
                break
            continue
        return base
    return ""


def _unwrap_bash_c(command: str) -> str:
    """Return the payload of ``bash -c "<payload>"``, else ''."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ""
    if len(tokens) < 3:
        return ""
    if tokens[0].rsplit("/", 1)[-1] not in {"bash", "sh"}:
        return ""
    # Skip any flags before -c.
    i = 1
    while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-c":
        i += 1
    if i >= len(tokens) or tokens[i] != "-c":
        return ""
    if i + 1 >= len(tokens):
        return ""
    return tokens[i + 1]


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
    bash_verb_counts: dict[str, int] = {}
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
                if name == "Bash":
                    cmd = (block.get("input") or {}).get("command")
                    verb = extract_bash_verb(cmd) if isinstance(cmd, str) else ""
                    if verb:
                        bash_verb_counts[verb] = (
                            bash_verb_counts.get(verb, 0) + 1
                        )
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
        "bash_verb_counts": bash_verb_counts,
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
        "bash_verb_counts": {},
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
