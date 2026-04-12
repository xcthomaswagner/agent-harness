"""Session observability panels for the trace detail view.

This module renders five collapsible panels on the /traces/<id> detail page,
surfacing the artifacts produced by the consolidation stage (commit 1 of the
post-mortem observability plan):

    1. Tool Usage          — compact, always-open; from ``tool_index`` artifact
    2. Agent Instructions  — collapsed; from ``effective_claude_md_artifact``
    3. Reasoning Narrative — collapsed; from ``session_log_artifact``
    4. Tool Calls Timeline — collapsed; paginated from ``session_stream_artifact``
    5. Raw Downloads       — collapsed; link placeholders to the bundle endpoint

The module is isolated from trace_dashboard.py to avoid merge conflicts with
commits 3 and 4 of the same plan which also touch that file. Integration is a
single-line call from _render_detail.

Design notes
------------
- Zero JavaScript. All collapsibles use native HTML ``<details>`` elements.
- All user-provided strings are HTML-escaped. Tool names, paths, payloads, and
  even error messages are rendered via ``html.escape``.
- The module degrades gracefully: missing artifacts omit their panel (except
  Tool Usage, which renders an explicit "not available" notice because its
  absence is itself informative — it means consolidation skipped or the run
  predates commit 1).
- Timeline panel opens the session-stream.jsonl file on disk via the
  ``artifact_path`` reference recorded by the tracer. Up to 100 tool_use /
  tool_result events are rendered inline; the rest link out to a
  /traces/<id>/stream?offset=100 endpoint owned by commit 4.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dashboard_common import escape_html as _e

# Artifact event name constants. Imported directly from tracer.py (commit 1)
# — the authoritative producer. Reading them from any other source would
# risk drifting out of sync with what the consolidation step actually writes.
from tracer import (
    ARTIFACT_EFFECTIVE_CLAUDE_MD,
    ARTIFACT_SESSION_LOG,
    ARTIFACT_SESSION_STREAM,
    ARTIFACT_TOOL_INDEX,
    latest_artifacts,
)

# --- Styling constants (inline; no external CSS dependency) ---

_PANEL_BORDER = "1px solid #E2E8F0"
_PANEL_HEADER_BG = "#F7F9FB"
_WARN_COLOR = "#C79004"
_WARN_BG = "#FEFCE8"

# Tool category → inline color. Used for the Timeline panel rows so a dev
# can pattern-recognise "all MCP" vs "lots of Bash" at a glance.
_TOOL_CATEGORY_COLORS: dict[str, str] = {
    "mcp": "#124D49",       # green — MCP tool
    "bash": "#DB2626",      # red — shell, side effects
    "read": "#3B82F5",      # blue — read-only inspection
    "neutral": "#64748B",   # gray — everything else
}


def _tool_color(tool_name: str) -> str:
    """Map a tool name to one of the four category colors."""
    if not tool_name:
        return _TOOL_CATEGORY_COLORS["neutral"]
    lowered = tool_name.lower()
    if lowered.startswith("mcp__"):
        return _TOOL_CATEGORY_COLORS["mcp"]
    if lowered == "bash":
        return _TOOL_CATEGORY_COLORS["bash"]
    if lowered in {"read", "glob", "grep"}:
        return _TOOL_CATEGORY_COLORS["read"]
    return _TOOL_CATEGORY_COLORS["neutral"]


# latest_artifacts is imported from tracer.py — it walks in reverse, which
# is what we want: on re-triggered traces (multi-run) the dashboard should
# render the latest artifact state, not the first-run version. One pass
# shared across every panel in this module.


def _ticket_id_from_entries(entries: list[dict]) -> str:
    """Best-effort ticket_id extraction for URL interpolation."""
    for entry in entries:
        tid = entry.get("ticket_id")
        if tid:
            return str(tid)
    return ""


def _panel_wrapper(header_html: str, body_html: str, open_by_default: bool) -> str:
    """Wrap a header + body in a <details> element with consistent styling."""
    open_attr = " open" if open_by_default else ""
    return (
        f'<details{open_attr} style="border:{_PANEL_BORDER};border-radius:8px;'
        f'margin-bottom:16px;overflow:hidden">'
        f'<summary style="padding:10px 16px;background:{_PANEL_HEADER_BG};'
        f'cursor:pointer;font-weight:600;font-size:13.2px;list-style:none;'
        f'border-bottom:{_PANEL_BORDER}">{header_html}</summary>'
        f'<div style="padding:12px 16px">{body_html}</div>'
        f'</details>'
    )


# --- Panel 1: Tool Usage (always visible, top of group) ---


def _render_tool_usage_panel(artifacts: dict[str, dict[str, Any]]) -> str:
    """Render the Tool Usage panel from the tool_index artifact.

    The panel is ALWAYS rendered (open-by-default) because its absence is
    informative: it tells the dev the trace predates commit 1 or skipped
    consolidation. ``artifacts`` is the pre-built index from
    ``latest_artifacts``.
    """
    artifact = artifacts.get(ARTIFACT_TOOL_INDEX)

    if artifact is None:
        body = (
            '<div style="color:#64748B;font-size:12px">Tool usage data not '
            'available for this trace (older run or consolidation skipped).</div>'
        )
        header = 'Tool Usage <span class="meta" style="font-weight:400">(unavailable)</span>'
        return _panel_wrapper(header, body, open_by_default=True)

    # tool_index may live in 'content' (JSON string) or 'index' (dict).
    # Shape is authoritative — produced by tool_index.build_tool_index which
    # writes `{"index": {"tool_counts": dict, "tool_errors": dict,
    # "tool_call_count": int, "assistant_turns": int, "mcp_servers_unused":
    # list, "first_tool_error": dict | None, ...}}`. Don't invent fallback
    # shapes; the producer is a single function and there's no legacy variant.
    index = artifact.get("index") or {}
    if not isinstance(index, dict):
        index = {}

    total_calls = int(index.get("tool_call_count", 0) or 0)
    total_turns = int(index.get("assistant_turns", 0) or 0)

    counts_raw = index.get("tool_counts") or {}
    errors_raw = index.get("tool_errors") or {}
    tool_rows: list[tuple[str, int, int]] = []
    if isinstance(counts_raw, dict):
        for name, count in counts_raw.items():
            err = 0
            if isinstance(errors_raw, dict):
                err = int(errors_raw.get(name, 0) or 0)
            tool_rows.append((str(name), int(count or 0), err))

    tool_rows.sort(key=lambda r: r[1], reverse=True)

    # Header: N tool calls across M assistant turns
    header = (
        f'Tool Usage <span class="meta" style="font-weight:400">'
        f'({total_calls} tool calls across {total_turns} assistant turns)</span>'
    )

    # Two-column grid of tool name → count
    grid_items = ""
    for name, count, err in tool_rows:
        err_html = (
            f' <span style="color:#DB2626">({err} errors)</span>' if err else ""
        )
        color = _tool_color(name)
        grid_items += (
            f'<div style="display:flex;align-items:center;gap:6px;padding:4px 8px;'
            f'border-left:3px solid {color};background:#F7F9FB;border-radius:3px;'
            f'font-size:12px"><span style="font-family:ui-monospace,Menlo,monospace;'
            f'flex:1">{_e(name)}</span><strong>{count}</strong>{err_html}</div>'
        )
    if grid_items:
        grid = (
            f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;'
            f'margin-bottom:8px">{grid_items}</div>'
        )
    else:
        grid = (
            '<div style="color:#64748B;font-size:12px;margin-bottom:8px">'
            'No tool calls recorded.</div>'
        )

    # Warning rows
    warnings_html = ""
    unused = index.get("mcp_servers_unused") or []
    if isinstance(unused, list):
        for server in unused:
            warnings_html += (
                f'<div style="padding:6px 10px;background:{_WARN_BG};color:{_WARN_COLOR};'
                f'border-left:3px solid {_WARN_COLOR};border-radius:3px;font-size:12px;'
                f'margin-top:4px">&#9888; MCP server "{_e(server)}" connected but never used</div>'
            )

    first_err = index.get("first_tool_error")
    if isinstance(first_err, dict):
        tool = first_err.get("tool", "unknown")
        line = first_err.get("line", "?")
        message = first_err.get("message", "")
        msg_html = f": {_e(message)}" if message else ""
        warnings_html += (
            f'<div style="padding:6px 10px;background:#FBE6F1;color:#DB2626;'
            f'border-left:3px solid #DB2626;border-radius:3px;font-size:12px;'
            f'margin-top:4px">&#9888; First tool error: {_e(tool)} '
            f'at line {_e(line)}{msg_html}</div>'
        )

    return _panel_wrapper(header, grid + warnings_html, open_by_default=True)


# --- Panel 2: Agent Instructions (injected CLAUDE.md) ---


def _render_agent_instructions_panel(
    artifacts: dict[str, dict[str, Any]],
) -> str:
    """Render the effective CLAUDE.md panel. Returns '' if artifact absent."""
    artifact = artifacts.get(ARTIFACT_EFFECTIVE_CLAUDE_MD)
    if artifact is None:
        return ""
    content = str(artifact.get("content", ""))
    header = (
        f'Agent Instructions (injected CLAUDE.md) '
        f'<span class="meta" style="font-weight:400">&mdash; {len(content):,} characters</span>'
    )
    body = (
        f'<pre style="white-space:pre-wrap;word-wrap:break-word;font-size:12px;'
        f'font-family:ui-monospace,Menlo,monospace;color:#334155;max-height:500px;'
        f'overflow-y:auto;background:#F7F9FB;padding:12px;border-radius:4px;'
        f'border:{_PANEL_BORDER}">{_e(content)}</pre>'
    )
    return _panel_wrapper(header, body, open_by_default=False)


# --- Panel 3: Reasoning Narrative (session.log) ---


def _render_reasoning_narrative_panel(
    artifacts: dict[str, dict[str, Any]],
) -> str:
    """Render the session.log narrative. Returns '' if artifact absent."""
    artifact = artifacts.get(ARTIFACT_SESSION_LOG)
    if artifact is None:
        return ""
    content = str(artifact.get("content", ""))
    header = (
        f'Reasoning Narrative (session.log) '
        f'<span class="meta" style="font-weight:400">&mdash; {len(content):,} characters</span>'
    )
    body = (
        f'<pre style="white-space:pre-wrap;word-wrap:break-word;font-size:12px;'
        f'font-family:ui-monospace,Menlo,monospace;color:#334155;max-height:500px;'
        f'overflow-y:auto;background:#F7F9FB;padding:12px;border-radius:4px;'
        f'border:{_PANEL_BORDER}">{_e(content)}</pre>'
    )
    return _panel_wrapper(header, body, open_by_default=False)


# --- Panel 4: Tool Calls Timeline (paginated from session-stream.jsonl) ---


_MAX_TIMELINE_EVENTS = 100
_INPUT_TRUNCATE = 200
_RESULT_TRUNCATE = 500


def _extract_tool_event(event: dict) -> dict | None:
    """Extract tool_use / tool_result info from a stream event.

    Returns a normalized dict with keys: kind, tool, input, result, ts.
    Returns None if the event is not a tool event.
    """
    etype = event.get("type", "")
    msg = event.get("message") or {}

    # Assistant tool_use: message.content[].type == 'tool_use'
    if etype == "assistant":
        for block in msg.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                inp = block.get("input", "")
                if isinstance(inp, dict | list):
                    inp = json.dumps(inp)[:_INPUT_TRUNCATE]
                else:
                    inp = str(inp)[:_INPUT_TRUNCATE]
                return {
                    "kind": "tool_use",
                    "tool": str(block.get("name", "")),
                    "input": inp,
                    "result": "",
                    "ts": event.get("timestamp", ""),
                }

    # User tool_result: message.content[].type == 'tool_result'
    if etype == "user":
        for block in msg.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                res = block.get("content", "")
                if isinstance(res, list):
                    parts = []
                    for p in res:
                        if isinstance(p, dict) and "text" in p:
                            parts.append(str(p["text"]))
                        else:
                            parts.append(str(p))
                    res = "\n".join(parts)
                res = str(res)[:_RESULT_TRUNCATE]
                return {
                    "kind": "tool_result",
                    "tool": "",
                    "input": "",
                    "result": res,
                    "ts": event.get("timestamp", ""),
                }
    return None


def _read_stream_events(path: Path, limit: int) -> tuple[list[dict], int]:
    """Read up to ``limit`` tool events from an NDJSON file. Returns (events, total_lines)."""
    events: list[dict] = []
    total_lines = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, raw_line in enumerate(f, start=1):
                total_lines = lineno
                if len(events) >= limit:
                    # Keep counting lines so the footer "of N" is accurate.
                    continue
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                extracted = _extract_tool_event(event)
                if extracted is not None:
                    extracted["line"] = lineno
                    events.append(extracted)
    except OSError:
        return [], 0
    return events, total_lines


def _render_timeline_row(event: dict) -> str:
    """Render a single timeline row with a <details> show-full toggle."""
    tool = event.get("tool", "")
    line = event.get("line", "?")
    ts = str(event.get("ts", ""))[:19] if event.get("ts") else f"line {line}"
    color = _tool_color(tool)
    kind = event.get("kind", "")

    label = _e(tool) if tool else f'<span style="color:#64748B">{_e(kind)}</span>'

    inp_short = event.get("input", "")
    res_short = event.get("result", "")

    details_html = ""
    if inp_short:
        details_html += (
            f'<details style="margin-top:4px"><summary style="cursor:pointer;'
            f'font-size:11px;color:#4D45E5">show input</summary>'
            f'<pre style="white-space:pre-wrap;word-wrap:break-word;font-size:11px;'
            f'margin-top:4px;padding:6px;background:#F1F5F9;border-radius:3px">'
            f'{_e(inp_short)}</pre></details>'
        )
    if res_short:
        details_html += (
            f'<details style="margin-top:4px"><summary style="cursor:pointer;'
            f'font-size:11px;color:#4D45E5">show result</summary>'
            f'<pre style="white-space:pre-wrap;word-wrap:break-word;font-size:11px;'
            f'margin-top:4px;padding:6px;background:#F1F5F9;border-radius:3px">'
            f'{_e(res_short)}</pre></details>'
        )

    return (
        f'<div style="padding:6px 10px;border-left:3px solid {color};'
        f'border-bottom:1px solid #F1F5F9;font-size:12px">'
        f'<div style="display:flex;gap:8px;align-items:center">'
        f'<span class="meta" style="min-width:70px;font-family:ui-monospace,Menlo,monospace">'
        f'{_e(ts)}</span>'
        f'<span style="font-weight:600;font-family:ui-monospace,Menlo,monospace">{label}</span>'
        f'</div>{details_html}</div>'
    )


def _render_timeline_panel(artifacts: dict[str, dict[str, Any]]) -> str:
    """Render the Tool Calls Timeline panel. Returns '' if artifact absent."""
    artifact = artifacts.get(ARTIFACT_SESSION_STREAM)
    if artifact is None:
        return ""

    artifact_path = artifact.get("artifact_path") or artifact.get("path") or ""
    line_count = int(artifact.get("line_count", 0) or 0)

    header = (
        f'Tool Calls Timeline '
        f'<span class="meta" style="font-weight:400">&mdash; {line_count:,} stream events</span>'
    )

    if not artifact_path or not Path(str(artifact_path)).exists():
        body = (
            '<div style="color:#64748B;font-size:12px">Session stream not '
            'available (file removed or archived elsewhere).</div>'
        )
        return _panel_wrapper(header, body, open_by_default=False)

    path = Path(str(artifact_path))
    events, total_lines = _read_stream_events(path, _MAX_TIMELINE_EVENTS)

    if not events:
        body = (
            '<div style="color:#64748B;font-size:12px">No tool events found in '
            f'session stream ({total_lines:,} total lines).</div>'
        )
        return _panel_wrapper(header, body, open_by_default=False)

    rows_html = "".join(_render_timeline_row(ev) for ev in events)

    # Footer — paginated browser-side loading is not yet implemented. Commit 4
    # ships /traces/<id>/artifact/session_stream (full download, no offset
    # support), so for now we surface a static count and direct users at the
    # raw download link in the Raw Downloads panel below.
    footer = ""
    shown = len(events)
    if shown >= _MAX_TIMELINE_EVENTS:
        footer = (
            f'<div style="padding:8px 10px;border-top:{_PANEL_BORDER};font-size:11px;'
            f'color:#64748B">Showing first {shown} of {total_lines:,} stream events '
            f'&mdash; use the Raw Downloads panel for the full stream.</div>'
        )
    else:
        footer = (
            f'<div style="padding:8px 10px;border-top:{_PANEL_BORDER};font-size:11px;'
            f'color:#64748B">Showing all {shown} tool events '
            f'({total_lines:,} stream lines)</div>'
        )

    body = (
        f'<div style="max-height:500px;overflow-y:auto;border:{_PANEL_BORDER};'
        f'border-radius:4px">{rows_html}</div>{footer}'
    )
    return _panel_wrapper(header, body, open_by_default=False)


# --- Panel 5: Conversation View (chat-style model interaction) ---


_MAX_CONVERSATION_EVENTS = 200
_REASONING_TRUNCATE = 800
_TOOL_INPUT_PREVIEW = 300
_TOOL_RESULT_PREVIEW = 600


def _render_conversation_bubble(
    role: str, content_html: str, meta: str = "",
) -> str:
    """Render a chat-style bubble for the conversation view."""
    if role == "assistant":
        bg = "#F0F4FF"
        border_color = "#4D45E5"
        label = "Agent"
        label_color = "#4D45E5"
    elif role == "tool_use":
        bg = "#F7F9FB"
        border_color = "#64748B"
        label = "Tool Call"
        label_color = "#64748B"
    elif role == "tool_result":
        bg = "#F1F5F9"
        border_color = "#94A3B8"
        label = "Result"
        label_color = "#94A3B8"
    else:
        bg = "#FFFFFF"
        border_color = "#E2E8F0"
        label = role
        label_color = "#64748B"

    meta_html = (
        f'<span style="color:#94A3B8;font-size:10px;margin-left:8px">{_e(meta)}</span>'
        if meta else ""
    )
    return (
        f'<div style="margin-bottom:6px;padding:8px 12px;background:{bg};'
        f'border-left:3px solid {border_color};border-radius:4px;font-size:12px">'
        f'<div style="font-weight:600;font-size:11px;color:{label_color};'
        f'margin-bottom:4px">{label}{meta_html}</div>'
        f'{content_html}</div>'
    )


def _render_conversation_panel(artifacts: dict[str, dict[str, Any]]) -> str:
    """Render a chat-style conversation view from session-stream.jsonl.

    Shows the model's reasoning interleaved with tool calls and results,
    similar to LangSmith's conversation view. Each message is a bubble
    with progressive disclosure for long content.
    """
    artifact = artifacts.get(ARTIFACT_SESSION_STREAM)
    if artifact is None:
        return ""

    artifact_path = artifact.get("artifact_path") or ""
    if not artifact_path or not Path(str(artifact_path)).exists():
        return ""

    path = Path(str(artifact_path))
    bubbles: list[str] = []
    event_count = 0
    total_lines = 0

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, raw_line in enumerate(f, start=1):
                total_lines = lineno
                if len(bubbles) >= _MAX_CONVERSATION_EVENTS:
                    continue  # keep counting lines
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue

                etype = event.get("type", "")
                msg = event.get("message") or {}
                content_blocks = msg.get("content") or []
                if not isinstance(content_blocks, list):
                    continue

                if etype == "assistant":
                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")

                        if btype == "text":
                            text = str(block.get("text", ""))
                            if not text.strip():
                                continue
                            if len(text) > _REASONING_TRUNCATE:
                                shown = _e(text[:_REASONING_TRUNCATE])
                                hidden = _e(text[_REASONING_TRUNCATE:])
                                content_html = (
                                    f'<div style="white-space:pre-wrap;word-wrap:break-word;'
                                    f'font-family:ui-sans-serif,system-ui,sans-serif">'
                                    f'{shown}<details style="display:inline"><summary '
                                    f'style="cursor:pointer;color:#4D45E5;font-size:11px">'
                                    f'show more ({len(text):,} chars)</summary>'
                                    f'{hidden}</details></div>'
                                )
                            else:
                                content_html = (
                                    f'<div style="white-space:pre-wrap;word-wrap:break-word;'
                                    f'font-family:ui-sans-serif,system-ui,sans-serif">'
                                    f'{_e(text)}</div>'
                                )
                            bubbles.append(_render_conversation_bubble(
                                "assistant", content_html
                            ))

                        elif btype == "tool_use":
                            tool_name = str(block.get("name", ""))
                            inp = block.get("input", "")
                            if isinstance(inp, dict | list):
                                inp_str = json.dumps(inp, indent=2)
                            else:
                                inp_str = str(inp)
                            color = _tool_color(tool_name)
                            if len(inp_str) > _TOOL_INPUT_PREVIEW:
                                inp_preview = _e(inp_str[:_TOOL_INPUT_PREVIEW])
                                inp_rest = _e(inp_str[_TOOL_INPUT_PREVIEW:])
                                inp_html = (
                                    f'<pre style="white-space:pre-wrap;word-wrap:break-word;'
                                    f'font-size:11px;margin:4px 0 0 0;padding:6px;'
                                    f'background:#FFFFFF;border-radius:3px;border:1px solid #E2E8F0">'
                                    f'{inp_preview}<details style="display:inline">'
                                    f'<summary style="cursor:pointer;color:#4D45E5;'
                                    f'font-size:10px">show full input</summary>'
                                    f'{inp_rest}</details></pre>'
                                )
                            else:
                                inp_html = (
                                    f'<pre style="white-space:pre-wrap;word-wrap:break-word;'
                                    f'font-size:11px;margin:4px 0 0 0;padding:6px;'
                                    f'background:#FFFFFF;border-radius:3px;border:1px solid #E2E8F0">'
                                    f'{_e(inp_str)}</pre>'
                                ) if inp_str.strip() else ""
                            label_html = (
                                f'<span style="font-family:ui-monospace,Menlo,monospace;'
                                f'font-weight:600;color:{color}">{_e(tool_name)}</span>'
                            )
                            bubbles.append(_render_conversation_bubble(
                                "tool_use", f'{label_html}{inp_html}'
                            ))

                elif etype == "user":
                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        is_error = block.get("is_error", False)
                        res = block.get("content", "")
                        if isinstance(res, list):
                            parts = []
                            for p in res:
                                if isinstance(p, dict) and "text" in p:
                                    parts.append(str(p["text"]))
                                else:
                                    parts.append(str(p))
                            res_str = "\n".join(parts)
                        else:
                            res_str = str(res)

                        if not res_str.strip():
                            continue

                        err_badge = (
                            '<span style="color:#DB2626;font-weight:600;'
                            'font-size:10px"> ERROR</span>'
                            if is_error else ""
                        )
                        if len(res_str) > _TOOL_RESULT_PREVIEW:
                            res_preview = _e(res_str[:_TOOL_RESULT_PREVIEW])
                            res_rest = _e(res_str[_TOOL_RESULT_PREVIEW:])
                            res_html = (
                                f'<pre style="white-space:pre-wrap;word-wrap:break-word;'
                                f'font-size:11px;padding:6px;background:#FFFFFF;'
                                f'border-radius:3px;border:1px solid #E2E8F0;'
                                f'max-height:200px;overflow-y:auto">'
                                f'{res_preview}<details style="display:inline">'
                                f'<summary style="cursor:pointer;color:#4D45E5;'
                                f'font-size:10px">show full result ({len(res_str):,} chars)</summary>'
                                f'{res_rest}</details></pre>'
                            )
                        else:
                            res_html = (
                                f'<pre style="white-space:pre-wrap;word-wrap:break-word;'
                                f'font-size:11px;padding:6px;background:#FFFFFF;'
                                f'border-radius:3px;border:1px solid #E2E8F0;'
                                f'max-height:200px;overflow-y:auto">{_e(res_str)}</pre>'
                            )
                        bubbles.append(_render_conversation_bubble(
                            "tool_result", f'{err_badge}{res_html}'
                        ))

                event_count += 1

    except OSError:
        return ""

    if not bubbles:
        return ""

    header = (
        f'Agent Conversation '
        f'<span class="meta" style="font-weight:400">&mdash; '
        f'{len(bubbles)} messages from {total_lines:,} stream events</span>'
    )

    footer = ""
    if len(bubbles) >= _MAX_CONVERSATION_EVENTS:
        footer = (
            f'<div style="padding:8px 10px;border-top:{_PANEL_BORDER};font-size:11px;'
            f'color:#64748B">Showing first {len(bubbles)} messages '
            f'&mdash; use Raw Downloads for the full stream.</div>'
        )

    body = (
        f'<div style="max-height:600px;overflow-y:auto;border:{_PANEL_BORDER};'
        f'border-radius:4px;padding:8px">{"".join(bubbles)}</div>{footer}'
    )
    return _panel_wrapper(header, body, open_by_default=False)


# --- Panel 6: Raw Downloads ---


_DOWNLOADABLE_ARTIFACTS = (
    ARTIFACT_SESSION_LOG,
    ARTIFACT_SESSION_STREAM,
    ARTIFACT_EFFECTIVE_CLAUDE_MD,
)


def _has_any_session_artifact(
    artifacts: dict[str, dict[str, Any]],
) -> bool:
    """Check if the trace has any of the three downloadable artifacts."""
    return any(name in artifacts for name in _DOWNLOADABLE_ARTIFACTS)


def _render_raw_downloads_panel(
    entries: list[dict], artifacts: dict[str, dict[str, Any]]
) -> str:
    """Render the raw downloads panel with an un-redacted warning."""
    if not _has_any_session_artifact(artifacts):
        return ""
    ticket_id = _e(_ticket_id_from_entries(entries))
    header = 'Raw Downloads'
    warning = (
        f'<p style="padding:8px 10px;background:{_WARN_BG};color:{_WARN_COLOR};'
        f'border-left:3px solid {_WARN_COLOR};border-radius:3px;font-size:12px;'
        f'margin-bottom:10px">&#9888; Raw session streams are not redacted in '
        f'this build. Redaction ships in commit 6 &mdash; until then, do not '
        f'share these files outside your local machine.</p>'
    )
    links = (
        f'<ul style="list-style:none;padding:0;margin:0;font-size:12px">'
        f'<li style="padding:4px 0"><a href="/traces/{ticket_id}/artifact/session_log">'
        f'session.log</a></li>'
        f'<li style="padding:4px 0"><a href="/traces/{ticket_id}/artifact/session_stream">'
        f'session-stream.jsonl</a></li>'
        f'<li style="padding:4px 0"><a href="/traces/{ticket_id}/artifact/effective_claude_md">'
        f'effective CLAUDE.md</a></li>'
        f'</ul>'
    )
    return _panel_wrapper(header, warning + links, open_by_default=False)


# --- Public entry point ---


def render_session_panels(entries: list[dict]) -> str:
    """Render the session observability panels for a trace detail page.

    Reads the four artifact types produced by commit 1 (session.log,
    effective CLAUDE.md, session-stream.jsonl reference, tool_index) from
    the flat trace entries list and returns an HTML fragment.

    Returns empty string if ``entries`` is empty. The Tool Usage panel is
    always rendered when there are entries (the absence of the tool_index
    artifact is itself meaningful information, so we surface it explicitly).
    """
    if not entries:
        return ""

    # Build the artifact index once — each panel used to call
    # ``find_artifact`` (O(N) reverse walk) independently, so a render
    # of all five panels did 5+ full scans of the entries list. Now
    # it's a single O(N) walk shared across every panel.
    artifacts = latest_artifacts(entries)
    parts = [
        _render_agent_instructions_panel(artifacts),
        _render_reasoning_narrative_panel(artifacts),
        _render_timeline_panel(artifacts),
        _render_conversation_panel(artifacts),
        _render_raw_downloads_panel(entries, artifacts),
    ]
    return "".join(p for p in parts if p)


def render_tool_usage_panel(entries: list[dict]) -> str:
    """Render just the Tool Usage panel. Separated so the caller can
    place it independently from the other session panels."""
    if not entries:
        return ""
    artifacts = latest_artifacts(entries)
    return _render_tool_usage_panel(artifacts)
