"""JSON endpoints for the operator dashboard under ``/api/operator``.

Split from ``operator_api.py`` (which handles the SPA shell + static
assets) to keep concerns clean: this module is data, that module is
HTML + static serving.

Each view of the dashboard gets one or two endpoints here. Shapes are
stable — the SPA's TypeScript types in services/operator_ui/src/api/
types.ts track them 1:1. Breaking-change discipline: if a field renames
or disappears, bump the route path (``/v2/...``) rather than silently
mutating the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from auth import _require_dashboard_auth
from autonomy_metrics import compute_profile_metrics
from autonomy_store import ensure_schema, open_connection
from autonomy_store.auto_merge import list_recent_auto_merge_decisions
from autonomy_store.lessons import list_lesson_candidates
from autonomy_store.pr_runs import list_pr_runs
from client_profile import list_profiles, load_profile
from tracer import (
    compute_phase_durations,
    find_run_start_idx,
    read_trace,
)
from tracer import (
    list_traces as _list_traces,
)

router = APIRouter(
    prefix="/api/operator",
    tags=["operator"],
    dependencies=[Depends(_require_dashboard_auth)],
)


def _db_path_from_settings() -> Path:
    """Resolve ``settings.autonomy_db_path`` at call time so tests'
    monkey-patched settings take effect per-request. Returns a Path
    because ``open_connection`` needs ``.parent`` for mkdir.
    """
    import main

    return Path(main.settings.autonomy_db_path)


def _compute_auto_merge_rate(
    conn: Any, profile: str, window_days: int
) -> float:
    """Auto-merge adoption rate = merged decisions / eligible decisions.

    Reads ``manual_overrides`` where ``override_type =
    'auto_merge_decision'``. Each row's payload carries a ``decision``
    field written by ``record_auto_merge_decision`` with values like
    ``merged`` / ``blocked`` / ``skipped`` / ``hold``. Rate = merged /
    (merged + blocked + hold); skipped rows are excluded because they
    mean the run wasn't eligible for auto-merge in the first place.

    Returns 0.0 when there are no eligible decisions in the window.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    rows = list_recent_auto_merge_decisions(
        conn,
        limit=500,
        since_iso=cutoff,
        client_profile=profile,
    )
    if not rows:
        return 0.0
    import json as _json

    eligible = 0
    merged = 0
    for r in rows:
        try:
            payload = _json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (ValueError, TypeError):
            continue
        decision = str(payload.get("decision", "")).lower()
        if decision in ("", "skipped", "not_eligible"):
            continue
        eligible += 1
        if decision in ("merged", "auto_merged", "merge"):
            merged += 1
    if eligible == 0:
        return 0.0
    return round(merged / eligible, 3)


def _count_pr_runs_in_window(
    conn: Any, profile: str, hours: int
) -> tuple[int, int]:
    """Return (in_flight_count, completed_in_window_count).

    in_flight = pr_runs not merged.
    completed = pr_runs merged in the last ``hours``.
    """
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    recent = list_pr_runs(conn, client_profile=profile, since_iso=since)
    in_flight = sum(1 for r in recent if not int(r["merged"]))
    completed = sum(1 for r in recent if int(r["merged"]))
    return in_flight, completed


@router.get("/profiles")
def get_profiles() -> dict[str, Any]:
    """List all client profiles with recent autonomy metrics.

    Reads ``runtime/client-profiles/*.yaml`` for the authoritative list
    (the profiles that actually exist — not the 4 mock profiles from
    the design). For each profile, joins with ``compute_profile_metrics``
    for FPA / escape / catch and ``_compute_auto_merge_rate`` for
    auto-merge adoption. Counts in-flight and completed-24h PR runs so
    the Home profile cards have live activity numbers.

    Contract (per profile):

      {
        "id": str,
        "name": str,
        "sample": str,                          # "Salesforce · Apex"
        "in_flight": int,
        "completed_24h": int,
        "fpa": float (0..1) | None,
        "escape": float (0..1) | None,
        "catch": float (0..1) | None,
        "auto_merge": float (0..1),
      }
    """
    profile_names = list_profiles()
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        out: list[dict[str, Any]] = []
        for name in profile_names:
            profile = load_profile(name)
            if profile is None:
                continue
            # ClientProfile exposes ``name`` and ``platform_profile`` —
            # use the profile name as the display label and the platform
            # string as the sample tag beside it.
            display = profile.name or name
            sample = profile.platform_profile or ""
            metrics = compute_profile_metrics(conn, name, window_days=30)
            in_flight, completed_24h = _count_pr_runs_in_window(
                conn, name, hours=24
            )
            auto_merge = _compute_auto_merge_rate(conn, name, window_days=30)
            out.append(
                {
                    "id": name,
                    "name": display,
                    "sample": sample,
                    "in_flight": in_flight,
                    "completed_24h": completed_24h,
                    "fpa": metrics.get("first_pass_acceptance_rate"),
                    "escape": metrics.get("defect_escape_rate"),
                    "catch": metrics.get("self_review_catch_rate"),
                    "auto_merge": auto_merge,
                }
            )
        # Sort by in-flight DESC, then name ASC — busiest profiles first.
        out.sort(key=lambda p: (-p["in_flight"], p["name"]))
        return {"profiles": out}
    finally:
        conn.close()


# --- Traces ---------------------------------------------------------------

# The tracer exposes a free-form status vocabulary ("Processing", "PR
# Created", "CI Fix", "Enriched", etc.). The operator dashboard wants a
# much coarser in-flight / stuck / queued / done partition so filter
# chips work. This table is the single source of truth; update it here
# when tracer adds a new status.
_STATUS_TO_BUCKET: dict[str, str] = {
    # Terminal successes + terminal failures both fall in "done" so the
    # board clears them out of the operator's immediate attention.
    "Complete": "done",
    "Merged": "done",
    "Failed": "done",
    "Timed Out": "done",
    # Stuck — the pipeline is alive but can't progress without help.
    "CI Fix": "stuck",
    "Agent Done (no PR)": "stuck",
    # Queued — before anything started.
    "Received": "queued",
    "Enriched": "queued",
    # Everything else is in-flight.
    "Processing": "in-flight",
    "Dispatched": "in-flight",
    "Planned": "in-flight",
    "Implementing": "in-flight",
    "Review Done": "in-flight",
    "QA Done": "in-flight",
    "PR Created": "in-flight",
}


def _normalize_trace_status(status: str) -> str:
    """Map tracer status → operator-bucket (in-flight/stuck/queued/done)."""
    return _STATUS_TO_BUCKET.get(status, "in-flight")


def _shape_trace_row(t: dict[str, Any]) -> dict[str, Any]:
    """Reshape a tracer.list_traces row to the operator dashboard contract."""
    return {
        "id": t["ticket_id"],
        "title": t.get("ticket_title") or "",
        "status": _normalize_trace_status(t.get("status", "")),
        "raw_status": t.get("status", ""),
        "phase": t.get("current_phase", ""),
        "elapsed": t.get("duration", ""),
        "started_at": t.get("run_started_at") or t.get("started_at", ""),
        "pr_url": t.get("pr_url") or None,
        "pipeline_mode": t.get("pipeline_mode", ""),
        "review_verdict": t.get("review_verdict", ""),
        "qa_result": t.get("qa_result", ""),
    }


@router.get("/traces")
def get_traces(
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Recent pipeline traces, newest first.

    Query params:
      * ``status`` — optional bucket filter (``in-flight`` | ``stuck`` |
        ``queued`` | ``done``). Unknown values yield zero rows.
      * ``limit`` — default 100, cap 500.
      * ``offset`` — for pagination.

    The source is tracer.list_traces (scans every JSONL under data/logs/).
    At scale this becomes expensive; mtime-sorted + limit provides
    adequate short-term pagination. A cache layer lands when list size
    exceeds ~2,500 runs (current scale: low hundreds).
    """
    capped_limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    raw = _list_traces(offset=offset, limit=capped_limit)
    shaped = [_shape_trace_row(t) for t in raw]
    if status is not None:
        shaped = [t for t in shaped if t["status"] == status]
    return {
        "traces": shaped,
        "count": len(shaped),
        "offset": offset,
        "limit": capped_limit,
    }


# --- Trace detail --------------------------------------------------------

# Fixed phase sequence the design renders as a 5-dot row. Runtime phase
# events map into this fixed set; unmapped phases fall into the closest
# bucket but are still returned in the raw timeline below.
_CANONICAL_PHASES: tuple[tuple[str, str], ...] = (
    ("planning", "Planning"),
    ("scaffolding", "Scaffolding"),
    ("implementing", "Implementing"),
    ("reviewing", "Reviewing"),
    ("merging", "Merging"),
)

_PHASE_ALIASES: dict[str, str] = {
    "plan": "planning",
    "planner": "planning",
    "worktree": "scaffolding",
    "spawn": "scaffolding",
    "develop": "implementing",
    "developer": "implementing",
    "implement": "implementing",
    "review": "reviewing",
    "reviewer": "reviewing",
    "judge": "reviewing",
    "qa": "reviewing",
    "merge": "merging",
    "merge_coordinator": "merging",
    "pr": "merging",
}


def _canonical_phase(phase: str) -> str | None:
    """Map an agent phase label to one of the 5 canonical buckets, or
    None when the phase is outside the L2 pipeline (webhook, pipeline,
    ticket_read — infrastructure phases the design doesn't render).
    """
    p = phase.lower()
    if not p or p in ("webhook", "pipeline", "ticket_read"):
        return None
    for canon, _label in _CANONICAL_PHASES:
        if canon in p:
            return canon
    return _PHASE_ALIASES.get(p)


def _shape_trace_detail(
    ticket_id: str, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """Reshape a full trace entry list into the operator detail payload.

    Returns:
      {
        id, title, status, raw_status, started_at, elapsed, pr_url,
        phases: [{ key, name, state, duration_seconds, event_count }],
        events: [{ t, ev, phase, msg }],   (latest 200 entries)
      }
    """
    if not entries:
        raise HTTPException(status_code=404, detail=f"trace not found: {ticket_id}")

    run_start_idx = find_run_start_idx(entries)
    run_entries = entries[run_start_idx:]
    metadata = _extract_trace_metadata_local(entries)

    # Phase durations + event counts, mapped into canonical buckets.
    durations = compute_phase_durations(entries, run_start_idx=run_start_idx)
    per_bucket_duration: dict[str, float] = {k: 0.0 for k, _ in _CANONICAL_PHASES}
    per_bucket_events: dict[str, int] = {k: 0 for k, _ in _CANONICAL_PHASES}
    for d in durations:
        canon = _canonical_phase(str(d.get("phase", "")))
        if canon is None:
            continue
        per_bucket_duration[canon] += float(d.get("duration_seconds") or 0.0)
        per_bucket_events[canon] += 1
    for e in run_entries:
        canon = _canonical_phase(str(e.get("phase", "")))
        if canon:
            per_bucket_events[canon] += 1

    # Derive per-phase state:
    #   done    — duration>0 AND a later phase has activity
    #   active  — currently-emitting phase (last agent-written phase)
    #   fail    — any error event in that phase
    #   pending — no activity yet
    current = _derive_current_phase_local(run_entries)
    current_canon = _canonical_phase(current) if current else None
    fail_phases: set[str] = set()
    for e in run_entries:
        if "error" in str(e.get("event", "")).lower() or e.get("level") == "error":
            canon = _canonical_phase(str(e.get("phase", "")))
            if canon:
                fail_phases.add(canon)

    # Ordered done detection: a phase is "done" when some later canonical
    # phase has ≥1 event AND the phase itself has ≥1 event.
    phase_order = [k for k, _ in _CANONICAL_PHASES]
    phases_out: list[dict[str, Any]] = []
    for i, (key, label) in enumerate(_CANONICAL_PHASES):
        if key in fail_phases:
            state = "fail"
        elif key == current_canon:
            state = "active"
        elif per_bucket_events[key] > 0:
            later_active = any(
                per_bucket_events[phase_order[j]] > 0
                for j in range(i + 1, len(phase_order))
            )
            state = "done" if later_active or (
                metadata["status"] in ("Complete", "Merged")
            ) else "active"
        else:
            state = "pending"
        phases_out.append(
            {
                "key": key,
                "name": label,
                "state": state,
                "duration_seconds": round(per_bucket_duration[key], 1),
                "event_count": per_bucket_events[key],
            }
        )

    # Raw event stream — last 200 items, agent-filtered, shaped for the
    # design's 3-column log.
    event_rows: list[dict[str, Any]] = []
    for e in run_entries[-200:]:
        phase = str(e.get("phase", ""))
        canon = _canonical_phase(phase)
        event_rows.append(
            {
                "t": str(e.get("timestamp", "")),
                "ev": str(e.get("event", "")),
                "phase": canon or phase,
                "msg": _event_message(e),
            }
        )

    return {
        "id": ticket_id,
        "title": metadata["ticket_title"],
        "status": _normalize_trace_status(metadata["status"]),
        "raw_status": metadata["status"],
        "pipeline_mode": metadata["pipeline_mode"],
        "started_at": metadata["started_at"],
        "elapsed": metadata["elapsed"],
        "pr_url": metadata["pr_url"] or None,
        "review_verdict": metadata["review_verdict"],
        "qa_result": metadata["qa_result"],
        "phases": phases_out,
        "events": event_rows,
    }


def _extract_trace_metadata_local(
    entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Pull a compact summary off an entries list.

    Uses the same helpers list_traces uses so the detail + summary views
    agree on status vocabulary.
    """
    from tracer import (
        _compute_run_duration,
        _extract_trace_metadata,
        derive_trace_status,
    )

    meta = _extract_trace_metadata(entries)
    events = [e.get("event", "") for e in entries]
    status = derive_trace_status(entries, events, meta.get("pr_url", ""))
    run_start_idx = find_run_start_idx(entries)
    run_entries = entries[run_start_idx:]
    started_at = (
        run_entries[0].get("timestamp", "") if run_entries else entries[0].get("timestamp", "")
    )
    elapsed = _compute_run_duration(run_entries) if run_entries else ""
    return {
        "ticket_title": meta.get("ticket_title", "") or "",
        "pr_url": meta.get("pr_url", "") or "",
        "review_verdict": meta.get("review_verdict", "") or "",
        "qa_result": meta.get("qa_result", "") or "",
        "pipeline_mode": meta.get("pipeline_mode", "") or "",
        "status": status,
        "started_at": started_at,
        "elapsed": elapsed,
    }


def _derive_current_phase_local(run_entries: list[dict[str, Any]]) -> str:
    """Duplicate of tracer._derive_current_phase walking only run_entries."""
    for e in reversed(run_entries):
        if e.get("source") == "agent":
            phase = str(e.get("phase", ""))
            if phase and phase != "ticket_read":
                return phase
    return ""


def _event_message(entry: dict[str, Any]) -> str:
    """Pull a human-friendly one-line message off a trace entry.

    Falls back through several field names the harness has used over
    time: ``message``, ``msg``, ``summary``, ``detail``.
    """
    for key in ("message", "msg", "summary", "detail"):
        val = entry.get(key)
        if val:
            return str(val)[:300]
    return ""


@router.get("/traces/{ticket_id}")
def get_trace_detail(ticket_id: str) -> dict[str, Any]:
    """Full trace timeline for one ticket.

    Reads the per-ticket JSONL via ``tracer.read_trace`` and reshapes
    into the design's 5-phase timeline + event stream.
    """
    entries = read_trace(ticket_id)
    if not entries:
        raise HTTPException(
            status_code=404,
            detail=f"trace not found: {ticket_id}",
        )
    return _shape_trace_detail(ticket_id, entries)


@router.get("/lessons/counts")
def get_lesson_counts() -> dict[str, Any]:
    """Counts of lesson candidates by state for the Home lesson-strip.

    Harness states are proposed / draft_ready / approved / applied /
    snoozed / rejected / reverted / stale. The design's strip shows 6
    cells mapped to the most operator-relevant transitions — we return
    the full 8 so the frontend can collapse display categories without
    a schema change here (draft_ready folds into "draft", reverted +
    stale are lifecycle states that rarely need surfacing).
    """
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        all_rows = list_lesson_candidates(conn, limit=10_000)
        counts = {
            "proposed": 0,
            "draft_ready": 0,
            "approved": 0,
            "applied": 0,
            "snoozed": 0,
            "rejected": 0,
            "reverted": 0,
            "stale": 0,
        }
        for r in all_rows:
            status = str(r["status"] or "").lower()
            if status in counts:
                counts[status] += 1
        return {"counts": counts}
    finally:
        conn.close()
