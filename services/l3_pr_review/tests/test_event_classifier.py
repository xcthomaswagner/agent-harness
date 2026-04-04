"""Tests for GitHub webhook event classification."""

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

    def test_pr_closed_ignored(self) -> None:
        headers = {"x-github-event": "pull_request"}
        payload = {"action": "closed"}
        assert classify_event(headers, payload) == EventType.IGNORED


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
