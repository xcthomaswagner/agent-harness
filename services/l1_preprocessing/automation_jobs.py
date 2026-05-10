"""Operator automation job implementations."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from automation_store import recent_event_exists, record_event
from autonomy_store import ensure_schema, open_connection
from client_profile import list_profiles, load_profile
from dashboard_reconciliation import reconcile_stale_runs
from tracer import append_trace
from tracer import list_traces as _list_traces

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLEANUP_SCRIPT = _REPO_ROOT / "scripts" / "cleanup_stale_worktrees.py"

_ACTIVE_TRACE_STATUSES = frozenset({
    "Received",
    "Enriched",
    "Processing",
    "Dispatched",
    "Planned",
    "Implementing",
    "Reviewing",
    "QA Running",
    "CI Fix",
})
_TERMINAL_TRACE_EVENTS = frozenset({
    "Pipeline complete",
    "agent_finished",
    "pr_merged",
    "pr_closed",
    "trace_suppressed",
    "trace_marked_misfire",
})
_TRACE_PROGRESS_EVENTS = frozenset({
    "Pipeline started",
    "processing_started",
    "processing_completed",
    "l2_dispatched",
    "agent_finished",
    "Pipeline complete",
})


def _bool_config(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    return bool(value)


def _int_config(
    config: dict[str, Any],
    key: str,
    default: int,
    *,
    min_value: int,
    max_value: int,
) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _record_event(
    db_path: Path,
    *,
    job_key: str,
    run_id: int,
    severity: str,
    message: str,
    target_type: str = "",
    target_id: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        record_event(
            conn,
            job_key=job_key,
            run_id=run_id,
            severity=severity,
            target_type=target_type,
            target_id=target_id,
            message=message,
            payload=payload or {},
        )
    finally:
        conn.close()


def _profile_names_for_scope(scope: str) -> list[str]:
    names = list_profiles()
    if not scope or scope == "all":
        return names
    return [name for name in names if name == scope]


def _profile_repo_paths(scope: str) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for name in _profile_names_for_scope(scope):
        profile = load_profile(name)
        if profile is None:
            continue
        path = Path(profile.client_repo_path).expanduser()
        if str(path):
            out.append((name, path))
    return out


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _last_progress_at(trace: dict[str, Any]) -> datetime | None:
    entries = trace.get("_raw_entries")
    run_start_idx = int(trace.get("_run_start_idx") or 0)
    if not isinstance(entries, list):
        return _parse_iso(str(trace.get("completed_at") or ""))
    for entry in reversed(entries[run_start_idx:]):
        event = str(entry.get("event") or "")
        phase = str(entry.get("phase") or "")
        if (
            entry.get("source") == "agent"
            or event in _TRACE_PROGRESS_EVENTS
            or phase in ("pipeline", "l2_dispatch")
        ):
            return _parse_iso(str(entry.get("timestamp") or ""))
    return _parse_iso(str(trace.get("completed_at") or ""))


def _terminal_event_seen(trace: dict[str, Any]) -> bool:
    entries = trace.get("_raw_entries")
    if not isinstance(entries, list):
        return False
    return any(str(entry.get("event") or "") in _TERMINAL_TRACE_EVENTS for entry in entries)


def _run_trace_reconciliation(
    db_path: Path,
    *,
    job_key: str,
    run_id: int,
    config: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    stale_after_hours = _int_config(
        config,
        "stale_after_hours",
        168,
        min_value=1,
        max_value=24 * 90,
    )
    dry_run = _bool_config(config, "dry_run", False)
    details = reconcile_stale_runs(
        db_path,
        stale_after_hours=stale_after_hours,
        dry_run=dry_run,
        created_by="automation",
    )
    changed = int(details["lifecycle_reconciled"]) + int(details["matched"])
    if changed:
        _record_event(
            db_path,
            job_key=job_key,
            run_id=run_id,
            severity="warning" if details["matched"] else "info",
            message=(
                f"Reconciled {details['lifecycle_reconciled']} lifecycle state(s) "
                f"and found {details['matched']} stale PR run(s)."
            ),
            payload=details,
        )
    suffix = "dry run" if dry_run else "applied"
    return (
        f"{details['lifecycle_reconciled']} reconciled, {details['matched']} stale ({suffix})",
        details,
    )


def _run_pipeline_watcher(
    db_path: Path,
    *,
    job_key: str,
    run_id: int,
    config: dict[str, Any],
    scope: str,
) -> tuple[str, dict[str, Any]]:
    stale_after_minutes = _int_config(
        config,
        "stale_after_minutes",
        120,
        min_value=5,
        max_value=60 * 24 * 14,
    )
    cooldown_minutes = _int_config(
        config,
        "event_cooldown_minutes",
        60,
        min_value=5,
        max_value=60 * 24,
    )
    dry_run = _bool_config(config, "dry_run", False)
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_after_minutes)
    event_cutoff = datetime.now(UTC) - timedelta(minutes=cooldown_minutes)
    scope_profiles = set(_profile_names_for_scope(scope)) if scope != "all" else set()
    emitted = 0
    stale: list[dict[str, Any]] = []

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        for trace in _list_traces(offset=0, limit=0):
            ticket_id = str(trace.get("ticket_id") or "")
            if not ticket_id:
                continue
            profile = str(trace.get("client_profile") or "")
            if scope_profiles and profile not in scope_profiles:
                continue
            status = str(trace.get("status") or "")
            if status not in _ACTIVE_TRACE_STATUSES or _terminal_event_seen(trace):
                continue
            last_progress = _last_progress_at(trace)
            if last_progress is None or last_progress >= cutoff:
                continue
            stale.append(
                {
                    "ticket_id": ticket_id,
                    "status": status,
                    "last_progress_at": last_progress.isoformat(),
                    "minutes_since_progress": int(
                        (datetime.now(UTC) - last_progress).total_seconds() / 60
                    ),
                }
            )
            if recent_event_exists(
                conn,
                job_key=job_key,
                target_type="trace",
                target_id=ticket_id,
                since_iso=event_cutoff.isoformat(),
            ):
                continue
            if not dry_run:
                trace_id = str(trace.get("run_id") or trace.get("trace_id") or "")
                append_trace(
                    ticket_id,
                    trace_id,
                    "automation",
                    "automation_stuck_detected",
                    source="automation",
                    status=status,
                    stale_after_minutes=stale_after_minutes,
                    last_progress_at=last_progress.isoformat(),
                )
            record_event(
                conn,
                job_key=job_key,
                run_id=run_id,
                severity="warning",
                target_type="trace",
                target_id=ticket_id,
                message=f"{ticket_id} has no progress for {stale[-1]['minutes_since_progress']}m.",
                payload=stale[-1],
            )
            emitted += 1
    finally:
        conn.close()

    return (
        f"{len(stale)} stale active trace(s), {emitted} event(s) emitted",
        {
            "stale_after_minutes": stale_after_minutes,
            "event_cooldown_minutes": cooldown_minutes,
            "dry_run": dry_run,
            "stale": stale[:50],
            "emitted": emitted,
        },
    )


def _run_stale_worktree_cleanup(
    db_path: Path,
    *,
    job_key: str,
    run_id: int,
    config: dict[str, Any],
    scope: str,
) -> tuple[str, dict[str, Any]]:
    max_age_hours = _int_config(
        config,
        "max_age_hours",
        48,
        min_value=1,
        max_value=24 * 90,
    )
    dry_run = _bool_config(config, "dry_run", True)
    profiles = _profile_repo_paths(scope)
    outputs: list[dict[str, Any]] = []
    affected = 0

    for profile_name, repo_path in profiles:
        if not repo_path.exists():
            outputs.append(
                {
                    "profile": profile_name,
                    "repo_path": str(repo_path),
                    "status": "skipped",
                    "summary": "repo path does not exist",
                }
            )
            continue
        cmd = [
            sys.executable,
            str(_CLEANUP_SCRIPT),
            "--client-repo",
            str(repo_path),
            "--max-age-hours",
            str(max_age_hours),
        ]
        if dry_run:
            cmd.append("--dry-run")
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        text = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        match = re.search(r"(?:Would remove|Removed) (\d+) worktree", text)
        count = int(match.group(1)) if match else 0
        affected += count
        outputs.append(
            {
                "profile": profile_name,
                "repo_path": str(repo_path),
                "status": "ok" if completed.returncode == 0 else "failed",
                "returncode": completed.returncode,
                "affected": count,
                "output_tail": text[-2000:],
            }
        )
        if count or completed.returncode != 0:
            _record_event(
                db_path,
                job_key=job_key,
                run_id=run_id,
                severity="error" if completed.returncode != 0 else "info",
                target_type="profile",
                target_id=profile_name,
                message=(
                    f"{'Would remove' if dry_run else 'Removed'} {count} stale "
                    f"worktree(s) for {profile_name}."
                ),
                payload=outputs[-1],
            )

    failures = sum(1 for item in outputs if item["status"] == "failed")
    return (
        f"{affected} stale worktree(s), {failures} failure(s)",
        {
            "max_age_hours": max_age_hours,
            "dry_run": dry_run,
            "profiles": len(profiles),
            "affected": affected,
            "failures": failures,
            "outputs": outputs,
        },
    )


def _run_trace_archive_retention(
    db_path: Path,
    *,
    job_key: str,
    run_id: int,
    config: dict[str, Any],
    scope: str,
) -> tuple[str, dict[str, Any]]:
    retention_days = _int_config(
        config,
        "retention_days",
        90,
        min_value=1,
        max_value=3650,
    )
    dry_run = _bool_config(config, "dry_run", True)
    cutoff_ts = (datetime.now(UTC) - timedelta(days=retention_days)).timestamp()
    roots: dict[str, Path] = {}
    for profile_name, repo_path in _profile_repo_paths(scope):
        roots.setdefault(profile_name, repo_path.expanduser().resolve().parent / "trace-archive")

    deleted: list[dict[str, Any]] = []
    for profile_name, root in roots.items():
        if not root.is_dir():
            continue
        for candidate in root.iterdir():
            if not candidate.is_dir():
                continue
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff_ts:
                continue
            item = {
                "profile": profile_name,
                "path": str(candidate),
                "mtime": datetime.fromtimestamp(mtime, UTC).isoformat(),
            }
            deleted.append(item)
            if not dry_run:
                shutil.rmtree(candidate)

    if deleted:
        _record_event(
            db_path,
            job_key=job_key,
            run_id=run_id,
            severity="info",
            target_type="trace_archive",
            target_id="all",
            message=(
                f"{'Would delete' if dry_run else 'Deleted'} {len(deleted)} "
                "old trace archive directorie(s)."
            ),
            payload={"retention_days": retention_days, "items": deleted[:50]},
        )

    return (
        f"{len(deleted)} archive directorie(s) {'eligible' if dry_run else 'deleted'}",
        {
            "retention_days": retention_days,
            "dry_run": dry_run,
            "matched": len(deleted),
            "items": deleted[:100],
        },
    )


def run_automation_job(
    job: dict[str, Any],
    *,
    db_path: Path,
    run_id: int,
) -> tuple[str, dict[str, Any]]:
    """Run one automation job and return ``(summary, details)``."""
    job_key = str(job["job_key"])
    config = dict(job.get("config") or {})
    scope = str(job.get("scope") or "all")

    if job_key == "trace_reconciliation":
        return _run_trace_reconciliation(
            db_path,
            job_key=job_key,
            run_id=run_id,
            config=config,
        )
    if job_key == "pipeline_watcher":
        return _run_pipeline_watcher(
            db_path,
            job_key=job_key,
            run_id=run_id,
            config=config,
            scope=scope,
        )
    if job_key == "stale_worktree_cleanup":
        return _run_stale_worktree_cleanup(
            db_path,
            job_key=job_key,
            run_id=run_id,
            config=config,
            scope=scope,
        )
    if job_key == "trace_archive_retention":
        return _run_trace_archive_retention(
            db_path,
            job_key=job_key,
            run_id=run_id,
            config=config,
            scope=scope,
        )
    raise ValueError(f"unknown automation job: {job_key}")
