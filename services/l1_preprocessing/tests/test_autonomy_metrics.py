"""Unit tests for autonomy_metrics.compute_profile_metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autonomy_metrics import (
    _recommend_mode,
    compute_daily_trend,
    compute_profile_metrics,
    compute_ticket_type_breakdown,
)
from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    insert_defect_link,
    insert_issue_match,
    insert_review_issue,
    open_connection,
    record_defect_sweep_heartbeat,
    upsert_pr_run,
)


def _mk_conn(db_path: Path):
    conn = open_connection(db_path)
    ensure_schema(conn)
    return conn


def _seed_pr(
    conn,
    *,
    profile: str = "rockwell",
    ticket_id: str = "RW-1",
    pr_number: int = 1,
    head_sha: str = "sha1",
    first_pass_accepted: int = 1,
    merged: int = 0,
) -> int:
    return upsert_pr_run(
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


def test_self_review_catch_none_when_no_humans(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        _seed_pr(conn)
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert m["human_issue_count"] == 0
        assert m["self_review_catch_rate"] is None
        assert m["matched_human_issue_count"] == 0
        assert m["unmatched_human_issue_count"] == 0
    finally:
        conn.close()


def test_self_review_catch_tier4_suggested_does_not_count(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        pr = _seed_pr(conn)
        h = insert_review_issue(
            conn,
            pr_run_id=pr,
            source="human_review",
            external_id="h1",
            summary="x",
            is_valid=1,
        )
        a = insert_review_issue(
            conn,
            pr_run_id=pr,
            source="ai_review",
            external_id="a1",
            summary="x",
            is_valid=1,
        )
        insert_issue_match(
            conn,
            human_issue_id=h,
            ai_issue_id=a,
            match_type="semantic_weak",
            confidence=0.7,
            matched_by="suggested",
        )
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert m["human_issue_count"] == 1
        assert m["matched_human_issue_count"] == 0
        assert m["self_review_catch_rate"] == 0.0
        assert m["unmatched_human_issue_count"] == 1
    finally:
        conn.close()


def test_sidecar_coverage_zero_when_no_ai_issues(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        for i in range(3):
            _seed_pr(
                conn,
                ticket_id=f"RW-{i}",
                pr_number=i + 1,
                head_sha=f"s{i}",
            )
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert m["sample_size"] == 3
        assert m["sidecar_coverage"] == 0.0
        assert m["ai_issue_count"] == 0
    finally:
        conn.close()


def test_data_quality_notes_populated(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        # 5 PRs, no AI issues → low_sidecar_coverage + no_human_baseline
        for i in range(5):
            _seed_pr(
                conn,
                ticket_id=f"RW-{i}",
                pr_number=i + 1,
                head_sha=f"s{i}",
            )
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert "low_sample_size" in m["data_quality_notes"]
        assert "low_sidecar_coverage" in m["data_quality_notes"]
        assert "no_human_baseline" in m["data_quality_notes"]
        assert m["data_quality_status"] == "insufficient_data"
    finally:
        conn.close()


def test_recommended_mode_conservative_on_low_coverage(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        for i in range(5):
            _seed_pr(
                conn,
                ticket_id=f"RW-{i}",
                pr_number=i + 1,
                head_sha=f"s{i}",
            )
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert m["recommended_mode"] == "conservative"
    finally:
        conn.close()


def test_self_review_catch_counts_qualifying_match(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        pr = _seed_pr(conn)
        h = insert_review_issue(
            conn,
            pr_run_id=pr,
            source="human_review",
            external_id="h1",
            summary="x",
            is_valid=1,
        )
        a = insert_review_issue(
            conn,
            pr_run_id=pr,
            source="ai_review",
            external_id="a1",
            summary="x",
            is_valid=1,
        )
        insert_issue_match(
            conn,
            human_issue_id=h,
            ai_issue_id=a,
            match_type="exact_line",
            confidence=0.95,
            matched_by="system",
        )
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert m["matched_human_issue_count"] == 1
        assert m["self_review_catch_rate"] == 1.0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "sample_size,expect_low",
    [(9, True), (10, False)],
)
def test_low_sample_size_threshold(
    tmp_path: Path, sample_size: int, expect_low: bool
) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        for i in range(sample_size):
            _seed_pr(
                conn,
                ticket_id=f"RW-{i}",
                pr_number=i + 1,
                head_sha=f"s{i}",
            )
        m = compute_profile_metrics(conn, "rockwell", 30)
        if expect_low:
            assert "low_sample_size" in m["data_quality_notes"]
        else:
            assert "low_sample_size" not in m["data_quality_notes"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 3 Step 2: defect escape rate, link coverage, tiered recommend mode
# ---------------------------------------------------------------------------

def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _seed_merged_pr(
    conn,
    *,
    profile: str = "rockwell",
    ticket_id: str = "RW-1",
    pr_number: int = 1,
    head_sha: str = "sha1",
    days_ago: int = 5,
    first_pass_accepted: int = 1,
    ticket_type: str = "",
) -> int:
    merged_at = _iso_days_ago(days_ago)
    return upsert_pr_run(
        conn,
        PrRunUpsert(
            ticket_id=ticket_id,
            pr_number=pr_number,
            repo_full_name="acme/widgets",
            head_sha=head_sha,
            client_profile=profile,
            ticket_type=ticket_type,
            opened_at=_iso_days_ago(days_ago + 1),
            first_pass_accepted=first_pass_accepted,
            merged=1,
            merged_at=merged_at,
        ),
    )


def test_defect_escape_rate_computed_from_confirmed_escaped_defects(
    tmp_path: Path,
) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        pr1 = _seed_merged_pr(conn, ticket_id="RW-1", pr_number=1, head_sha="s1", days_ago=5)
        _seed_merged_pr(conn, ticket_id="RW-2", pr_number=2, head_sha="s2", days_ago=5)
        # defect reported 1 day after merge, confirmed, category=escaped
        insert_defect_link(
            conn,
            pr_run_id=pr1,
            defect_key="BUG-1",
            source="jira",
            reported_at=_iso_days_ago(4),
            confirmed=1,
            category="escaped",
        )
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert m["merged_count"] == 2
        assert m["defect_escape_rate"] == 0.5
    finally:
        conn.close()


def test_defect_escape_rate_none_when_no_merged_prs(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        # PRs opened but never merged
        _seed_pr(conn, ticket_id="RW-1", pr_number=1, head_sha="s1", merged=0)
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert m["defect_escape_rate"] is None
    finally:
        conn.close()


def test_defect_link_coverage_1_when_heartbeat_fresh(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        _seed_merged_pr(conn, days_ago=3)
        # heartbeat 1 hour ago
        record_defect_sweep_heartbeat(
            conn,
            client_profile="rockwell",
            swept_through_iso=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        )
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert m["defect_link_coverage"] == 1.0
    finally:
        conn.close()


def test_defect_link_coverage_half_when_heartbeat_stale(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        _seed_merged_pr(conn, days_ago=3)
        # heartbeat 2 days ago
        record_defect_sweep_heartbeat(
            conn,
            client_profile="rockwell",
            swept_through_iso=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
        )
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert m["defect_link_coverage"] == 0.5
    finally:
        conn.close()


def test_low_defect_link_coverage_note_when_threshold_unmet(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        # 5 merged PRs, no heartbeat → coverage=0 < 0.8
        for i in range(5):
            _seed_merged_pr(
                conn,
                ticket_id=f"RW-{i}",
                pr_number=i + 1,
                head_sha=f"s{i}",
                days_ago=3,
            )
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert "low_defect_link_coverage" in m["data_quality_notes"]
    finally:
        conn.close()


def test_defect_escape_unknown_note_when_merged_but_none(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        # 5 PRs with merged=1 but NO merged_at → defect_escape_rate=None
        for i in range(5):
            upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id=f"RW-{i}",
                    pr_number=i + 1,
                    repo_full_name="acme/widgets",
                    head_sha=f"s{i}",
                    client_profile="rockwell",
                    opened_at="2026-04-01T12:00:00+00:00",
                    first_pass_accepted=1,
                    merged=1,
                ),
            )
        m = compute_profile_metrics(conn, "rockwell", 30)
        assert m["merged_count"] == 5
        assert m["defect_escape_rate"] is None
        assert "defect_escape_unknown" in m["data_quality_notes"]
    finally:
        conn.close()


def test_recommended_mode_full_autonomous_happy_path() -> None:
    assert _recommend_mode(
        sample_size=50,
        first_pass_acceptance_rate=0.95,
        defect_escape_rate=0.02,
        self_review_catch_rate=0.90,
        dq_status="good",
    ) == "full_autonomous"


def test_recommended_mode_semi_autonomous_happy_path() -> None:
    assert _recommend_mode(
        sample_size=20,
        first_pass_acceptance_rate=0.90,
        defect_escape_rate=0.04,
        self_review_catch_rate=None,
        dq_status="good",
    ) == "semi_autonomous"


def test_recommended_mode_conservative_when_dq_degraded() -> None:
    assert _recommend_mode(
        sample_size=50,
        first_pass_acceptance_rate=0.95,
        defect_escape_rate=0.02,
        self_review_catch_rate=0.90,
        dq_status="degraded",
    ) == "conservative"


def test_recommended_mode_conservative_when_defect_escape_none() -> None:
    assert _recommend_mode(
        sample_size=50,
        first_pass_acceptance_rate=0.95,
        defect_escape_rate=None,
        self_review_catch_rate=0.90,
        dq_status="good",
    ) == "conservative"


def test_recommended_mode_full_requires_catch_rate() -> None:
    # All thresholds met except catch_rate is None → not full
    assert _recommend_mode(
        sample_size=50,
        first_pass_acceptance_rate=0.95,
        defect_escape_rate=0.02,
        self_review_catch_rate=None,
        dq_status="good",
    ) == "semi_autonomous"


# ---------------------------------------------------------------------------
# Phase 3 Step 7: ticket_type_breakdown + daily_trend
# ---------------------------------------------------------------------------


def test_daily_trend_returns_window_days_buckets(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        _seed_pr(conn)
        buckets = compute_daily_trend(conn, "rockwell", 30, "fpa")
        assert len(buckets) == 30
        # Each bucket is a tuple of (date, value_or_None, sample_count)
        for d, _v, _n in buckets:
            assert isinstance(d, str)
            assert len(d) == 10  # YYYY-MM-DD
    finally:
        conn.close()


def test_ticket_type_breakdown_groups_correctly(tmp_path: Path) -> None:
    conn = _mk_conn(tmp_path / "a.db")
    try:
        _seed_merged_pr(
            conn, ticket_id="RW-1", pr_number=1, head_sha="s1", ticket_type="bug"
        )
        _seed_merged_pr(
            conn, ticket_id="RW-2", pr_number=2, head_sha="s2", ticket_type="bug"
        )
        _seed_merged_pr(
            conn, ticket_id="RW-3", pr_number=3, head_sha="s3", ticket_type="feature"
        )
        rows = compute_ticket_type_breakdown(conn, "rockwell", 30)
        by_type = {r["ticket_type"]: r for r in rows}
        assert by_type["bug"]["sample_size"] == 2
        assert by_type["feature"]["sample_size"] == 1
    finally:
        conn.close()
