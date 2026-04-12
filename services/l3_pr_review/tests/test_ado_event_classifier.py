"""Tests for ado_event_classifier: ADO webhook event classification."""
from __future__ import annotations

from ado_event_classifier import classify_ado_event
from event_classifier import EventType

# --- build.complete events ---


def test_classify_build_complete_succeeded() -> None:
    payload = {
        "eventType": "build.complete",
        "resource": {"result": "succeeded"},
    }
    assert classify_ado_event(payload) == EventType.CI_PASSED


def test_classify_build_complete_failed() -> None:
    payload = {
        "eventType": "build.complete",
        "resource": {"result": "failed"},
    }
    assert classify_ado_event(payload) == EventType.CI_FAILED


def test_classify_build_complete_partially_succeeded() -> None:
    payload = {
        "eventType": "build.complete",
        "resource": {"result": "partiallySucceeded"},
    }
    assert classify_ado_event(payload) == EventType.CI_FAILED


def test_classify_build_complete_canceled() -> None:
    payload = {
        "eventType": "build.complete",
        "resource": {"result": "canceled"},
    }
    assert classify_ado_event(payload) == EventType.IGNORED


# --- existing PR event classification (regression checks) ---


def test_classify_pr_created() -> None:
    payload = {
        "eventType": "git.pullrequest.created",
        "resource": {},
    }
    assert classify_ado_event(payload) == EventType.PR_OPENED


def test_classify_pr_updated_approved() -> None:
    payload = {
        "eventType": "git.pullrequest.updated",
        "resource": {
            "status": "active",
            "reviewers": [{"vote": 10}],
        },
    }
    assert classify_ado_event(payload) == EventType.REVIEW_APPROVED


def test_classify_pr_updated_rejected() -> None:
    payload = {
        "eventType": "git.pullrequest.updated",
        "resource": {
            "status": "active",
            "reviewers": [{"vote": -10}],
        },
    }
    assert classify_ado_event(payload) == EventType.REVIEW_CHANGES_REQUESTED


def test_classify_pr_completed() -> None:
    payload = {
        "eventType": "git.pullrequest.updated",
        "resource": {"status": "completed"},
    }
    assert classify_ado_event(payload) == EventType.PR_MERGED
