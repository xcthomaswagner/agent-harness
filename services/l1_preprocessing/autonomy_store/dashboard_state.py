"""Dashboard lifecycle suppression helpers.

The trace store is append-only JSONL, while operator decisions need a
queryable index so profile cards and lists can hide known-bad data without
deleting the forensic trace. This module owns that small index.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from .schema import _now_iso

VALID_SUPPRESSION_TARGETS = frozenset({"trace", "ticket", "pr_run"})


def suppress_dashboard_target(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    reason: str = "",
    created_by: str = "operator",
    payload: dict[str, Any] | None = None,
    expires_at: str = "",
) -> int:
    """Mark a trace/ticket/pr_run hidden from default operator views."""
    if target_type not in VALID_SUPPRESSION_TARGETS:
        raise ValueError(f"unsupported suppression target_type: {target_type}")
    now = _now_iso()
    payload_json = json.dumps(payload or {}, sort_keys=True)
    with conn:
        conn.execute(
            "UPDATE dashboard_suppressions SET active = 0, cleared_at = ?, "
            "cleared_by = ?, clear_reason = ? "
            "WHERE target_type = ? AND target_id = ? AND active = 1",
            (now, created_by, "superseded", target_type, target_id),
        )
        cur = conn.execute(
            """
            INSERT INTO dashboard_suppressions (
                target_type, target_id, reason, payload_json, active,
                expires_at, created_at, created_by
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                target_type,
                target_id,
                reason,
                payload_json,
                expires_at,
                now,
                created_by,
            ),
        )
    return int(cur.lastrowid or 0)


def clear_dashboard_suppression(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    reason: str = "",
    cleared_by: str = "operator",
) -> int:
    """Clear active suppression rows for a target; returns affected count."""
    if target_type not in VALID_SUPPRESSION_TARGETS:
        raise ValueError(f"unsupported suppression target_type: {target_type}")
    now = _now_iso()
    with conn:
        cur = conn.execute(
            "UPDATE dashboard_suppressions SET active = 0, cleared_at = ?, "
            "cleared_by = ?, clear_reason = ? "
            "WHERE target_type = ? AND target_id = ? AND active = 1",
            (now, cleared_by, reason, target_type, target_id),
        )
    return int(cur.rowcount or 0)


def list_active_suppressions(
    conn: sqlite3.Connection,
    *,
    target_type: str,
) -> dict[str, sqlite3.Row]:
    """Return active, unexpired suppressions keyed by target_id."""
    if target_type not in VALID_SUPPRESSION_TARGETS:
        raise ValueError(f"unsupported suppression target_type: {target_type}")
    now = _now_iso()
    rows = conn.execute(
        "SELECT * FROM dashboard_suppressions "
        "WHERE target_type = ? AND active = 1 "
        "AND (expires_at = '' OR expires_at > ?) "
        "ORDER BY id DESC",
        (target_type, now),
    ).fetchall()
    out: dict[str, sqlite3.Row] = {}
    for row in rows:
        target_id = str(row["target_id"] or "")
        if target_id and target_id not in out:
            out[target_id] = row
    return out


def get_active_suppression(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
) -> sqlite3.Row | None:
    """Return the latest active suppression for one target, if any."""
    if target_type not in VALID_SUPPRESSION_TARGETS:
        raise ValueError(f"unsupported suppression target_type: {target_type}")
    now = _now_iso()
    return cast(
        sqlite3.Row | None,
        conn.execute(
            "SELECT * FROM dashboard_suppressions "
            "WHERE target_type = ? AND target_id = ? AND active = 1 "
            "AND (expires_at = '' OR expires_at > ?) "
            "ORDER BY id DESC LIMIT 1",
            (target_type, target_id, now),
        ).fetchone(),
    )
