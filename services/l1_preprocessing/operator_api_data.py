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

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from auth import _require_dashboard_auth
from autonomy_metrics import (
    compute_daily_trend,
    compute_profile_metrics,
    compute_ticket_type_breakdown,
)
from autonomy_store import ensure_schema, open_connection
from autonomy_store.auto_merge import list_recent_auto_merge_decisions
from autonomy_store.defects import list_confirmed_escaped_defects
from autonomy_store.issues import (
    list_pr_commits,
    list_review_issues_by_pr_run,
)
from autonomy_store.lessons import list_lesson_candidates
from autonomy_store.pr_runs import list_pr_runs
from client_profile import list_profiles, load_profile
from live_stream import _find_session_streams, _worktree_root_for_ticket
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
    eligible = 0
    merged = 0
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
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


# --- Autonomy ------------------------------------------------------------


@router.get("/autonomy/{profile}")
def get_autonomy(profile: str, window_days: int = 30) -> dict[str, Any]:
    """Per-profile autonomy report.

    Bundles together every metric the Autonomy view renders:
      * headline numbers (FPA / escape / catch / auto-merge)
      * daily trends for each of the four metrics
      * by-ticket-type breakdown
      * recent escaped defects (30d)

    One endpoint per view, one fetch per page load. The trend arrays
    reuse compute_daily_trend; auto-merge was added to that function in
    the same commit as this endpoint.
    """
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        metrics = compute_profile_metrics(conn, profile, window_days=window_days)
        auto_merge = _compute_auto_merge_rate(conn, profile, window_days=window_days)
        by_type = compute_ticket_type_breakdown(
            conn, profile, window_days=window_days
        )
        trend_fpa = compute_daily_trend(conn, profile, window_days, "fpa")
        trend_escape = compute_daily_trend(
            conn, profile, window_days, "defect_escape"
        )
        trend_catch = compute_daily_trend(
            conn, profile, window_days, "catch_rate"
        )
        trend_auto = compute_daily_trend(
            conn, profile, window_days, "auto_merge"
        )

        # Recent escaped defects — list_confirmed_escaped_defects needs a
        # pr_run_ids list; pull all merged pr_runs in window and feed
        # them. Caller filters to the top-N most recent for display.
        pr_rows = list_pr_runs(conn, client_profile=profile)
        merged_ids = [int(r["id"]) for r in pr_rows if int(r["merged"])]
        escaped_rows = list_confirmed_escaped_defects(
            conn, merged_ids, window_days=30
        )
        escaped: list[dict[str, Any]] = []
        for r in escaped_rows[:20]:
            escaped.append(
                {
                    "id": f"D-{r['id']}",
                    "ticket_id": r["ticket_id"] or "",
                    "pr_number": int(r["pr_number"]) if r["pr_number"] else None,
                    "severity": str(r["severity"] or "").lower() or "minor",
                    "where": str(r["target_id"] or "")[:120],
                    "reported_at": r["reported_at"] or "",
                    "note": str(r["note"] or "")[:300],
                }
            )

        return {
            "profile": profile,
            "window_days": window_days,
            "metrics": {
                "fpa": metrics.get("first_pass_acceptance_rate"),
                "escape": metrics.get("defect_escape_rate"),
                "catch": metrics.get("self_review_catch_rate"),
                "auto_merge": auto_merge,
                "sample_size": metrics.get("sample_size", 0),
                "merged_count": metrics.get("merged_count", 0),
                "recommended_mode": metrics.get("recommended_mode", ""),
                "data_quality_status": metrics.get("data_quality_status", ""),
            },
            "trends": {
                "fpa": _shape_trend(trend_fpa),
                "escape": _shape_trend(trend_escape),
                "catch": _shape_trend(trend_catch),
                "auto_merge": _shape_trend(trend_auto),
            },
            "by_type": [
                {
                    "ticket_type": row["ticket_type"],
                    "volume": row["sample_size"],
                    "fpa": row["first_pass_acceptance_rate"],
                    "catch": row["self_review_catch_rate"],
                    "escape": row["defect_escape_rate"],
                    "merged": row["merged_count"],
                }
                for row in by_type
            ],
            "escaped": escaped,
        }
    finally:
        conn.close()


def _shape_trend(
    series: list[tuple[str, float | None, int]],
) -> list[dict[str, Any]]:
    """Turn compute_daily_trend tuples into JSON-friendly dicts."""
    return [
        {"date": d, "value": v, "sample": n} for (d, v, n) in series
    ]


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


# --- PR drilldown --------------------------------------------------------


def _severity_order(severity: str) -> int:
    """Sort severities in descending importance for the design's
    "most serious first" list."""
    s = severity.lower()
    return {"critical": 0, "blocker": 1, "major": 2, "minor": 3, "info": 4}.get(s, 5)


# --- Agent roster (per-ticket) ------------------------------------------


def _last_event_time(path: Path) -> datetime | None:
    """Tail the last JSONL line and pull its timestamp.

    Efficient enough for the scale we're at (single ticket's file, ~KB
    to low-MB). Returns None on any parse error — callers treat that
    as "no recent activity".
    """
    try:
        with path.open("rb") as fh:
            # Seek near the end and read the last ~4 KB; good enough for
            # a JSONL file where one line is under 2 KB.
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        last = json.loads(lines[-1])
    except (ValueError, TypeError):
        return None
    ts = (
        last.get("timestamp")
        or last.get("started_at")
        or last.get("t")
    )
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _roster_state(last: datetime | None) -> str:
    """Derive a human-friendly state from last-event-age.

    <= 60s since last event → running
    <= 5 min                → idle
    > 5 min or None         → stale
    """
    if last is None:
        return "stale"
    age = (datetime.now(UTC) - last).total_seconds()
    if age <= 60:
        return "running"
    if age <= 5 * 60:
        return "idle"
    return "stale"


@router.get("/tickets/{ticket_id}/agents")
def get_ticket_agents(ticket_id: str) -> dict[str, Any]:
    """Agent roster for one ticket.

    Walks the ticket's worktree via live_stream._find_session_streams and
    classifies each teammate by the freshness of their session-stream.jsonl
    tail. Returns:

      {
        "agents": [
          { "teammate", "state" (running|idle|stale), "last_at" (iso|None) },
          ...
        ]
      }

    When the ticket has no active worktree (pipeline not spawned, or
    already cleaned up), returns an empty list — the view can render
    "no agents spawned yet".
    """
    worktree = _worktree_root_for_ticket(ticket_id)
    if worktree is None:
        return {"agents": []}
    streams = _find_session_streams(worktree)
    agents: list[dict[str, Any]] = []
    for teammate, path in streams:
        last = _last_event_time(path)
        agents.append(
            {
                "teammate": teammate,
                "state": _roster_state(last),
                "last_at": last.isoformat() if last else None,
            }
        )
    return {"agents": agents}


@router.get("/pr/{pr_run_id}")
def get_pr_detail(pr_run_id: int) -> dict[str, Any]:
    """Full PR drilldown.

    Returns:
      {
        pr_run_id, ticket_id, pr_number, repo, pr_url, head_sha, branch,
        author, client_profile, opened_at, merged, merged_at,
        commits: [{ sha, message, author_name, authored_at }],
        checks: [],                                  # ← placeholder; see below
        issues: [{ id, source, severity, category, summary, where,
                   line_start, matched_lesson_id, matched_confidence }],
        matches: [{ lesson_id, confidence, applied }],
        auto_merge: {
          decision, reason, confidence, payload  — most recent
        } | null,
      }

    CI check list is not persisted today (plan-review gap #7). We
    return [] so the frontend renders a "CI checks not ingested"
    banner rather than fake rows.
    """
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        pr_row = conn.execute(
            "SELECT * FROM pr_runs WHERE id = ?", (pr_run_id,)
        ).fetchone()
        if pr_row is None:
            raise HTTPException(status_code=404, detail=f"pr_run {pr_run_id} not found")

        commits = list_pr_commits(conn, pr_run_id)
        issues = list_review_issues_by_pr_run(conn, pr_run_id)
        # Issue-to-issue matches inside this PR (ai_review ↔ human_review
        # pairings), used to render the "reviewer caught it" column.
        issue_match_rows = conn.execute(
            "SELECT im.* FROM issue_matches im "
            "JOIN review_issues ri ON ri.id = im.human_issue_id "
            "WHERE ri.pr_run_id = ?",
            (pr_run_id,),
        ).fetchall()
        # Lesson matches come from lesson_evidence rows keyed by pr_run_id —
        # each row ties a lesson_candidate to this PR with a source_ref
        # that describes the hit.
        lesson_match_rows = conn.execute(
            "SELECT le.lesson_id, le.source_ref, le.snippet, "
            "       lc.status as lesson_status "
            "FROM lesson_evidence le "
            "LEFT JOIN lesson_candidates lc "
            "       ON lc.lesson_id = le.lesson_id "
            "WHERE le.pr_run_id = ?",
            (pr_run_id,),
        ).fetchall()

        # Most recent auto-merge decision for this PR (latest row).
        am_rows = conn.execute(
            "SELECT * FROM manual_overrides "
            "WHERE override_type = 'auto_merge_decision' "
            "AND target_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (f"{pr_row['repo_full_name']}#{int(pr_row['pr_number'])}",),
        ).fetchall()
        auto_merge: dict[str, Any] | None
        if am_rows:
            try:
                payload = json.loads(am_rows[0]["payload_json"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            auto_merge = {
                "decision": str(payload.get("decision") or ""),
                "reason": str(payload.get("reason") or ""),
                "confidence": payload.get("confidence"),
                "created_at": am_rows[0]["created_at"],
                "gates": payload.get("gates") or {},
            }
        else:
            auto_merge = None

        match_by_issue: dict[int, dict[str, Any]] = {}
        for m in issue_match_rows:
            match_by_issue[int(m["human_issue_id"])] = {
                "ai_issue_id": int(m["ai_issue_id"]),
                "confidence": float(m["confidence"]),
                "matched_by": str(m["matched_by"] or ""),
            }

        issues_shaped = [
            {
                "id": int(issue["id"]),
                "source": str(issue["source"] or ""),
                "severity": str(issue["severity"] or "minor").lower(),
                "category": str(issue["category"] or ""),
                "summary": str(issue["summary"] or "")[:300],
                "where": str(issue["file_path"] or ""),
                "line_start": issue["line_start"],
                "matched": match_by_issue.get(int(issue["id"])),
            }
            for issue in issues
        ]
        issues_shaped.sort(
            key=lambda i: (_severity_order(i["severity"]), -int(i["id"]))
        )

        # Collapse multiple lesson_evidence rows for the same lesson into one
        # "lesson match" row — the design shows one per lesson, not per hit.
        seen_lessons: dict[str, dict[str, Any]] = {}
        for m in lesson_match_rows:
            lid = str(m["lesson_id"])
            if lid in seen_lessons:
                continue
            seen_lessons[lid] = {
                "lesson_id": lid,
                "status": str(m["lesson_status"] or ""),
                "applied": str(m["lesson_status"] or "") == "applied",
                "source_ref": str(m["source_ref"] or ""),
                "snippet": str(m["snippet"] or "")[:200],
            }
        matches_shaped = list(seen_lessons.values())

        return {
            "pr_run_id": pr_run_id,
            "ticket_id": pr_row["ticket_id"] or "",
            "pr_number": int(pr_row["pr_number"]),
            "repo_full_name": pr_row["repo_full_name"] or "",
            "pr_url": pr_row["pr_url"] or "",
            "head_sha": pr_row["head_sha"] or "",
            "client_profile": pr_row["client_profile"] or "",
            "opened_at": pr_row["opened_at"] or "",
            "merged": bool(int(pr_row["merged"] or 0)),
            "merged_at": pr_row["merged_at"] or "",
            "first_pass_accepted": bool(int(pr_row["first_pass_accepted"] or 0)),
            "commits": [
                {
                    "sha": c["commit_sha"],
                    "message": str(c["commit_message"] or "")[:200],
                    "author": c["commit_author"] or "",
                    "authored_at": c["authored_at"] or "",
                }
                for c in commits
            ],
            "issues": issues_shaped,
            "matches": matches_shaped,
            "auto_merge": auto_merge,
            "ci_checks_available": False,
        }
    finally:
        conn.close()


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
