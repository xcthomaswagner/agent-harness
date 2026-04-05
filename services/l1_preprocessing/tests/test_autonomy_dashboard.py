"""Tests for autonomy_dashboard — /autonomy HTML rendering + multi-profile isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autonomy_dashboard import _render_sparkline_svg, router
from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    insert_defect_link,
    insert_issue_match,
    insert_review_issue,
    open_connection,
    record_auto_merge_decision,
    set_auto_merge_toggle,
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


# ---------------------------------------------------------------------------
# Phase 3 Step 5/6/7 dashboard tests
# ---------------------------------------------------------------------------

def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


def _seed_merged(
    db_path: Path,
    *,
    profile: str = "rockwell",
    ticket_id: str = "RW-1",
    pr_number: int = 1,
    head_sha: str = "sha1",
    days_ago: int = 5,
    first_pass_accepted: int = 1,
) -> int:
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        pr_id = upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id=ticket_id,
                pr_number=pr_number,
                repo_full_name="acme/widgets",
                head_sha=head_sha,
                client_profile=profile,
                opened_at=_days_ago(days_ago + 1),
                first_pass_accepted=first_pass_accepted,
                merged=1,
                merged_at=_days_ago(days_ago),
            ),
        )
    finally:
        conn.close()
    return pr_id


def test_defect_escape_badge_green_when_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    # 100 merged PRs, 2 escaped → 0.02
    for i in range(100):
        _seed_merged(
            db_path,
            ticket_id=f"RW-{i}",
            pr_number=i + 1,
            head_sha=f"s{i}",
            days_ago=5,
        )
    conn = open_connection(db_path)
    try:
        for i in range(2):
            insert_defect_link(
                conn,
                pr_run_id=i + 1,
                defect_key=f"BUG-{i}",
                source="jira",
                reported_at=_days_ago(4),
                confirmed=1,
                category="escaped",
            )
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "badge-success" in r.text
    assert "Defect escape" in r.text


def test_defect_escape_badge_red_when_high(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    # 10 merged PRs, 1 escaped → 0.10 > 0.05
    for i in range(10):
        _seed_merged(
            db_path,
            ticket_id=f"RW-{i}",
            pr_number=i + 1,
            head_sha=f"s{i}",
        )
    conn = open_connection(db_path)
    try:
        insert_defect_link(
            conn,
            pr_run_id=1,
            defect_key="BUG-1",
            source="jira",
            reported_at=_days_ago(4),
            confirmed=1,
            category="escaped",
        )
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "badge-error" in r.text


def test_defect_escape_shows_unknown_when_no_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_pr_run(db_path, profile="rockwell", merged=0)
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "unknown" in r.text


def test_escaped_defects_section_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    pr_id = _seed_merged(db_path, ticket_id="RW-42", pr_number=7)
    conn = open_connection(db_path)
    try:
        insert_defect_link(
            conn,
            pr_run_id=pr_id,
            defect_key="ESCAPE-9",
            source="jira",
            reported_at=_days_ago(2),
            confirmed=1,
            category="escaped",
        )
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "Escaped Defects" in r.text
    assert "ESCAPE-9" in r.text


def test_escaped_defects_section_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_merged(db_path)
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "Escaped Defects" in r.text
    assert "No escaped defects in window." in r.text


def test_suggested_matches_include_promote_curl_snippet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    pr_run_id = _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    conn = open_connection(db_path)
    try:
        human_id = insert_review_issue(
            conn, pr_run_id=pr_run_id, source="human_review",
            external_id="h1", summary="x", is_valid=1,
        )
        ai_id = insert_review_issue(
            conn, pr_run_id=pr_run_id, source="ai_review",
            external_id="a1", summary="y", is_valid=1,
        )
        insert_issue_match(
            conn,
            human_issue_id=human_id,
            ai_issue_id=ai_id,
            match_type="semantic_weak",
            confidence=0.7,
            matched_by="suggested",
        )
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "manual-match" in r.text
    assert "promote" in r.text
    assert "X-Autonomy-Admin-Token" in r.text


# --- Drilldown ---

def test_drilldown_200_for_existing_pr_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    pr_id = _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    c = TestClient(_mk_app())
    r = c.get(f"/autonomy/pr/{pr_id}")
    assert r.status_code == 200
    assert "PR Drilldown" in r.text
    assert "RW-1" in r.text


def test_drilldown_404_for_missing_pr_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    c = TestClient(_mk_app())
    r = c.get("/autonomy/pr/9999")
    assert r.status_code == 404


def test_drilldown_renders_human_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    pr_id = _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    conn = open_connection(db_path)
    try:
        insert_review_issue(
            conn, pr_run_id=pr_id, source="human_review",
            external_id="h1", file_path="app.py",
            line_start=5, line_end=5,
            summary="Missing null guard here xyz", is_valid=1,
        )
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get(f"/autonomy/pr/{pr_id}")
    assert r.status_code == 200
    assert "Human Issues" in r.text
    assert "Missing null guard here xyz" in r.text


def test_drilldown_renders_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    pr_id = _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    conn = open_connection(db_path)
    try:
        human_id = insert_review_issue(
            conn, pr_run_id=pr_id, source="human_review",
            external_id="h1", summary="human-issue-zzz", is_valid=1,
        )
        ai_id = insert_review_issue(
            conn, pr_run_id=pr_id, source="ai_review",
            external_id="a1", summary="ai-issue-qqq", is_valid=1,
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
    r = c.get(f"/autonomy/pr/{pr_id}")
    assert r.status_code == 200
    assert "Matches" in r.text
    assert "human-issue-zzz" in r.text


def test_drilldown_renders_defects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    pr_id = _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    conn = open_connection(db_path)
    try:
        insert_defect_link(
            conn,
            pr_run_id=pr_id,
            defect_key="DRILLBUG-1",
            source="jira",
            reported_at=_days_ago(1),
            confirmed=1,
            category="escaped",
        )
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get(f"/autonomy/pr/{pr_id}")
    assert r.status_code == 200
    assert "Defect Links" in r.text
    assert "DRILLBUG-1" in r.text


def test_sparkline_renders_with_points() -> None:
    svg = _render_sparkline_svg(
        [("2026-04-01", 0.5), ("2026-04-02", None), ("2026-04-03", 0.9)],
        label="test",
    )
    assert "<svg" in svg
    # Two visible points (one None → skipped)
    assert svg.count("<circle") == 2


# ---------------------------------------------------------------------------
# Auto-merge decisions section (Task 1)
# ---------------------------------------------------------------------------

def _seed_auto_merge_decision(
    db_path: Path,
    *,
    repo_full_name: str = "acme/widgets",
    pr_number: int = 42,
    decision: str = "dry_run",
    reason: str = "policy says dry-run",
    client_profile: str = "rockwell",
    dry_run: bool = True,
    recommended_mode: str = "conservative",
    gates: dict | None = None,
) -> int:
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        return record_auto_merge_decision(
            conn,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            decision=decision,
            reason=reason,
            payload={
                "client_profile": client_profile,
                "dry_run": dry_run,
                "recommended_mode": recommended_mode,
                "gates": gates or {"ci": True, "review": True},
                "ticket_id": "RW-1",
                "ticket_type": "feature",
                "evaluated_at": "2026-04-05T10:00:00+00:00",
            },
        )
    finally:
        conn.close()


def test_auto_merge_decisions_section_renders_when_rows_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    _seed_auto_merge_decision(
        db_path,
        repo_full_name="acme/widgets",
        pr_number=42,
        decision="dry_run",
        reason="policy says dry-run",
        client_profile="rockwell",
    )
    c = TestClient(_mk_app())
    r = c.get("/autonomy")
    assert r.status_code == 200
    assert "Auto-merge Decisions" in r.text
    assert "acme/widgets#42" in r.text
    assert "policy says dry-run" in r.text
    # Links to GitHub PR
    assert "https://github.com/acme/widgets/pull/42" in r.text


def test_auto_merge_decisions_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    c = TestClient(_mk_app())
    r = c.get("/autonomy")
    assert r.status_code == 200
    assert "Auto-merge Decisions" in r.text
    assert "No auto-merge decisions in the last 7 days" in r.text


def test_auto_merge_decisions_shows_dry_run_badge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_auto_merge_decision(
        db_path, decision="dry_run", client_profile="rockwell", pr_number=7
    )
    c = TestClient(_mk_app())
    r = c.get("/autonomy")
    assert r.status_code == 200
    # badge-blue wraps dry_run decisions
    assert "badge-blue" in r.text
    assert "dry_run" in r.text


def test_auto_merge_decisions_shows_merged_badge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_auto_merge_decision(
        db_path, decision="merged", client_profile="rockwell", pr_number=8
    )
    c = TestClient(_mk_app())
    r = c.get("/autonomy")
    assert r.status_code == 200
    assert "badge-success" in r.text
    # The word "merged" should appear in an auto-merge decision badge
    assert ">merged<" in r.text


def test_auto_merge_decisions_filters_by_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    _seed_pr_run(db_path, profile="harness-test", ticket_id="HT-1", pr_number=2, head_sha="s2")
    _seed_auto_merge_decision(
        db_path,
        pr_number=111,
        decision="merged",
        reason="rockwell reason only",
        client_profile="rockwell",
    )
    _seed_auto_merge_decision(
        db_path,
        pr_number=222,
        decision="dry_run",
        reason="harness-test reason only",
        client_profile="harness-test",
    )
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "rockwell reason only" in r.text
    assert "harness-test reason only" not in r.text


def test_profile_card_shows_auto_merge_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no runtime toggle and yaml default False → DRY-RUN (source: yaml)."""
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "Auto-merge" in r.text
    assert "DRY-RUN" in r.text
    assert "source: yaml" in r.text


def test_profile_card_shows_toggle_curl_snippet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "/api/autonomy/auto-merge-toggle" in r.text
    assert "X-Autonomy-Admin-Token" in r.text
    assert "rockwell" in r.text


def test_profile_card_auto_merge_state_runtime_toggle_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    _seed_pr_run(db_path, profile="rockwell", ticket_id="RW-1")
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        set_auto_merge_toggle(conn, client_profile="rockwell", enabled=True)
    finally:
        conn.close()
    c = TestClient(_mk_app())
    r = c.get("/autonomy?client_profile=rockwell")
    assert r.status_code == 200
    assert "ENABLED" in r.text
    assert "source: runtime_toggle" in r.text
