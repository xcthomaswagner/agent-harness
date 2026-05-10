"""Dashboard lifecycle reconciliation helpers.

Shared by the operator API's manual "reconcile stale" action and the
automation scheduler. Keeping the state repair logic here prevents the
scheduler from importing the dashboard router module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from autonomy_store import ensure_schema, open_connection
from autonomy_store.pr_runs import (
    ACTIVE_PR_RUN_STATES,
    mark_stale_pr_runs,
    set_pr_run_lifecycle_state,
)
from tracer import read_trace


@dataclass(frozen=True)
class TraceLifecycleDecision:
    state: str
    event: str
    event_at: str
    pr_run_id: int


def _entry_pr_matches(entry: dict[str, Any], pr_number: int) -> bool:
    raw = entry.get("pr_number")
    if raw in (None, ""):
        return True
    try:
        return int(str(raw)) == pr_number
    except (TypeError, ValueError):
        return False


def _latest_trace_lifecycle_decision(
    *,
    pr_run_id: int,
    ticket_id: str,
    pr_number: int,
    current_state: str,
) -> TraceLifecycleDecision | None:
    entries = read_trace(ticket_id)
    candidates: list[tuple[int, str, str]] = []
    for index, entry in enumerate(entries):
        if not _entry_pr_matches(entry, pr_number):
            continue
        event = str(entry.get("event") or "")
        event_at = str(entry.get("timestamp") or "")
        if event == "pr_merged":
            candidates.append((index, "merged", event_at))
        elif event == "pr_closed":
            candidates.append((index, "closed", event_at))
        elif current_state != "stale" and event == "review_approved":
            candidates.append((index, "reviewed", event_at))
        elif current_state != "stale" and event in (
            "review_changes_requested",
            "changes_requested_spawned",
        ):
            candidates.append((index, "needs_changes", event_at))
    if not candidates:
        return None

    terminal = [c for c in candidates if c[1] in ("merged", "closed")]
    index, state, event_at = max(terminal or candidates, key=lambda c: c[0])
    event = str(entries[index].get("event") or "")
    return TraceLifecycleDecision(
        state=state,
        event=event,
        event_at=event_at,
        pr_run_id=pr_run_id,
    )


def _reconcile_pr_run_lifecycle_from_traces(
    conn: Any,
    *,
    dry_run: bool,
    created_by: str,
) -> tuple[int, set[int]]:
    rows = conn.execute(
        "SELECT id, ticket_id, pr_number, state FROM pr_runs "
        "WHERE ticket_id != '' "
        "AND state NOT IN ('merged', 'closed', 'suppressed', 'misfire')"
    ).fetchall()
    count = 0
    terminal_ids: set[int] = set()
    for row in rows:
        current_state = str(row["state"] or "open")
        decision = _latest_trace_lifecycle_decision(
            pr_run_id=int(row["id"]),
            ticket_id=str(row["ticket_id"]),
            pr_number=int(row["pr_number"]),
            current_state=current_state,
        )
        if decision is None or decision.state == current_state:
            continue
        if decision.state in ("merged", "closed"):
            terminal_ids.add(decision.pr_run_id)
        count += 1
        if dry_run:
            continue
        set_pr_run_lifecycle_state(
            conn,
            decision.pr_run_id,
            state=decision.state,
            reason=f"reconciled from trace event {decision.event}",
            created_by=created_by,
            exclude_metrics=False,
            terminal_at=(
                decision.event_at
                if decision.state in ("merged", "closed")
                else None
            ),
        )
    return count, terminal_ids


def _count_stale_pr_runs_after_trace_reconcile(
    conn: Any,
    *,
    older_than_iso: str,
    exclude_ids: set[int],
) -> int:
    placeholders = ",".join("?" * len(ACTIVE_PR_RUN_STATES))
    params: list[object] = [
        *sorted(ACTIVE_PR_RUN_STATES),
        older_than_iso,
        older_than_iso,
    ]
    exclude_clause = ""
    if exclude_ids:
        exclude_placeholders = ",".join("?" * len(exclude_ids))
        exclude_clause = f"AND id NOT IN ({exclude_placeholders}) "
        params.extend(sorted(exclude_ids))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pr_runs "
        f"WHERE state IN ({placeholders}) "
        "AND merged = 0 AND closed_at = '' AND suppressed_at = '' "
        "AND COALESCE(NULLIF(last_observed_at, ''), opened_at) < ? "
        "AND opened_at < ? "
        f"{exclude_clause}",
        params,
    ).fetchone()
    return int(row["n"] if row else 0)


def reconcile_stale_runs(
    db_path: Path,
    *,
    stale_after_hours: int,
    dry_run: bool,
    created_by: str,
) -> dict[str, Any]:
    """Backfill trace lifecycle, then mark old active pr_runs stale."""
    cutoff = (datetime.now(UTC) - timedelta(hours=stale_after_hours)).isoformat()
    reason = f"no lifecycle observation since {cutoff}"
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        lifecycle_count, terminal_ids = _reconcile_pr_run_lifecycle_from_traces(
            conn,
            dry_run=dry_run,
            created_by=created_by,
        )
        if dry_run:
            stale_count = _count_stale_pr_runs_after_trace_reconcile(
                conn,
                older_than_iso=cutoff,
                exclude_ids=terminal_ids,
            )
        else:
            stale_count = mark_stale_pr_runs(
                conn,
                older_than_iso=cutoff,
                reason=reason,
                created_by=created_by,
                dry_run=False,
            )
    finally:
        conn.close()
    return {
        "status": "dry_run" if dry_run else "accepted",
        "stale_after_hours": stale_after_hours,
        "lifecycle_reconciled": lifecycle_count,
        "matched": stale_count,
    }
