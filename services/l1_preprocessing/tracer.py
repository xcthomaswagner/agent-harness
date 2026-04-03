"""Tracing — generates trace IDs, writes persistent logs, consolidates artifacts.

Every ticket gets a single JSONL trace file at data/logs/<ticket-id>.jsonl
containing the complete audit trail from webhook to PR.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

LOGS_DIR = Path(__file__).resolve().parents[2] / "data" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def generate_trace_id() -> str:
    """Generate a unique trace ID for a ticket run."""
    return uuid.uuid4().hex[:12]


def trace_path(ticket_id: str) -> Path:
    """Get the path to a ticket's trace file."""
    return LOGS_DIR / f"{ticket_id}.jsonl"


def append_trace(
    ticket_id: str,
    trace_id: str,
    phase: str,
    event: str,
    **kwargs: Any,
) -> None:
    """Append a trace entry to the ticket's persistent log."""
    entry = {
        "trace_id": trace_id,
        "ticket_id": ticket_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "phase": phase,
        "event": event,
        **kwargs,
    }
    path = trace_path(ticket_id)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def read_trace(ticket_id: str) -> list[dict[str, Any]]:
    """Read all trace entries for a ticket."""
    path = trace_path(ticket_id)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                entries.append(json.loads(line))
    return entries


def count_traces() -> int:
    """Count total number of trace files."""
    return sum(1 for _ in LOGS_DIR.glob("*.jsonl"))


def list_traces(offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    """List ticket traces with summary info, paginated.

    Args:
        offset: Number of traces to skip (0-based).
        limit: Maximum traces to return (default 50, 0 = all).
    """
    traces: list[dict[str, Any]] = []
    all_paths = sorted(LOGS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    end = offset + limit if limit else len(all_paths)
    for path in all_paths[offset:end]:
        ticket_id = path.stem
        entries = read_trace(ticket_id)
        if not entries:
            continue

        first = entries[0]
        last = entries[-1]

        # Extract key info
        pr_url = ""
        review_verdict = ""
        qa_result = ""
        pipeline_mode = ""
        total_phases = len({e.get("phase") for e in entries})
        events = [e.get("event", "") for e in entries]

        for e in entries:
            if e.get("pr_url"):
                pr_url = str(e["pr_url"])
            if e.get("review_verdict"):
                review_verdict = str(e["review_verdict"])
            if e.get("qa_result"):
                qa_result = str(e["qa_result"])
            if e.get("pipeline_mode"):
                pipeline_mode = str(e["pipeline_mode"])
            if e.get("event") == "Pipeline complete":
                review_verdict = str(e.get("review_verdict", ""))
                qa_result = str(e.get("qa_result", ""))

        # --- Derive meaningful status from event history ---
        if "Escalated" in events:
            status = "Escalated"
        elif "Pipeline complete" in events:
            status = "Complete"
        elif pr_url and not any("Pipeline complete" in ev for ev in events):
            status = "PR Created"
        elif any("QA complete" in ev for ev in events):
            status = "QA Done"
        elif any("Review complete" in ev for ev in events):
            status = "Review Done"
        elif any("Merge complete" in ev for ev in events):
            status = "Merged"
        elif any("unit-" in ev and "complete" in ev for ev in events):
            status = "Implementing"
        elif any("Plan" in ev and ("complete" in ev or "approved" in ev) for ev in events):
            status = "Planned"
        elif any("l2_dispatched" in ev for ev in events):
            status = "Dispatched"
        elif any("ci_fix_spawned" in ev for ev in events):
            status = "CI Fix"
        elif any("agent_finished" in ev for ev in events) and not pr_url:
            status = "Agent Done (no PR)"
        elif any("processing_completed" in ev for ev in events):
            status = "Enriched"
        elif any("processing_started" in ev for ev in events):
            status = "Processing"
        elif any("webhook_received" in ev for ev in events):
            status = "Received"
        else:
            status = last.get("event", "Unknown")

        # --- Compute duration using only entries from the last run ---
        duration = ""
        run_started_at = ""
        try:
            run_start_idx = _find_run_start_idx(entries)
            run_entries = entries[run_start_idx:]
            if run_entries:
                run_started_at = run_entries[0].get("timestamp", "")
                start_ts = datetime.fromisoformat(run_started_at)
                end_ts = datetime.fromisoformat(run_entries[-1].get("timestamp", ""))
                delta = end_ts - start_ts
                total_secs = delta.total_seconds()
                # Cap at 24 hours — anything longer is data from separate runs
                if 0 < total_secs <= 86400:
                    minutes = int(total_secs // 60)
                    seconds = int(total_secs % 60)
                    duration = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
                elif total_secs > 86400:
                    duration = ">24h (multi-run)"
        except (ValueError, TypeError):
            pass

        traces.append({
            "ticket_id": ticket_id,
            "trace_id": first.get("trace_id", ""),
            "started_at": first.get("timestamp", ""),
            "run_started_at": run_started_at or first.get("timestamp", ""),
            "completed_at": last.get("timestamp", ""),
            "duration": duration,
            "status": status,
            "pr_url": pr_url,
            "review_verdict": review_verdict,
            "qa_result": qa_result,
            "pipeline_mode": pipeline_mode,
            "phases": total_phases,
            "entries": len(entries),
        })

    return traces


def _find_run_start_idx(entries: list[dict[str, Any]]) -> int:
    """Find the index of the last pipeline start or webhook event (run boundary)."""
    run_start_idx = 0
    for i, e in enumerate(entries):
        ev = e.get("event", "")
        if "Pipeline started" in ev or "webhook_received" in ev:
            run_start_idx = i
    return run_start_idx


def compute_phase_durations(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute per-phase durations from consecutive agent-phase timestamps.

    Filters to the last pipeline run and to ``source == "agent"`` entries
    (L2 pipeline phases written to pipeline.jsonl). Returns a list of dicts
    with ``phase``, ``event``, and ``duration_seconds`` for each phase.
    """
    if not entries:
        return []

    run_start_idx = _find_run_start_idx(entries)
    run_entries = entries[run_start_idx:]

    # Filter to agent-written phase entries (L2 pipeline)
    agent_entries = [e for e in run_entries if e.get("source") == "agent"]
    if len(agent_entries) < 2:
        return []

    # Sort by timestamp (should already be ordered, but be safe)
    try:
        agent_entries.sort(key=lambda e: e.get("timestamp", ""))
    except TypeError:
        return []

    durations: list[dict[str, Any]] = []
    for i in range(len(agent_entries) - 1):
        try:
            ts_start = datetime.fromisoformat(agent_entries[i].get("timestamp", ""))
            ts_end = datetime.fromisoformat(agent_entries[i + 1].get("timestamp", ""))
            delta = (ts_end - ts_start).total_seconds()
            if delta < 0:
                continue
            durations.append({
                "phase": agent_entries[i].get("phase", ""),
                "event": agent_entries[i].get("event", ""),
                "duration_seconds": round(delta, 1),
            })
        except (ValueError, TypeError):
            continue

    return durations


def extract_escalation_reason(entries: list[dict[str, Any]]) -> str:
    """Extract a human-readable escalation reason from trace entries.

    Checks (in order):
    1. ``escalation_artifact`` event — first non-heading, non-blank content line
    2. ``Escalated`` event — return the event string
    3. Staleness — if no terminal event and last entry is old
    """
    for entry in entries:
        if entry.get("event") == "escalation_artifact":
            content = entry.get("content", "")
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line

    events = [e.get("event", "") for e in entries]
    if "Escalated" in events:
        return "Escalated"

    # Check for staleness (no terminal event, last entry > 1 hour ago)
    terminal_events = {"Pipeline complete", "agent_finished", "Escalated"}
    if entries and not any(e.get("event", "") in terminal_events for e in entries):
        last_ts = entries[-1].get("timestamp", "")
        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts)
                age_hours = (datetime.now(UTC) - last_dt).total_seconds() / 3600
                if age_hours > 1:
                    return f"No progress since {last_ts[:19]}"
            except (ValueError, TypeError):
                pass

    return ""


def extract_diagnostic_info(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract error diagnostics and hints from trace entries.

    Returns a dict with ``errors`` (list of error events from the last run),
    ``hint`` (human-readable suggestion), and ``last_event`` (last non-error event).
    """
    if not entries:
        return {"errors": [], "hint": "", "last_event": ""}

    run_start_idx = _find_run_start_idx(entries)
    run_entries = entries[run_start_idx:]

    # Collect error events
    errors = []
    for e in run_entries:
        if e.get("event") == "error":
            errors.append({
                "error_type": e.get("error_type", "Unknown"),
                "error_message": e.get("error_message", ""),
                "timestamp": e.get("timestamp", "")[:19],
                "phase": e.get("phase", ""),
                "error_context": e.get("error_context", {}),
            })

    # Find last non-error event
    last_event = ""
    for e in reversed(run_entries):
        if e.get("event") != "error":
            last_event = e.get("event", "")
            break

    # Generate context-aware hint
    hint = _generate_hint(last_event, errors)

    return {"errors": errors, "hint": hint, "last_event": last_event}


def _generate_hint(last_event: str, errors: list[dict[str, Any]]) -> str:
    """Generate a diagnostic hint based on last pipeline state and errors."""
    if not errors:
        # No error events recorded
        if "processing_started" in last_event:
            return "Processing started but no further events. Check terminal logs."
        if "l2_dispatched" in last_event:
            return (
                "Agent dispatched but never reported back. "
                "Check if session is running or inspect worktree."
            )
        if "Pipeline complete" in last_event or "agent_finished" in last_event:
            return ""  # Completed successfully, no hint needed
        if last_event:
            return "No error recorded. Check terminal output."
        return ""

    # Use the first error for the hint (most likely root cause)
    err = errors[0]
    msg = err["error_message"].lower()
    etype = err["error_type"]

    if "processing_started" in last_event or last_event == "":
        if "rate limit" in msg:
            return "Anthropic API rate limited. Retry or check usage."
        if "connection" in msg:
            return "Cannot reach Anthropic API. Check network."
        if "json" in msg:
            return "Analyst returned invalid JSON. Check analyst prompt."
        if "empty" in msg:
            return "Analyst returned empty response. May be a model error."
        return f"Analyst failed: {err['error_message'][:200]}"

    if "l2_dispatched" in last_event or etype == "SpawnFailed":
        stderr = err.get("error_context", {}).get("stderr", "")
        if stderr:
            return f"Spawn failed: {stderr[:200]}"
        return "Spawn script failed. Check worktree/git state."

    if "processing_completed" in last_event:
        return f"Post-processing failed: {err['error_message'][:200]}"

    return f"{etype}: {err['error_message'][:200]}"


# --- Span tree construction ---

_ARTIFACT_PHASE_MAP: dict[str, str] = {
    "code_review_artifact": "code_review",
    "qa_matrix_artifact": "qa_validation",
    "judge_verdict_artifact": "code_review",
    "merge_report_artifact": "merge",
    "plan_review_artifact": "plan_review",
    "plan_artifact": "planning",
    "blocked_units_artifact": "implementation",
    "simplify_artifact": "simplify",
    "escalation_artifact": "complete",
}

# Phase icon types for the span tree UI
_PHASE_ICON_TYPE: dict[str, str] = {
    "webhook": "event",
    "analyst": "span",
    "pipeline": "event",
    "ticket_read": "span",
    "planning": "agent",
    "plan_review": "span",
    "implementation": "tool",
    "merge": "span",
    "code_review": "span",
    "judge": "span",
    "qa_validation": "span",
    "simplify": "tool",
    "pr_created": "event",
    "complete": "trace",
    "completion": "event",
    "spawn": "event",
}


def build_span_tree(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Group flat trace entries into an L1/L2/L3 span tree with artifact linking.

    Returns a dict with ``l1``, ``l2``, ``l3``, ``errors``, and ``summary`` keys.
    Designed to power both the Langfuse-style detail view and the trace list view.
    """
    if not entries:
        return {
            "l1": [], "l2": [], "l3": [], "errors": [],
            "summary": {},
        }

    run_start_idx = _find_run_start_idx(entries)
    run_entries = entries[run_start_idx:]

    # Separate entries by layer — use ALL entries for L1 (they precede run boundary),
    # but only run_entries for L2/L3/artifacts/errors
    l1_entries: list[dict[str, Any]] = []
    l2_phase_events: list[dict[str, Any]] = []
    l2_started_events: dict[str, dict[str, Any]] = {}  # phase → started entry
    artifact_entries: list[dict[str, Any]] = []
    l3_entries: list[dict[str, Any]] = []
    error_entries: list[dict[str, Any]] = []

    # L1 entries: non-agent, non-artifact entries from the full trace
    for e in entries:
        source = e.get("source", "")
        phase = e.get("phase", "")
        is_l1 = (
            source != "agent"
            and phase != "artifact"
            and not phase.startswith("l3_")
            and e.get("event") != "error"
        )
        if is_l1:
            l1_entries.append(e)

    # L2, L3, artifacts, errors: from the last run only
    for e in run_entries:
        source = e.get("source", "")
        phase = e.get("phase", "")
        event = e.get("event", "")

        if event == "error":
            error_entries.append(e)
        elif phase.startswith("l3_") or phase == "l3_session":
            l3_entries.append(e)
        elif phase == "artifact":
            artifact_entries.append(e)
        elif source == "agent":
            if event == "phase_started":
                l2_started_events[phase] = e
            else:
                l2_phase_events.append(e)

    # Build L1 nodes
    l1_nodes = [{"entry": e, "icon": _PHASE_ICON_TYPE.get(e.get("phase", ""), "event")}
                for e in l1_entries]

    # Build L2 nodes with artifact linking and duration
    durations = compute_phase_durations(entries)
    duration_map = {d["phase"]: d["duration_seconds"] for d in durations}

    l2_nodes: list[dict[str, Any]] = []
    for e in l2_phase_events:
        phase = e.get("phase", "")

        # Find matching artifacts
        artifacts = [
            a for a in artifact_entries
            if _ARTIFACT_PHASE_MAP.get(a.get("event", "")) == phase
        ]

        l2_nodes.append({
            "entry": e,
            "started_entry": l2_started_events.get(phase),
            "duration_seconds": duration_map.get(phase),
            "artifacts": artifacts,
            "icon": _PHASE_ICON_TYPE.get(phase, "span"),
        })

    # Build L3 nodes
    l3_nodes = [{"entry": e, "icon": "event"} for e in l3_entries]

    # Build summary
    summary = _build_summary(run_entries, l2_phase_events, durations)

    return {
        "l1": l1_nodes,
        "l2": l2_nodes,
        "l3": l3_nodes,
        "errors": [{"entry": e} for e in error_entries],
        "summary": summary,
    }


def _build_summary(
    run_entries: list[dict[str, Any]],
    l2_phases: list[dict[str, Any]],
    durations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Extract summary metrics from trace entries."""
    summary: dict[str, Any] = {
        "status": "", "duration": "", "pipeline_mode": "",
        "review_verdict": "", "qa_result": "",
        "qa_passed": 0, "qa_total": 0,
        "pr_url": "", "tokens_in": 0, "tokens_out": 0,
        "phases_completed": [],
    }

    events = [e.get("event", "") for e in run_entries]
    phases_seen: list[str] = []

    for e in run_entries:
        if e.get("pr_url"):
            summary["pr_url"] = str(e["pr_url"])
        if e.get("pipeline_mode"):
            summary["pipeline_mode"] = str(e["pipeline_mode"])
        if e.get("event") == "Pipeline complete":
            summary["review_verdict"] = str(e.get("review_verdict", ""))
            summary["qa_result"] = str(e.get("qa_result", ""))
        if e.get("event") == "analyst_completed":
            ti = e.get("tokens_in", 0)
            to_ = e.get("tokens_out", 0)
            if isinstance(ti, int):
                summary["tokens_in"] = ti
            if isinstance(to_, int):
                summary["tokens_out"] = to_
        if e.get("event") == "QA complete":
            summary["qa_passed"] = e.get("criteria_passed", 0)
            summary["qa_total"] = e.get("criteria_total", 0)

    for e in l2_phases:
        phase = e.get("phase", "")
        if phase and phase not in phases_seen:
            phases_seen.append(phase)
    summary["phases_completed"] = phases_seen

    # Derive status (reuse existing logic pattern)
    if "Escalated" in events:
        summary["status"] = "Escalated"
    elif "Pipeline complete" in events:
        summary["status"] = "Complete"
    elif summary["pr_url"] and "Pipeline complete" not in events:
        summary["status"] = "PR Created"
    elif "QA complete" in events:
        summary["status"] = "QA Done"
    elif "Review complete" in events:
        summary["status"] = "Review Done"
    elif any("l2_dispatched" in ev for ev in events):
        summary["status"] = "Dispatched"
    elif any("processing_completed" in ev for ev in events):
        summary["status"] = "Enriched"
    else:
        summary["status"] = events[-1] if events else "Unknown"

    # Duration
    if run_entries:
        try:
            start_ts = datetime.fromisoformat(run_entries[0].get("timestamp", ""))
            end_ts = datetime.fromisoformat(run_entries[-1].get("timestamp", ""))
            total_secs = (end_ts - start_ts).total_seconds()
            if 0 < total_secs <= 86400:
                minutes = int(total_secs // 60)
                seconds = int(total_secs % 60)
                summary["duration"] = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
            elif total_secs > 86400:
                summary["duration"] = ">24h (multi-run)"
        except (ValueError, TypeError):
            pass

    return summary


def build_trace_list_row(
    trace_summary: dict[str, Any], entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """Enrich a trace summary with phase dots and duration percentage for list rendering.

    Adds ``phase_dots`` (list of {phase, color} dicts) and ``duration_pct``
    (0-100 relative to a 30-minute baseline) to the trace summary.
    """
    phase_dot_colors: dict[str, str] = {
        "ticket_read": "#64748B",
        "planning": "#9333EA",
        "plan_review": "#9333EA",
        "implementation": "#EA580C",
        "merge": "#82CB15",
        "code_review": "#6466F1",
        "judge": "#6466F1",
        "qa_validation": "#124D49",
        "simplify": "#64748B",
        "pr_created": "#64748B",
        "complete": "#64748B",
    }

    # Extract L2 phases from entries
    run_start_idx = _find_run_start_idx(entries)
    run_entries = entries[run_start_idx:]
    phase_dots: list[dict[str, str]] = []
    seen_phases: set[str] = set()

    for e in run_entries:
        if e.get("source") != "agent" or e.get("event") == "phase_started":
            continue
        phase = e.get("phase", "")
        if phase and phase not in seen_phases:
            seen_phases.add(phase)
            color = phase_dot_colors.get(phase, "#64748B")
            phase_dots.append({"phase": phase, "color": color})

    # Duration percentage (relative to 30-minute baseline)
    duration_pct = 0
    duration = trace_summary.get("duration", "")
    if duration and duration != ">24h (multi-run)":
        try:
            parts = duration.replace("s", "").split("m ")
            total_secs = int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
            duration_pct = min(100, int((total_secs / 1800) * 100))  # 30 min = 100%
        except (ValueError, IndexError):
            pass
    elif duration == ">24h (multi-run)":
        duration_pct = 100

    # Duration color
    duration_color = "#124D49"  # green
    if duration_pct > 50:
        duration_color = "#C79004"  # yellow
    if duration_pct > 80 or duration == ">24h (multi-run)":
        duration_color = "#DB2626"  # red

    row = dict(trace_summary)
    row["phase_dots"] = phase_dots
    row["duration_pct"] = duration_pct
    row["duration_color"] = duration_color
    return row


def consolidate_worktree_logs(
    ticket_id: str, trace_id: str, worktree_path: str
) -> None:
    """Import pipeline.jsonl and artifacts from a worktree into the persistent trace.

    Called by the completion callback after the agent finishes.
    """
    wt = Path(worktree_path)

    # Import pipeline.jsonl entries
    pipeline_log = wt / ".harness" / "logs" / "pipeline.jsonl"
    if pipeline_log.exists():
        for line in pipeline_log.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entry["trace_id"] = trace_id
                entry["ticket_id"] = ticket_id
                entry["source"] = "agent"
                path = trace_path(ticket_id)
                with path.open("a") as f:
                    f.write(json.dumps(entry) + "\n")
            except json.JSONDecodeError:
                pass

    # Import span detail files — matches the Observability Model in harness-CLAUDE.md
    artifact_files = {
        "code-review.md": "code_review_artifact",
        "qa-matrix.md": "qa_matrix_artifact",
        "judge-verdict.md": "judge_verdict_artifact",
        "merge-report.md": "merge_report_artifact",
        "plan-review.md": "plan_review_artifact",
        "blocked-units.md": "blocked_units_artifact",
        "simplify.md": "simplify_artifact",
        "escalation.md": "escalation_artifact",
    }

    logs_dir = wt / ".harness" / "logs"
    for filename, event_name in artifact_files.items():
        artifact_path = logs_dir / filename
        if artifact_path.exists():
            append_trace(
                ticket_id, trace_id,
                phase="artifact",
                event=event_name,
                content=artifact_path.read_text()[:5000],
            )

    # Import plan if exists
    for plan_path in sorted((wt / ".harness" / "plans").glob("plan-v*.json")):
        append_trace(
            ticket_id, trace_id,
            phase="artifact",
            event="plan_artifact",
            plan_version=plan_path.stem,
            content=plan_path.read_text()[:5000],
        )

    logger.info("worktree_logs_consolidated", ticket_id=ticket_id, trace_id=trace_id)
