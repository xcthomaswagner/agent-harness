"""Tests for unified_dashboard — /dashboard landing page."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    open_connection,
    record_auto_merge_decision,
    upsert_pr_run,
)
from config import settings
from unified_dashboard import router


def _mk_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _seed_pr_runs(db_path: Path, rows: list[dict]) -> None:
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        for i, row in enumerate(rows):
            upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id=row.get("ticket_id", f"TK-{i}"),
                    pr_number=row.get("pr_number", i + 1),
                    repo_full_name=row.get("repo_full_name", "acme/widgets"),
                    pr_url=row.get("pr_url", f"https://example.test/pr/{i + 1}"),
                    head_sha=row.get("head_sha", f"sha{i}"),
                    client_profile=row["client_profile"],
                    opened_at=row.get("opened_at", "2026-04-01T12:00:00+00:00"),
                    first_pass_accepted=row.get("first_pass_accepted", 0),
                    merged=row.get("merged", 0),
                ),
            )
    finally:
        conn.close()


def _seed_trace(logs_dir: Path, ticket_id: str) -> None:
    """Write a minimal trace JSONL file."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    trace_file = logs_dir / f"{ticket_id}.jsonl"
    entries = [
        {
            "ticket_id": ticket_id,
            "trace_id": "t-001",
            "phase": "webhook",
            "event": "jira_webhook_received",
            "timestamp": "2026-04-01T12:00:00+00:00",
            "ticket_type": "story",
            "source": "jira",
        },
        {
            "ticket_id": ticket_id,
            "trace_id": "t-001",
            "phase": "pipeline",
            "event": "processing_completed",
            "timestamp": "2026-04-01T12:05:00+00:00",
            "status": "Complete",
        },
    ]
    with trace_file.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    # Ensure DB schema exists
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    return TestClient(_mk_app())


def test_dashboard_returns_200_html(client: TestClient) -> None:
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_dashboard_has_nav_links(client: TestClient) -> None:
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert 'href="/traces"' in r.text
    assert 'href="/autonomy"' in r.text
    assert 'href="/dashboard"' in r.text


def test_dashboard_renders_profile_summary_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_pr_runs(db_path, [
        {"client_profile": "my-test-project", "first_pass_accepted": 1, "merged": 1},
        {"client_profile": "my-test-project", "first_pass_accepted": 0, "merged": 0,
         "ticket_id": "TK-2", "pr_number": 2, "head_sha": "sha2"},
    ])
    c = TestClient(_mk_app())
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "my-test-project" in r.text
    assert "2 PRs" in r.text


def test_dashboard_renders_recent_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    logs_dir = tmp_path / "trace_logs"
    _seed_trace(logs_dir, "PROJ-42")
    # Point the tracer LOGS_DIR to our tmp dir
    import tracer

    monkeypatch.setattr(tracer, "LOGS_DIR", logs_dir)
    c = TestClient(_mk_app())
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "PROJ-42" in r.text


def test_dashboard_auto_merge_section_hidden_when_no_decisions(
    client: TestClient,
) -> None:
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Auto-merge Decisions" not in r.text


def test_dashboard_auto_merge_section_shown_when_decisions_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        record_auto_merge_decision(
            conn,
            repo_full_name="acme/widgets",
            pr_number=10,
            decision="dry_run",
            reason="Conservative mode active",
            payload={
                "client_profile": "test-profile",
            },
        )
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "Auto-merge Decisions" in r.text
    assert "dry_run" in r.text


def test_root_redirects_to_dashboard(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"


def test_dashboard_escapes_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XSS test: angle brackets in ticket_id should be escaped."""
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    logs_dir = tmp_path / "trace_logs"
    # Use a filename-safe string that still tests HTML escaping
    malicious_id = "XSS-<img>"
    _seed_trace(logs_dir, malicious_id)
    import tracer

    monkeypatch.setattr(tracer, "LOGS_DIR", logs_dir)
    c = TestClient(_mk_app())
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "<img>" not in r.text
    assert "&lt;img&gt;" in r.text


def test_status_badge_shared_with_trace_dashboard() -> None:
    """Improvement regression: unified_dashboard._STATUS_BADGE used to be
    a hand-copied dict that had drifted from trace_dashboard's copy —
    unified was missing ``Failed``, ``Timed Out``, and ``Cleaned Up``,
    so any trace in those states silently fell through to the
    secondary-fallback class on the /dashboard landing page. Both
    modules now import from dashboard_common, so the mapping is
    literally the same object."""
    import trace_dashboard
    import unified_dashboard
    from dashboard_common import STATUS_BADGE

    # Both dashboards see the same canonical mapping.
    assert trace_dashboard._STATUS_BADGE is STATUS_BADGE
    assert unified_dashboard._STATUS_BADGE is STATUS_BADGE

    # And the statuses that the unified dashboard used to be missing
    # are now present — the whole point of the fix.
    for previously_missing in ("Failed", "Timed Out", "Cleaned Up"):
        assert previously_missing in STATUS_BADGE
