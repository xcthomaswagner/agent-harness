"""PR-run CRUD helpers.

Extracted from ``autonomy_store.py``. Owns ``PrRunUpsert`` + the four
pr_runs query helpers + ``list_client_profiles``. Imports ``_now_iso``
from ``schema`` to avoid re-defining it.
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel

from .schema import _now_iso


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
)
_INT_FIELDS = ("first_pass_accepted", "merged", "escalated")


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
        with conn:
            cur = conn.execute(
                """
                INSERT INTO pr_runs (
                    ticket_id, pr_number, repo_full_name, pr_url, ticket_type,
                    pipeline_mode, head_sha, base_sha, client_profile,
                    opened_at, approved_at, merged_at, closed_at,
                    first_pass_accepted, merged, escalated, backfilled,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    # Update path: patch-style
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
    sql = "SELECT * FROM pr_runs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY opened_at DESC, id DESC"
    return list(conn.execute(sql, params).fetchall())


def list_client_profiles(conn: sqlite3.Connection) -> list[str]:
    """Return distinct non-empty client_profile values from pr_runs."""
    rows = conn.execute(
        "SELECT DISTINCT client_profile FROM pr_runs "
        "WHERE client_profile != '' ORDER BY client_profile"
    ).fetchall()
    return [r["client_profile"] for r in rows]
