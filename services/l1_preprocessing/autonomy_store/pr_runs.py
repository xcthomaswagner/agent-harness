"""PR-run CRUD helpers.

Extracted from ``autonomy_store.py``. Owns ``PrRunUpsert`` + the four
pr_runs query helpers + ``list_client_profiles``. Imports ``_now_iso``
from ``schema`` to avoid re-defining it.
"""

from __future__ import annotations

import json
import sqlite3

from pydantic import BaseModel

from .schema import _now_iso

ACTIVE_PR_RUN_STATES = frozenset({
    "open",
    "reviewed",
    "needs_changes",
    "awaiting_merge",
})
TERMINAL_PR_RUN_STATES = frozenset({
    "merged",
    "closed",
    "escalated",
    "suppressed",
    "misfire",
})
VALID_PR_RUN_STATES = ACTIVE_PR_RUN_STATES | TERMINAL_PR_RUN_STATES | frozenset({
    "stale",
})


class PrRunUpsert(BaseModel):
    """Patch-style upsert payload for pr_runs.

    On update, only non-None fields are applied, and text fields are only
    overwritten when non-empty. Integer boolean flags (first_pass_accepted,
    merged, escalated) use None to indicate "do not touch".
    """

    ticket_id: str
    pr_number: int
    repo_full_name: str
    pr_url: str = ""
    ticket_type: str = ""
    pipeline_mode: str = ""
    head_sha: str
    base_sha: str = ""
    client_profile: str = ""
    opened_at: str = ""
    approved_at: str | None = None
    merged_at: str | None = None
    closed_at: str | None = None
    first_pass_accepted: int | None = None
    merged: int | None = None
    escalated: int | None = None
    backfilled: int = 0
    state: str | None = None
    state_reason: str | None = None
    terminal_at: str | None = None
    suppressed_at: str | None = None
    suppressed_reason: str | None = None
    excluded_from_metrics: int | None = None
    last_observed_at: str | None = None
    run_id: str | None = None


_TEXT_FIELDS = (
    "ticket_id",
    "pr_url",
    "ticket_type",
    "pipeline_mode",
    "base_sha",
    "client_profile",
    "opened_at",
    "approved_at",
    "merged_at",
    "closed_at",
    "state",
    "state_reason",
    "terminal_at",
    "suppressed_at",
    "suppressed_reason",
    "last_observed_at",
    "run_id",
)
_INT_FIELDS = (
    "first_pass_accepted",
    "merged",
    "escalated",
    "excluded_from_metrics",
)


def _inferred_state(row: PrRunUpsert) -> str:
    if row.state:
        return row.state
    if row.merged == 1:
        return "merged"
    if row.closed_at:
        return "closed"
    if row.escalated == 1:
        return "escalated"
    return "open"


def _inferred_terminal_at(row: PrRunUpsert) -> str:
    if row.terminal_at:
        return row.terminal_at
    if row.merged == 1 and row.merged_at:
        return row.merged_at
    if row.closed_at:
        return row.closed_at
    return ""


def upsert_pr_run(conn: sqlite3.Connection, row: PrRunUpsert) -> int:
    """Insert or patch-update a pr_runs row keyed on (repo, pr_number, head_sha).

    Returns the pr_run id. On update, text fields are only overwritten when
    the new value is non-empty; nullable integer flags are only overwritten
    when not None. `backfilled` and `pr_number`/`ticket_id` always get written
    on insert. `updated_at` is bumped on every write.
    """
    now = _now_iso()
    existing = get_pr_run_by_unique(
        conn, row.repo_full_name, row.pr_number, row.head_sha
    )

    if existing is None:
        state = _inferred_state(row)
        terminal_at = _inferred_terminal_at(row)
        with conn:
            cur = conn.execute(
                """
                INSERT INTO pr_runs (
                    ticket_id, pr_number, repo_full_name, pr_url, ticket_type,
                    pipeline_mode, head_sha, base_sha, client_profile,
                    opened_at, approved_at, merged_at, closed_at,
                    first_pass_accepted, merged, escalated, backfilled,
                    state, state_reason, terminal_at, suppressed_at,
                    suppressed_reason, excluded_from_metrics, last_observed_at,
                    run_id,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.ticket_id,
                    row.pr_number,
                    row.repo_full_name,
                    row.pr_url,
                    row.ticket_type,
                    row.pipeline_mode,
                    row.head_sha,
                    row.base_sha,
                    row.client_profile,
                    row.opened_at,
                    row.approved_at or "",
                    row.merged_at or "",
                    row.closed_at or "",
                    row.first_pass_accepted if row.first_pass_accepted is not None else 0,
                    row.merged if row.merged is not None else 0,
                    row.escalated if row.escalated is not None else 0,
                    row.backfilled,
                    state,
                    row.state_reason or "",
                    terminal_at,
                    row.suppressed_at or "",
                    row.suppressed_reason or "",
                    (
                        row.excluded_from_metrics
                        if row.excluded_from_metrics is not None
                        else 0
                    ),
                    row.last_observed_at or row.opened_at or "",
                    row.run_id or "",
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    # Update path: patch-style
    if row.state is None:
        if row.merged == 1:
            row.state = "merged"
            row.terminal_at = row.terminal_at or row.merged_at
        elif row.closed_at:
            row.state = "closed"
            row.terminal_at = row.terminal_at or row.closed_at
        elif row.escalated == 1:
            row.state = "escalated"
    sets: list[str] = []
    params: list[object] = []

    for field in _TEXT_FIELDS:
        value = getattr(row, field)
        # Nullable text fields may be None
        if value is None:
            continue
        if value == "":
            continue
        sets.append(f"{field} = ?")
        params.append(value)

    for field in _INT_FIELDS:
        value = getattr(row, field)
        if value is None:
            continue
        sets.append(f"{field} = ?")
        params.append(int(value))

    # backfilled is always defined (int); only update if caller set it to 1
    if row.backfilled:
        sets.append("backfilled = ?")
        params.append(row.backfilled)

    sets.append("updated_at = ?")
    params.append(now)
    params.append(existing["id"])

    with conn:
        conn.execute(
            f"UPDATE pr_runs SET {', '.join(sets)} WHERE id = ?",
            params,
        )
    return int(existing["id"])


def get_pr_run_by_unique(
    conn: sqlite3.Connection,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
) -> sqlite3.Row | None:
    """Fetch a pr_runs row by its unique key, or None if not found."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM pr_runs WHERE repo_full_name = ? AND pr_number = ? "
        "AND head_sha = ?",
        (repo_full_name, pr_number, head_sha),
    ).fetchone()
    return row


def find_latest_merged_pr_run_by_ticket(
    conn: sqlite3.Connection, ticket_id: str
) -> sqlite3.Row | None:
    """Return the most recently merged pr_run for this ticket, or None."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM pr_runs WHERE ticket_id = ? AND merged = 1 "
        "ORDER BY datetime(merged_at) DESC, id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    return row


def list_pr_runs(
    conn: sqlite3.Connection,
    *,
    client_profile: str | None = None,
    since_iso: str | None = None,
    until_iso: str | None = None,
    include_excluded: bool = False,
) -> list[sqlite3.Row]:
    """List pr_runs rows with optional client_profile + opened_at filters.

    ``since_iso`` / ``until_iso`` are inclusive/exclusive respectively —
    matching the half-open window convention the outcomes job uses
    to avoid double-counting PRs that open at midnight between
    pre/post windows.
    """
    clauses: list[str] = []
    params: list[object] = []
    if client_profile is not None:
        clauses.append("client_profile = ?")
        params.append(client_profile)
    if since_iso is not None:
        clauses.append("opened_at >= ?")
        params.append(since_iso)
    if until_iso is not None:
        clauses.append("opened_at < ?")
        params.append(until_iso)
    if not include_excluded:
        clauses.append("excluded_from_metrics = 0")
        clauses.append("state NOT IN ('suppressed', 'misfire')")
    sql = "SELECT * FROM pr_runs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY opened_at DESC, id DESC"
    return list(conn.execute(sql, params).fetchall())


def list_client_profiles(conn: sqlite3.Connection) -> list[str]:
    """Return distinct non-empty client_profile values from pr_runs."""
    rows = conn.execute(
        "SELECT DISTINCT client_profile FROM pr_runs "
        "WHERE client_profile != '' "
        "AND excluded_from_metrics = 0 "
        "AND state NOT IN ('suppressed', 'misfire') "
        "ORDER BY client_profile"
    ).fetchall()
    return [r["client_profile"] for r in rows]


def set_pr_run_lifecycle_state(
    conn: sqlite3.Connection,
    pr_run_id: int,
    *,
    state: str,
    reason: str = "",
    created_by: str = "operator",
    exclude_metrics: bool | None = None,
    terminal_at: str | None = None,
) -> bool:
    """Set explicit lifecycle state for one pr_run and audit the operator action."""
    if state not in VALID_PR_RUN_STATES:
        raise ValueError(f"unsupported pr_run state: {state}")
    row = conn.execute("SELECT * FROM pr_runs WHERE id = ?", (pr_run_id,)).fetchone()
    if row is None:
        return False

    now = _now_iso()
    effective_terminal_at = terminal_at or (
        now if state in TERMINAL_PR_RUN_STATES or state == "stale" else ""
    )
    excluded = int(exclude_metrics) if exclude_metrics is not None else None
    suppressed_at = now if state in ("suppressed", "misfire") else None
    suppressed_reason = reason if state in ("suppressed", "misfire") else None

    sets = [
        "state = ?",
        "state_reason = ?",
        "last_observed_at = ?",
        "updated_at = ?",
    ]
    params: list[object] = [state, reason, now, now]
    if effective_terminal_at:
        sets.append("terminal_at = ?")
        params.append(effective_terminal_at)
    if state == "merged":
        sets.extend([
            "merged = 1",
            "merged_at = CASE WHEN merged_at = '' THEN ? ELSE merged_at END",
        ])
        params.append(effective_terminal_at or now)
    if state == "closed":
        sets.append("closed_at = CASE WHEN closed_at = '' THEN ? ELSE closed_at END")
        params.append(effective_terminal_at or now)
    if suppressed_at is not None:
        sets.append("suppressed_at = ?")
        params.append(suppressed_at)
    if suppressed_reason is not None:
        sets.append("suppressed_reason = ?")
        params.append(suppressed_reason)
    if state in ACTIVE_PR_RUN_STATES:
        sets.extend([
            "suppressed_at = ''",
            "suppressed_reason = ''",
            "terminal_at = ''",
        ])
    if excluded is not None:
        sets.append("excluded_from_metrics = ?")
        params.append(excluded)

    params.append(pr_run_id)
    with conn:
        conn.execute(f"UPDATE pr_runs SET {', '.join(sets)} WHERE id = ?", params)
        conn.execute(
            """
            INSERT INTO manual_overrides (
                override_type, target_id, payload_json, created_at, created_by
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "pr_run_state",
                str(pr_run_id),
                json.dumps(
                    {
                        "state": state,
                        "reason": reason,
                        "exclude_metrics": exclude_metrics,
                    },
                    sort_keys=True,
                ),
                now,
                created_by,
            ),
        )
    return True


def set_pr_runs_lifecycle_state_for_ticket(
    conn: sqlite3.Connection,
    ticket_id: str,
    *,
    state: str,
    reason: str = "",
    created_by: str = "operator",
    exclude_metrics: bool | None = None,
    only_active: bool = True,
    source_states: tuple[str, ...] | None = None,
) -> int:
    """Apply a lifecycle state to pr_runs for a ticket; returns affected rows."""
    if state not in VALID_PR_RUN_STATES:
        raise ValueError(f"unsupported pr_run state: {state}")
    clauses = ["ticket_id = ?"]
    params: list[object] = [ticket_id]
    if source_states:
        placeholders = ",".join("?" * len(source_states))
        clauses.append(f"state IN ({placeholders})")
        params.extend(source_states)
    elif only_active:
        placeholders = ",".join("?" * len(ACTIVE_PR_RUN_STATES))
        clauses.append(f"state IN ({placeholders})")
        params.extend(sorted(ACTIVE_PR_RUN_STATES))
    rows = conn.execute(
        "SELECT id FROM pr_runs WHERE " + " AND ".join(clauses),
        params,
    ).fetchall()
    count = 0
    for row in rows:
        if set_pr_run_lifecycle_state(
            conn,
            int(row["id"]),
            state=state,
            reason=reason,
            created_by=created_by,
            exclude_metrics=exclude_metrics,
        ):
            count += 1
    return count


def mark_stale_pr_runs(
    conn: sqlite3.Connection,
    *,
    older_than_iso: str,
    reason: str,
    created_by: str = "system",
    dry_run: bool = False,
) -> int:
    """Mark active unmerged PR runs stale when no observation landed after cutoff."""
    placeholders = ",".join("?" * len(ACTIVE_PR_RUN_STATES))
    params: list[object] = [*sorted(ACTIVE_PR_RUN_STATES), older_than_iso, older_than_iso]
    rows = conn.execute(
        "SELECT id FROM pr_runs "
        f"WHERE state IN ({placeholders}) "
        "AND merged = 0 AND closed_at = '' AND suppressed_at = '' "
        "AND COALESCE(NULLIF(last_observed_at, ''), opened_at) < ? "
        "AND opened_at < ?",
        params,
    ).fetchall()
    if dry_run:
        return len(rows)
    count = 0
    for row in rows:
        if set_pr_run_lifecycle_state(
            conn,
            int(row["id"]),
            state="stale",
            reason=reason,
            created_by=created_by,
            exclude_metrics=False,
        ):
            count += 1
    return count
