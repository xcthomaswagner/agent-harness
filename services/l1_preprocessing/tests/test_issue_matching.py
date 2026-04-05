"""Tests for autonomy_matching — issue matcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from autonomy_matching import (
    MatchResult,
    _lines_overlap,
    match_human_issue,
    match_human_issues_for_pr_run,
    normalize_summary,
    similarity,
)
from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    insert_review_issue,
    list_issue_matches_for_human,
    open_connection,
    upsert_pr_run,
)


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "autonomy.db"
    c = open_connection(db_path)
    ensure_schema(c)
    yield c
    c.close()


def _make_pr_run(conn, **overrides):
    data = {
        "ticket_id": "SCRUM-1",
        "pr_number": 1,
        "repo_full_name": "acme/app",
        "head_sha": "sha1",
    }
    data.update(overrides)
    return upsert_pr_run(conn, PrRunUpsert(**data))


def _insert_human(
    conn,
    pr_run_id,
    *,
    file_path="",
    line_start=0,
    line_end=0,
    category="",
    summary="human summary",
    acceptance_criterion_ref="",
    is_valid=1,
):
    return insert_review_issue(
        conn,
        pr_run_id=pr_run_id,
        source="human_review",
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        category=category,
        summary=summary,
        acceptance_criterion_ref=acceptance_criterion_ref,
        is_valid=is_valid,
    )


def _insert_ai(
    conn,
    pr_run_id,
    *,
    source="ai_review",
    file_path="",
    line_start=0,
    line_end=0,
    category="",
    summary="ai summary",
    acceptance_criterion_ref="",
    is_valid=1,
):
    return insert_review_issue(
        conn,
        pr_run_id=pr_run_id,
        source=source,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        category=category,
        summary=summary,
        acceptance_criterion_ref=acceptance_criterion_ref,
        is_valid=is_valid,
    )


class TestNormalizeAndSimilarity:
    def test_normalize_lowercases_strips_punctuation(self):
        assert normalize_summary("Null Check, Missing!") == "null check missing"

    def test_normalize_collapses_whitespace(self):
        assert normalize_summary("  foo   bar\t\tbaz  ") == "foo bar baz"

    def test_similarity_identical_is_1_0(self):
        assert similarity("Hello World", "hello world") == 1.0

    def test_similarity_very_different_is_low(self):
        assert similarity("cats love tuna", "xyz abc qrs") < 0.3

    def test_similarity_empty_returns_zero(self):
        assert similarity("", "foo") == 0.0
        assert similarity("foo", "") == 0.0
        assert similarity("", "") == 0.0


class TestLinesOverlap:
    def test_overlap_contained(self):
        assert _lines_overlap(10, 14, 12, 12) is True

    def test_overlap_edge(self):
        assert _lines_overlap(10, 14, 14, 20) is True

    def test_no_overlap(self):
        assert _lines_overlap(10, 14, 15, 20) is False

    def test_identical(self):
        assert _lines_overlap(10, 10, 10, 10) is True


class TestMatchHumanIssue:
    def test_tier1_line_overlap(self, conn):
        pr = _make_pr_run(conn)
        ai_id = _insert_ai(
            conn, pr, file_path="foo.py", line_start=10, line_end=14,
            summary="unrelated",
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", line_start=12, line_end=12,
            summary="other",
        )
        result = match_human_issue(conn, h_id)
        assert result == MatchResult(
            ai_issue_id=ai_id, match_type="line_overlap", confidence=1.0
        )

    def test_tier1_no_anchor_on_either_side_skips_tier1(self, conn):
        pr = _make_pr_run(conn)
        # AI: same file, no line anchor, same category, similar summary → tier 2
        ai_id = _insert_ai(
            conn, pr, file_path="foo.py", line_start=0, line_end=0,
            category="bug", summary="null check missing on user input",
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", line_start=0, line_end=0,
            category="bug", summary="null check missing on user input",
        )
        result = match_human_issue(conn, h_id)
        assert result is not None
        assert result.match_type == "category_summary"
        assert result.confidence == 0.9
        assert result.ai_issue_id == ai_id

    def test_tier2_same_category_similar_summary(self, conn):
        pr = _make_pr_run(conn)
        ai_id = _insert_ai(
            conn, pr, file_path="foo.py", line_start=100, line_end=100,
            category="security",
            summary="SQL injection in user query builder",
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", line_start=200, line_end=200,
            category="security",
            summary="SQL injection in user query builder",
        )
        result = match_human_issue(conn, h_id)
        assert result is not None
        assert result.confidence == 0.9
        assert result.match_type == "category_summary"
        assert result.ai_issue_id == ai_id

    def test_tier3_similar_summary_only(self, conn):
        pr = _make_pr_run(conn)
        ai_id = _insert_ai(
            conn, pr, file_path="foo.py", line_start=100, line_end=100,
            category="style",
            summary="function name should be camelCase please fix",
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", line_start=200, line_end=200,
            category="bug",
            summary="function name should use snake_case convention now",
        )
        result = match_human_issue(conn, h_id)
        assert result is not None
        assert result.confidence == 0.8
        assert result.match_type == "summary_moderate"
        assert result.ai_issue_id == ai_id

    def test_tier4_ac_ref_only(self, conn):
        pr = _make_pr_run(conn)
        ai_id = _insert_ai(
            conn, pr, file_path="other.py", summary="totally different",
            acceptance_criterion_ref="AC-3",
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", summary="nothing alike",
            acceptance_criterion_ref="AC-3",
        )
        result = match_human_issue(conn, h_id)
        assert result is not None
        assert result.confidence == 0.7
        assert result.match_type == "ac_ref"
        assert result.ai_issue_id == ai_id

    def test_prefers_higher_tier(self, conn):
        pr = _make_pr_run(conn)
        # AI 1: tier 4 candidate (ac_ref match, different file)
        _insert_ai(
            conn, pr, file_path="other.py", summary="unrelated thing here",
            acceptance_criterion_ref="AC-1",
        )
        # AI 2: tier 2 candidate (same file, category, similar summary)
        ai2 = _insert_ai(
            conn, pr, file_path="foo.py", line_start=500, line_end=500,
            category="bug",
            summary="missing null check on response",
            acceptance_criterion_ref="AC-1",
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", line_start=900, line_end=900,
            category="bug",
            summary="missing null check on response",
            acceptance_criterion_ref="AC-1",
        )
        result = match_human_issue(conn, h_id)
        assert result is not None
        assert result.confidence == 0.9
        assert result.ai_issue_id == ai2

    def test_ignores_invalid_ai_issues(self, conn):
        pr = _make_pr_run(conn)
        _insert_ai(
            conn, pr, file_path="foo.py", line_start=10, line_end=14,
            is_valid=0,
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", line_start=12, line_end=12,
        )
        result = match_human_issue(conn, h_id)
        assert result is None

    def test_ignores_other_pr_run(self, conn):
        pr1 = _make_pr_run(conn, pr_number=1, head_sha="sha1")
        pr2 = _make_pr_run(conn, pr_number=2, head_sha="sha2")
        _insert_ai(
            conn, pr2, file_path="foo.py", line_start=10, line_end=14,
        )
        h_id = _insert_human(
            conn, pr1, file_path="foo.py", line_start=12, line_end=12,
        )
        result = match_human_issue(conn, h_id)
        assert result is None

    def test_ignores_human_source(self, conn):
        pr = _make_pr_run(conn)
        # Another human_review issue that would line-overlap
        _insert_human(
            conn, pr, file_path="foo.py", line_start=10, line_end=14,
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", line_start=12, line_end=12,
        )
        result = match_human_issue(conn, h_id)
        assert result is None

    def test_returns_none_when_no_match(self, conn):
        pr = _make_pr_run(conn)
        _insert_ai(
            conn, pr, file_path="other.py", line_start=1, line_end=2,
            category="style", summary="aaa bbb ccc",
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", line_start=12, line_end=12,
            category="bug", summary="zzz yyy xxx",
        )
        result = match_human_issue(conn, h_id)
        assert result is None

    def test_tie_break_by_ai_issue_id(self, conn):
        pr = _make_pr_run(conn)
        ai1 = _insert_ai(
            conn, pr, file_path="foo.py", line_start=10, line_end=20,
        )
        _insert_ai(
            conn, pr, file_path="foo.py", line_start=12, line_end=14,
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", line_start=12, line_end=13,
        )
        result = match_human_issue(conn, h_id)
        assert result is not None
        assert result.ai_issue_id == ai1


class TestMatchHumanIssuesForPrRun:
    def test_auto_matches_tier1(self, conn):
        pr = _make_pr_run(conn)
        _insert_ai(
            conn, pr, file_path="foo.py", line_start=10, line_end=14,
        )
        _insert_human(
            conn, pr, file_path="foo.py", line_start=12, line_end=12,
        )
        summary = match_human_issues_for_pr_run(conn, pr)
        assert summary == {"auto_matched": 1, "suggested": 0, "unmatched": 0}

    def test_suggested_matches_tier4_only_no_auto(self, conn):
        pr = _make_pr_run(conn)
        _insert_ai(
            conn, pr, file_path="other.py", summary="completely different",
            acceptance_criterion_ref="AC-9",
        )
        h_id = _insert_human(
            conn, pr, file_path="foo.py", summary="nothing alike",
            acceptance_criterion_ref="AC-9",
        )
        summary = match_human_issues_for_pr_run(conn, pr)
        assert summary == {"auto_matched": 0, "suggested": 1, "unmatched": 0}
        matches = list_issue_matches_for_human(conn, h_id)
        assert len(matches) == 1
        assert matches[0]["matched_by"] == "suggested"
        assert matches[0]["confidence"] == 0.7

    def test_unmatched_reported(self, conn):
        pr = _make_pr_run(conn)
        _insert_ai(
            conn, pr, file_path="other.py", summary="zzz",
        )
        _insert_human(
            conn, pr, file_path="foo.py", summary="aaa",
        )
        summary = match_human_issues_for_pr_run(conn, pr)
        assert summary == {"auto_matched": 0, "suggested": 0, "unmatched": 1}

    def test_skips_already_matched_humans(self, conn):
        pr = _make_pr_run(conn)
        _insert_ai(
            conn, pr, file_path="foo.py", line_start=10, line_end=14,
        )
        _insert_human(
            conn, pr, file_path="foo.py", line_start=12, line_end=12,
        )
        first = match_human_issues_for_pr_run(conn, pr)
        assert first["auto_matched"] == 1
        second = match_human_issues_for_pr_run(conn, pr)
        assert second == {"auto_matched": 0, "suggested": 0, "unmatched": 0}
