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
