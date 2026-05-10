"""SQLite persistence for operator automations.

The scheduler is intentionally small, but its state is not ephemeral:
operators need to see what is enabled, when it last ran, what failed,
and which runs/tickets were affected. Keeping config, run history, and
events in autonomy.db also avoids JSON-file races between scheduler ticks
and "Run now" actions from the dashboard.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str) -> dict[str, Any]:
    try:
        raw = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


@dataclass(frozen=True)
class AutomationDefault:
    job_key: str
    label: str
    description: str
    enabled: bool
    interval_seconds: int
    config: dict[str, Any]


DEFAULT_AUTOMATION_JOBS: tuple[AutomationDefault, ...] = (
    AutomationDefault(
        job_key="trace_reconciliation",
        label="Trace reconciliation",
        description=(
            "Reconciles PR lifecycle state from trace events and marks old "
            "active PR runs stale after a conservative threshold."
        ),
        enabled=True,
        interval_seconds=60 * 60,
        config={"stale_after_hours": 168, "dry_run": False},
    ),
    AutomationDefault(
        job_key="pipeline_watcher",
        label="Pipeline watcher",
        description=(
            "Looks for active traces with no recent progress and writes an "
            "operator-visible automation event plus a trace event."
        ),
        enabled=True,
        interval_seconds=5 * 60,
        config={
            "stale_after_minutes": 120,
            "event_cooldown_minutes": 60,
            "dry_run": False,
        },
    ),
    AutomationDefault(
        job_key="stale_worktree_cleanup",
        label="Stale worktree cleanup",
        description=(
            "Runs the existing worktree cleanup script for configured project "
            "repos. Dry run is on by default because it can remove directories."
        ),
        enabled=False,
        interval_seconds=12 * 60 * 60,
        config={"max_age_hours": 48, "dry_run": True},
    ),
    AutomationDefault(
        job_key="trace_archive_retention",
        label="Trace archive retention",
        description=(
            "Deletes old trace-archive ticket directories for configured "
            "project repos when dry run is disabled."
        ),
        enabled=False,
        interval_seconds=24 * 60 * 60,
        config={"retention_days": 90, "dry_run": True},
    ),
)

INTERVAL_OPTIONS: tuple[int, ...] = (
    5 * 60,
    10 * 60,
    15 * 60,
    30 * 60,
    60 * 60,
    6 * 60 * 60,
    12 * 60 * 60,
    24 * 60 * 60,
    7 * 24 * 60 * 60,
)


def default_job_keys() -> set[str]:
    return {job.job_key for job in DEFAULT_AUTOMATION_JOBS}


def ensure_default_jobs(conn: sqlite3.Connection) -> None:
    """Seed default jobs without overwriting operator edits."""
    now = _now_iso()
    with conn:
        for job in DEFAULT_AUTOMATION_JOBS:
            row = conn.execute(
                "SELECT job_key FROM automation_jobs WHERE job_key = ?",
                (job.job_key,),
            ).fetchone()
            if row is not None:
                continue
            next_run = now if job.enabled else ""
            conn.execute(
                """
                INSERT INTO automation_jobs (
                    job_key, label, description, enabled, interval_seconds,
                    scope, config_json, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_key,
                    job.label,
                    job.description,
                    int(job.enabled),
                    job.interval_seconds,
                    "all",
                    _json_dumps(job.config),
                    next_run,
                    now,
                    now,
                ),
            )


def _shape_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_key": str(row["job_key"]),
        "label": str(row["label"]),
        "description": str(row["description"] or ""),
        "enabled": bool(row["enabled"]),
        "interval_seconds": int(row["interval_seconds"]),
        "scope": str(row["scope"] or "all"),
        "config": _json_loads(str(row["config_json"] or "{}")),
        "next_run_at": str(row["next_run_at"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def _shape_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "job_key": str(row["job_key"]),
        "status": str(row["status"]),
        "triggered_by": str(row["triggered_by"] or ""),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "duration_ms": int(row["duration_ms"] or 0),
        "summary": str(row["summary"] or ""),
        "details": _json_loads(str(row["details_json"] or "{}")),
        "error": str(row["error"] or ""),
    }


def _shape_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "job_key": str(row["job_key"]),
        "run_id": int(row["run_id"]) if row["run_id"] is not None else None,
        "severity": str(row["severity"] or "info"),
        "target_type": str(row["target_type"] or ""),
        "target_id": str(row["target_id"] or ""),
        "message": str(row["message"] or ""),
        "payload": _json_loads(str(row["payload_json"] or "{}")),
        "created_at": str(row["created_at"] or ""),
    }


def list_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_default_jobs(conn)
    rows = conn.execute(
        "SELECT * FROM automation_jobs ORDER BY job_key"
    ).fetchall()
    jobs: list[dict[str, Any]] = []
    for row in rows:
        job = _shape_job(row)
        last = conn.execute(
            "SELECT * FROM automation_runs WHERE job_key = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (job["job_key"],),
        ).fetchone()
        job["last_run"] = _shape_run(last)
        jobs.append(job)
    return jobs


def get_job(conn: sqlite3.Connection, job_key: str) -> dict[str, Any] | None:
    ensure_default_jobs(conn)
    row = conn.execute(
        "SELECT * FROM automation_jobs WHERE job_key = ?",
        (job_key,),
    ).fetchone()
    return _shape_job(row) if row is not None else None


def list_due_jobs(conn: sqlite3.Connection, now_iso: str | None = None) -> list[dict[str, Any]]:
    ensure_default_jobs(conn)
    now = now_iso or _now_iso()
    rows = conn.execute(
        "SELECT * FROM automation_jobs "
        "WHERE enabled = 1 AND next_run_at != '' AND next_run_at <= ? "
        "ORDER BY next_run_at ASC",
        (now,),
    ).fetchall()
    return [_shape_job(row) for row in rows]


def update_job(
    conn: sqlite3.Connection,
    job_key: str,
    *,
    enabled: bool,
    interval_seconds: int,
    scope: str = "all",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if job_key not in default_job_keys():
        raise KeyError(job_key)
    if interval_seconds < 60:
        raise ValueError("interval_seconds must be at least 60")
    now = _now_iso()
    current = get_job(conn, job_key)
    if current is None:
        raise KeyError(job_key)
    merged_config = dict(current["config"])
    if config is not None:
        merged_config.update(config)
    next_run_at = current["next_run_at"]
    if enabled and not current["enabled"]:
        next_run_at = (datetime.now(UTC) + timedelta(seconds=interval_seconds)).isoformat()
    if not enabled:
        next_run_at = ""
    with conn:
        conn.execute(
            """
            UPDATE automation_jobs
            SET enabled = ?, interval_seconds = ?, scope = ?,
                config_json = ?, next_run_at = ?, updated_at = ?
            WHERE job_key = ?
            """,
            (
                int(enabled),
                int(interval_seconds),
                scope or "all",
                _json_dumps(merged_config),
                next_run_at,
                now,
                job_key,
            ),
        )
    updated = get_job(conn, job_key)
    if updated is None:
        raise KeyError(job_key)
    return updated


def schedule_next_run(conn: sqlite3.Connection, job_key: str, interval_seconds: int) -> None:
    next_run = (datetime.now(UTC) + timedelta(seconds=interval_seconds)).isoformat()
    with conn:
        conn.execute(
            "UPDATE automation_jobs SET next_run_at = ?, updated_at = ? "
            "WHERE job_key = ? AND enabled = 1",
            (next_run, _now_iso(), job_key),
        )


def start_run(
    conn: sqlite3.Connection,
    job_key: str,
    *,
    triggered_by: str,
) -> dict[str, Any]:
    now = _now_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO automation_runs (
                job_key, status, triggered_by, started_at
            ) VALUES (?, ?, ?, ?)
            """,
            (job_key, "running", triggered_by, now),
        )
    run = get_run(conn, int(cur.lastrowid or 0))
    if run is None:
        raise RuntimeError("automation run insert failed")
    return run


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    summary: str = "",
    details: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    finished = datetime.now(UTC)
    row = conn.execute(
        "SELECT started_at FROM automation_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    started = finished
    if row is not None:
        try:
            started = datetime.fromisoformat(str(row["started_at"]))
        except ValueError:
            started = finished
    duration_ms = max(0, int((finished - started).total_seconds() * 1000))
    with conn:
        conn.execute(
            """
            UPDATE automation_runs
            SET status = ?, finished_at = ?, duration_ms = ?, summary = ?,
                details_json = ?, error = ?
            WHERE id = ?
            """,
            (
                status,
                finished.isoformat(),
                duration_ms,
                summary[:500],
                _json_dumps(details or {}),
                error[:2000],
                run_id,
            ),
        )
    run = get_run(conn, run_id)
    if run is None:
        raise RuntimeError("automation run vanished")
    return run


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM automation_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    return _shape_run(row)


def list_runs(
    conn: sqlite3.Connection,
    *,
    job_key: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    sql = "SELECT * FROM automation_runs"
    if job_key:
        sql += " WHERE job_key = ?"
        params.append(job_key)
    sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(limit, 100)))
    runs: list[dict[str, Any]] = []
    for row in conn.execute(sql, params).fetchall():
        run = _shape_run(row)
        if run is not None:
            runs.append(run)
    return runs


def record_event(
    conn: sqlite3.Connection,
    *,
    job_key: str,
    run_id: int | None,
    severity: str,
    target_type: str = "",
    target_id: str = "",
    message: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO automation_events (
                job_key, run_id, severity, target_type, target_id,
                message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_key,
                run_id,
                severity,
                target_type,
                target_id,
                message[:500],
                _json_dumps(payload or {}),
                now,
            ),
        )
    row = conn.execute(
        "SELECT * FROM automation_events WHERE id = ?",
        (int(cur.lastrowid or 0),),
    ).fetchone()
    return _shape_event(row)


def list_events(
    conn: sqlite3.Connection,
    *,
    job_key: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    sql = "SELECT * FROM automation_events"
    if job_key:
        sql += " WHERE job_key = ?"
        params.append(job_key)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    return [_shape_event(row) for row in conn.execute(sql, params).fetchall()]


def recent_event_exists(
    conn: sqlite3.Connection,
    *,
    job_key: str,
    target_type: str,
    target_id: str,
    since_iso: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM automation_events "
        "WHERE job_key = ? AND target_type = ? AND target_id = ? "
        "AND created_at >= ? LIMIT 1",
        (job_key, target_type, target_id, since_iso),
    ).fetchone()
    return row is not None
