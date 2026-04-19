"""Tests for operator_api_data — /api/operator JSON endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    open_connection,
    record_auto_merge_decision,
    upsert_lesson_candidate,
    upsert_pr_run,
)
from autonomy_store.lessons import LessonCandidateUpsert
from config import settings
from operator_api_data import router


def _mk_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _write_profile(
    profiles_dir: Path, name: str, platform: str = "salesforce"
) -> None:
    """Write a minimal client-profile YAML the loader can parse."""
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "client": name,
                "platform_profile": platform,
                "ticket_source": {
                    "kind": "jira",
                    "instance": "example.atlassian.net",
                    "project_key": "TEST",
                    "ai_label": "ai-implement",
                    "quick_label": "ai-quick",
                },
                "source_control": {"kind": "github", "owner": "x", "repo": "y"},
                "client_repo": {"local_path": "/tmp/x"},
            }
        )
    )


def _seed_pr_run(
    db_path: Path,
    *,
    ticket_id: str,
    pr_number: int,
    client_profile: str,
    merged: int = 0,
    first_pass_accepted: int = 0,
    opened_at: str = "2026-04-18T12:00:00+00:00",
) -> int:
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        return upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id=ticket_id,
                pr_number=pr_number,
                repo_full_name="acme/widgets",
                pr_url=f"https://example.test/pr/{pr_number}",
                head_sha=f"sha{pr_number}",
                client_profile=client_profile,
                opened_at=opened_at,
                first_pass_accepted=first_pass_accepted,
                merged=merged,
            ),
        )
    finally:
        conn.close()


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    # Point client_profile loader at an empty scratch dir by default so
    # repo-real profiles don't leak into the test.
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


# ---------- /api/operator/profiles ----------


def test_profiles_empty(client: TestClient, tmp_path: Path) -> None:
    r = client.get("/api/operator/profiles")
    assert r.status_code == 200
    assert r.json() == {"profiles": []}


def test_profiles_lists_yaml_profiles_with_zero_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed YAML profile but no PR runs — endpoint returns the profile
    # with zeroed metrics, not nothing.
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha", platform="salesforce")
    _write_profile(profiles_dir, "bravo", platform="sitecore")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    c = TestClient(_mk_app())
    r = c.get("/api/operator/profiles")
    assert r.status_code == 200
    profiles = r.json()["profiles"]
    assert len(profiles) == 2
    names = {p["id"] for p in profiles}
    assert names == {"alpha", "bravo"}
    # Every profile populated with zeroed counts.
    for p in profiles:
        assert p["in_flight"] == 0
        assert p["completed_24h"] == 0
        assert p["auto_merge"] == 0.0


def test_profiles_counts_in_flight_and_completed_24h(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    # Use NOW-1h so the row falls inside the 24h window.
    from datetime import UTC, datetime, timedelta

    recent_iso = (
        datetime.now(UTC) - timedelta(hours=1)
    ).isoformat()

    _seed_pr_run(
        db_path,
        ticket_id="T-1",
        pr_number=1,
        client_profile="alpha",
        merged=0,
        opened_at=recent_iso,
    )
    _seed_pr_run(
        db_path,
        ticket_id="T-2",
        pr_number=2,
        client_profile="alpha",
        merged=1,
        first_pass_accepted=1,
        opened_at=recent_iso,
    )

    c = TestClient(_mk_app())
    r = c.get("/api/operator/profiles")
    assert r.status_code == 200
    [p] = r.json()["profiles"]
    assert p["id"] == "alpha"
    assert p["in_flight"] == 1
    assert p["completed_24h"] == 1


def test_profiles_sorted_by_in_flight_desc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    _write_profile(profiles_dir, "bravo")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    from datetime import UTC, datetime, timedelta

    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    # bravo has 2 in-flight, alpha has 1
    _seed_pr_run(
        db_path,
        ticket_id="T-1",
        pr_number=1,
        client_profile="alpha",
        opened_at=recent,
    )
    _seed_pr_run(
        db_path,
        ticket_id="T-2",
        pr_number=2,
        client_profile="bravo",
        opened_at=recent,
    )
    _seed_pr_run(
        db_path,
        ticket_id="T-3",
        pr_number=3,
        client_profile="bravo",
        opened_at=recent,
    )

    c = TestClient(_mk_app())
    profiles = c.get("/api/operator/profiles").json()["profiles"]
    assert [p["id"] for p in profiles] == ["bravo", "alpha"]


def test_profiles_auto_merge_rate_computed_from_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        # 3 merged, 1 blocked, 1 skipped → 3/4 eligible = 0.75
        for i, decision in enumerate(
            ["merged", "merged", "merged", "blocked", "skipped"]
        ):
            record_auto_merge_decision(
                conn,
                repo_full_name="acme/widgets",
                pr_number=i + 1,
                decision=decision,
                reason="test",
                payload={"client_profile": "alpha"},
            )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    [p] = c.get("/api/operator/profiles").json()["profiles"]
    assert p["auto_merge"] == pytest.approx(0.75, abs=1e-3)


@pytest.mark.parametrize(
    "path",
    [
        "/api/operator/profiles",
        "/api/operator/traces",
        "/api/operator/traces/HARN-1",
        "/api/operator/autonomy/xcsf30",
        "/api/operator/lessons/counts",
        "/api/operator/tickets/HARN-1/agents",
        "/api/operator/pr/1",
    ],
)
def test_all_operator_endpoints_require_auth_when_key_set(
    path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router-level Depends(_require_dashboard_auth) covers every route."""
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "secret")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", False)

    profiles_dir = tmp_path / "client-profiles"
    profiles_dir.mkdir()
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    c = TestClient(_mk_app())
    # Missing key → 401.
    assert c.get(path).status_code == 401
    # Correct key → not 401 (may be 200/404 depending on fixture state).
    r = c.get(path, headers={"X-API-Key": "secret"})
    assert r.status_code != 401


def test_profiles_requires_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", False)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    c = TestClient(_mk_app())
    assert c.get("/api/operator/profiles").status_code == 401
    ok = c.get(
        "/api/operator/profiles", headers={"X-API-Key": "secret-key"}
    )
    assert ok.status_code == 200


# ---------- /api/operator/lessons/counts ----------


# ---------- /api/operator/traces ----------


def _write_trace(
    logs_dir: Path,
    ticket_id: str,
    *,
    events: list[tuple[str, str]],
    title: str = "",
) -> None:
    """Seed a JSONL trace file the tracer reads.

    events: list of (phase, event) pairs. Timestamps auto-generated.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"{ticket_id}.jsonl"
    with path.open("w") as f:
        for i, (phase, ev) in enumerate(events):
            entry = {
                "ticket_id": ticket_id,
                "trace_id": f"t-{ticket_id}",
                "phase": phase,
                "event": ev,
                "timestamp": f"2026-04-18T12:00:{i:02d}+00:00",
                "source": "agent",
            }
            if i == 0 and title:
                entry["ticket_title"] = title
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def traces_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """Test client with isolated data/logs and autonomy.db."""
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    return TestClient(_mk_app())


def test_traces_empty(traces_client: TestClient) -> None:
    r = traces_client.get("/api/operator/traces")
    assert r.status_code == 200
    data = r.json()
    assert data == {"traces": [], "count": 0, "offset": 0, "limit": 100}


def test_traces_returns_shaped_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_trace(
        logs_dir,
        "HARN-100",
        events=[
            ("webhook", "webhook_received"),
            ("pipeline", "processing_completed"),
            ("pipeline", "Pipeline complete"),
        ],
        title="Ship the thing",
    )

    c = TestClient(_mk_app())
    rows = c.get("/api/operator/traces").json()["traces"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "HARN-100"
    assert row["status"] == "done"
    assert row["raw_status"] == "Complete"
    assert "elapsed" in row


def test_traces_status_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    # One "done" trace, one "queued" trace.
    _write_trace(
        logs_dir,
        "HARN-DONE",
        events=[
            ("webhook", "webhook_received"),
            ("pipeline", "Pipeline complete"),
        ],
    )
    _write_trace(
        logs_dir,
        "HARN-Q",
        events=[("webhook", "webhook_received")],
    )

    c = TestClient(_mk_app())
    done = c.get("/api/operator/traces?status=done").json()["traces"]
    queued = c.get("/api/operator/traces?status=queued").json()["traces"]
    assert [r["id"] for r in done] == ["HARN-DONE"]
    assert [r["id"] for r in queued] == ["HARN-Q"]


def test_traces_limit_caps_at_500(traces_client: TestClient) -> None:
    r = traces_client.get("/api/operator/traces?limit=9999")
    assert r.status_code == 200
    assert r.json()["limit"] == 500


# ---------- /api/operator/traces/{id} (trace detail) ----------


def _write_rich_trace(
    logs_dir: Path,
    ticket_id: str,
    phases_events: list[tuple[str, str, str]],
) -> None:
    """Write a JSONL trace with agent-phase entries.

    phases_events: list of (phase, event, message). Timestamps
    auto-incremented by 30s per entry so compute_phase_durations has
    non-zero deltas.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"{ticket_id}.jsonl"
    with path.open("w") as f:
        # A webhook entry to anchor the run start.
        f.write(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "trace_id": f"t-{ticket_id}",
                    "phase": "webhook",
                    "event": "webhook_received",
                    "timestamp": "2026-04-18T12:00:00+00:00",
                    "source": "pipeline",
                }
            )
            + "\n"
        )
        for i, (phase, ev, msg) in enumerate(phases_events):
            ts_sec = 30 + i * 30
            f.write(
                json.dumps(
                    {
                        "ticket_id": ticket_id,
                        "trace_id": f"t-{ticket_id}",
                        "phase": phase,
                        "event": ev,
                        "message": msg,
                        "timestamp": f"2026-04-18T12:{ts_sec // 60:02d}:{ts_sec % 60:02d}+00:00",
                        "source": "agent",
                    }
                )
                + "\n"
            )


def test_trace_detail_404_when_missing(traces_client: TestClient) -> None:
    r = traces_client.get("/api/operator/traces/HARN-MISSING")
    assert r.status_code == 404


def test_trace_detail_shapes_phases_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_rich_trace(
        logs_dir,
        "HARN-777",
        phases_events=[
            ("planning", "plan_drafted", "Plan drafted"),
            ("planning", "plan_approved", "Plan approved"),
            ("scaffolding", "worktree_ready", "Worktree ready"),
            ("implementing", "unit_01_spawn", "unit-01 spawned"),
            ("implementing", "unit_01_done", "unit-01 done"),
            ("reviewing", "review_complete", "Review complete"),
        ],
    )

    c = TestClient(_mk_app())
    r = c.get("/api/operator/traces/HARN-777")
    assert r.status_code == 200
    data = r.json()

    # Core fields present.
    assert data["id"] == "HARN-777"
    assert "phases" in data and "events" in data
    assert len(data["phases"]) == 5

    phase_by_key = {p["key"]: p for p in data["phases"]}
    # Planning + scaffolding + implementing have events -> not pending.
    # The current phase (last agent-written) is reviewing -> active.
    assert phase_by_key["planning"]["state"] in ("done", "active")
    assert phase_by_key["planning"]["event_count"] >= 2
    assert phase_by_key["scaffolding"]["state"] in ("done", "active")
    assert phase_by_key["implementing"]["state"] in ("done", "active")
    assert phase_by_key["reviewing"]["state"] == "active"
    assert phase_by_key["merging"]["state"] == "pending"

    # Event stream preserves message text.
    messages = [e["msg"] for e in data["events"]]
    assert any("Plan drafted" in m for m in messages)
    assert any("Review complete" in m for m in messages)


def test_trace_detail_marks_failed_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_rich_trace(
        logs_dir,
        "HARN-FAIL",
        phases_events=[
            ("planning", "plan_drafted", "Plan drafted"),
            ("implementing", "unit_01_spawn", "unit-01 spawned"),
            ("implementing", "unit_01_error", "Unit failed with error"),
        ],
    )

    c = TestClient(_mk_app())
    data = c.get("/api/operator/traces/HARN-FAIL").json()
    impl = next(p for p in data["phases"] if p["key"] == "implementing")
    assert impl["state"] == "fail"


# ---------- /api/operator/autonomy/{profile} ----------


def test_autonomy_returns_empty_shape_for_unknown_profile(
    client: TestClient,
) -> None:
    r = client.get("/api/operator/autonomy/does-not-exist")
    assert r.status_code == 200
    data = r.json()
    assert data["profile"] == "does-not-exist"
    assert "metrics" in data
    assert "trends" in data
    assert data["by_type"] == []
    assert data["escaped"] == []
    # 30 days x 4 trends, empty arrays with all-None values
    assert len(data["trends"]["fpa"]) == 30
    assert all(d["value"] is None for d in data["trends"]["fpa"])


def test_autonomy_includes_auto_merge_trend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        for i, decision in enumerate(["merged", "blocked"]):
            record_auto_merge_decision(
                conn,
                repo_full_name="acme/widgets",
                pr_number=i + 1,
                decision=decision,
                reason="test",
                payload={"client_profile": "alpha"},
            )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    data = c.get("/api/operator/autonomy/alpha").json()
    trend = data["trends"]["auto_merge"]
    today_entry = next(d for d in trend if d["sample"] > 0)
    # 1 merged / 2 eligible
    assert today_entry["value"] == 0.5
    assert today_entry["sample"] == 2


def test_autonomy_ticket_type_breakdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    from datetime import UTC, datetime, timedelta

    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        for i, tt in enumerate(["bug", "feature", "chore"]):
            upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id=f"T-{i}",
                    pr_number=i + 10,
                    repo_full_name="acme/widgets",
                    pr_url=f"https://example.test/pr/{i + 10}",
                    head_sha=f"sha{i}",
                    client_profile="alpha",
                    opened_at=recent,
                    ticket_type=tt,
                    first_pass_accepted=1,
                    merged=1,
                ),
            )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    data = c.get("/api/operator/autonomy/alpha").json()
    types = sorted({row["ticket_type"] for row in data["by_type"]})
    assert types == ["bug", "chore", "feature"]


# ---------- /api/operator/pr/{pr_run_id} ----------


def test_pr_detail_404_when_missing(client: TestClient) -> None:
    r = client.get("/api/operator/pr/999999")
    assert r.status_code == 404


def test_pr_detail_shapes_pr_row_and_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autonomy_store import insert_review_issue

    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        pr_run_id = upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id="T-42",
                pr_number=100,
                repo_full_name="acme/widgets",
                pr_url="https://example.test/pr/100",
                head_sha="sha100",
                client_profile="alpha",
                opened_at="2026-04-18T12:00:00+00:00",
                first_pass_accepted=0,
                merged=0,
            ),
        )
        insert_review_issue(
            conn,
            pr_run_id=pr_run_id,
            source="ai_review",
            file_path="src/foo.py",
            line_start=10,
            category="correctness",
            severity="major",
            summary="Unbounded loop risk",
            is_valid=1,
        )
        insert_review_issue(
            conn,
            pr_run_id=pr_run_id,
            source="ai_review",
            file_path="src/bar.py",
            category="style",
            severity="minor",
            summary="Missing trailing newline",
            is_valid=1,
        )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    r = c.get(f"/api/operator/pr/{pr_run_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["pr_run_id"] == pr_run_id
    assert data["ticket_id"] == "T-42"
    assert data["pr_number"] == 100
    # Issues sorted with major before minor.
    assert [i["severity"] for i in data["issues"]] == ["major", "minor"]
    assert data["issues"][0]["summary"] == "Unbounded loop risk"
    assert data["ci_checks_available"] is False


# ---------- /api/operator/tickets/{id}/agents ----------


def test_agents_empty_when_no_worktree(client: TestClient) -> None:
    r = client.get("/api/operator/tickets/HARN-NONE/agents")
    assert r.status_code == 200
    assert r.json() == {"agents": []}


def test_agents_lists_teammates_with_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    # Build a fake worktree tree with two session-stream.jsonl files:
    # one recent (running), one old (stale).
    worktree = tmp_path / "wt" / "HARN-ROSTER"
    main_stream = worktree / ".harness" / "logs" / "session-stream.jsonl"
    main_stream.parent.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    main_stream.write_text(json.dumps({"timestamp": now, "type": "system"}) + "\n")

    sub_stream = (
        worktree / ".claude" / "worktrees" / "dev-01" / ".harness" / "logs"
        / "session-stream.jsonl"
    )
    sub_stream.parent.mkdir(parents=True)
    old = "2024-01-01T00:00:00+00:00"
    sub_stream.write_text(json.dumps({"timestamp": old, "type": "system"}) + "\n")

    import live_stream as ls

    monkeypatch.setattr(
        ls, "_worktree_root_for_ticket", lambda _tid: worktree
    )
    # operator_api_data imports _worktree_root_for_ticket from live_stream
    # at module load — patch both sites.
    import operator_api_data as oad

    monkeypatch.setattr(oad, "_worktree_root_for_ticket", lambda _tid: worktree)

    c = TestClient(_mk_app())
    data = c.get("/api/operator/tickets/HARN-ROSTER/agents").json()
    agents = data["agents"]
    assert len(agents) == 2
    states = {a["teammate"]: a["state"] for a in agents}
    # At least one running (from recent), one stale (from 2024 timestamp).
    assert any(v == "running" for v in states.values())
    assert any(v == "stale" for v in states.values())


def test_pr_detail_surfaces_auto_merge_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        pr_run_id = upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id="T-1",
                pr_number=7,
                repo_full_name="acme/widgets",
                pr_url="https://example.test/pr/7",
                head_sha="sha7",
                client_profile="alpha",
                opened_at="2026-04-18T12:00:00+00:00",
            ),
        )
        record_auto_merge_decision(
            conn,
            repo_full_name="acme/widgets",
            pr_number=7,
            decision="hold",
            reason="pending playwright smoke",
            payload={
                "client_profile": "alpha",
                "confidence": 0.43,
                "gates": {
                    "ci_passed": False,
                    "review_clean": True,
                    "is_mergeable": True,
                },
            },
        )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    data = c.get(f"/api/operator/pr/{pr_run_id}").json()
    am = data["auto_merge"]
    assert am["decision"] == "hold"
    assert am["reason"] == "pending playwright smoke"
    assert am["confidence"] == 0.43
    assert am["gates"]["ci_passed"] is False


# ---------- /api/operator/lessons/counts ----------


def test_lesson_counts_empty(client: TestClient) -> None:
    r = client.get("/api/operator/lessons/counts")
    assert r.status_code == 200
    counts = r.json()["counts"]
    expected_keys = {
        "proposed",
        "draft_ready",
        "approved",
        "applied",
        "snoozed",
        "rejected",
        "reverted",
        "stale",
    }
    assert set(counts.keys()) == expected_keys
    assert all(v == 0 for v in counts.values())


def test_lesson_counts_tallies_every_state(
    client: TestClient, tmp_path: Path
) -> None:
    from autonomy_store.lessons import update_lesson_status

    db_path = Path(settings.autonomy_db_path)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        # Seed 4 lessons as "proposed" (only entry state).
        for i in range(4):
            upsert_lesson_candidate(
                conn,
                LessonCandidateUpsert(
                    lesson_id=f"LSN-{i:04x}",
                    client_profile="alpha",
                    platform_profile="salesforce",
                    detector_name="test-detector",
                    pattern_key=f"test|{i}",
                    scope_key=f"scope|{i}",
                ),
            )
        # Transition 2 through the valid state machine:
        #   proposed → draft_ready → approved → applied
        update_lesson_status(conn, "LSN-0002", "draft_ready")
        update_lesson_status(conn, "LSN-0002", "approved")
        update_lesson_status(conn, "LSN-0002", "applied")
        update_lesson_status(conn, "LSN-0003", "rejected")
    finally:
        conn.close()

    counts = client.get("/api/operator/lessons/counts").json()["counts"]
    # 2 still in proposed (0 and 1), 1 applied (2), 1 rejected (3).
    assert counts["proposed"] == 2
    assert counts["applied"] == 1
    assert counts["rejected"] == 1
    assert counts["approved"] == 0
    assert counts["draft_ready"] == 0
