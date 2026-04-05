"""Unit tests for autonomy_metrics.compute_profile_metrics."""

from __future__ import annotations

from pathlib import Path

import pytest

from autonomy_metrics import compute_profile_metrics
from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    insert_issue_match,
    insert_review_issue,
    open_connection,
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
