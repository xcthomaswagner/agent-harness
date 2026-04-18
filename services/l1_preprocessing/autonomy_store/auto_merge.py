"""Auto-merge decision + kill-switch toggle helpers (Phase 4).

Extracted from ``autonomy_store.py``. Decisions are logged via
``manual_overrides`` rows; the per-profile toggle is the same
``manual_overrides`` stream keyed on override_type='auto_merge_toggle'.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .defects import insert_manual_override


def record_auto_merge_decision(
    conn: sqlite3.Connection,
    *,
    repo_full_name: str,
    pr_number: int,
    decision: str,
    reason: str,
    payload: dict[str, Any],
    created_by: str = "l3_auto_merge",
) -> int:
    """Log an auto-merge decision via manual_overrides.

    override_type='auto_merge_decision', target_id='{repo}#{pr_number}'.
    payload_json merges {decision, reason} with the caller-supplied payload.
    """
    merged = {"decision": decision, "reason": reason, **payload}
    return insert_manual_override(
        conn,
        override_type="auto_merge_decision",
        target_id=f"{repo_full_name}#{pr_number}",
        payload_json=json.dumps(merged),
        created_by=created_by,
    )


def get_auto_merge_toggle(
    conn: sqlite3.Connection, client_profile: str
) -> bool | None:
    """Read the latest per-profile auto-merge runtime toggle.

    Returns None if no toggle has ever been set (caller falls back to YAML).
    """
    row = conn.execute(
        "SELECT payload_json FROM manual_overrides "
        "WHERE override_type = 'auto_merge_toggle' AND target_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (client_profile,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return bool(payload.get("enabled"))


def set_auto_merge_toggle(
    conn: sqlite3.Connection,
    *,
    client_profile: str,
    enabled: bool,
    created_by: str = "admin",
) -> int:
    """Insert a new toggle row for `client_profile`. Latest wins."""
    return insert_manual_override(
        conn,
        override_type="auto_merge_toggle",
        target_id=client_profile,
        payload_json=json.dumps({"enabled": bool(enabled)}),
        created_by=created_by,
    )


def _escape_like(value: str) -> str:
    """Escape SQLite ``LIKE`` wildcards in user-supplied values.

    Callers must pair the returned value with ``LIKE ? ESCAPE '\\'``
    (see ``list_recent_auto_merge_decisions``). Without escaping,
    ``_`` matches any single character and ``%`` matches any sequence,
    so user input like ``foo_bar`` would match ``fooXbar`` and a
    single ``_`` would match every row — turning a per-profile filter
    into a data leak across profiles.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_recent_auto_merge_decisions(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    since_iso: str | None = None,
    repo_full_name: str | None = None,
    client_profile: str | None = None,
) -> list[sqlite3.Row]:
    """Return recent auto-merge decision rows from manual_overrides.

    Filters:
      * since_iso — created_at >= value
      * repo_full_name — target_id starts with "{repo}#"
      * client_profile — payload JSON contains this profile name

    User-supplied values in ``repo_full_name`` and ``client_profile``
    are LIKE-escaped before being spliced into the pattern so
    underscores and percent signs in legitimate profile/repo names
    (``first_pass_acceptance``, ``my_repo``) match literally instead
    of acting as wildcards. Uses ``LIKE ? ESCAPE '\\'`` to tell SQLite
    how to interpret the backslash escape.

    Ordered by id DESC, capped at `limit`.
    """
    clauses = ["override_type = 'auto_merge_decision'"]
    params: list[Any] = []
    if since_iso:
        clauses.append("created_at >= ?")
        params.append(since_iso)
    if repo_full_name:
        clauses.append("target_id LIKE ? ESCAPE '\\'")
        params.append(f"{_escape_like(repo_full_name)}#%")
    if client_profile:
        # Crude substring match on serialized JSON — sufficient for dashboard.
        clauses.append("payload_json LIKE ? ESCAPE '\\'")
        params.append(
            f'%"client_profile": "{_escape_like(client_profile)}"%'
        )
    sql = (
        "SELECT * FROM manual_overrides WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT ?"
    )
    params.append(limit)
    return list(conn.execute(sql, params).fetchall())
