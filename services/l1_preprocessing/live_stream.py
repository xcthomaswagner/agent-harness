"""Live activity SSE endpoint for the dashboard.

Tails ``session-stream.jsonl`` under a ticket's L2 worktree and streams
filtered events to connected browsers via Server-Sent Events. Replays
the last 100 filtered events on connect, then switches to a 1-second
polling tail. Multi-teammate runs (main worktree + subworktrees) are
merged and labeled so the operator sees a single coherent feed.

Why this exists: ``pipeline.jsonl`` only writes on phase transitions
(every 3-30 minutes), so during a long code-review or QA phase the
dashboard looks frozen even when the agent is hammering away. The
per-tool-call events already live in ``session-stream.jsonl`` — this
module just surfaces them.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
from collections.abc import AsyncIterator
from html import escape as _html_escape
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from auth import _require_dashboard_auth_query_or_header
from redaction import redact
from tracer import read_trace

logger = structlog.get_logger()
router = APIRouter()

# How many historical events to replay on connect.
REPLAY_LIMIT = 100

# Poll interval when tailing (lower = tighter live feel, higher =
# less CPU). 1s feels instant enough and keeps CPU use negligible.
TAIL_POLL_INTERVAL_SEC = 1.0

# Heartbeat cadence — must be shorter than proxy/browser idle timeout.
# Standard nginx default is 60s; 15s leaves a comfortable safety margin.
HEARTBEAT_INTERVAL_SEC = 15.0

# Character ceiling applied to assistant text/thinking snippets. Longer
# passages get truncated with an ellipsis marker. 300 chars fits ~4
# lines on a dashboard feed without blowing out the layout.
TEXT_SNIPPET_MAX = 300

# Ticket_id validator — mirrors completion._validate_ticket_id. Lives
# here too to avoid cross-module import for a one-liner.
_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_ROLE_LABELS: dict[str, str] = {
    "team-lead": "Team Lead",
    "team_lead": "Team Lead",
    "planner": "Planner",
    "plan-reviewer": "Plan Reviewer",
    "plan_reviewer": "Plan Reviewer",
    "developer": "Developer",
    "dev": "Developer",
    "code-reviewer": "Code Reviewer",
    "code_reviewer": "Code Reviewer",
    "reviewer": "Code Reviewer",
    "judge": "Judge",
    "qa": "QA",
    "merge-coordinator": "Merge Coordinator",
    "merge_coordinator": "Merge Coordinator",
    "simplify": "Simplify",
    "simplifier": "Simplify",
    "run-reflector": "Run Reflector",
    "run_reflector": "Run Reflector",
}

_ROLE_GROUPS: dict[str, str] = {
    "team_lead": "team_lead",
    "planner": "team_lead",
    "plan_reviewer": "team_lead",
    "developer": "dev",
    "code_reviewer": "review",
    "judge": "review",
    "qa": "qa",
    "merge_coordinator": "other",
    "simplify": "other",
    "run_reflector": "other",
    "unknown": "other",
}

_ROLE_ALIASES: tuple[tuple[str, str], ...] = (
    ("team-lead", "team_lead"),
    ("team_lead", "team_lead"),
    ("planner", "planner"),
    ("plan-review", "plan_reviewer"),
    ("plan_reviewer", "plan_reviewer"),
    ("plan-reviewer", "plan_reviewer"),
    ("code-review", "code_reviewer"),
    ("code_reviewer", "code_reviewer"),
    ("code-reviewer", "code_reviewer"),
    ("reviewer", "code_reviewer"),
    ("judge", "judge"),
    ("qa", "qa"),
    ("merge-coordinator", "merge_coordinator"),
    ("merge_coordinator", "merge_coordinator"),
    ("merge", "merge_coordinator"),
    ("simplify", "simplify"),
    ("simplifier", "simplify"),
    ("run-reflector", "run_reflector"),
    ("run_reflector", "run_reflector"),
    ("developer", "developer"),
    ("dev", "developer"),
    ("unit", "developer"),
)


def _validate_ticket_id(ticket_id: str) -> str:
    """Reject path-like ticket ids that could escape the worktree root."""
    if not ticket_id or not _TICKET_ID_RE.match(ticket_id):
        raise HTTPException(status_code=400, detail="Invalid ticket_id")
    return ticket_id


def _worktree_root_for_ticket(ticket_id: str) -> Path | None:
    """Resolve a ticket worktree from trace metadata, then legacy default path.

    Returns ``None`` when the directory doesn't exist (L2 never
    spawned for this ticket). The caller is responsible for
    degrading gracefully — a non-existent worktree is expected, not
    an error condition.
    """
    for entry in reversed(read_trace(ticket_id)):
        raw_path = entry.get("worktree_path") or entry.get("worktree")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        candidate = Path(raw_path).expanduser().resolve()
        manifest = candidate / ".harness" / "spawn-manifest.json"
        ticket_json = candidate / ".harness" / "ticket.json"
        if not candidate.is_dir() or not (manifest.is_file() or ticket_json.is_file()):
            continue
        try:
            data = _read_json_object(manifest) or _read_json_object(ticket_json) or {}
            if data.get("ticket_id") == ticket_id or data.get("id") == ticket_id:
                return candidate
        except Exception:
            continue

    base = Path(os.path.expanduser("~/.harness/clients/worktrees/ai"))
    candidate = (base / ticket_id).resolve()
    # Containment guard — the regex above already rejects ``..`` and
    # slashes, so this is belt-and-braces against a future relaxation.
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    if not candidate.exists() or not candidate.is_dir():
        return None
    return candidate


def _teammate_name_for(worktree_root: Path, stream_path: Path) -> str:
    """Derive a human-readable teammate label from the stream path.

    Main worktree → ``"team-lead"``. Subworktrees under
    ``.claude/worktrees/<dir>/`` use ``<dir>``; if the subworktree
    has a ``ticket.json`` with a ``role`` field, prefer that.
    """
    try:
        rel = stream_path.relative_to(worktree_root)
    except ValueError:
        return "unknown"
    parts = rel.parts
    # Main: .harness/logs/session-stream.jsonl
    if parts[:2] == (".harness", "logs"):
        return "team-lead"
    # Sub: .claude/worktrees/<dir>/.harness/logs/session-stream.jsonl
    if len(parts) >= 3 and parts[0] == ".claude" and parts[1] == "worktrees":
        sub_dir = parts[2]
        # Try ticket.json role hint — cheap best-effort.
        sub_root = worktree_root / ".claude" / "worktrees" / sub_dir
        ticket_json = sub_root / ".harness" / "ticket.json"
        if ticket_json.is_file():
            try:
                data = json.loads(ticket_json.read_text())
                role = data.get("role")
                if isinstance(role, str) and role:
                    return role
            except (OSError, json.JSONDecodeError):
                pass
        return sub_dir
    # Fallback: use the first path component.
    return parts[0] if parts else "unknown"


def _canonical_role(label: str) -> str:
    normalized = label.strip().lower().replace(" ", "_")
    dashed = normalized.replace("_", "-")
    for needle, role in _ROLE_ALIASES:
        if needle in normalized or needle in dashed:
            return role
    return "unknown"


def _role_group(role: str) -> str:
    return _ROLE_GROUPS.get(role, "other")


def _display_name(teammate: str, role: str) -> str:
    if role == "unknown":
        return teammate
    base = _ROLE_LABELS.get(role, role.replace("_", " ").title())
    match = re.search(r"(?:^|[-_])(?:dev|developer|unit)[-_]?(\d+)$", teammate, re.I)
    if role == "developer" and match:
        return f"{base} {match.group(1)}"
    return base


def _redact_live_text(text: str) -> str:
    redacted, _ = redact(text)
    return _CONTENTSTACK_TOKEN_RE.sub("[CONTENTSTACK_TOKEN_REDACTED]", redacted)


def _event_id(teammate: str, source_line: int | None) -> str:
    safe_team = re.sub(r"[^A-Za-z0-9_.-]+", "-", teammate).strip("-") or "unknown"
    line = int(source_line or 0)
    return f"{safe_team}:{line}"


def _base_event(
    *,
    teammate: str,
    source_line: int | None,
    timestamp: Any,
    observed_at: str,
) -> dict[str, Any]:
    role = _canonical_role(teammate)
    return {
        "event_id": _event_id(teammate, source_line),
        "teammate": teammate,
        "role": role,
        "role_group": _role_group(role),
        "display_name": _display_name(teammate, role),
        "timestamp": timestamp if isinstance(timestamp, str) else "",
        "observed_at": observed_at,
        "source_line": source_line,
    }


def _find_session_streams(worktree_root: Path) -> list[tuple[str, Path]]:
    """Return ``[(teammate_name, session_stream_path), ...]`` for all
    session-stream.jsonl files under the worktree tree.

    Scans the main location plus any ``.claude/worktrees/*/``
    subdirectories. Missing files are simply absent from the list.
    """
    streams: list[tuple[str, Path]] = []
    main = worktree_root / ".harness" / "logs" / "session-stream.jsonl"
    if main.is_file():
        streams.append((_teammate_name_for(worktree_root, main), main))
    sub_root = worktree_root / ".claude" / "worktrees"
    if sub_root.is_dir():
        for sub in sorted(sub_root.iterdir()):
            if not sub.is_dir():
                continue
            sub_stream = sub / ".harness" / "logs" / "session-stream.jsonl"
            if sub_stream.is_file():
                streams.append(
                    (_teammate_name_for(worktree_root, sub_stream), sub_stream)
                )
    return streams


def _truncate(text: str, limit: int = TEXT_SNIPPET_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _describe_tool_use(block: dict[str, Any]) -> str:
    """Derive a short description for a tool_use block.

    Prefers the ``description`` input (some tools set it explicitly),
    falls back to shell command / file path / truncated JSON of
    other inputs. Keeps the feed readable.
    """
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        return ""
    for key in ("description", "command", "file_path", "pattern", "path", "query", "url"):
        val = inp.get(key)
        if isinstance(val, str) and val:
            return _truncate(_redact_live_text(val), 200)
    # Last resort: summarize the input keys so the feed isn't silent.
    if inp:
        return _truncate(_redact_live_text(json.dumps(inp, default=str)), 200)
    return ""


def _filter_and_shape_event(
    raw: dict[str, Any],
    teammate: str,
    source_line: int | None = None,
    observed_at: str = "",
) -> dict[str, Any] | None:
    """Apply the inclusion filter — return a shaped event ready for the
    wire, or ``None`` when the raw event should be skipped.

    See the brief (Design decision 2) for the full inclusion list. The
    ``task_progress`` subtype is intentionally NOT emitted as a feed
    row (too noisy) — its running ``usage`` numbers are surfaced as
    a ``progress_update`` event that the header-card consumes.
    """
    etype = raw.get("type")
    timestamp = raw.get("timestamp")
    shaped_base = _base_event(
        teammate=teammate,
        source_line=source_line,
        timestamp=timestamp,
        observed_at=observed_at or (timestamp if isinstance(timestamp, str) else ""),
    )

    if etype == "assistant":
        message = raw.get("message") or {}
        if not isinstance(message, dict):
            return None
        content = message.get("content") or []
        if not isinstance(content, list):
            return None
        # One raw assistant event can contain multiple blocks — for the
        # feed we prefer the FIRST actionable block (tool_use wins over
        # text). Keeping one row per raw event keeps the source_line
        # accounting unambiguous.
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = block.get("name")
                if not isinstance(name, str):
                    continue
                return {
                    **shaped_base,
                    "kind": "tool_use",
                    "tool_name": name,
                    "description": _describe_tool_use(block),
                }
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return {
                        **shaped_base,
                        "kind": "text",
                        "text": _truncate(_redact_live_text(text.strip())),
                    }
        return None

    if etype == "system":
        subtype = raw.get("subtype")
        if subtype == "task_started":
            return {
                **shaped_base,
                "kind": "task_started",
                "description": _truncate(
                    _redact_live_text(str(raw.get("description") or "")), 200
                ),
            }
        if subtype == "task_notification":
            return {
                **shaped_base,
                "kind": "task_notification",
                "summary": _truncate(
                    _redact_live_text(str(raw.get("summary") or "")), 200
                ),
                "status": str(raw.get("status") or ""),
            }
        if subtype == "task_progress":
            usage = raw.get("usage") or {}
            if not isinstance(usage, dict):
                return None
            return {
                **shaped_base,
                "kind": "progress_update",
                "tool_uses": int(usage.get("tool_uses") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "last_tool_name": str(raw.get("last_tool_name") or ""),
                "description": _truncate(
                    _redact_live_text(str(raw.get("description") or "")), 200
                ),
            }
        return None

    if etype == "rate_limit_event":
        return {
            **shaped_base,
            "kind": "rate_limit",
            "info": raw.get("rate_limit_info") or {},
        }

    # type=user (tool_result) — explicitly skipped per the brief.
    return None


def _read_last_lines(path: Path, limit: int) -> list[tuple[int, str]]:
    """Return up to the last ``limit`` non-empty lines with their
    1-based line numbers.

    Simple implementation: read the whole file (session-stream.jsonl
    files top out in the low MB range), split, tail. A reverse-block
    reader would be faster for giant files but adds complexity the
    current workload doesn't justify.
    """
    try:
        raw = path.read_text(errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    out: list[tuple[int, str]] = []
    # Walk from the end so we stop early once we have enough non-empty lines.
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx].strip()
        if not line:
            continue
        out.append((idx + 1, line))
        if len(out) >= limit:
            break
    out.reverse()
    return out


def _sort_key(event: dict[str, Any]) -> tuple[str, int]:
    """Order events by (timestamp, source_line).

    Session-stream events don't all carry a ``timestamp`` field
    (assistant + most system events omit it). Missing timestamps sort
    as empty strings which lexicographically precede any ISO-8601
    value — in practice that's fine because the source_line tiebreak
    preserves the write-order within a single file.
    """
    ts = event.get("observed_at") or event.get("timestamp") or ""
    line = event.get("source_line") or 0
    return (str(ts), int(line))


def _raw_timestamp(raw: dict[str, Any]) -> str:
    ts = raw.get("timestamp") or raw.get("started_at") or raw.get("t")
    return ts if isinstance(ts, str) else ""


def _event_message(event: dict[str, Any]) -> str:
    kind = str(event.get("kind") or "")
    if kind == "tool_use":
        tool = str(event.get("tool_name") or "tool")
        desc = str(event.get("description") or "")
        return f"{tool}: {desc}".strip()
    if kind == "text":
        return str(event.get("text") or "")
    if kind == "task_started":
        return f"started: {event.get('description') or ''}".strip()
    if kind == "task_notification":
        return f"done: {event.get('summary') or ''}".strip()
    if kind == "rate_limit":
        return "rate limit"
    return kind


def _replay_events_for_stream(
    teammate: str, path: Path, limit: int
) -> list[dict[str, Any]]:
    """Parse the last ``limit`` filtered events from a single stream.

    We read more raw lines than we need (``limit * 4``) because
    filtering drops task_progress + user + hook events — without the
    multiplier the replay frequently returns <limit events on a file
    that actually has plenty.
    """
    raw_budget = max(limit * 4, limit + 50)
    last_lines = _read_last_lines(path, raw_budget)
    shaped: list[dict[str, Any]] = []
    last_observed_at = ""
    for lineno, raw in last_lines:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        raw_ts = _raw_timestamp(parsed)
        if raw_ts:
            last_observed_at = raw_ts
        ev = _filter_and_shape_event(
            parsed,
            teammate,
            source_line=lineno,
            observed_at=raw_ts or last_observed_at,
        )
        if ev is not None:
            shaped.append(ev)
    return shaped[-limit:] if len(shaped) > limit else shaped


def summarize_session_stream(
    teammate: str,
    path: Path,
    *,
    limit: int = 200,
    latest_events: int = 5,
) -> dict[str, Any]:
    """Return a redacted activity summary for one teammate stream."""
    raw_lines = _read_last_lines(path, max(limit * 4, limit + 50))
    role = _canonical_role(teammate)
    last_raw_ts = ""
    last_at = ""
    shaped_events: list[dict[str, Any]] = []
    tool_uses_progress = 0
    total_tokens = 0
    last_tool = ""

    for lineno, raw in raw_lines:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        raw_ts = _raw_timestamp(parsed)
        if raw_ts:
            last_raw_ts = raw_ts
            last_at = raw_ts
        ev = _filter_and_shape_event(
            parsed,
            teammate,
            source_line=lineno,
            observed_at=raw_ts or last_raw_ts,
        )
        if ev is None:
            continue
        if ev.get("observed_at"):
            last_at = str(ev["observed_at"])
        if ev.get("kind") == "progress_update":
            tool_uses_progress = max(tool_uses_progress, int(ev.get("tool_uses") or 0))
            total_tokens = max(total_tokens, int(ev.get("total_tokens") or 0))
            if ev.get("last_tool_name"):
                last_tool = str(ev["last_tool_name"])
            continue
        if ev.get("kind") == "tool_use" and ev.get("tool_name"):
            last_tool = str(ev["tool_name"])
        shaped_events.append(ev)

    visible = shaped_events[-latest_events:]
    current = visible[-1] if visible else None
    tool_use_count = sum(1 for e in shaped_events if e.get("kind") == "tool_use")
    return {
        "teammate": teammate,
        "role": role,
        "role_group": _role_group(role),
        "display_name": _display_name(teammate, role),
        "last_at": last_at or None,
        "last_tool": last_tool,
        "last_event_kind": str(current.get("kind") or "") if current else "",
        "current_activity": _event_message(current) if current else "",
        "last_summary": _event_message(current) if current else "",
        "tool_uses": max(tool_uses_progress, tool_use_count),
        "total_tokens": total_tokens,
        "event_count": len(shaped_events),
        "stream_path_present": path.is_file(),
        "latest_events": visible,
    }


_PHASE_TEAMMATES: dict[str, str] = {
    "implementation": "developer",
    "security_scan": "code-reviewer",
    "judge": "judge",
    "code_review": "code-reviewer",
    "qa_validation": "qa",
    "simplify": "simplify",
    "reflection": "run-reflector",
    "pr_created": "merge-coordinator",
}


def _synthetic_activity_event(
    *,
    teammate: str,
    source_id: str,
    message: str,
    timestamp: str,
    kind: str = "task_notification",
) -> dict[str, Any]:
    message = _truncate(_redact_live_text(message), 240)
    event = {
        **_base_event(
            teammate=teammate,
            source_line=0,
            timestamp=timestamp,
            observed_at=timestamp,
        ),
        "event_id": f"{teammate}:{source_id}",
        "kind": kind,
        "summary": message,
    }
    if kind == "text":
        event["text"] = message
    return event


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _file_mtime_iso(path: Path) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime))
    except OSError:
        return ""


def _pipeline_activity_message(entry: dict[str, Any]) -> str:
    phase = str(entry.get("phase") or "")
    event = str(entry.get("event") or phase or "Pipeline event")
    if phase == "implementation":
        parts = [event]
        if entry.get("files_changed") is not None:
            parts.append(f"{entry.get('files_changed')} files changed")
        if entry.get("tests_passed") is not None:
            parts.append(f"{entry.get('tests_passed')} tests passed")
        return ": ".join([parts[0], ", ".join(parts[1:])]) if len(parts) > 1 else event
    if phase == "security_scan":
        return (
            f"{event}: {entry.get('semgrep_findings', 0)} semgrep findings, "
            f"{entry.get('dependency_cves', 0)} CVEs, "
            f"{entry.get('secrets_found', 0)} secrets"
        )
    if phase == "judge":
        return (
            f"{event}: {entry.get('validated', 0)} validated, "
            f"{entry.get('rejected', 0)} rejected"
        )
    if phase == "code_review":
        return (
            f"{event}: verdict {entry.get('verdict', 'unknown')}, "
            f"{entry.get('issues', 0)} issues, "
            f"{entry.get('critical', 0)} critical, "
            f"{entry.get('warnings', 0)} warnings"
        )
    if phase == "qa_validation" and event == "QA complete":
        return (
            f"{event}: {entry.get('overall', 'unknown')}, "
            f"{entry.get('criteria_passed', 0)}/"
            f"{entry.get('criteria_total', 0)} criteria passed"
        )
    if phase == "reflection":
        return f"{event}: {entry.get('candidates', 0)} lesson candidates"
    return event


def _collect_pipeline_activity(logs_dir: Path) -> list[dict[str, Any]]:
    path = logs_dir / "pipeline.jsonl"
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for idx, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        phase = str(entry.get("phase") or "")
        teammate = _PHASE_TEAMMATES.get(phase)
        if not teammate:
            continue
        event_name = str(entry.get("event") or "")
        if event_name == "phase_started":
            continue
        events.append(
            _synthetic_activity_event(
                teammate=teammate,
                source_id=f"pipeline:{idx}",
                message=_pipeline_activity_message(entry),
                timestamp=str(entry.get("timestamp") or ""),
            )
        )
    return events


def _issue_summaries(items: Any, limit: int = 3) -> list[str]:
    if not isinstance(items, list):
        return []
    summaries: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary") or item.get("criterion") or item.get("details")
        if isinstance(summary, str) and summary.strip():
            summaries.append(_truncate(_redact_live_text(summary.strip()), 160))
        if len(summaries) >= limit:
            break
    return summaries


def _json_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _collect_artifact_activity(logs_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    review_path = logs_dir / "code-review.json"
    review = _read_json_object(review_path)
    if review:
        issues = _json_list(review.get("issues"))
        events.append(
            _synthetic_activity_event(
                teammate="code-reviewer",
                source_id="artifact:code-review",
                message=(
                    f"Code review artifact: verdict {review.get('verdict', 'unknown')}, "
                    f"{len(issues)} issues found"
                ),
                timestamp=_file_mtime_iso(review_path),
            )
        )
        for idx, summary in enumerate(_issue_summaries(issues), start=1):
            events.append(
                _synthetic_activity_event(
                    teammate="code-reviewer",
                    source_id=f"artifact:code-review:{idx}",
                    message=f"Review finding: {summary}",
                    timestamp=_file_mtime_iso(review_path),
                    kind="text",
                )
            )

    judge_path = logs_dir / "judge-verdict.json"
    judge = _read_json_object(judge_path)
    if judge:
        validated = judge.get("validated_issues")
        rejected = judge.get("rejected_issues")
        validated_count = len(validated) if isinstance(validated, list) else 0
        rejected_count = len(rejected) if isinstance(rejected, list) else 0
        events.append(
            _synthetic_activity_event(
                teammate="judge",
                source_id="artifact:judge-verdict",
                message=(
                    f"Judge artifact: {validated_count} validated, "
                    f"{rejected_count} rejected"
                ),
                timestamp=_file_mtime_iso(judge_path),
            )
        )
        for idx, summary in enumerate(_issue_summaries(validated), start=1):
            events.append(
                _synthetic_activity_event(
                    teammate="judge",
                    source_id=f"artifact:judge-verdict:{idx}",
                    message=f"Validated issue: {summary}",
                    timestamp=_file_mtime_iso(judge_path),
                    kind="text",
                )
            )

    qa_path = logs_dir / "qa-matrix.json"
    qa = _read_json_object(qa_path)
    if qa:
        issues = _json_list(qa.get("issues"))
        passed = sum(
            1
            for item in issues
            if isinstance(item, dict)
            and str(item.get("status") or "").upper().startswith("PASS")
        )
        events.append(
            _synthetic_activity_event(
                teammate="qa",
                source_id="artifact:qa-matrix",
                message=(
                    f"QA matrix artifact: {qa.get('overall', 'unknown')}, "
                    f"{passed}/{len(issues)} checks passed"
                ),
                timestamp=_file_mtime_iso(qa_path),
            )
        )
        failing = [
            item
            for item in issues
            if isinstance(item, dict)
            and not str(item.get("status") or "").upper().startswith("PASS")
        ]
        for idx, summary in enumerate(_issue_summaries(failing), start=1):
            events.append(
                _synthetic_activity_event(
                    teammate="qa",
                    source_id=f"artifact:qa-matrix:{idx}",
                    message=f"QA issue: {summary}",
                    timestamp=_file_mtime_iso(qa_path),
                    kind="text",
                )
            )

    retro_path = logs_dir / "retrospective.json"
    retro = _read_json_object(retro_path)
    if retro:
        candidates = retro.get("lesson_candidates")
        count = len(candidates) if isinstance(candidates, list) else 0
        events.append(
            _synthetic_activity_event(
                teammate="run-reflector",
                source_id="artifact:retrospective",
                message=f"Reflection artifact: {count} lesson candidates",
                timestamp=_file_mtime_iso(retro_path),
            )
        )

    return events


def collect_finished_activity(worktree_root: Path) -> list[dict[str, Any]]:
    logs_dir = worktree_root / ".harness" / "logs"
    if not logs_dir.is_dir():
        return []
    return _collect_pipeline_activity(logs_dir) + _collect_artifact_activity(logs_dir)


_WARNING_RE = re.compile(
    r"\b(error|fail(?:ed|ure)?|blocked|missing|invalid|auth|token|secret|"
    r"rate limit|rejected|stale|conflict|exception)\b",
    re.IGNORECASE,
)

_LOW_SIGNAL_RE = re.compile(
    r"^(read|ls|pwd|glob|grep|rg|find|tool|todo|notebookread):?\s*$",
    re.IGNORECASE,
)

_CONTENTSTACK_TOKEN_RE = re.compile(
    r"\b(?:csa|cs|blt)[A-Za-z0-9]{18,}\b",
    re.IGNORECASE,
)


def _normalize_activity_message(message: str) -> str:
    normalized = message.lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", normalized)
    normalized = re.sub(r"\b\d+\b", "<n>", normalized)
    normalized = re.sub(r"/Users/[^ ]+", "<path>", normalized)
    return normalized


def _activity_summary_event(event: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(event.get("kind") or "")
    if kind == "progress_update":
        return None
    message = _truncate(_event_message(event).strip(), 240)
    if not message or _LOW_SIGNAL_RE.match(message):
        return None
    return {
        "event_id": event.get("event_id") or "",
        "teammate": event.get("teammate") or "",
        "display_name": event.get("display_name") or event.get("teammate") or "",
        "role": event.get("role") or "unknown",
        "role_group": event.get("role_group") or "other",
        "kind": kind,
        "tool_name": event.get("tool_name") or "",
        "message": message,
        "last_at": event.get("observed_at") or event.get("timestamp") or "",
        "count": 1,
    }


def _dedupe_activity_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for event in events:
        item = _activity_summary_event(event)
        if item is None:
            continue
        key = (
            str(item["role_group"]),
            str(item["kind"]),
            _normalize_activity_message(str(item["message"])),
        )
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            order.append(key)
        else:
            existing["count"] = int(existing["count"]) + 1
            existing["last_at"] = item["last_at"]
            existing["event_id"] = item["event_id"]
    return [by_key[key] for key in order]


def summarize_ticket_activity(
    ticket_id: str,
    streams: list[tuple[str, Path]],
    *,
    per_stream_limit: int = 500,
    finished_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a de-duplicated post-run summary from teammate streams."""
    teammates: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    raw_event_count = 0
    seen_teammates: set[str] = set()

    for teammate, path in streams:
        roster = summarize_session_stream(
            teammate, path, limit=per_stream_limit, latest_events=5
        )
        events = _replay_events_for_stream(teammate, path, per_stream_limit)
        raw_event_count += len(events)
        visible_events = [e for e in events if e.get("kind") != "progress_update"]
        deduped = _dedupe_activity_events(visible_events)
        tools: dict[str, int] = {}
        warnings: list[str] = []
        for item in deduped:
            tool = str(item.get("tool_name") or "")
            if tool:
                tools[tool] = tools.get(tool, 0) + int(item.get("count") or 1)
            msg = str(item.get("message") or "")
            if _WARNING_RE.search(msg):
                warnings.append(msg)
        teammates.append(
            {
                "teammate": teammate,
                "role": roster["role"],
                "role_group": roster["role_group"],
                "display_name": roster["display_name"],
                "state": "unknown",
                "last_at": roster["last_at"],
                "event_count": len(visible_events),
                "raw_event_count": len(events),
                "deduped_event_count": len(deduped),
                "tool_uses": roster["tool_uses"],
                "total_tokens": roster["total_tokens"],
                "tools": [
                    {"name": name, "count": count}
                    for name, count in sorted(
                        tools.items(), key=lambda kv: (-kv[1], kv[0])
                    )[:8]
                ],
                "actions": deduped[-10:],
                "warnings": warnings[:5],
                "current_activity": roster["current_activity"],
            }
        )
        seen_teammates.add(teammate)
        all_events.extend(visible_events)

    extra_events = finished_events or []
    raw_event_count += len(extra_events)
    all_events.extend(extra_events)
    extra_by_teammate: dict[str, list[dict[str, Any]]] = {}
    for event in extra_events:
        teammate = str(event.get("teammate") or "unknown")
        if teammate in seen_teammates:
            continue
        extra_by_teammate.setdefault(teammate, []).append(event)
    for teammate, events in sorted(extra_by_teammate.items()):
        events.sort(key=_sort_key)
        deduped = _dedupe_activity_events(events)
        warnings = [
            str(item.get("message") or "")
            for item in deduped
            if _WARNING_RE.search(str(item.get("message") or ""))
        ]
        role = _canonical_role(teammate)
        current = deduped[-1] if deduped else None
        teammates.append(
            {
                "teammate": teammate,
                "role": role,
                "role_group": _role_group(role),
                "display_name": _display_name(teammate, role),
                "state": "completed",
                "last_at": str(current.get("last_at") or "") if current else None,
                "event_count": len(events),
                "raw_event_count": len(events),
                "deduped_event_count": len(deduped),
                "tool_uses": 0,
                "total_tokens": 0,
                "tools": [],
                "actions": deduped[-10:],
                "warnings": warnings[:5],
                "current_activity": str(current.get("message") or "") if current else "",
            }
        )

    all_events.sort(key=_sort_key)
    deduped_all = _dedupe_activity_events(all_events)
    warnings_all = [
        str(item["message"])
        for item in deduped_all
        if _WARNING_RE.search(str(item.get("message") or ""))
    ]
    highlights = [
        item
        for item in deduped_all
        if item.get("kind") in {"task_notification", "text", "tool_use"}
    ][-12:]
    actor_count = len(teammates)
    deduped_count = len(deduped_all)
    summary = (
        f"{ticket_id}: {actor_count} teammate"
        f"{'' if actor_count == 1 else 's'} produced {raw_event_count} "
        f"events, reduced to {deduped_count} de-duplicated activity items."
    )
    return {
        "ticket_id": ticket_id,
        "summary": summary,
        "raw_event_count": raw_event_count,
        "deduped_event_count": deduped_count,
        "teammates": teammates,
        "highlights": highlights,
        "warnings": warnings_all[:10],
    }


def _collect_replay(
    streams: list[tuple[str, Path]], n: int
) -> list[dict[str, Any]]:
    """Merge the last N filtered events across all streams, oldest first."""
    merged: list[dict[str, Any]] = []
    for teammate, path in streams:
        merged.extend(_replay_events_for_stream(teammate, path, n))
    merged.sort(key=_sort_key)
    return merged[-n:] if len(merged) > n else merged


class _StreamPosition:
    """Tracks byte offset and inode for rotate-tolerant tailing.

    When the inode changes (log rotated) or the file shrinks
    (truncated), reset to offset 0 so we don't skip the new content.
    """

    __slots__ = ("inode", "last_observed_at", "next_lineno", "offset", "path")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.inode: int | None = None
        self.last_observed_at = ""
        self.next_lineno = 1

    def read_new_lines(self) -> list[tuple[int, str]]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            # File hasn't been created yet (or was removed). Reset so
            # we start from the beginning when it reappears.
            self.offset = 0
            self.inode = None
            self.last_observed_at = ""
            self.next_lineno = 1
            return []
        if self.inode is None:
            self.inode = st.st_ino
        elif st.st_ino != self.inode or st.st_size < self.offset:
            # Rotation or truncation detected.
            self.offset = 0
            self.inode = st.st_ino
            self.last_observed_at = ""
            self.next_lineno = 1
        if st.st_size == self.offset:
            return []
        out: list[tuple[int, str]] = []
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
                self.offset = fh.tell()
        except OSError:
            return []
        text = chunk.decode("utf-8", errors="replace")
        # If the chunk ended mid-line, back off the offset so the
        # incomplete tail is re-read next poll. Trailing newline =
        # full chunk, no adjustment.
        if text and not text.endswith("\n"):
            last_nl = text.rfind("\n")
            if last_nl == -1:
                # The whole chunk is a partial line — rewind fully and
                # wait for the write to complete on the next poll.
                self.offset -= len(chunk)
                return []
            tail_len = len(text) - last_nl - 1
            self.offset -= tail_len
            text = text[: last_nl + 1]
        for line in text.splitlines():
            lineno = self.next_lineno
            self.next_lineno += 1
            stripped = line.strip()
            if not stripped:
                continue
            out.append((lineno, stripped))
        return out


def _sse_pack(event: dict[str, Any]) -> str:
    """Encode a shaped event as a single SSE ``data:`` frame."""
    return f"data: {json.dumps(event, default=str)}\n\n"


async def _stream_generator(
    streams: list[tuple[str, Path]], request: Request
) -> AsyncIterator[str]:
    """Full SSE generator: replay-then-tail.

    1. Emit the last ``REPLAY_LIMIT`` filtered events in timestamp order.
    2. Initialize per-stream tail positions pointing past the end
       (so we don't re-emit the replay window).
    3. Poll every ``TAIL_POLL_INTERVAL_SEC`` for new lines; yield
       shaped events as they arrive.
    4. Check ``request.is_disconnected()`` each iteration so the
       generator exits cleanly when the client closes the tab.
    """
    # Replay phase
    replay = _collect_replay(streams, REPLAY_LIMIT)
    for ev in replay:
        yield _sse_pack(ev)

    # Tail phase — start at end-of-file for each stream so we don't
    # re-emit replay content. A fresh write appended after the replay
    # computed offsets will still be caught on the next poll.
    positions: list[tuple[str, _StreamPosition]] = []
    for teammate, path in streams:
        pos = _StreamPosition(path)
        try:
            st = path.stat()
            pos.offset = st.st_size
            pos.inode = st.st_ino
            # Count current lines so new-line numbering stays accurate
            # for sort tiebreaks (we don't strictly need this for
            # correctness — fresh tail lines already order themselves
            # — but it keeps the data shape consistent with replay).
            with path.open("rb") as fh:
                pos.next_lineno = sum(1 for _ in fh) + 1
        except OSError:
            pass
        positions.append((teammate, pos))

    last_heartbeat = time.monotonic()
    yield ": ping\n\n"

    while True:
        if await request.is_disconnected():
            return
        new_events: list[dict[str, Any]] = []
        for teammate, pos in positions:
            for lineno, raw in pos.read_new_lines():
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                raw_ts = _raw_timestamp(parsed)
                if raw_ts:
                    pos.last_observed_at = raw_ts
                shaped = _filter_and_shape_event(
                    parsed,
                    teammate,
                    source_line=lineno,
                    observed_at=raw_ts or pos.last_observed_at,
                )
                if shaped is not None:
                    new_events.append(shaped)
        if new_events:
            new_events.sort(key=_sort_key)
            for ev in new_events:
                yield _sse_pack(ev)
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            yield ": ping\n\n"
            last_heartbeat = now
        await asyncio.sleep(TAIL_POLL_INTERVAL_SEC)


async def _no_activity_generator() -> AsyncIterator[str]:
    """Emit a single ``no_activity`` event and end the stream."""
    yield _sse_pack({"kind": "no_activity"})


@router.get("/api/traces/{ticket_id}/stream")
async def stream_events(
    ticket_id: str,
    request: Request,
    _auth: None = Depends(_require_dashboard_auth_query_or_header),
) -> StreamingResponse:
    """Server-Sent Events endpoint for live L2 activity.

    Replays the last 100 events on connect, then polls for new
    writes. Query-param auth (``?api_key=``) is accepted because
    EventSource cannot send custom headers.
    """
    _validate_ticket_id(ticket_id)
    worktree_root = _worktree_root_for_ticket(ticket_id)
    streams: list[tuple[str, Path]] = []
    if worktree_root is not None:
        streams = _find_session_streams(worktree_root)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",  # disable nginx buffering
        "Connection": "keep-alive",
    }
    if not streams:
        logger.info(
            "live_stream_no_activity",
            ticket_id=ticket_id,
            worktree_exists=worktree_root is not None,
        )
        return StreamingResponse(
            _no_activity_generator(),
            media_type="text/event-stream",
            headers=headers,
        )
    logger.info(
        "live_stream_started",
        ticket_id=ticket_id,
        teammates=[t for t, _ in streams],
    )
    return StreamingResponse(
        _stream_generator(streams, request),
        media_type="text/event-stream",
        headers=headers,
    )


# --- HTML page ---

_LIVE_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Live activity — {ticket_id}</title>
<style>
  :root {{
    color-scheme: dark light;
    --bg: #0f1419;
    --fg: #e6edf3;
    --muted: #8b949e;
    --panel: #161b22;
    --border: #30363d;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --accent: #58a6ff;
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--fg);
    margin: 0;
    padding: 1.5rem;
    line-height: 1.45;
  }}
  h1 {{ margin: 0 0 0.25rem; font-size: 1.25rem; }}
  .header {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 0.75rem;
    padding: 1rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 1rem;
  }}
  .stat {{ display: flex; flex-direction: column; gap: 0.15rem; }}
  .stat .label {{ font-size: 0.72rem; text-transform: uppercase; color: var(--muted); letter-spacing: 0.05em; }}
  .stat .val {{ font-size: 1.05rem; font-variant-numeric: tabular-nums; }}
  .dot {{ display: inline-block; width: 0.6rem; height: 0.6rem; border-radius: 50%; margin-right: 0.35rem; vertical-align: middle; }}
  .dot.green {{ background: var(--green); }}
  .dot.yellow {{ background: var(--yellow); }}
  .dot.red {{ background: var(--red); }}
  .banner {{ padding: 0.75rem 1rem; border-radius: 6px; margin-bottom: 1rem; font-size: 0.9rem; }}
  .banner.warn {{ background: rgba(248, 81, 73, 0.15); border: 1px solid var(--red); color: var(--red); }}
  .banner.info {{ background: var(--panel); border: 1px solid var(--border); color: var(--muted); }}
  #feed {{ display: flex; flex-direction: column-reverse; gap: 0.25rem; padding: 0; margin: 0; list-style: none; }}
  .ev {{ padding: 0.4rem 0.6rem; border-left: 2px solid var(--border); font-size: 0.88rem; word-break: break-word; }}
  .ev .time {{ color: var(--muted); font-size: 0.78rem; margin-right: 0.5rem; font-variant-numeric: tabular-nums; }}
  .ev .team {{ color: var(--accent); font-weight: 600; margin-right: 0.35rem; }}
  .ev.tool {{ border-left-color: var(--accent); }}
  .ev.started {{ border-left-color: var(--green); }}
  .ev.done {{ border-left-color: var(--green); }}
  .ev.text {{ border-left-color: var(--muted); color: var(--muted); font-style: italic; padding-left: 1.5rem; }}
  .ev.rate_limit {{ background: rgba(248, 81, 73, 0.15); border-left-color: var(--red); }}
  #older {{ margin-bottom: 0.5rem; padding: 0.35rem 0.75rem; background: transparent; color: var(--muted);
           border: 1px solid var(--border); border-radius: 4px; cursor: pointer; font-size: 0.82rem; }}
  #older:disabled {{ opacity: 0.4; cursor: default; }}
  a {{ color: var(--accent); }}
</style>
</head>
<body>
  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:0.5rem">
    <h1>Live activity — {ticket_id}</h1>
    <div class="meta" style="font-size:0.85rem">
      <a href="/dashboard">Dashboard</a> |
      <a href="/traces">Traces</a> |
      <a href="/autonomy">Autonomy</a> |
      <a href="/autonomy/learning">Learning</a> |
      <a href="/traces/{ticket_id}">Full trace</a>
    </div>
  </div>
  <div id="connStatus" class="banner info">Connecting…</div>
  <div class="header">
    <div class="stat"><span class="label">Last activity</span><span class="val" id="lastActivity"><span class="dot red"></span>—</span></div>
    <div class="stat"><span class="label">Tool uses</span><span class="val" id="toolUses">0</span></div>
    <div class="stat"><span class="label">Total tokens</span><span class="val" id="totalTokens">0</span></div>
    <div class="stat"><span class="label">Current phase</span><span class="val" id="phase">—</span></div>
    <div class="stat"><span class="label">Static detail</span><span class="val"><a href="/traces/{ticket_id}">full trace &rsaquo;</a></span></div>
  </div>
  <!-- "Load older" deferred — v1 ships replay-plus-tail only. See TODO below. -->
  <!-- TODO(live-stream): implement ``?before=<event_id>&limit=100`` for backward paging. -->
  <ul id="feed"></ul>
<script>
  // NOTE: EventSource cannot send custom headers, so the API key is
  // passed via query string. The page itself is auth-gated by the
  // same policy, so if this page loaded the key is already known to
  // whoever opened it. The key lands in server access logs and
  // browser history — acceptable for local-dev dashboards, not for
  // a publicly-exposed deployment.
  const params = new URLSearchParams(window.location.search);
  const apiKey = params.get('api_key') || '';
  const ticketId = {ticket_id_json};
  const feed = document.getElementById('feed');
  const connStatus = document.getElementById('connStatus');
  const lastActivityEl = document.getElementById('lastActivity');
  const toolUsesEl = document.getElementById('toolUses');
  const totalTokensEl = document.getElementById('totalTokens');

  let newestTs = null;
  const progressByTeammate = new Map();

  function recomputeTotals() {{
    let tools = 0, tokens = 0;
    for (const p of progressByTeammate.values()) {{
      tools += p.tool_uses || 0;
      tokens += p.total_tokens || 0;
    }}
    toolUsesEl.textContent = tools.toLocaleString();
    totalTokensEl.textContent = tokens.toLocaleString();
  }}

  function refreshLastActivity() {{
    if (!newestTs) {{
      lastActivityEl.innerHTML = '<span class="dot red"></span>—';
      return;
    }}
    const ageSec = Math.max(0, Math.round((Date.now() - new Date(newestTs).getTime()) / 1000));
    let cls = 'red';
    if (ageSec < 60) cls = 'green';
    else if (ageSec < 300) cls = 'yellow';
    let text;
    if (ageSec < 60) text = ageSec + 's ago';
    else if (ageSec < 3600) text = Math.round(ageSec / 60) + 'm ago';
    else text = Math.round(ageSec / 3600) + 'h ago';
    lastActivityEl.innerHTML = '<span class="dot ' + cls + '"></span>' + text;
  }}

  setInterval(refreshLastActivity, 5000);

  function renderEvent(ev) {{
    if (ev.kind === 'no_activity') {{
      connStatus.className = 'banner info';
      connStatus.textContent = 'No live activity — this ticket has not spawned an L2 team yet.';
      return;
    }}
    if (ev.timestamp) newestTs = ev.timestamp > (newestTs || '') ? ev.timestamp : newestTs;
    if (ev.kind === 'progress_update') {{
      progressByTeammate.set(ev.teammate, {{ tool_uses: ev.tool_uses, total_tokens: ev.total_tokens }});
      recomputeTotals();
      refreshLastActivity();
      return;  // progress updates don't render a feed row
    }}
    const li = document.createElement('li');
    li.className = 'ev ' + (ev.kind || '');
    const time = document.createElement('span');
    time.className = 'time';
    time.textContent = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : '';
    li.appendChild(time);
    const team = document.createElement('span');
    team.className = 'team';
    team.textContent = '[' + (ev.teammate || '?') + ']';
    li.appendChild(team);
    let body = '';
    if (ev.kind === 'tool_use') body = (ev.tool_name || 'tool') + ': ' + (ev.description || '');
    else if (ev.kind === 'task_started') body = '▶ started: ' + (ev.description || '');
    else if (ev.kind === 'task_notification') body = '✓ done: ' + (ev.summary || '');
    else if (ev.kind === 'text') body = ev.text || '';
    else if (ev.kind === 'rate_limit') body = '⚠ rate limit: ' + JSON.stringify(ev.info || {{}});
    else body = JSON.stringify(ev);
    const bodyNode = document.createElement('span');
    bodyNode.textContent = body;
    li.appendChild(bodyNode);
    feed.appendChild(li);
    refreshLastActivity();
  }}

  function connect() {{
    const url = '/api/traces/' + encodeURIComponent(ticketId) + '/stream' + (apiKey ? '?api_key=' + encodeURIComponent(apiKey) : '');
    const es = new EventSource(url);
    es.onopen = () => {{ connStatus.className = 'banner info'; connStatus.textContent = 'Connected — replaying recent activity…'; }};
    es.onmessage = (e) => {{
      try {{ renderEvent(JSON.parse(e.data)); }}
      catch (err) {{ console.error('bad event', err, e.data); }}
    }};
    es.onerror = () => {{
      connStatus.className = 'banner warn';
      connStatus.textContent = 'Disconnected — will retry automatically.';
    }};
  }}
  connect();
</script>
</body>
</html>
"""


@router.get("/traces/{ticket_id}/live", response_class=HTMLResponse)
async def live_page(
    ticket_id: str,
    _auth: None = Depends(_require_dashboard_auth_query_or_header),
) -> HTMLResponse:
    """HTML page that opens an EventSource to ``/api/traces/{id}/stream``.

    Renders even when no worktree exists — the SSE stream will emit
    a ``no_activity`` event which the client turns into a banner.
    """
    _validate_ticket_id(ticket_id)
    safe_id = _html_escape(ticket_id)
    body = _LIVE_PAGE_TEMPLATE.format(
        ticket_id=safe_id,
        ticket_id_json=json.dumps(ticket_id),
    )
    return HTMLResponse(content=body)


# Re-export the constant-time compare so tests don't need to monkey-patch
# around it if they want to exercise query-param auth directly.
__all__ = [
    "HEARTBEAT_INTERVAL_SEC",
    "REPLAY_LIMIT",
    "TAIL_POLL_INTERVAL_SEC",
    "_collect_replay",
    "_filter_and_shape_event",
    "_find_session_streams",
    "_teammate_name_for",
    "_worktree_root_for_ticket",
    "router",
]


# hmac re-exported via import so mypy doesn't flag it as unused in future
# simplifications that move the compare into this module.
_ = hmac
