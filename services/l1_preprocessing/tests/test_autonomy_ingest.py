"""Tests for autonomy_ingest — TokenBucket, resolution, events, HTTP endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import autonomy_ingest
from autonomy_ingest import (
    AutonomyEventIn,
    TokenBucket,
    apply_event,
    resolve_client_profile,
    router,
)
from autonomy_store import ensure_schema, open_connection
from config import settings

# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------

def test_token_bucket_starts_full() -> None:
    bucket = TokenBucket(capacity=3, refill_per_sec=1.0)
    assert bucket.try_consume() is True
    assert bucket.try_consume() is True
    assert bucket.try_consume() is True


def test_token_bucket_returns_false_when_empty() -> None:
    bucket = TokenBucket(capacity=2, refill_per_sec=0.0)
    assert bucket.try_consume() is True
    assert bucket.try_consume() is True
    assert bucket.try_consume() is False


def test_token_bucket_refills_over_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # Control monotonic clock
    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(autonomy_ingest.time, "monotonic", fake_monotonic)

    bucket = TokenBucket(capacity=2, refill_per_sec=1.0)
    assert bucket.try_consume() is True
    assert bucket.try_consume() is True
    assert bucket.try_consume() is False

    # Advance time by 1.5 seconds -> refill 1.5 tokens (should allow 1)
    fake_now[0] += 1.5
    assert bucket.try_consume() is True
    # Only 0.5 tokens remain
    assert bucket.try_consume() is False


# ---------------------------------------------------------------------------
# resolve_client_profile
# ---------------------------------------------------------------------------

def test_resolve_client_profile_supplied_is_trusted() -> None:
    name, degraded = resolve_client_profile("SCRUM-42", "my-profile")
    assert name == "my-profile"
    assert degraded is False


def test_resolve_client_profile_looks_up_by_project_key() -> None:
    # SCRUM project_key maps to harness-test profile
    name, degraded = resolve_client_profile("SCRUM-42", "")
    assert degraded is False
    assert name != ""


def test_resolve_client_profile_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        autonomy_ingest, "find_profile_by_project_key", lambda _key: None
    )
    name, degraded = resolve_client_profile("UNKNOWN-1", "")
    assert name == ""
    assert degraded is True


def test_resolve_client_profile_no_hyphen() -> None:
    name, degraded = resolve_client_profile("bogus", "")
    assert name == ""
    assert degraded is True


# ---------------------------------------------------------------------------
# apply_event
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path: Path) -> Any:
    db_path = tmp_path / "test.db"
    c = open_connection(db_path)
    ensure_schema(c)
    yield c
    c.close()


def _base_event(**overrides: Any) -> AutonomyEventIn:
    data = {
        "event_type": "pr_opened",
        "repo_full_name": "acme/widgets",
        "pr_number": 1,
        "pr_url": "https://github.com/acme/widgets/pull/1",
        "head_ref": "feature/foo",
        "head_sha": "abc123",
        "base_sha": "def456",
        "ticket_id": "SCRUM-1",
        "ticket_type": "story",
        "client_profile": "harness-test",
        "event_at": "2026-04-05T12:00:00+00:00",
    }
    data.update(overrides)
    return AutonomyEventIn(**data)


def test_apply_event_pr_opened_creates_row(conn: Any) -> None:
    event = _base_event(event_type="pr_opened")
    pr_run_id = apply_event(conn, event, "harness-test")
    assert pr_run_id > 0

    row = conn.execute("SELECT * FROM pr_runs WHERE id = ?", (pr_run_id,)).fetchone()
    assert row["client_profile"] == "harness-test"
    assert row["opened_at"] == "2026-04-05T12:00:00+00:00"
    assert row["ticket_id"] == "SCRUM-1"


def test_apply_event_review_approved_fresh_sets_first_pass_accepted(conn: Any) -> None:
    event = _base_event(
        event_type="review_approved",
        event_at="2026-04-05T13:00:00+00:00",
    )
    pr_run_id = apply_event(conn, event, "harness-test")
    row = conn.execute("SELECT * FROM pr_runs WHERE id = ?", (pr_run_id,)).fetchone()
    assert row["first_pass_accepted"] == 1
    assert row["approved_at"] == "2026-04-05T13:00:00+00:00"


def test_apply_event_changes_requested_sets_fpa_zero(conn: Any) -> None:
    # First open the PR
    apply_event(conn, _base_event(event_type="pr_opened"), "harness-test")
    # Then request changes
    event = _base_event(event_type="review_changes_requested")
    apply_event(conn, event, "harness-test")
    row = conn.execute(
        "SELECT * FROM pr_runs WHERE repo_full_name=? AND pr_number=? AND head_sha=?",
        ("acme/widgets", 1, "abc123"),
    ).fetchone()
    assert row["first_pass_accepted"] == 0


def test_apply_event_approved_after_changes_keeps_zero(conn: Any) -> None:
    apply_event(conn, _base_event(event_type="pr_opened"), "harness-test")
    apply_event(conn, _base_event(event_type="review_changes_requested"), "harness-test")
    apply_event(conn, _base_event(event_type="review_approved"), "harness-test")
    row = conn.execute(
        "SELECT * FROM pr_runs WHERE repo_full_name=? AND pr_number=? AND head_sha=?",
        ("acme/widgets", 1, "abc123"),
    ).fetchone()
    assert row["first_pass_accepted"] == 0


def test_apply_event_pr_merged(conn: Any) -> None:
    apply_event(conn, _base_event(event_type="pr_opened"), "harness-test")
    event = _base_event(
        event_type="pr_merged",
        event_at="2026-04-05T14:00:00+00:00",
        merged_at="2026-04-05T14:00:00+00:00",
    )
    apply_event(conn, event, "harness-test")
    row = conn.execute(
        "SELECT * FROM pr_runs WHERE repo_full_name=? AND pr_number=? AND head_sha=?",
        ("acme/widgets", 1, "abc123"),
    ).fetchone()
    assert row["merged"] == 1
    assert row["merged_at"] == "2026-04-05T14:00:00+00:00"


def test_apply_event_pr_synchronized_is_noop_upsert(conn: Any) -> None:
    apply_event(conn, _base_event(event_type="pr_opened"), "harness-test")
    pr_run_id = apply_event(conn, _base_event(event_type="pr_synchronized"), "harness-test")
    row = conn.execute("SELECT * FROM pr_runs WHERE id = ?", (pr_run_id,)).fetchone()
    # State unchanged
    assert row["first_pass_accepted"] == 0
    assert row["merged"] == 0


def test_apply_event_review_comment_is_noop(conn: Any) -> None:
    apply_event(conn, _base_event(event_type="pr_opened"), "harness-test")
    pr_run_id = apply_event(conn, _base_event(event_type="review_comment"), "harness-test")
    row = conn.execute("SELECT * FROM pr_runs WHERE id = ?", (pr_run_id,)).fetchone()
    assert row["first_pass_accepted"] == 0
    assert row["merged"] == 0


# ---------------------------------------------------------------------------
# HTTP endpoint: POST /api/internal/autonomy/events
# ---------------------------------------------------------------------------

@pytest.fixture
def test_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    # Per-test DB + fresh bucket + known token
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "l1_internal_api_token", "test-token")
    monkeypatch.setattr(settings, "autonomy_internal_max_body_bytes", 262_144)
    # Reset rate limiter so tests don't share state
    autonomy_ingest._bucket = TokenBucket(capacity=100, refill_per_sec=100.0)
    app = FastAPI()
    app.include_router(router)
    return app


def _payload(**overrides: Any) -> dict[str, Any]:
    data = {
        "event_type": "pr_opened",
        "repo_full_name": "acme/widgets",
        "pr_number": 1,
        "head_sha": "abc123",
        "ticket_id": "SCRUM-1",
        "event_at": "2026-04-05T12:00:00+00:00",
    }
    data.update(overrides)
    return data


def test_post_event_401_missing_token(test_app: FastAPI) -> None:
    client = TestClient(test_app)
    r = client.post("/api/internal/autonomy/events", json=_payload())
    assert r.status_code == 401


def test_post_event_401_wrong_token(test_app: FastAPI) -> None:
    client = TestClient(test_app)
    r = client.post(
        "/api/internal/autonomy/events",
        json=_payload(),
        headers={"X-Internal-Api-Token": "wrong"},
    )
    assert r.status_code == 401


def test_post_event_503_when_token_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "autonomy_db_path", str(tmp_path / "a.db"))
    monkeypatch.setattr(settings, "l1_internal_api_token", "")
    autonomy_ingest._bucket = TokenBucket(capacity=100, refill_per_sec=100.0)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    r = client.post(
        "/api/internal/autonomy/events",
        json=_payload(),
        headers={"X-Internal-Api-Token": "anything"},
    )
    assert r.status_code == 503


def test_post_event_413_oversized_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "autonomy_db_path", str(tmp_path / "a.db"))
    monkeypatch.setattr(settings, "l1_internal_api_token", "test-token")
    monkeypatch.setattr(settings, "autonomy_internal_max_body_bytes", 50)
    autonomy_ingest._bucket = TokenBucket(capacity=100, refill_per_sec=100.0)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    big_payload = _payload(review_body="x" * 500)
    r = client.post(
        "/api/internal/autonomy/events",
        json=big_payload,
        headers={"X-Internal-Api-Token": "test-token"},
    )
    assert r.status_code == 413


def test_post_event_422_malformed_body(test_app: FastAPI) -> None:
    client = TestClient(test_app)
    # Missing required fields
    r = client.post(
        "/api/internal/autonomy/events",
        json={"event_type": "pr_opened"},
        headers={"X-Internal-Api-Token": "test-token"},
    )
    assert r.status_code == 422


def test_post_event_422_invalid_json(test_app: FastAPI) -> None:
    client = TestClient(test_app)
    r = client.post(
        "/api/internal/autonomy/events",
        content=b"not json{",
        headers={
            "X-Internal-Api-Token": "test-token",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 422


def test_post_event_happy_path_inserts_row(
    test_app: FastAPI, tmp_path: Path
) -> None:
    client = TestClient(test_app)
    r = client.post(
        "/api/internal/autonomy/events",
        json=_payload(client_profile="harness-test"),
        headers={"X-Internal-Api-Token": "test-token"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["pr_run_id"] > 0
    assert body["client_profile"] == "harness-test"

    # Verify DB
    db_path = Path(settings.autonomy_db_path)
    c = open_connection(db_path)
    try:
        row = c.execute("SELECT * FROM pr_runs WHERE id = ?", (body["pr_run_id"],)).fetchone()
        assert row["client_profile"] == "harness-test"
        assert row["ticket_id"] == "SCRUM-1"
    finally:
        c.close()


def test_post_event_approved_sets_fpa_in_db(test_app: FastAPI) -> None:
    client = TestClient(test_app)
    r = client.post(
        "/api/internal/autonomy/events",
        json=_payload(
            event_type="review_approved",
            client_profile="harness-test",
        ),
        headers={"X-Internal-Api-Token": "test-token"},
    )
    assert r.status_code == 200
    body = r.json()

    db_path = Path(settings.autonomy_db_path)
    c = open_connection(db_path)
    try:
        row = c.execute(
            "SELECT * FROM pr_runs WHERE id = ?", (body["pr_run_id"],)
        ).fetchone()
        assert row["first_pass_accepted"] == 1
    finally:
        c.close()


# ---------------------------------------------------------------------------
# HTTP endpoint: GET /api/autonomy
# ---------------------------------------------------------------------------

def test_get_autonomy_empty_returns_empty_profiles(test_app: FastAPI) -> None:
    client = TestClient(test_app)
    r = client.get("/api/autonomy")
    assert r.status_code == 200
    body = r.json()
    assert body["profiles"] == []
    assert body["global_summary"]["profile_count"] == 0
    assert body["global_summary"]["total_sample_size"] == 0
    # Ensure no top-level averaged FPA field
    assert "first_pass_acceptance_rate" not in body


def test_get_autonomy_single_profile_filter(test_app: FastAPI) -> None:
    client = TestClient(test_app)
    # Seed an event
    now_iso = datetime.now(UTC).isoformat()
    client.post(
        "/api/internal/autonomy/events",
        json=_payload(
            event_type="pr_opened",
            client_profile="harness-test",
            event_at=now_iso,
        ),
        headers={"X-Internal-Api-Token": "test-token"},
    )
    r = client.get("/api/autonomy?client_profile=harness-test")
    assert r.status_code == 200
    body = r.json()
    assert body["client_profile"] == "harness-test"
    assert body["sample_size"] == 1
    assert body["data_quality"]["status"] == "phase1_partial"


def test_get_autonomy_list_shape_no_top_level_average(test_app: FastAPI) -> None:
    client = TestClient(test_app)
    now_iso = datetime.now(UTC).isoformat()
    client.post(
        "/api/internal/autonomy/events",
        json=_payload(
            event_type="pr_opened",
            client_profile="harness-test",
            event_at=now_iso,
        ),
        headers={"X-Internal-Api-Token": "test-token"},
    )
    r = client.get("/api/autonomy")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["profiles"], list)
    assert len(body["profiles"]) >= 1
    assert "first_pass_acceptance_rate" not in body
    assert body["global_summary"]["total_sample_size"] >= 1
