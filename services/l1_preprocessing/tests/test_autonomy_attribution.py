"""Tests for autonomy_attribution.attribute_human_issues_to_commits."""

from __future__ import annotations

from autonomy_attribution import attribute_human_issues_to_commits


def test_empty_human_issues_returns_empty() -> None:
    result = attribute_human_issues_to_commits(
        [],
        [{"sha": "a", "committed_at": "2026-04-05T12:00:00+00:00"}],
        "2026-04-05T13:00:00+00:00",
    )
    assert result == []


def test_no_commits_no_attribution() -> None:
    result = attribute_human_issues_to_commits(
        [{"id": 1, "created_at": "2026-04-05T12:00:00+00:00"}],
        [],
        "2026-04-05T13:00:00+00:00",
    )
    assert result == []


def test_comment_followed_by_commit_is_triggering() -> None:
    result = attribute_human_issues_to_commits(
        [{"id": 1, "created_at": "2026-04-05T12:00:00+00:00"}],
        [{"sha": "a", "committed_at": "2026-04-05T12:30:00+00:00"}],
        "2026-04-05T13:00:00+00:00",
    )
    assert result == [1]


def test_comment_not_followed_by_commit_not_triggering() -> None:
    result = attribute_human_issues_to_commits(
        [{"id": 1, "created_at": "2026-04-05T12:00:00+00:00"}],
        [],
        "2026-04-05T13:00:00+00:00",
    )
    assert result == []


def test_commit_before_any_comment_not_attributed() -> None:
    result = attribute_human_issues_to_commits(
        [{"id": 1, "created_at": "2026-04-05T12:00:00+00:00"}],
        [{"sha": "a", "committed_at": "2026-04-05T11:00:00+00:00"}],
        "2026-04-05T13:00:00+00:00",
    )
    assert result == []


def test_concurrent_comments_only_latest_triggering() -> None:
    # Two comments within minutes of each other, then one commit.
    # Only the LATEST comment before the commit should be attributed.
    result = attribute_human_issues_to_commits(
        [
            {"id": 1, "created_at": "2026-04-05T12:00:00+00:00"},
            {"id": 2, "created_at": "2026-04-05T12:05:00+00:00"},
        ],
        [{"sha": "a", "committed_at": "2026-04-05T12:30:00+00:00"}],
        "2026-04-05T13:00:00+00:00",
    )
    assert result == [2]


def test_multiple_commits_in_window_single_attribution() -> None:
    # One comment, then three commits before approval: comment flagged once.
    result = attribute_human_issues_to_commits(
        [{"id": 1, "created_at": "2026-04-05T12:00:00+00:00"}],
        [
            {"sha": "a", "committed_at": "2026-04-05T12:10:00+00:00"},
            {"sha": "b", "committed_at": "2026-04-05T12:20:00+00:00"},
            {"sha": "c", "committed_at": "2026-04-05T12:30:00+00:00"},
        ],
        "2026-04-05T13:00:00+00:00",
    )
    assert result == [1]


def test_commit_after_approval_not_attributed() -> None:
    # Commit post-dates approval → ignored.
    result = attribute_human_issues_to_commits(
        [{"id": 1, "created_at": "2026-04-05T12:00:00+00:00"}],
        [{"sha": "a", "committed_at": "2026-04-05T14:00:00+00:00"}],
        "2026-04-05T13:00:00+00:00",
    )
    assert result == []


def test_commit_between_comments_attributes_to_earlier_one() -> None:
    # Commit lands between two comments → attributed to the earlier comment
    # (it is the "latest unresolved before commit" at that moment).
    result = attribute_human_issues_to_commits(
        [
            {"id": 1, "created_at": "2026-04-05T12:00:00+00:00"},
            {"id": 2, "created_at": "2026-04-05T12:20:00+00:00"},
        ],
        [{"sha": "a", "committed_at": "2026-04-05T12:10:00+00:00"}],
        "2026-04-05T13:00:00+00:00",
    )
    assert result == [1]


def test_comment_at_or_after_approval_ignored() -> None:
    # Comment posted after approval should not be considered.
    result = attribute_human_issues_to_commits(
        [
            {"id": 1, "created_at": "2026-04-05T12:00:00+00:00"},
            {"id": 2, "created_at": "2026-04-05T13:30:00+00:00"},
        ],
        [{"sha": "a", "committed_at": "2026-04-05T12:10:00+00:00"}],
        "2026-04-05T13:00:00+00:00",
    )
    assert result == [1]


def test_each_of_two_comments_has_own_followup() -> None:
    # Comment 1 → commit → Comment 2 → commit: both triggering.
    result = attribute_human_issues_to_commits(
        [
            {"id": 1, "created_at": "2026-04-05T12:00:00+00:00"},
            {"id": 2, "created_at": "2026-04-05T12:20:00+00:00"},
        ],
        [
            {"sha": "a", "committed_at": "2026-04-05T12:10:00+00:00"},
            {"sha": "b", "committed_at": "2026-04-05T12:30:00+00:00"},
        ],
        "2026-04-05T13:00:00+00:00",
    )
    assert sorted(result) == [1, 2]
