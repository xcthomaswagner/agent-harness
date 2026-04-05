"""Tests for the Phase 0 autonomy backfill script."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autonomy_store import ensure_schema, open_connection
from scripts import backfill_autonomy
from scripts.backfill_autonomy import (
    BackfillStats,
    _in_range,
    extract_ticket_rows,
    parse_pr_url,
    run_backfill,
)

PR_URL = "https://github.com/acme/repo/pull/42"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_trace(logs_dir: Path, ticket_id: str, entries: list[dict[str, Any]]) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"{ticket_id}.jsonl"
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def _complete_pipeline_entries(
    ticket_id: str = "SCRUM-101",
    pr_url: str = PR_URL,
    ts_base: str = "2026-03-24T01:05:05+00:00",
) -> list[dict[str, Any]]:
    return [
        {
            "trace_id": "t1",
            "ticket_id": ticket_id,
            "timestamp": ts_base,
            "phase": "webhook",
            "event": "jira_webhook_received",
            "ticket_type": "story",
            "source": "jira",
        },
        {
            "trace_id": "t1",
            "ticket_id": ticket_id,
            "timestamp": "2026-03-24T01:10:00+00:00",
            "phase": "pr_created",
            "event": "PR created",
            "pr_url": pr_url,
            "source": "agent",
        },
        {
            "trace_id": "t1",
            "ticket_id": ticket_id,
            "timestamp": "2026-03-24T01:16:49+00:00",
            "phase": "completion",
            "event": "agent_finished",
            "status": "complete",
            "pr_url": pr_url,
        },
        {
            "trace_id": "t1",
            "ticket_id": ticket_id,
            "timestamp": "2026-03-24T01:17:00+00:00",
            "phase": "complete",
            "event": "Pipeline complete",
            "pr_url": pr_url,
            "pipeline_mode": "simple",
            "source": "agent",
        },
    ]


# ---------------------------------------------------------------------------
# parse_pr_url
# ---------------------------------------------------------------------------

def test_parse_pr_url_ok() -> None:
    assert parse_pr_url("https://github.com/acme/repo/pull/42") == ("acme/repo", 42)


def test_parse_pr_url_malformed() -> None:
    assert parse_pr_url("https://example.com/foo/bar") is None
    assert parse_pr_url("not a url") is None


def test_parse_pr_url_empty() -> None:
    assert parse_pr_url("") is None


# ---------------------------------------------------------------------------
# extract_ticket_rows
# ---------------------------------------------------------------------------

def test_extract_ticket_rows_complete_pipeline() -> None:
    entries = _complete_pipeline_entries()
    rows = extract_ticket_rows("SCRUM-101", entries)
    assert len(rows) == 1
    r = rows[0]
    assert r.repo_full_name == "acme/repo"
    assert r.pr_number == 42
    assert r.pr_url == PR_URL
    assert r.ticket_type == "story"
    assert r.pipeline_mode == "simple"
    assert r.escalated == 0
    assert r.merged == 0
    assert r.merged_at == ""
    assert r.opened_at == "2026-03-24T01:10:00+00:00"
    assert r.head_sha == "backfill:SCRUM-101:42"


def test_extract_ticket_rows_escalated() -> None:
    entries = [
        {
            "trace_id": "t1",
            "ticket_id": "SCRUM-9",
            "timestamp": "2026-03-24T01:00:00+00:00",
            "phase": "webhook",
            "event": "jira_webhook_received",
            "ticket_type": "bug",
            "source": "jira",
        },
        # an agent_finished escalated with a pr_url but no Pipeline complete after
        {
            "trace_id": "t1",
            "ticket_id": "SCRUM-9",
            "timestamp": "2026-03-24T01:05:00+00:00",
            "phase": "completion",
            "event": "agent_finished",
            "status": "escalated",
            "pr_url": PR_URL,
        },
    ]
    rows = extract_ticket_rows("SCRUM-9", entries)
    assert len(rows) == 1
    assert rows[0].escalated == 1
    assert rows[0].opened_at == "2026-03-24T01:05:00+00:00"
    assert rows[0].ticket_type == "bug"


def test_extract_ticket_rows_escalated_then_complete() -> None:
    # escalated attempt followed by successful Pipeline complete → not escalated
    entries = [
        {
            "ticket_id": "SCRUM-9",
            "timestamp": "2026-03-24T01:05:00+00:00",
            "phase": "completion",
            "event": "agent_finished",
            "status": "escalated",
            "pr_url": PR_URL,
        },
        {
            "ticket_id": "SCRUM-9",
            "timestamp": "2026-03-24T01:10:00+00:00",
            "phase": "completion",
            "event": "agent_finished",
            "status": "complete",
            "pr_url": PR_URL,
        },
        {
            "ticket_id": "SCRUM-9",
            "timestamp": "2026-03-24T01:11:00+00:00",
            "phase": "complete",
            "event": "Pipeline complete",
            "pr_url": PR_URL,
            "pipeline_mode": "simple",
            "source": "agent",
        },
    ]
    rows = extract_ticket_rows("SCRUM-9", entries)
    assert len(rows) == 1
    assert rows[0].escalated == 0


def test_extract_ticket_rows_no_pr() -> None:
    entries = [
        {
            "ticket_id": "SCRUM-5",
            "timestamp": "2026-03-24T01:00:00+00:00",
            "phase": "webhook",
            "event": "jira_webhook_received",
            "ticket_type": "story",
            "source": "jira",
        },
        {
            "ticket_id": "SCRUM-5",
            "timestamp": "2026-03-24T01:05:00+00:00",
            "phase": "completion",
            "event": "agent_finished",
            "status": "escalated",
            "pr_url": "",
        },
    ]
    assert extract_ticket_rows("SCRUM-5", entries) == []


def test_extract_ticket_rows_multiple_runs_same_pr() -> None:
    # Several runs that all converge on the same pr_url → single row.
    entries: list[dict[str, Any]] = []
    for i, ts in enumerate(
        [
            "2026-03-24T01:05:00+00:00",
            "2026-03-24T01:06:00+00:00",
            "2026-03-24T01:10:00+00:00",
        ]
    ):
        entries.append({
            "ticket_id": "SCRUM-14",
            "timestamp": ts,
            "phase": "pr_created",
            "event": "PR updated" if i else "PR created",
            "pr_url": PR_URL,
            "source": "agent",
        })
    entries.append({
        "ticket_id": "SCRUM-14",
        "timestamp": "2026-03-24T01:16:49+00:00",
        "phase": "completion",
        "event": "agent_finished",
        "status": "complete",
        "pr_url": PR_URL,
    })
    rows = extract_ticket_rows("SCRUM-14", entries)
    assert len(rows) == 1
    assert rows[0].opened_at == "2026-03-24T01:05:00+00:00"


def test_head_sha_is_deterministic() -> None:
    entries = _complete_pipeline_entries("SCRUM-101")
    r1 = extract_ticket_rows("SCRUM-101", entries)[0]
    r2 = extract_ticket_rows("SCRUM-101", entries)[0]
    assert r1.head_sha == r2.head_sha == "backfill:SCRUM-101:42"


# ---------------------------------------------------------------------------
# _in_range
# ---------------------------------------------------------------------------

def test_in_range_filters() -> None:
    # No bounds → True regardless
    assert _in_range("", "", "") is True
    assert _in_range("2026-01-01", "", "") is True
    # Missing timestamp with bounds set → False
    assert _in_range("", "2026-01-01", "") is False
    # Inside range
    assert _in_range("2026-03-24", "2026-03-01", "2026-04-01") is True
    # Below since
    assert _in_range("2026-02-01", "2026-03-01", "") is False
    # Above until
    assert _in_range("2026-05-01", "", "2026-04-01") is False


# ---------------------------------------------------------------------------
# run_backfill
# ---------------------------------------------------------------------------

def test_run_backfill_drops_unresolved_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs_dir = tmp_path / "logs"
    _write_trace(
        logs_dir, "UNKNOWN-5", _complete_pipeline_entries("UNKNOWN-5")
    )
    # Force lookup to fail for UNKNOWN project key
    import autonomy_ingest
    monkeypatch.setattr(
        autonomy_ingest, "find_profile_by_project_key", lambda _key: None
    )

    db_path = tmp_path / "autonomy.db"
    stats = run_backfill(logs_dir=logs_dir, db_path=db_path, dry_run=False)
    assert stats.files_scanned == 1
    assert stats.rows_dropped_no_profile == 1
    assert stats.rows_written == 0
    assert "UNKNOWN-5" in stats.unresolved_profiles


def test_run_backfill_dry_run_writes_nothing(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    _write_trace(logs_dir, "SCRUM-77", _complete_pipeline_entries("SCRUM-77"))

    db_path = tmp_path / "autonomy.db"
    stats = run_backfill(
        logs_dir=logs_dir, db_path=db_path, dry_run=True
    )
    assert stats.files_scanned == 1
    assert stats.rows_extracted == 1
    assert stats.rows_written == 0
    # DB file never created in dry-run
    assert not db_path.exists()


def test_run_backfill_idempotent(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    _write_trace(logs_dir, "SCRUM-77", _complete_pipeline_entries("SCRUM-77"))
    db_path = tmp_path / "autonomy.db"

    s1 = run_backfill(logs_dir=logs_dir, db_path=db_path, dry_run=False)
    s2 = run_backfill(logs_dir=logs_dir, db_path=db_path, dry_run=False)
    assert s1.rows_written == 1
    assert s2.rows_written == 1

    conn = open_connection(db_path)
    ensure_schema(conn)
    try:
        count = conn.execute("SELECT COUNT(*) AS c FROM pr_runs").fetchone()["c"]
        assert count == 1
    finally:
        conn.close()


def test_run_backfill_writes_backfilled_flag(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    _write_trace(logs_dir, "SCRUM-77", _complete_pipeline_entries("SCRUM-77"))
    db_path = tmp_path / "autonomy.db"

    stats = run_backfill(logs_dir=logs_dir, db_path=db_path, dry_run=False)
    assert stats.rows_written == 1

    conn = open_connection(db_path)
    ensure_schema(conn)
    try:
        row = conn.execute(
            "SELECT backfilled, head_sha, merged, escalated, "
            "client_profile, opened_at, ticket_type, pipeline_mode "
            "FROM pr_runs"
        ).fetchone()
        assert row["backfilled"] == 1
        assert row["head_sha"] == "backfill:SCRUM-77:42"
        assert row["merged"] == 0
        assert row["escalated"] == 0
        assert row["client_profile"] != ""
        assert row["ticket_type"] == "story"
        assert row["pipeline_mode"] == "simple"
    finally:
        conn.close()


def test_run_backfill_respects_ticket_filter(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    _write_trace(logs_dir, "SCRUM-1", _complete_pipeline_entries("SCRUM-1"))
    _write_trace(logs_dir, "SCRUM-2", _complete_pipeline_entries("SCRUM-2"))
    db_path = tmp_path / "autonomy.db"

    stats = run_backfill(
        logs_dir=logs_dir, db_path=db_path, tickets=["SCRUM-1"], dry_run=False
    )
    assert stats.files_scanned == 1
    assert stats.rows_written == 1


def test_run_backfill_since_until_filter(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    _write_trace(logs_dir, "SCRUM-77", _complete_pipeline_entries("SCRUM-77"))
    db_path = tmp_path / "autonomy.db"

    # Since 2027 → out of range
    stats = run_backfill(
        logs_dir=logs_dir,
        db_path=db_path,
        since="2027-01-01",
        dry_run=False,
    )
    assert stats.rows_extracted == 0
    assert stats.rows_dropped_out_of_range == 1
    assert stats.rows_written == 0


def test_run_backfill_empty_file_skipped(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "EMPTY-1.jsonl").write_text("")
    db_path = tmp_path / "autonomy.db"
    stats = run_backfill(logs_dir=logs_dir, db_path=db_path, dry_run=True)
    assert stats.files_scanned == 0


def test_backfill_stats_default() -> None:
    s = BackfillStats()
    assert s.files_scanned == 0
    assert s.unresolved_profiles == []


def test_backfill_module_importable() -> None:
    assert hasattr(backfill_autonomy, "main")
    assert hasattr(backfill_autonomy, "run_backfill")
