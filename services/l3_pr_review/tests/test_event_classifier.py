"""Tests for GitHub + ADO webhook event classification."""

from ado_event_classifier import classify_ado_event
from event_classifier import EventType, classify_event


class TestPullRequestEvents:
    def test_pr_opened(self) -> None:
        headers = {"x-github-event": "pull_request"}
        payload = {"action": "opened", "pull_request": {"number": 1}}
        assert classify_event(headers, payload) == EventType.PR_OPENED

    def test_pr_ready_for_review(self) -> None:
        headers = {"x-github-event": "pull_request"}
        payload = {"action": "ready_for_review"}
        assert classify_event(headers, payload) == EventType.PR_READY_FOR_REVIEW

    def test_pr_synchronize_distinct_from_opened(self) -> None:
        """New commits pushed to open PR classified as PR_SYNCHRONIZE, not PR_OPENED."""
        headers = {"x-github-event": "pull_request"}
        payload = {"action": "synchronize"}
        assert classify_event(headers, payload) == EventType.PR_SYNCHRONIZE

    def test_pr_closed_without_merge_is_terminal(self) -> None:
        headers = {"x-github-event": "pull_request"}
        payload = {"action": "closed", "pull_request": {"merged": False}}
        assert classify_event(headers, payload) == EventType.PR_CLOSED

    def test_pr_closed_with_merge_is_merged(self) -> None:
        headers = {"x-github-event": "pull_request"}
        payload = {"action": "closed", "pull_request": {"merged": True}}
        assert classify_event(headers, payload) == EventType.PR_MERGED


class TestCheckSuiteEvents:
    def test_check_suite_failure(self) -> None:
        headers = {"x-github-event": "check_suite"}
        payload = {
            "action": "completed",
            "check_suite": {
                "conclusion": "failure",
                "pull_requests": [{"number": 5}],
                "head_branch": "ai/PROJ-123",
            },
        }
        assert classify_event(headers, payload) == EventType.CI_FAILED

    def test_check_suite_success(self) -> None:
        headers = {"x-github-event": "check_suite"}
        payload = {
            "action": "completed",
            "check_suite": {"conclusion": "success"},
        }
        assert classify_event(headers, payload) == EventType.CI_PASSED

    def test_check_run_failure(self) -> None:
        headers = {"x-github-event": "check_run"}
        payload = {
            "action": "completed",
            "check_run": {"conclusion": "failure", "pull_requests": []},
        }
        assert classify_event(headers, payload) == EventType.CI_FAILED

    def test_check_suite_pending_ignored(self) -> None:
        headers = {"x-github-event": "check_suite"}
        payload = {"action": "completed", "check_suite": {"conclusion": ""}}
        assert classify_event(headers, payload) == EventType.IGNORED


class TestReviewEvents:
    def test_review_approved(self) -> None:
        headers = {"x-github-event": "pull_request_review"}
        payload = {
            "action": "submitted",
            "review": {"state": "approved", "user": {"login": "dev"}},
        }
        assert classify_event(headers, payload) == EventType.REVIEW_APPROVED

    def test_review_changes_requested(self) -> None:
        headers = {"x-github-event": "pull_request_review"}
        payload = {
            "action": "submitted",
            "review": {"state": "changes_requested", "body": "Fix the auth check"},
        }
        assert classify_event(headers, payload) == EventType.REVIEW_CHANGES_REQUESTED

    def test_review_commented(self) -> None:
        headers = {"x-github-event": "pull_request_review"}
        payload = {"action": "submitted", "review": {"state": "commented"}}
        assert classify_event(headers, payload) == EventType.REVIEW_COMMENT

    def test_issue_comment_on_pr(self) -> None:
        headers = {"x-github-event": "issue_comment"}
        payload = {
            "action": "created",
            "issue": {"number": 10, "pull_request": {"url": "..."}},
            "comment": {"body": "Looks good"},
        }
        assert classify_event(headers, payload) == EventType.REVIEW_COMMENT

    def test_issue_comment_not_on_pr(self) -> None:
        headers = {"x-github-event": "issue_comment"}
        payload = {"action": "created", "issue": {"number": 10}}
        assert classify_event(headers, payload) == EventType.IGNORED

    def test_issue_comment_deleted_ignored(self) -> None:
        headers = {"x-github-event": "issue_comment"}
        payload = {
            "action": "deleted",
            "issue": {"number": 10, "pull_request": {"url": "..."}},
        }
        assert classify_event(headers, payload) == EventType.IGNORED

    def test_issue_comment_edited_ignored(self) -> None:
        headers = {"x-github-event": "issue_comment"}
        payload = {
            "action": "edited",
            "issue": {"number": 10, "pull_request": {"url": "..."}},
        }
        assert classify_event(headers, payload) == EventType.IGNORED


class TestUnknownEvents:
    def test_push_event_ignored(self) -> None:
        headers = {"x-github-event": "push"}
        payload = {}
        assert classify_event(headers, payload) == EventType.IGNORED

    def test_missing_event_header(self) -> None:
        headers = {}
        payload = {}
        assert classify_event(headers, payload) == EventType.IGNORED


# --- ADO vote classification (rejection-wins safety) ---
#
# Bug: ado_event_classifier._extract_latest_vote walked the reviewers
# array and returned the first non-zero vote under the assumption
# "ADO sends the changed reviewer first" — which is NOT guaranteed by
# the ADO webhook contract. When reviewer X previously approved (+10)
# and reviewer Y then rejected (-10), list order could place X first
# and the classifier would return REVIEW_APPROVED, masking Y's
# rejection and default-OPENing evaluate_and_maybe_merge.
# Fix: collapse all votes with "any rejection wins".


class TestADOReviewerVotes:
    def _updated_payload(self, reviewers: list[dict]) -> dict:
        return {
            "eventType": "git.pullrequest.updated",
            "resource": {
                "status": "active",
                "reviewers": reviewers,
            },
        }

    def test_rejection_wins_over_earlier_approval(self) -> None:
        """X approved +10 then Y rejected -10; rejection must win regardless of list order."""
        payload = self._updated_payload([
            {"displayName": "X", "vote": 10},
            {"displayName": "Y", "vote": -10},
        ])
        assert classify_ado_event(payload) == EventType.REVIEW_CHANGES_REQUESTED

    def test_rejection_wins_when_listed_last(self) -> None:
        payload = self._updated_payload([
            {"displayName": "A", "vote": 10},
            {"displayName": "B", "vote": 10},
            {"displayName": "C", "vote": -10},
        ])
        assert classify_ado_event(payload) == EventType.REVIEW_CHANGES_REQUESTED

    def test_waiting_for_author_treated_as_rejection(self) -> None:
        """ADO -5 = waiting for author — treat as changes-requested for auto-merge safety."""
        payload = self._updated_payload([
            {"displayName": "X", "vote": -5},
        ])
        assert classify_ado_event(payload) == EventType.REVIEW_CHANGES_REQUESTED

    def test_all_approved_classifies_approved(self) -> None:
        payload = self._updated_payload([
            {"displayName": "X", "vote": 10},
            {"displayName": "Y", "vote": 10},
        ])
        assert classify_ado_event(payload) == EventType.REVIEW_APPROVED

    def test_approved_with_suggestions_classifies_comment(self) -> None:
        """ADO 5 = approved with suggestions — classify as a comment, not approval."""
        payload = self._updated_payload([
            {"displayName": "X", "vote": 5},
        ])
        assert classify_ado_event(payload) == EventType.REVIEW_COMMENT

    def test_all_zero_votes_falls_through(self) -> None:
        """No actionable votes — should not classify as a review event."""
        payload = self._updated_payload([
            {"displayName": "X", "vote": 0},
        ])
        # Falls through to lastMergeSourceCommit / unhandled → IGNORED.
        assert classify_ado_event(payload) == EventType.IGNORED

    def test_empty_reviewers_with_commit_falls_through_to_sync(self) -> None:
        payload = {
            "eventType": "git.pullrequest.updated",
            "resource": {
                "status": "active",
                "reviewers": [],
                "lastMergeSourceCommit": {"commitId": "abc"},
            },
        }
        assert classify_ado_event(payload) == EventType.PR_SYNCHRONIZE
