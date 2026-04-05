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
from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    insert_pending_ai_issue,
    insert_review_issue,
    open_connection,
    upsert_pr_run,
)
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
    # Phase 2: small samples classified as insufficient_data
    assert body["data_quality"]["status"] in ("insufficient_data", "degraded", "good")


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


# ---------------------------------------------------------------------------
# Drain pending_ai_issues on pr_opened / pr_synchronized
# ---------------------------------------------------------------------------

def _stage_pending_ai_issue(
    db_path: Path,
    *,
    repo_full_name: str = "acme/widgets",
    head_sha: str = "abc123",
    ticket_id: str = "SCRUM-1",
    source: str = "ai_review",
    external_id: str = "ai-1",
    file_path: str = "src/foo.py",
    line_start: int = 10,
    line_end: int = 12,
    category: str = "bug",
    summary: str = "null pointer risk",
) -> None:
    c = open_connection(db_path)
    try:
        ensure_schema(c)
        insert_pending_ai_issue(
            c,
            repo_full_name=repo_full_name,
            head_sha=head_sha,
            ticket_id=ticket_id,
            source=source,
            external_id=external_id,
            file_path=file_path,
            line_start=line_start,
            line_end=line_end,
            category=category,
            severity="high",
            summary=summary,
            details="",
            acceptance_criterion_ref="",
            is_valid=1,
            is_code_change_request=0,
        )
    finally:
        c.close()


class TestDrainOnPrOpened:
    def test_pr_opened_drains_pending_ai_issues(self, test_app: FastAPI) -> None:
        db_path = Path(settings.autonomy_db_path)
        _stage_pending_ai_issue(db_path, external_id="ai-1")
        _stage_pending_ai_issue(db_path, external_id="ai-2")

        client = TestClient(test_app)
        r = client.post(
            "/api/internal/autonomy/events",
            json=_payload(
                event_type="pr_opened", client_profile="harness-test"
            ),
            headers={"X-Internal-Api-Token": "test-token"},
        )
        assert r.status_code == 200
        pr_run_id = r.json()["pr_run_id"]

        c = open_connection(db_path)
        try:
            rows = c.execute(
                "SELECT * FROM review_issues WHERE pr_run_id = ? AND source = 'ai_review'",
                (pr_run_id,),
            ).fetchall()
            assert len(rows) == 2
            pending = c.execute(
                "SELECT COUNT(*) AS n FROM pending_ai_issues"
            ).fetchone()
            assert pending["n"] == 0
        finally:
            c.close()

    def test_pr_synchronized_also_drains(self, test_app: FastAPI) -> None:
        db_path = Path(settings.autonomy_db_path)
        _stage_pending_ai_issue(db_path, external_id="ai-1")

        client = TestClient(test_app)
        r = client.post(
            "/api/internal/autonomy/events",
            json=_payload(
                event_type="pr_synchronized", client_profile="harness-test"
            ),
            headers={"X-Internal-Api-Token": "test-token"},
        )
        assert r.status_code == 200
        pr_run_id = r.json()["pr_run_id"]

        c = open_connection(db_path)
        try:
            rows = c.execute(
                "SELECT * FROM review_issues WHERE pr_run_id = ? AND source = 'ai_review'",
                (pr_run_id,),
            ).fetchall()
            assert len(rows) == 1
        finally:
            c.close()

    def test_drain_is_idempotent_on_repeat(self, test_app: FastAPI) -> None:
        db_path = Path(settings.autonomy_db_path)
        _stage_pending_ai_issue(db_path, external_id="ai-1")
        _stage_pending_ai_issue(db_path, external_id="ai-2")

        client = TestClient(test_app)
        for _ in range(2):
            r = client.post(
                "/api/internal/autonomy/events",
                json=_payload(
                    event_type="pr_opened", client_profile="harness-test"
                ),
                headers={"X-Internal-Api-Token": "test-token"},
            )
            assert r.status_code == 200
        pr_run_id = r.json()["pr_run_id"]

        c = open_connection(db_path)
        try:
            rows = c.execute(
                "SELECT * FROM review_issues WHERE pr_run_id = ? AND source = 'ai_review'",
                (pr_run_id,),
            ).fetchall()
            assert len(rows) == 2
        finally:
            c.close()

    def test_drain_triggers_rematch(self, test_app: FastAPI) -> None:
        db_path = Path(settings.autonomy_db_path)

        # Pre-seed a pr_run via upsert + a human_review issue
        c = open_connection(db_path)
        try:
            ensure_schema(c)
            pr_run_id = upsert_pr_run(
                c,
                PrRunUpsert(
                    ticket_id="SCRUM-1",
                    pr_number=1,
                    repo_full_name="acme/widgets",
                    head_sha="abc123",
                    client_profile="harness-test",
                    opened_at="2026-04-05T11:00:00+00:00",
                ),
            )
            insert_review_issue(
                c,
                pr_run_id=pr_run_id,
                source="human_review",
                external_id="human-1",
                file_path="src/foo.py",
                line_start=10,
                line_end=12,
                summary="null pointer risk here",
                is_valid=1,
            )
        finally:
            c.close()

        # Stage an AI issue that should match on line overlap
        _stage_pending_ai_issue(
            db_path,
            external_id="ai-1",
            file_path="src/foo.py",
            line_start=10,
            line_end=12,
            summary="null pointer risk",
        )

        client = TestClient(test_app)
        r = client.post(
            "/api/internal/autonomy/events",
            json=_payload(
                event_type="pr_opened", client_profile="harness-test"
            ),
            headers={"X-Internal-Api-Token": "test-token"},
        )
        assert r.status_code == 200

        c = open_connection(db_path)
        try:
            matches = c.execute("SELECT * FROM issue_matches").fetchall()
            assert len(matches) == 1
            assert matches[0]["match_type"] == "line_overlap"
        finally:
            c.close()


# ---------------------------------------------------------------------------
# HTTP endpoint: POST /api/internal/autonomy/human-issues
# ---------------------------------------------------------------------------

def _human_payload(**overrides: Any) -> dict[str, Any]:
    data = {
        "repo_full_name": "acme/widgets",
        "pr_number": 1,
        "head_sha": "abc123",
        "ticket_id": "SCRUM-1",
        "client_profile": "harness-test",
        "external_id": "comment-42",
        "event_type": "review_comment",
        "file_path": "src/foo.py",
        "line_start": 10,
        "line_end": 12,
        "summary": "consider null-checking here",
        "details": "this variable could be None",
        "reviewer_login": "alice",
        "event_at": "2026-04-05T12:00:00+00:00",
        "comment_url": "https://github.com/acme/widgets/pull/1#discussion_r42",
    }
    data.update(overrides)
    return data


class TestHumanIssueEndpoint:
    def test_auth_required(self, test_app: FastAPI) -> None:
        client = TestClient(test_app)
        r = client.post(
            "/api/internal/autonomy/human-issues", json=_human_payload()
        )
        assert r.status_code == 401

    def test_fail_closed_when_token_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "autonomy_db_path", str(tmp_path / "a.db"))
        monkeypatch.setattr(settings, "l1_internal_api_token", "")
        autonomy_ingest._bucket = TokenBucket(capacity=100, refill_per_sec=100.0)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        r = client.post(
            "/api/internal/autonomy/human-issues",
            json=_human_payload(),
            headers={"X-Internal-Api-Token": "anything"},
        )
        assert r.status_code == 503

    def test_oversize_413(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "autonomy_db_path", str(tmp_path / "a.db"))
        monkeypatch.setattr(settings, "l1_internal_api_token", "test-token")
        monkeypatch.setattr(settings, "autonomy_internal_max_body_bytes", 50)
        autonomy_ingest._bucket = TokenBucket(capacity=100, refill_per_sec=100.0)
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        big = _human_payload(details="x" * 500)
        r = client.post(
            "/api/internal/autonomy/human-issues",
            json=big,
            headers={"X-Internal-Api-Token": "test-token"},
        )
        assert r.status_code == 413

    def test_creates_human_issue_and_pr_run(self, test_app: FastAPI) -> None:
        client = TestClient(test_app)
        r = client.post(
            "/api/internal/autonomy/human-issues",
            json=_human_payload(),
            headers={"X-Internal-Api-Token": "test-token"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "accepted"
        assert body["action"] == "inserted"
        assert body["human_issue_id"] > 0
        assert body["pr_run_id"] > 0

        db_path = Path(settings.autonomy_db_path)
        c = open_connection(db_path)
        try:
            pr = c.execute(
                "SELECT * FROM pr_runs WHERE id = ?", (body["pr_run_id"],)
            ).fetchone()
            assert pr is not None
            assert pr["ticket_id"] == "SCRUM-1"
            issue = c.execute(
                "SELECT * FROM review_issues WHERE id = ?",
                (body["human_issue_id"],),
            ).fetchone()
            assert issue["source"] == "human_review"
            assert issue["external_id"] == "comment-42"
            assert issue["is_valid"] == 1
            assert issue["is_code_change_request"] == 0
            assert issue["source_ref"] == (
                "https://github.com/acme/widgets/pull/1#discussion_r42"
            )
        finally:
            c.close()

    def test_updates_existing_human_issue_on_repeat_external_id(
        self, test_app: FastAPI
    ) -> None:
        client = TestClient(test_app)
        r1 = client.post(
            "/api/internal/autonomy/human-issues",
            json=_human_payload(summary="first version"),
            headers={"X-Internal-Api-Token": "test-token"},
        )
        assert r1.status_code == 200
        first_id = r1.json()["human_issue_id"]

        r2 = client.post(
            "/api/internal/autonomy/human-issues",
            json=_human_payload(summary="edited version"),
            headers={"X-Internal-Api-Token": "test-token"},
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body["action"] == "updated"
        assert body["human_issue_id"] == first_id

        db_path = Path(settings.autonomy_db_path)
        c = open_connection(db_path)
        try:
            rows = c.execute(
                "SELECT * FROM review_issues WHERE source='human_review' "
                "AND external_id=?",
                ("comment-42",),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["summary"] == "edited version"
        finally:
            c.close()

    def test_changes_requested_sets_is_code_change_request(
        self, test_app: FastAPI
    ) -> None:
        client = TestClient(test_app)
        r = client.post(
            "/api/internal/autonomy/human-issues",
            json=_human_payload(
                event_type="review_changes_requested",
                external_id="review-99",
            ),
            headers={"X-Internal-Api-Token": "test-token"},
        )
        assert r.status_code == 200
        body = r.json()
        db_path = Path(settings.autonomy_db_path)
        c = open_connection(db_path)
        try:
            issue = c.execute(
                "SELECT * FROM review_issues WHERE id = ?",
                (body["human_issue_id"],),
            ).fetchone()
            assert issue["is_code_change_request"] == 1
        finally:
            c.close()

    def test_review_comment_defaults_flag_zero(self, test_app: FastAPI) -> None:
        client = TestClient(test_app)
        r = client.post(
            "/api/internal/autonomy/human-issues",
            json=_human_payload(event_type="review_comment"),
            headers={"X-Internal-Api-Token": "test-token"},
        )
        assert r.status_code == 200
        body = r.json()
        db_path = Path(settings.autonomy_db_path)
        c = open_connection(db_path)
        try:
            issue = c.execute(
                "SELECT * FROM review_issues WHERE id = ?",
                (body["human_issue_id"],),
            ).fetchone()
            assert issue["is_code_change_request"] == 0
        finally:
            c.close()

    def test_match_runs_after_insert(self, test_app: FastAPI) -> None:
        db_path = Path(settings.autonomy_db_path)

        # Pre-seed a pr_run + AI issue that should match the incoming human
        c = open_connection(db_path)
        try:
            ensure_schema(c)
            pr_run_id = upsert_pr_run(
                c,
                PrRunUpsert(
                    ticket_id="SCRUM-1",
                    pr_number=1,
                    repo_full_name="acme/widgets",
                    head_sha="abc123",
                    client_profile="harness-test",
                    opened_at="2026-04-05T11:00:00+00:00",
                ),
            )
            insert_review_issue(
                c,
                pr_run_id=pr_run_id,
                source="ai_review",
                external_id="ai-1",
                file_path="src/foo.py",
                line_start=10,
                line_end=12,
                summary="null pointer risk",
                is_valid=1,
            )
        finally:
            c.close()

        client = TestClient(test_app)
        r = client.post(
            "/api/internal/autonomy/human-issues",
            json=_human_payload(
                file_path="src/foo.py", line_start=10, line_end=12,
                summary="null pointer risk here",
            ),
            headers={"X-Internal-Api-Token": "test-token"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["match_summary"]["auto_matched"] == 1

        c = open_connection(db_path)
        try:
            matches = c.execute("SELECT * FROM issue_matches").fetchall()
            assert len(matches) == 1
            assert matches[0]["match_type"] == "line_overlap"
        finally:
            c.close()


# ---------------------------------------------------------------------------
# Phase 3 admin endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "autonomy_admin_token", "admin-token")
    monkeypatch.setattr(settings, "autonomy_internal_max_body_bytes", 262_144)
    autonomy_ingest._bucket = TokenBucket(capacity=100, refill_per_sec=100.0)
    app = FastAPI()
    app.include_router(router)
    return app


def _seed_pr_for_admin(db_path: Path) -> int:
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        pr_id = upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id="RW-1",
                pr_number=1,
                repo_full_name="acme/widgets",
                head_sha="sha1",
                client_profile="rockwell",
                opened_at="2026-04-01T00:00:00+00:00",
                merged=1,
                merged_at="2026-04-02T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()
    return pr_id


def test_manual_defect_401_without_token(admin_app: FastAPI, tmp_path: Path) -> None:
    _seed_pr_for_admin(Path(settings.autonomy_db_path))
    c = TestClient(admin_app)
    r = c.post(
        "/api/autonomy/manual-defect",
        json={
            "pr_run_id": 1,
            "defect_key": "BUG-1",
            "reported_at": "2026-04-03T00:00:00+00:00",
        },
    )
    assert r.status_code == 401


def test_manual_defect_503_when_token_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "autonomy_db_path", str(tmp_path / "a.db"))
    monkeypatch.setattr(settings, "autonomy_admin_token", "")
    autonomy_ingest._bucket = TokenBucket(capacity=100, refill_per_sec=100.0)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    r = c.post(
        "/api/autonomy/manual-defect",
        json={"pr_run_id": 1, "defect_key": "X", "reported_at": "x"},
        headers={"X-Autonomy-Admin-Token": "anything"},
    )
    assert r.status_code == 503


def test_manual_defect_happy_path(admin_app: FastAPI) -> None:
    pr_id = _seed_pr_for_admin(Path(settings.autonomy_db_path))
    c = TestClient(admin_app)
    r = c.post(
        "/api/autonomy/manual-defect",
        json={
            "pr_run_id": pr_id,
            "defect_key": "BUG-1",
            "source": "jira",
            "severity": "high",
            "reported_at": "2026-04-03T00:00:00+00:00",
            "confirmed": True,
            "category": "escaped",
        },
        headers={"X-Autonomy-Admin-Token": "admin-token"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["pr_run_id"] == pr_id
    assert body["defect_link_id"] > 0


def test_manual_defect_404_unknown_pr_run(admin_app: FastAPI) -> None:
    c = TestClient(admin_app)
    r = c.post(
        "/api/autonomy/manual-defect",
        json={
            "pr_run_id": 9999,
            "defect_key": "BUG-1",
            "reported_at": "2026-04-03T00:00:00+00:00",
        },
        headers={"X-Autonomy-Admin-Token": "admin-token"},
    )
    assert r.status_code == 404


def test_manual_defect_by_repo_tuple(admin_app: FastAPI) -> None:
    _seed_pr_for_admin(Path(settings.autonomy_db_path))
    c = TestClient(admin_app)
    r = c.post(
        "/api/autonomy/manual-defect",
        json={
            "repo_full_name": "acme/widgets",
            "pr_number": 1,
            "head_sha": "sha1",
            "defect_key": "BUG-2",
            "reported_at": "2026-04-03T00:00:00+00:00",
        },
        headers={"X-Autonomy-Admin-Token": "admin-token"},
    )
    assert r.status_code == 200


def test_manual_defect_422_missing_lookup(admin_app: FastAPI) -> None:
    c = TestClient(admin_app)
    r = c.post(
        "/api/autonomy/manual-defect",
        json={
            "defect_key": "BUG-1",
            "reported_at": "2026-04-03T00:00:00+00:00",
        },
        headers={"X-Autonomy-Admin-Token": "admin-token"},
    )
    assert r.status_code == 422


def test_manual_match_promote_happy_path(admin_app: FastAPI) -> None:
    db_path = Path(settings.autonomy_db_path)
    pr_id = _seed_pr_for_admin(db_path)
    conn = open_connection(db_path)
    try:
        h = insert_review_issue(
            conn, pr_run_id=pr_id, source="human_review",
            external_id="h", summary="x", is_valid=1,
        )
        a = insert_review_issue(
            conn, pr_run_id=pr_id, source="ai_review",
            external_id="a", summary="x", is_valid=1,
        )
        from autonomy_store import insert_issue_match
        match_id = insert_issue_match(
            conn,
            human_issue_id=h,
            ai_issue_id=a,
            match_type="semantic_weak",
            confidence=0.7,
            matched_by="suggested",
        )
    finally:
        conn.close()
    c = TestClient(admin_app)
    r = c.post(
        "/api/autonomy/manual-match",
        json={"mode": "promote", "match_id": match_id},
        headers={"X-Autonomy-Admin-Token": "admin-token"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["match_id"] == match_id


def test_manual_match_promote_409_when_not_suggested(admin_app: FastAPI) -> None:
    db_path = Path(settings.autonomy_db_path)
    pr_id = _seed_pr_for_admin(db_path)
    conn = open_connection(db_path)
    try:
        h = insert_review_issue(
            conn, pr_run_id=pr_id, source="human_review",
            external_id="h", summary="x", is_valid=1,
        )
        a = insert_review_issue(
            conn, pr_run_id=pr_id, source="ai_review",
            external_id="a", summary="x", is_valid=1,
        )
        from autonomy_store import insert_issue_match
        match_id = insert_issue_match(
            conn,
            human_issue_id=h,
            ai_issue_id=a,
            match_type="exact_line",
            confidence=0.95,
            matched_by="system",
        )
    finally:
        conn.close()
    c = TestClient(admin_app)
    r = c.post(
        "/api/autonomy/manual-match",
        json={"mode": "promote", "match_id": match_id},
        headers={"X-Autonomy-Admin-Token": "admin-token"},
    )
    assert r.status_code == 409


def test_manual_match_create_happy_path(admin_app: FastAPI) -> None:
    db_path = Path(settings.autonomy_db_path)
    pr_id = _seed_pr_for_admin(db_path)
    conn = open_connection(db_path)
    try:
        h = insert_review_issue(
            conn, pr_run_id=pr_id, source="human_review",
            external_id="h", summary="x", is_valid=1,
        )
        a = insert_review_issue(
            conn, pr_run_id=pr_id, source="ai_review",
            external_id="a", summary="x", is_valid=1,
        )
    finally:
        conn.close()
    c = TestClient(admin_app)
    r = c.post(
        "/api/autonomy/manual-match",
        json={"mode": "create", "human_issue_id": h, "ai_issue_id": a},
        headers={"X-Autonomy-Admin-Token": "admin-token"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["match_id"] > 0


def test_manual_match_create_422_on_validation_error(admin_app: FastAPI) -> None:
    c = TestClient(admin_app)
    r = c.post(
        "/api/autonomy/manual-match",
        json={"mode": "create", "human_issue_id": 9999, "ai_issue_id": 9998},
        headers={"X-Autonomy-Admin-Token": "admin-token"},
    )
    assert r.status_code == 422


def test_defect_sweep_heartbeat_happy_path(admin_app: FastAPI) -> None:
    c = TestClient(admin_app)
    r = c.post(
        "/api/autonomy/defect-sweep-heartbeat",
        json={
            "client_profile": "rockwell",
            "swept_through": "2026-04-05T00:00:00+00:00",
        },
        headers={"X-Autonomy-Admin-Token": "admin-token"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
