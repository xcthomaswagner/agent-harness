"""Tests for autonomy_dashboard — /autonomy HTML rendering + multi-profile isolation."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autonomy_dashboard import router
from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    insert_issue_match,
    insert_review_issue,
    open_connection,
    upsert_pr_run,
)
from config import settings


def _mk_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _seed(db_path: Path, rows: list[dict]) -> None:
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


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    return TestClient(_mk_app())


def test_autonomy_page_returns_200_html(client: TestClient) -> None:
    r = client.get("/autonomy")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_autonomy_page_has_profile_selector(client: TestClient) -> None:
    r = client.get("/autonomy")
    assert r.status_code == 200
    assert "Project:" in r.text
    assert 'href="/autonomy"' in r.text


def test_autonomy_all_view_renders_per_profile_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    # rockwell: 2 PRs, both first-pass → 100%
    # harness-test: 2 PRs, one first-pass → 50%
    rows = [
        {"client_profile": "rockwell", "first_pass_accepted": 1, "merged": 1},
        {"client_profile": "rockwell", "first_pass_accepted": 1, "merged": 1},
        {"client_profile": "harness-test", "first_pass_accepted": 1, "merged": 0},
        {"client_profile": "harness-test", "first_pass_accepted": 0, "merged": 0},
    ]
    _seed(db_path, rows)
    c = TestClient(_mk_app())
    r = c.get("/autonomy")
    assert r.status_code == 200
    # Both profiles rendered as separate cards
    assert "rockwell" in r.text
    assert "harness-test" in r.text
    # Per-profile percentages visible
    assert "100%" in r.text
    assert "50%" in r.text


def test_autonomy_all_view_no_global_averaged_fpa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§14a.3: All view MUST NOT show a single averaged FPA.

    rockwell: 1/1 = 100%; harness-test: 0/1 = 0%. Average would be 50%.
    The summary line shows total PR count (2) but must not show 50% as a
    single averaged headline metric for the "All" view.
    """
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    rows = [
        {"client_profile": "rockwell", "first_pass_accepted": 1, "merged": 1},
        {"client_profile": "harness-test", "first_pass_accepted": 0, "merged": 0},
    ]
    _seed(db_path, rows)
    c = TestClient(_mk_app())
    r = c.get("/autonomy")
    assert r.status_code == 200
    text = r.text
    # Per-profile values still present
    assert "100%" in text
    assert "0%" in text
    # Must NOT contain a "Global" averaged FPA headline
    assert "Global First-Pass" not in text
    assert "Global FPA" not in text
    # 50% (the average) must not appear anywhere
    assert "50%" not in text


def test_autonomy_filter_single_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    rows = [
        {
            "client_profile": "rockwell",
            "ticket_id": "RW-1",
            "first_pass_accepted": 1,
            "merged": 1,
        },
        {
            "client_profile": "harness-test",
            "ticket_id": "HT-1",
            "first_pass_accepted": 0,
            "merged": 0,
        },
    ]
    _seed(db_path, rows)
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    # Only rockwell's ticket should be in the table
    assert "RW-1" in r.text
    assert "HT-1" not in r.text


def test_autonomy_link_to_traces_present(client: TestClient) -> None:
    r = client.get("/autonomy")
    assert r.status_code == 200
    assert 'href="/traces"' in r.text


def _seed_pr_run(
    db_path: Path,
    *,
    profile: str,
    ticket_id: str = "TK-1",
    pr_number: int = 1,
    head_sha: str = "sha1",
    first_pass_accepted: int = 1,
    merged: int = 0,
) -> int:
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        pr_run_id = upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id=ticket_id,
                pr_number=pr_number,
                repo_full_name="acme/widgets",
                head_sha=head_sha,
                client_profile=profile,
                opened_at="2026-04-01T12:00:00+00:00",
                first_pass_accepted=first_pass_accepted,
                merged=merged,
            ),
        )
    finally:
        conn.close()
    return pr_run_id


def test_self_review_catch_renders_when_humans_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    pr_run_id = _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    conn = open_connection(db_path)
    try:
        human_id = insert_review_issue(
            conn,
            pr_run_id=pr_run_id,
            source="human_review",
            external_id="h1",
            file_path="app.py",
            line_start=10,
            line_end=12,
            summary="Null check missing",
            is_valid=1,
        )
        ai_id = insert_review_issue(
            conn,
            pr_run_id=pr_run_id,
            source="ai_review",
            external_id="a1",
            file_path="app.py",
            line_start=10,
            line_end=12,
            summary="Null check missing",
            is_valid=1,
        )
        insert_issue_match(
            conn,
            human_issue_id=human_id,
            ai_issue_id=ai_id,
            match_type="exact_line",
            confidence=0.95,
            matched_by="system",
        )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    # catch rate of 100% should render; dash should not be the catch value
    assert "100%" in r.text
    assert "Self-review catch" in r.text


def test_self_review_catch_shows_dash_when_no_humans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_pr_run(db_path, profile="rockwell")
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "Self-review catch" in r.text
    # An em-dash appears for the catch rate row
    assert "—" in r.text


def test_sidecar_coverage_computed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    pr1 = _seed_pr_run(
        db_path, profile="rockwell", ticket_id="RW-1", pr_number=1, head_sha="s1"
    )
    _seed_pr_run(
        db_path, profile="rockwell", ticket_id="RW-2", pr_number=2, head_sha="s2"
    )
    conn = open_connection(db_path)
    try:
        insert_review_issue(
            conn,
            pr_run_id=pr1,
            source="ai_review",
            external_id="a1",
            summary="Some AI issue",
            is_valid=1,
        )
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "Sidecar coverage" in r.text
    assert "50%" in r.text


def test_unmatched_issues_section_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    pr_run_id = _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    conn = open_connection(db_path)
    try:
        insert_review_issue(
            conn,
            pr_run_id=pr_run_id,
            source="human_review",
            external_id="h1",
            file_path="app.py",
            line_start=42,
            line_end=42,
            summary="Missing error handling in handler",
            is_valid=1,
        )
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "Unmatched Human Issues" in r.text
    assert "Missing error handling in handler" in r.text


def test_suggested_matches_section_shows_tier4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    pr_run_id = _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    conn = open_connection(db_path)
    try:
        human_id = insert_review_issue(
            conn,
            pr_run_id=pr_run_id,
            source="human_review",
            external_id="h1",
            summary="Possibly wrong return type",
            is_valid=1,
        )
        ai_id = insert_review_issue(
            conn,
            pr_run_id=pr_run_id,
            source="ai_review",
            external_id="a1",
            summary="Return type may be incorrect",
            is_valid=1,
        )
        insert_issue_match(
            conn,
            human_issue_id=human_id,
            ai_issue_id=ai_id,
            match_type="semantic_weak",
            confidence=0.70,
            matched_by="suggested",
        )
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "Suggested Matches" in r.text
    assert "Possibly wrong return type" in r.text
    assert "Return type may be incorrect" in r.text


def test_data_quality_notes_shown_on_low_sidecar_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    # Seed 5 PRs, none with AI issues → sidecar_coverage = 0 < 0.8
    for i in range(5):
        _seed_pr_run(
            db_path,
            profile="rockwell",
            ticket_id=f"RW-{i}",
            pr_number=i + 1,
            head_sha=f"s{i}",
        )
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "low_sidecar_coverage" in r.text


def test_autonomy_escapes_profile_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    rows = [
        {
            "client_profile": "<script>alert(1)</script>",
            "first_pass_accepted": 1,
            "merged": 1,
        },
    ]
    _seed(db_path, rows)
    c = TestClient(_mk_app())
    r = c.get("/autonomy")
    assert r.status_code == 200
    # Raw script tag must not appear
    assert "<script>alert(1)</script>" not in r.text
    # Escaped form must appear
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
