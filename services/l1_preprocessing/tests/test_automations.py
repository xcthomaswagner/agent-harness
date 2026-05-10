"""Tests for operator automations."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tracer as tracer_module
from automation_jobs import run_automation_job
from automation_store import (
    ensure_default_jobs,
    get_job,
    list_events,
    start_run,
    update_job,
)
from autonomy_store import ensure_schema, open_connection
from config import settings
from operator_api_data import router
from tracer import read_trace


def _mk_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    profiles_dir.mkdir()
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    return TestClient(_mk_app())


def test_automations_api_seeds_defaults_and_updates_job(client: TestClient) -> None:
    r = client.get("/api/operator/automations")
    assert r.status_code == 200
    body = r.json()
    keys = {job["job_key"] for job in body["jobs"]}
    assert {
        "trace_reconciliation",
        "pipeline_watcher",
        "stale_worktree_cleanup",
        "trace_archive_retention",
    }.issubset(keys)

    r = client.put(
        "/api/operator/automations/pipeline_watcher",
        json={
            "enabled": False,
            "interval_seconds": 600,
            "scope": "all",
            "config": {
                "stale_after_minutes": 45,
                "event_cooldown_minutes": 30,
                "dry_run": True,
            },
        },
    )
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["enabled"] is False
    assert job["interval_seconds"] == 600
    assert job["config"]["stale_after_minutes"] == 45
    assert job["config"]["dry_run"] is True


def test_pipeline_watcher_emits_event_and_trace_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)

    old = datetime.now(UTC) - timedelta(hours=3)
    trace_path = logs_dir / "RND-1.jsonl"
    trace_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "timestamp": old.isoformat(),
                    "trace_id": "trace-rnd-1",
                    "ticket_id": "RND-1",
                    "phase": "pipeline",
                    "event": "processing_started",
                    "source": "pipeline",
                    "ticket_title": "Old active run",
                },
                {
                    "timestamp": (old + timedelta(minutes=1)).isoformat(),
                    "trace_id": "trace-rnd-1",
                    "ticket_id": "RND-1",
                    "phase": "l2_dispatch",
                    "event": "l2_dispatched",
                    "source": "pipeline",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        ensure_default_jobs(conn)
        job = update_job(
            conn,
            "pipeline_watcher",
            enabled=True,
            interval_seconds=300,
            config={
                "stale_after_minutes": 30,
                "event_cooldown_minutes": 60,
                "dry_run": False,
            },
        )
        run = start_run(conn, "pipeline_watcher", triggered_by="test")
    finally:
        conn.close()

    summary, details = run_automation_job(job, db_path=db_path, run_id=int(run["id"]))
    assert "1 stale active trace" in summary
    assert details["emitted"] == 1

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        events = list_events(conn, job_key="pipeline_watcher")
    finally:
        conn.close()
    assert len(events) == 1
    assert events[0]["target_id"] == "RND-1"
    assert events[0]["severity"] == "warning"

    trace_events = [entry["event"] for entry in read_trace("RND-1")]
    assert "automation_stuck_detected" in trace_events


def test_trace_reconciliation_job_uses_dashboard_repair_path(tmp_path: Path) -> None:
    db_path = tmp_path / "autonomy.db"
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        ensure_default_jobs(conn)
        job = get_job(conn, "trace_reconciliation")
        assert job is not None
        run = start_run(conn, "trace_reconciliation", triggered_by="test")
    finally:
        conn.close()

    summary, details = run_automation_job(job, db_path=db_path, run_id=int(run["id"]))

    assert "reconciled" in summary
    assert details["stale_after_hours"] == 168
    assert details["status"] == "accepted"
