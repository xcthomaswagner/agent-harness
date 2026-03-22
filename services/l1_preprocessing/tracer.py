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


def list_traces() -> list[dict[str, Any]]:
    """List all ticket traces with summary info."""
    traces: list[dict[str, Any]] = []
    for path in sorted(LOGS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
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

        traces.append({
            "ticket_id": ticket_id,
            "trace_id": first.get("trace_id", ""),
            "started_at": first.get("timestamp", ""),
            "completed_at": last.get("timestamp", ""),
            "status": last.get("event", ""),
            "pr_url": pr_url,
            "review_verdict": review_verdict,
            "qa_result": qa_result,
            "pipeline_mode": pipeline_mode,
            "phases": total_phases,
            "entries": len(entries),
        })

    return traces


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

    # Import code review
    review_path = wt / ".harness" / "logs" / "code-review.md"
    if review_path.exists():
        append_trace(
            ticket_id, trace_id,
            phase="artifact",
            event="code_review_artifact",
            content=review_path.read_text()[:5000],
        )

    # Import QA matrix
    qa_path = wt / ".harness" / "logs" / "qa-matrix.md"
    if qa_path.exists():
        append_trace(
            ticket_id, trace_id,
            phase="artifact",
            event="qa_matrix_artifact",
            content=qa_path.read_text()[:5000],
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
