"""Tracing — generates trace IDs, writes persistent logs, consolidates artifacts.

Every ticket gets a single JSONL trace file at data/logs/<ticket-id>.jsonl
containing the complete audit trail from webhook to PR.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from redaction import redact

logger = structlog.get_logger()

LOGS_DIR = Path(__file__).resolve().parents[2] / "data" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


# Per-ticket write lock, lazily created. Guards ``append_trace`` appends
# and ``admin_re_redact`` in-place rewrites on the SAME ticket file so
# they can't interleave.
#
# Why threading.Lock (not asyncio.Lock): ``append_trace`` is sync and
# called from many sync callsites (queue worker, pipeline.py,
# test fixtures). A thread-based lock is safe from both sync and
# async contexts — an async FastAPI handler calling a sync-locked
# function just blocks its event loop briefly, which is fine because
# trace writes are microseconds.
#
# Why per-ticket (not global): concurrent appends for DIFFERENT tickets
# are the common case (multiple tickets processing in parallel) and
# must not serialize on each other. Only same-ticket operations need
# mutual exclusion.
#
# Why atomicity matters: POSIX append atomicity only holds for writes
# smaller than PIPE_BUF (4096 bytes on Linux, 512 on some BSD). Many
# trace entries are well over 4KB once they embed debug_payload /
# tool_result fields — without a lock two concurrent appends can
# interleave bytes and corrupt the JSONL stream (an entry starts in
# one thread, another thread's entry inserts mid-write, the first
# continues, and readline() returns two half-entries separated by
# a bogus interior newline).
_ticket_locks: dict[str, threading.Lock] = {}
_ticket_locks_mutex = threading.Lock()


def _get_ticket_lock(ticket_id: str) -> threading.Lock:
    """Return the per-ticket write lock, creating it under mutex.

    The mutex serializes dict mutation; the returned Lock serializes
    writes to the ticket's JSONL file. Once created, locks persist
    for the lifetime of the process — there's no eviction. On a busy
    day L1 sees ~50-100 distinct tickets, so memory is bounded.
    """
    with _ticket_locks_mutex:
        lock = _ticket_locks.get(ticket_id)
        if lock is None:
            lock = threading.Lock()
            _ticket_locks[ticket_id] = lock
    return lock

# Fields in imported pipeline.jsonl entries that may contain tool output /
# error content and therefore may leak credentials. These are documented
# hints for contributors — the actual redaction path walks recursively
# across every string value in the entry so new fields don't need to be
# added here to be covered.
_REDACT_IMPORTED_FIELDS = frozenset({
    "content",      # already redacted by the legacy path — safe to include
    "data",
    "error",
    "message",
    "output",
    "stderr",
    "stdout",
    "debug_payload",
    "tool_result",
    "details",
    "evidence",     # diagnostic checklist outputs
})

# Keys whose values are plain metadata and should NEVER be mutated by
# the recursive redactor. ``trace_id`` / ``ticket_id`` / ``phase`` /
# ``event`` are deterministic identifiers — redacting them on a false
# positive would break dedup and dashboards. Timestamps are ISO 8601
# strings and have no credential shape.
_RECURSE_SKIP_KEYS = frozenset({
    "trace_id",
    "ticket_id",
    "timestamp",
    "phase",
    "event",
    "source",
    "billing",
    "status",
    "ticket_title",
})


def _redact_recursive(obj: Any) -> tuple[Any, int]:
    """Walk ``obj`` (dict/list/str/anything) and redact every string
    value reachable from it.

    Returns ``(new_obj, count)``. The ``count`` is the total number of
    redaction-pattern matches made. Dicts and lists are mutated in
    place so callers that already held references keep seeing the
    redacted content; strings can't be mutated so the caller is
    responsible for putting the returned string back.

    Skips well-known metadata keys (``trace_id``, ``timestamp``, etc.)
    so false positives from the entropy pass can't corrupt identifier
    fields downstream dashboards rely on for dedup / ordering.

    Previously the tracer only redacted a fixed top-level allowlist of
    fields. That fixed list meant any agent step writing a credential
    into a sibling field — or into a nested dict under ``error_context``
    / ``details`` / ``tool_result`` / etc. — landed in the trace store
    verbatim. The recursive walk catches the nested case too, which
    matters in practice: ``error_context`` is a dict with ``stderr``
    inside it, and the old flat walk skipped the stderr body because
    it was one level down.
    """
    if isinstance(obj, str):
        if not obj:
            return obj, 0
        redacted, n = redact(obj)
        return redacted, n
    if isinstance(obj, dict):
        total = 0
        for key, value in list(obj.items()):
            if key in _RECURSE_SKIP_KEYS:
                continue
            new_value, n = _redact_recursive(value)
            if n:
                obj[key] = new_value
                total += n
        return obj, total
    if isinstance(obj, list):
        total = 0
        for i, value in enumerate(obj):
            new_value, n = _redact_recursive(value)
            if n:
                obj[i] = new_value
                total += n
        return obj, total
    # Numbers, bools, None — nothing to redact.
    return obj, 0


def redact_entry_in_place(entry: dict[str, Any]) -> int:
    """Redact every reachable string pocket in a trace entry in place.

    Walks the entry recursively — any nested dict/list/string is
    scanned, regardless of the field name. Metadata keys
    (``trace_id`` / ``timestamp`` / ``phase`` / ``event``) are skipped
    so deterministic identifiers stay intact.

    Returns the total number of redact-pattern matches made across all
    fields. The entry is mutated in place; callers that need to compare
    before/after can clone ahead of the call.

    This helper is the single source of truth for "what constitutes an
    entry's redactable surface area." Both the import path
    (``consolidate_worktree_logs``) and the rescan path
    (``POST /admin/re-redact`` in ``main.py``) call it so the two
    directions stay in sync automatically.
    """
    _, total = _redact_recursive(entry)
    return total


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically via a sibling temp file.

    Uses the same pattern ``admin_re_redact`` already had inline:
    write to ``<path>.<suffix>.atomic-write.tmp``, then ``os.replace``
    it over the real path. A crash mid-write leaves the original
    file intact instead of producing a truncated one. The winning
    ``os.replace`` is atomic on POSIX and any loser's temp file is
    silently overwritten, so concurrent writers cannot corrupt the
    target.

    On failure the temp file is best-effort unlinked so a botched
    write doesn't leave garbage next to the target.
    """
    tmp = path.with_suffix(path.suffix + ".atomic-write.tmp")
    try:
        tmp.write_text(content)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


# Billing source constants for trace entries.
BILLING_API = "api"
BILLING_MAX = "max_subscription"


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
    """Append a trace entry to the ticket's persistent log.

    Held under the per-ticket write lock for the duration of the write
    so the append cannot interleave with a concurrent append to the
    same file OR with ``/admin/re-redact``'s in-place rewrite.
    """
    entry = {
        "trace_id": trace_id,
        "ticket_id": ticket_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "phase": phase,
        "event": event,
        **kwargs,
    }
    path = trace_path(ticket_id)
    with _get_ticket_lock(ticket_id), path.open("a") as f:
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
    if not LOGS_DIR.exists():
        return 0
    return sum(1 for _ in LOGS_DIR.glob("*.jsonl"))


def _extract_trace_metadata(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Walk ``entries`` once and pull the common metadata fields.

    Returns a dict with keys ``pr_url``, ``review_verdict``,
    ``qa_result``, ``pipeline_mode``, and ``ticket_title``. The
    ``review_verdict`` / ``qa_result`` pair is overridden when a
    ``Pipeline complete`` entry is present (its values are the
    authoritative end-of-run verdict).

    Previously this loop was duplicated verbatim in ``list_traces``
    and ``_build_summary`` with subtly different field coverage —
    ``ticket_title`` lived only in list_traces, so the detail view
    had no way to surface it. Shared helper now guarantees both
    views extract the same set.
    """
    metadata: dict[str, str] = {
        "pr_url": "",
        "review_verdict": "",
        "qa_result": "",
        "pipeline_mode": "",
        "ticket_title": "",
    }
    for e in entries:
        if e.get("ticket_title") and not metadata["ticket_title"]:
            metadata["ticket_title"] = str(e["ticket_title"])
        if e.get("pr_url"):
            metadata["pr_url"] = str(e["pr_url"])
        if e.get("review_verdict"):
            metadata["review_verdict"] = str(e["review_verdict"])
        if e.get("qa_result"):
            metadata["qa_result"] = str(e["qa_result"])
        if e.get("pipeline_mode"):
            metadata["pipeline_mode"] = str(e["pipeline_mode"])
        if e.get("event") == "Pipeline complete":
            metadata["review_verdict"] = str(e.get("review_verdict", ""))
            metadata["qa_result"] = str(e.get("qa_result", ""))
    return metadata


def _compute_run_duration(run_entries: list[dict[str, Any]]) -> str:
    """Format the first-to-last-timestamp delta across ``run_entries``.

    Returns ``""`` when the list is empty, the timestamps don't parse,
    or the two timestamps can't be subtracted (mixed naive/aware is
    common in test fixtures and some legacy entries). Returns
    ``"Nm Ns"``/``"Ns"`` for durations ≤24h, and the
    ``">24h (multi-run)"`` marker for anything longer (a single
    trace that spans >24h almost always means multiple re-runs
    merged into one trace store file).

    Previously this try/except block was duplicated verbatim in
    ``list_traces`` and ``_build_summary`` with identical numeric
    constants and format strings — any tweak (e.g. switching to
    ``Xh Ym`` for ≥1h durations) had to land in two places. The
    original sites wrapped the whole subtract in the try, so we do
    the same here to preserve naive/aware-mixing tolerance.
    """
    if not run_entries:
        return ""
    # Use the last non-artifact entry as the end time — artifact
    # consolidation can happen minutes/hours after the run completes
    # and would inflate the duration.
    end_entry = run_entries[-1]
    for e in reversed(run_entries):
        if e.get("phase") != "artifact":
            end_entry = e
            break
    try:
        start_ts = datetime.fromisoformat(run_entries[0].get("timestamp", ""))
        end_ts = datetime.fromisoformat(end_entry.get("timestamp", ""))
        total_secs = (end_ts - start_ts).total_seconds()
    except (ValueError, TypeError):
        return ""
    if 0 < total_secs <= 86400:
        minutes = int(total_secs // 60)
        seconds = int(total_secs % 60)
        return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    if total_secs > 86400:
        return ">24h (multi-run)"
    return ""


def derive_trace_status(
    entries: list[dict[str, Any]],
    events: list[str],
    pr_url: str,
) -> str:
    """Single source of truth for trace → status-label mapping.

    Previously this logic was duplicated between ``list_traces`` (18
    branches, covering the full set of dashboard labels) and
    ``_build_summary`` (8 branches, silently missing half the cases —
    Cleaned Up / Failed / Timed Out / Merged / Implementing / Planned /
    CI Fix / Agent Done / Processing / Received — so the detail view
    fell back to ``events[-1] if events else "Unknown"`` for those
    states). A re-triggered trace could then show one label in the list
    view and a different label in the detail view.

    The predicates run in order — the first match wins. ``entries`` is
    accepted so branches that need to look at fields on individual
    entries (not just the flat ``event`` name list) can do so.
    """
    if not entries:
        return "Unknown"

    if "stale_worktree_cleaned" in events:
        # This run was cleaned up by a subsequent spawn.
        return "Cleaned Up"
    if "Escalated" in events:
        return "Escalated"
    if any(
        e.get("event") == "agent_finished" and e.get("status") == "escalated"
        for e in entries
    ):
        return "Failed"
    if any("timed out" in ev.lower() for ev in events):
        return "Timed Out"
    if "Pipeline complete" in events:
        return "Complete"
    if pr_url and not any("Pipeline complete" in ev for ev in events):
        return "PR Created"
    if any("QA complete" in ev for ev in events):
        return "QA Done"
    if any("Review complete" in ev for ev in events):
        return "Review Done"
    if any("Merge complete" in ev for ev in events):
        return "Merged"
    if any("unit-" in ev and "complete" in ev for ev in events):
        return "Implementing"
    if any("Plan" in ev and ("complete" in ev or "approved" in ev) for ev in events):
        return "Planned"
    if any("l2_dispatched" in ev for ev in events):
        return "Dispatched"
    if any("ci_fix_spawned" in ev for ev in events):
        return "CI Fix"
    if any("agent_finished" in ev for ev in events) and not pr_url:
        return "Agent Done (no PR)"
    if any("processing_completed" in ev for ev in events):
        return "Enriched"
    if any("processing_started" in ev for ev in events):
        return "Processing"
    if any("webhook_received" in ev for ev in events):
        return "Received"
    last = entries[-1]
    return str(last.get("event", "Unknown"))


def list_traces(offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    """List ticket traces with summary info, paginated.

    Args:
        offset: Number of traces to skip (0-based).
        limit: Maximum traces to return (default 50, 0 = all).
    """
    traces: list[dict[str, Any]] = []
    if not LOGS_DIR.exists():
        return traces
    all_paths = sorted(LOGS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    end = offset + limit if limit else len(all_paths)
    for path in all_paths[offset:end]:
        ticket_id = path.stem
        entries = read_trace(ticket_id)
        if not entries:
            continue

        first = entries[0]
        last = entries[-1]

        metadata = _extract_trace_metadata(entries)
        total_phases = len({e.get("phase") for e in entries})
        events = [e.get("event", "") for e in entries]

        # Derive status label from the event history. Single source of
        # truth shared with build_span_tree's _build_summary so both
        # views agree on what a trace's status is.
        status = derive_trace_status(entries, events, metadata["pr_url"])

        # Compute the run-start index once per trace — threaded into
        # _derive_current_phase here AND down to build_trace_list_row via
        # the _run_start_idx field stashed on the trace dict. Prevents
        # the list view from scanning the entries list 3x per trace.
        run_start_idx = _find_run_start_idx(entries)
        run_entries = entries[run_start_idx:]

        run_started_at = (
            run_entries[0].get("timestamp", "") if run_entries else ""
        )
        duration = _compute_run_duration(run_entries)

        traces.append({
            "ticket_id": ticket_id,
            "ticket_title": metadata["ticket_title"],
            "trace_id": first.get("trace_id", ""),
            "started_at": first.get("timestamp", ""),
            "run_started_at": run_started_at or first.get("timestamp", ""),
            "completed_at": last.get("timestamp", ""),
            "duration": duration,
            "status": status,
            "pr_url": metadata["pr_url"],
            "review_verdict": metadata["review_verdict"],
            "qa_result": metadata["qa_result"],
            "pipeline_mode": metadata["pipeline_mode"],
            "current_phase": _derive_current_phase(
                entries, run_start_idx=run_start_idx
            ),
            "phases": total_phases,
            "entries": len(entries),
            "_raw_entries": entries,  # cached for dashboard; excluded from JSON API
            "_run_start_idx": run_start_idx,  # cached for build_trace_list_row
        })

    return traces


def _derive_current_phase(
    entries: list[dict[str, Any]],
    *,
    run_start_idx: int | None = None,
) -> str:
    """Return the most recent agent phase name for live progress display.

    Walks the entries in reverse looking for the last agent-written phase.
    Empty string if no agent activity yet. Pass ``run_start_idx`` to avoid
    recomputing it when the caller already has it.
    """
    if run_start_idx is None:
        run_start_idx = _find_run_start_idx(entries)
    for e in reversed(entries[run_start_idx:]):
        if e.get("source") == "agent":
            phase = e.get("phase", "")
            if phase and phase not in ("ticket_read",):
                return str(phase)
    return ""


def find_run_start_idx(entries: list[dict[str, Any]]) -> int:
    """Public alias for ``_find_run_start_idx`` — callers that need to
    compute the run-start index once and thread it through several
    consumers (``build_span_tree``, ``compute_phase_durations``,
    ``extract_diagnostic_info``, etc.) should call this and pass the
    result in via the ``run_start_idx`` kwarg to each consumer. On
    multi-thousand-entry traces this replaces 4-6 redundant scans with
    a single O(N) walk per request.
    """
    return _find_run_start_idx(entries)


def _find_run_start_idx(entries: list[dict[str, Any]]) -> int:
    """Find the index of the last pipeline run boundary.

    A valid run boundary is a webhook_received or "Pipeline started" event
    that is followed by either agent-written entries (pipeline.jsonl via
    live trace) or L1 processing_started. Webhooks that were dedup-skipped
    (no subsequent processing) are NOT run boundaries.
    """
    # Candidate boundary indices
    candidates: list[int] = []
    for i, e in enumerate(entries):
        ev = e.get("event", "")
        if "Pipeline started" in ev or "webhook_received" in ev:
            candidates.append(i)

    if not candidates:
        return 0

    # Walk candidates from latest to earliest, pick the first one that has
    # agent entries OR processing_started/Pipeline complete after it
    for idx in reversed(candidates):
        for j in range(idx + 1, len(entries)):
            after = entries[j]
            if after.get("source") == "agent":
                return idx
            ev = after.get("event", "")
            if ev == "processing_started" or "Pipeline" in ev:
                return idx
    # Fallback: use newest candidate (most likely the current run)
    return candidates[-1]


def compute_phase_durations(
    entries: list[dict[str, Any]],
    *,
    run_start_idx: int | None = None,
) -> list[dict[str, Any]]:
    """Compute per-phase durations from consecutive agent-phase timestamps.

    Filters to the last pipeline run and to ``source == "agent"`` entries
    (L2 pipeline phases written to pipeline.jsonl). Returns a list of dicts
    with ``phase``, ``event``, and ``duration_seconds`` for each phase.
    Pass ``run_start_idx`` to avoid recomputing it when the caller already
    has it.
    """
    if not entries:
        return []

    if run_start_idx is None:
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

    # Build per-event deltas first, then collapse same-phase entries
    # so the duration bar shows one segment per logical phase.
    raw: list[dict[str, Any]] = []
    for i in range(len(agent_entries) - 1):
        try:
            ts_start = datetime.fromisoformat(agent_entries[i].get("timestamp", ""))
            ts_end = datetime.fromisoformat(agent_entries[i + 1].get("timestamp", ""))
            delta = (ts_end - ts_start).total_seconds()
            if delta < 0:
                continue
            raw.append({
                "phase": agent_entries[i].get("phase", ""),
                "event": agent_entries[i].get("event", ""),
                "duration_seconds": round(delta, 1),
            })
        except (ValueError, TypeError):
            continue

    # Collapse consecutive entries with the same phase into one segment
    durations: list[dict[str, Any]] = []
    for entry in raw:
        if durations and durations[-1]["phase"] == entry["phase"]:
            durations[-1]["duration_seconds"] += entry["duration_seconds"]
            durations[-1]["event"] = entry["event"]  # keep the later event name
        else:
            durations.append(dict(entry))

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
            content = str(entry.get("content", ""))
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


def extract_diagnostic_info(
    entries: list[dict[str, Any]],
    *,
    run_start_idx: int | None = None,
) -> dict[str, Any]:
    """Extract error diagnostics and hints from trace entries.

    Returns a dict with ``errors`` (list of error events from the last run),
    ``hint`` (human-readable suggestion), and ``last_event`` (last non-error event).
    Pass ``run_start_idx`` to avoid recomputing it when the caller already has it.
    """
    if not entries:
        return {"errors": [], "hint": "", "last_event": ""}

    if run_start_idx is None:
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

# Artifact event name constants — single source of truth used both by the
# consolidation dispatch below and by downstream consumers (dashboard panels,
# diagnostic checklist, bundle endpoint). Symbolic references protect against
# typos and make it easy to grep for where an artifact type is rendered.
ARTIFACT_CODE_REVIEW = "code_review_artifact"
ARTIFACT_QA_MATRIX = "qa_matrix_artifact"
ARTIFACT_JUDGE_VERDICT = "judge_verdict_artifact"
ARTIFACT_MERGE_REPORT = "merge_report_artifact"
ARTIFACT_PLAN_REVIEW = "plan_review_artifact"
ARTIFACT_PLAN = "plan_artifact"
ARTIFACT_BLOCKED_UNITS = "blocked_units_artifact"
ARTIFACT_SIMPLIFY = "simplify_artifact"
ARTIFACT_ESCALATION = "escalation_artifact"
ARTIFACT_SESSION_LOG = "session_log_artifact"
ARTIFACT_EFFECTIVE_CLAUDE_MD = "effective_claude_md_artifact"
ARTIFACT_SESSION_STREAM = "session_stream_artifact"
ARTIFACT_TOOL_INDEX = "tool_index"

_ARTIFACT_PHASE_MAP: dict[str, str] = {
    ARTIFACT_CODE_REVIEW: "code_review",
    ARTIFACT_QA_MATRIX: "qa_validation",
    ARTIFACT_JUDGE_VERDICT: "code_review",
    ARTIFACT_MERGE_REPORT: "merge",
    ARTIFACT_PLAN_REVIEW: "plan_review",
    ARTIFACT_PLAN: "planning",
    ARTIFACT_BLOCKED_UNITS: "implementation",
    ARTIFACT_SIMPLIFY: "simplify",
    ARTIFACT_ESCALATION: "complete",
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


def find_artifact(
    entries: list[dict[str, Any]],
    event_name: str,
    *,
    latest: bool = True,
) -> dict[str, Any] | None:
    """Return the artifact entry for ``event_name`` or None.

    Walks `entries` looking for a row where `phase == "artifact"` and
    `event == event_name`. On re-triggered traces (multiple runs for the
    same ticket) there can be more than one match; `latest=True` (default)
    returns the most recent by scanning in reverse, which is what every
    current caller wants — dashboards render the latest state, diagnostic
    consumes the latest tool_index, the bundle exports the latest artifacts.

    Set `latest=False` to get the first match (first-run artifact) if you
    specifically need historical state.

    For hot-path callers that need multiple artifacts from the same
    entries list in one shot (e.g. the bundle builder and dashboard
    panels), prefer ``latest_artifacts(entries)`` which does a single
    O(N) walk once and returns a dict keyed by event name.
    """
    iterator = reversed(entries) if latest else iter(entries)
    for entry in iterator:
        if entry.get("phase") == "artifact" and entry.get("event") == event_name:
            return entry
    return None


def latest_artifacts(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One-pass index of the latest artifact entry per event name.

    Walks ``entries`` once in reverse and records the first
    ``phase == "artifact"`` hit seen for each distinct ``event`` value.
    Callers that need several artifacts from the same list — ``_build_bundle``
    asks for six, the dashboard panels for five — should call this once
    upfront and then do O(1) dict lookups instead of paying O(N) per
    artifact in repeated ``find_artifact`` calls.

    Semantics match ``find_artifact(..., latest=True)``: the entry closest
    to the end of the list wins. For small traces this is microseconds
    either way; for multi-thousand-entry traces (common after live-stream
    and consolidation merge the same run) it turns ~6-9 full-list scans
    into one.
    """
    out: dict[str, dict[str, Any]] = {}
    for entry in reversed(entries):
        if entry.get("phase") != "artifact":
            continue
        event = entry.get("event")
        if isinstance(event, str) and event and event not in out:
            out[event] = entry
    return out


def build_span_tree(
    entries: list[dict[str, Any]],
    *,
    run_start_idx: int | None = None,
) -> dict[str, Any]:
    """Group flat trace entries into an L1/L2/L3 span tree with artifact linking.

    Returns a dict with ``l1``, ``l2``, ``l3``, ``errors``, and ``summary`` keys.
    Designed to power both the Langfuse-style detail view and the trace list view.
    Pass ``run_start_idx`` to avoid recomputing it when the caller already has it.
    """
    if not entries:
        return {
            "l1": [], "l2": [], "l3": [], "errors": [],
            "summary": {},
        }

    if run_start_idx is None:
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

    # L1 entries: ticket intake only (webhook received, processing, analyst,
    # dispatch). Exclude skipped/duplicate webhooks, completion callbacks,
    # and artifact consolidation — those are infrastructure noise, not intake.
    l1_skip_events = {"ado_webhook_skipped_not_edge", "agent_finished"}
    l1_skip_phases = {"artifact", "completion"}
    for e in entries:
        source = e.get("source", "")
        phase = e.get("phase", "")
        event = e.get("event", "")
        is_l1 = (
            source != "agent"
            and phase not in l1_skip_phases
            and not phase.startswith("l3_")
            and event != "error"
            and event not in l1_skip_events
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

    # Build L2 nodes with artifact linking and duration.
    #
    # compute_phase_durations returns deltas between consecutive agent
    # events keyed by (phase, event). Match each L2 node to its duration
    # entry so every row shows the time it took, not a raw timestamp.
    # The last agent event has no "next" event — compute its duration as
    # the delta from the previous event to it.
    durations = compute_phase_durations(entries)
    # Compute per-event duration as "time from previous agent event to
    # this one" — the natural "how long did this step take" measure.
    # compute_phase_durations gives forward deltas (event N → N+1);
    # we need backward deltas (event N-1 → N). Build from sorted agent
    # timestamps directly.
    agent_entries_sorted = sorted(
        [e for e in run_entries if e.get("source") == "agent"],
        key=lambda e: e.get("timestamp", ""),
    )
    # Map (phase, event) → backward delta in seconds
    dur_lookup: dict[tuple[str, str], float] = {}
    for i, ae in enumerate(agent_entries_sorted):
        if i == 0:
            dur_lookup[(ae.get("phase", ""), ae.get("event", ""))] = 0.0
            continue
        try:
            ts_prev = datetime.fromisoformat(agent_entries_sorted[i - 1].get("timestamp", ""))
            ts_curr = datetime.fromisoformat(ae.get("timestamp", ""))
            delta = max(0, (ts_curr - ts_prev).total_seconds())
        except (ValueError, TypeError):
            delta = 0.0
        dur_lookup[(ae.get("phase", ""), ae.get("event", ""))] = round(delta, 1)

    l2_nodes: list[dict[str, Any]] = []
    for e in l2_phase_events:
        phase = e.get("phase", "")
        event = e.get("event", "")

        # Find matching artifacts
        artifacts = [
            a for a in artifact_entries
            if _ARTIFACT_PHASE_MAP.get(a.get("event", "")) == phase
        ]

        # Look up duration (backward delta: time from previous event to this one)
        dur = dur_lookup.get((phase, event), 0.0)

        l2_nodes.append({
            "entry": e,
            "started_entry": l2_started_events.get(phase),
            "duration_seconds": dur,
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

    # Pull pr_url/review_verdict/qa_result/pipeline_mode/ticket_title
    # in one shared walk. Token counts and QA criteria pass/total are
    # detail-view-only so we do those in a small loop below, but the
    # common metadata now matches list_traces exactly.
    metadata = _extract_trace_metadata(run_entries)
    summary["pr_url"] = metadata["pr_url"]
    summary["pipeline_mode"] = metadata["pipeline_mode"]
    summary["review_verdict"] = metadata["review_verdict"]
    summary["qa_result"] = metadata["qa_result"]

    billing_api_in = 0
    billing_api_out = 0
    billing_max_in = 0
    billing_max_out = 0

    for e in run_entries:
        if e.get("event") == "analyst_completed":
            ti = e.get("tokens_in", 0)
            to_ = e.get("tokens_out", 0)
            if isinstance(ti, int):
                summary["tokens_in"] = ti
                billing_api_in += ti
            if isinstance(to_, int):
                summary["tokens_out"] = to_
                billing_api_out += to_
        if e.get("source") == "agent":
            ti = e.get("tokens_in", 0)
            to_ = e.get("tokens_out", 0)
            if isinstance(ti, int):
                billing_max_in += ti
            if isinstance(to_, int):
                billing_max_out += to_
        if e.get("event") == "QA complete":
            summary["qa_passed"] = e.get("criteria_passed", 0)
            summary["qa_total"] = e.get("criteria_total", 0)

    summary["billing_api_tokens_in"] = billing_api_in
    summary["billing_api_tokens_out"] = billing_api_out
    summary["billing_max_tokens_in"] = billing_max_in
    summary["billing_max_tokens_out"] = billing_max_out

    phases_seen: list[str] = []
    for e in l2_phases:
        phase = e.get("phase", "")
        if phase and phase not in phases_seen:
            phases_seen.append(phase)
    summary["phases_completed"] = phases_seen

    # Derive status via the shared helper — this block used to be an
    # inline 8-branch chain that was missing half the cases covered by
    # list_traces, so the detail view and the list view could disagree
    # on the same trace. Now they can't.
    summary["status"] = derive_trace_status(run_entries, events, summary["pr_url"])
    summary["duration"] = _compute_run_duration(run_entries)

    return summary


def build_trace_list_row(
    trace_summary: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    run_start_idx: int | None = None,
) -> dict[str, Any]:
    """Enrich a trace summary with phase dots and duration percentage for list rendering.

    Adds ``phase_dots`` (list of {phase, color} dicts) and ``duration_pct``
    (0-100 relative to a 30-minute baseline) to the trace summary. Pass
    ``run_start_idx`` to avoid recomputing it when the caller already has it.
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
    if run_start_idx is None:
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
    # Duration format: "Nm Ss" or "Ss" (from _format_duration)
    duration_pct = 0
    duration = trace_summary.get("duration", "")
    if duration and duration != ">24h (multi-run)":
        try:
            total_secs = 0
            if "m " in duration:
                # "5m 30s" → minutes + seconds
                m_part, s_part = duration.split("m ")
                total_secs = int(m_part) * 60 + int(s_part.rstrip("s"))
            elif duration.endswith("s"):
                # "30s" → seconds only
                total_secs = int(duration.rstrip("s"))
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
    ticket_id: str,
    trace_id: str,
    worktree_path: str,
    *,
    repo_full_name: str = "",
    head_sha: str = "",
) -> None:
    """Import pipeline.jsonl and artifacts from a worktree into the persistent trace.

    Called by the completion callback after the agent finishes.
    Idempotent — skips if agent entries for this trace_id are already consolidated.
    """
    # Build dedup set from existing live-reported agent entries
    existing = read_trace(ticket_id)
    existing_keys: set[tuple[str, str]] = set()
    for e in existing:
        if e.get("source") == "agent":
            existing_keys.add((e.get("phase", ""), e.get("event", "")))

    if existing_keys:
        logger.info("consolidation_dedup_active",
                     ticket_id=ticket_id, live_entries=len(existing_keys))

    wt = Path(worktree_path)

    # Redaction-on-consolidation: every artifact entry's string fields in the
    # KNOWN-RISKY set get a redact() pass before hitting the trace store. The
    # running count is reported in the consolidation log line below.
    total_redacted = 0

    def _redact_and_count(content: str) -> str:
        nonlocal total_redacted
        redacted_content, n = redact(content)
        total_redacted += n
        return redacted_content

    # Import pipeline.jsonl entries (skip any already live-reported).
    #
    # Every imported entry has its known-risky top-level string fields
    # (tool_result, debug_payload, stderr/stdout, error, etc.) run through
    # the redactor. The legacy path only redacted ``content`` — which meant
    # any agent step that wrote a credential into a sibling field landed
    # in the trace store verbatim. Fixed by walking _REDACT_IMPORTED_FIELDS.
    pipeline_log = wt / ".harness" / "logs" / "pipeline.jsonl"
    imported = 0
    skipped = 0
    if pipeline_log.exists():
        # errors="replace" tolerates any stray invalid UTF-8 bytes
        # (ANSI escapes, binary-ish tool stderr that leaked into the
        # log). A strict decode here used to abort consolidation
        # mid-function, silently dropping every artifact file that
        # gets imported AFTER pipeline.jsonl (code-review, qa-matrix,
        # judge-verdict, effective CLAUDE.md, plans, session-stream
        # reference, tool_index). The caller wraps consolidation in
        # a broad try/except so the endpoint survived, but the
        # resulting trace lost most of its observability — this is
        # the same "non-UTF-8 silent bypass" class of bug that
        # _redact_bytes had before the surrogateescape fix.
        for line in pipeline_log.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                dedup_key = (entry.get("phase", ""), entry.get("event", ""))
                if dedup_key in existing_keys:
                    skipped += 1
                    continue
                entry["trace_id"] = trace_id
                entry["ticket_id"] = ticket_id
                entry["source"] = "agent"
                entry["billing"] = BILLING_MAX
                # One helper call covers every known-risky string pocket
                # in the entry, so this path cannot drift from the
                # /admin/re-redact rescan path. ``total_redacted`` is the
                # consolidation summary counter (also bumped by
                # ``_redact_and_count`` below for artifact content).
                total_redacted += redact_entry_in_place(entry)
                path = trace_path(ticket_id)
                with path.open("a") as f:
                    f.write(json.dumps(entry) + "\n")
                imported += 1
            except json.JSONDecodeError:
                pass
    if imported or skipped:
        logger.info("consolidation_complete",
                     ticket_id=ticket_id, imported=imported, skipped=skipped)

    # Redaction-on-consolidation (artifact content):
    #
    # Every artifact entry's `content` field gets a redact() pass before
    # hitting the trace store. This means the trace store is safe-to-share
    # from the moment it lands — bundle, artifact downloads, and dashboard
    # panels all read from a pre-redacted source.
    #
    # session-stream.jsonl is deliberately skipped here: it's stored by
    # reference (artifact_path pointer), not inline. Redacting the on-disk
    # file at consolidation time would either (a) mutate a file another
    # process may still be writing to, or (b) require copying it to a second
    # location. Instead, session-stream redaction happens lazily at bundle-
    # export time (see _build_bundle in main.py), which preserves the raw
    # stream as a local-only forensic escape hatch and lets a future
    # POST /admin/re-redact pick up pattern updates by re-scanning the store.
    #
    # NOTE: ``total_redacted`` and ``_redact_and_count`` are defined above
    # next to the pipeline.jsonl import so both paths feed the same counter.

    # Import span detail files — matches the Observability Model in harness-CLAUDE.md
    artifact_files = {
        "code-review.md": ARTIFACT_CODE_REVIEW,
        "qa-matrix.md": ARTIFACT_QA_MATRIX,
        "judge-verdict.md": ARTIFACT_JUDGE_VERDICT,
        "merge-report.md": ARTIFACT_MERGE_REPORT,
        "plan-review.md": ARTIFACT_PLAN_REVIEW,
        "blocked-units.md": ARTIFACT_BLOCKED_UNITS,
        "simplify.md": ARTIFACT_SIMPLIFY,
        "escalation.md": ARTIFACT_ESCALATION,
        "session.log": ARTIFACT_SESSION_LOG,
    }

    logs_dir = wt / ".harness" / "logs"
    # Resolve archive root: walk up from the worktree until we find
    # a directory that contains a ``trace-archive`` sibling (created
    # by spawn_team.py). Production worktrees live at
    # ``<root>/worktrees/ai/<ticket>`` (3 levels up), but test fixtures
    # may use ``<root>/worktrees/ai-<ticket>`` (2 levels up). Walking
    # avoids hard-coding the depth.
    archive_root = wt.parent
    for _ in range(4):
        if (archive_root / "trace-archive").is_dir():
            break
        archive_root = archive_root.parent
    archive_logs_dir = archive_root / "trace-archive" / ticket_id
    for filename, event_name in artifact_files.items():
        artifact_path = logs_dir / filename
        if not artifact_path.exists():
            artifact_path = archive_logs_dir / filename
        if artifact_path.exists():
            append_trace(
                ticket_id, trace_id,
                phase="artifact",
                event=event_name,
                content=_redact_and_count(
                    artifact_path.read_text(
                        encoding="utf-8", errors="replace"
                    )[:5000]
                ),
            )

    # Effective CLAUDE.md — injected at worktree root, captures the instructions
    # the agent was actually operating under for this run.
    effective_claude_md = wt / "CLAUDE.md"
    if effective_claude_md.exists():
        append_trace(
            ticket_id, trace_id,
            phase="artifact",
            event=ARTIFACT_EFFECTIVE_CLAUDE_MD,
            content=_redact_and_count(
                effective_claude_md.read_text(
                    encoding="utf-8", errors="replace"
                )[:5000]
            ),
        )

    # session-stream.jsonl is stored by reference — it can be megabytes and
    # is preserved separately in <client_repo.parent>/trace-archive/<ticket>/
    # by the cleanup step in scripts/spawn_team.py. Worktrees live at
    # <client_repo.parent>/worktrees/ai/<ticket>, so the archive is THREE
    # levels up from the worktree (ticket → ai → worktrees → parent),
    # not two. The previous code used wt.parent.parent which resolved to
    # the ``worktrees/`` dir, missing the archive entirely.
    #
    # Prefer the archive path when it exists (stable — survives worktree
    # cleanup). For failed/escalated runs the archive may not exist yet
    # because spawn_team.py only archives on status == "complete", so fall
    # back to the live worktree path which is still on disk for those runs.
    # If neither exists, skip the reference entry entirely.
    stream_path = logs_dir / "session-stream.jsonl"
    archive_path = (
        archive_root / "trace-archive" / ticket_id / "session-stream.jsonl"
    )
    # Prefer archive (stable — survives worktree cleanup), fall back to
    # live worktree path for in-progress or failed runs.
    effective_stream = (
        archive_path if archive_path.exists()
        else stream_path if stream_path.exists()
        else None
    )
    if effective_stream is not None:
        try:
            size_bytes = effective_stream.stat().st_size
            with effective_stream.open() as f:
                line_count = sum(1 for _ in f)
        except OSError:
            size_bytes = 0
            line_count = 0
        stream_ref_path: Path | None = effective_stream

        if stream_ref_path is not None:
            append_trace(
                ticket_id, trace_id,
                phase="artifact",
                event=ARTIFACT_SESSION_STREAM,
                artifact_path=str(stream_ref_path),
                size_bytes=size_bytes,
                line_count=line_count,
            )

        # Parse the stream once to build a declarative tool-call summary.
        try:
            from tool_index import build_tool_index
            index = build_tool_index(effective_stream)
            # tool_index.first_tool_error.message captures up to 500 chars of
            # raw tool-error output (e.g. `sf org display --json` can echo a
            # live access token into stderr). Redact it in place so the trace
            # store entry — and every dashboard panel that reads it — is safe
            # to share. Handled here, not in tool_index.py, to keep all
            # redaction decisions centralized in the tracer.
            if index and isinstance(index.get("first_tool_error"), dict):
                msg = index["first_tool_error"].get("message", "")
                if isinstance(msg, str) and msg:
                    index["first_tool_error"]["message"] = _redact_and_count(msg)
            append_trace(
                ticket_id, trace_id,
                phase="artifact",
                event=ARTIFACT_TOOL_INDEX,
                index=index,
            )
        except Exception:
            logger.exception("tool_index_build_failed", ticket_id=ticket_id)

    # Import plan if exists
    for plan_path in sorted((wt / ".harness" / "plans").glob("plan-v*.json")):
        append_trace(
            ticket_id, trace_id,
            phase="artifact",
            event=ARTIFACT_PLAN,
            plan_version=plan_path.stem,
            content=_redact_and_count(
                plan_path.read_text(encoding="utf-8", errors="replace")[:5000]
            ),
        )

    if total_redacted:
        logger.info(
            "consolidation_redacted",
            ticket_id=ticket_id,
            redaction_count=total_redacted,
        )

    # Autonomy sidecar ingest (best-effort; must never break consolidation)
    if repo_full_name and head_sha:
        try:
            from autonomy_artifact_ingest import ingest_worktree_sidecars
            result = ingest_worktree_sidecars(
                worktree_path,
                ticket_id=ticket_id,
                repo_full_name=repo_full_name,
                head_sha=head_sha,
            )
            logger.info(
                "autonomy_sidecars_ingested",
                ticket_id=ticket_id,
                sidecars_present=result.sidecars_present,
                code_review=result.code_review_issues_staged,
                qa=result.qa_issues_staged,
                validated=result.judge_validated,
                rejected=result.judge_rejected,
                failures=result.parse_failures,
            )
        except Exception:
            logger.exception(
                "autonomy_sidecar_ingest_failed", ticket_id=ticket_id
            )

    logger.info(
        "worktree_logs_consolidated",
        ticket_id=ticket_id,
        trace_id=trace_id,
        redaction_count=total_redacted,
    )
