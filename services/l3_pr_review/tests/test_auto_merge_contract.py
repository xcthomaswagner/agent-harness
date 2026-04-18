"""HTTP-level contract tests for ``auto_merge.evaluate_and_maybe_merge``.

Instead of mocking at the ``get_pr_state`` / ``merge_pr`` seam (which
is what ``test_auto_merge.py`` already does extensively), these tests
mock at the ``httpx`` transport using ``respx``. That catches a class
of bugs invisible to function-level mocks: the API *request shape*
(method, path, headers, body), which is what actually hits GitHub in
production.

If someone ever swaps PUT for POST on ``/merge``, drops the
``Authorization: token`` header, renames ``sha`` to ``head_sha`` in
the body, or points at the wrong ``api.github.com`` path, function-
level mocks will happily return their canned response. Transport-
level assertions will fail loud.

Two test cases here:
  * happy path — mode=balanced + kill-switch on, merge returns 200, we
    observe the exact PUT request GitHub gets.
  * failure contract — merge returns 409 (sha_mismatch), we observe
    auto_merge records ``failed`` + ``sha_mismatch`` reason instead of
    re-trying or crashing.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx  # type: ignore[import-not-found]

import auto_merge
import autonomy_policy as ap
from auto_merge import evaluate_and_maybe_merge


@pytest.fixture(autouse=True)
def _clear_state(monkeypatch: pytest.MonkeyPatch) -> None:
    auto_merge._clear_dedup()
    ap._cache_clear()
    # Env for auto-merge:
    monkeypatch.setenv("L1_SERVICE_URL", "http://l1.test")
    monkeypatch.setenv("L1_INTERNAL_API_TOKEN", "l1-secret")
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")
    monkeypatch.setenv("BOT_GITHUB_USERNAME", "xcagentrockwell")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")


def _profile_body() -> dict[str, Any]:
    return {
        "client_profile": "acme",
        "low_risk_ticket_types": ["bug", "chore"],
        "auto_merge_enabled_yaml": True,
    }


def _autonomy_body() -> dict[str, Any]:
    # semi_autonomous + low-risk ticket type + bot author + human
    # approval satisfies every gate in ``autonomy_policy._GATES``.
    return {
        "recommended_mode": "semi_autonomous",
        "data_quality": {"status": "good"},
    }


def _toggle_body() -> dict[str, Any]:
    return {"enabled": True}


def _pr_body(head_sha: str = "abc123") -> dict[str, Any]:
    return {
        "user": {"login": "xcagentrockwell"},
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": head_sha},
        "labels": [{"name": "bug"}],
        "title": "fix: something",
    }


def _reviews_body(head_sha: str = "abc123") -> list[dict[str, Any]]:
    """One human APPROVED review against the target head_sha.

    The approval gate requires ``human_approvals_count > 0`` — and
    ``get_pr_state`` only counts APPROVED reviews whose ``commit_id``
    matches the current head_sha (stale-approval defense).
    """
    return [
        {
            "state": "APPROVED",
            "commit_id": head_sha,
            "user": {"login": "human-reviewer", "type": "User"},
        },
    ]


def _check_suites_body() -> dict[str, Any]:
    return {
        "check_suites": [
            {"status": "completed", "conclusion": "success"},
        ],
    }


def _merge_success_body() -> dict[str, Any]:
    return {"merged": True, "message": "PR merged"}


@respx.mock  # type: ignore[misc]
async def test_evaluate_and_maybe_merge_issues_correct_github_requests() -> None:
    """Happy path: every outbound request has the right method, path, and auth.

    Proves the minimal GitHub contract: one GET to fetch PR, one GET
    for reviews, one GET for check-suites, and one PUT to /merge with
    the captured head_sha in the body.
    """
    # L1 audit endpoints (autonomy_policy fetches + audit POST).
    profile_route = respx.get(
        "http://l1.test/api/internal/autonomy/profile-by-repo"
    ).mock(return_value=httpx.Response(200, json=_profile_body()))
    mode_route = respx.get("http://l1.test/api/autonomy").mock(
        return_value=httpx.Response(200, json=_autonomy_body())
    )
    toggle_route = respx.get(
        "http://l1.test/api/autonomy/auto-merge-toggle"
    ).mock(return_value=httpx.Response(200, json=_toggle_body()))
    audit_route = respx.post(
        "http://l1.test/api/internal/autonomy/auto-merge-decisions"
    ).mock(return_value=httpx.Response(200, json={"ok": True}))

    # GitHub endpoints.
    pr_route = respx.get(
        "https://api.github.com/repos/acme/repo/pulls/1"
    ).mock(return_value=httpx.Response(200, json=_pr_body()))
    reviews_route = respx.get(
        "https://api.github.com/repos/acme/repo/pulls/1/reviews"
    ).mock(return_value=httpx.Response(200, json=_reviews_body("abc123")))
    checks_route = respx.get(
        "https://api.github.com/repos/acme/repo/commits/abc123/check-suites"
    ).mock(return_value=httpx.Response(200, json=_check_suites_body()))
    merge_route = respx.put(
        "https://api.github.com/repos/acme/repo/pulls/1/merge",
        name="merge",
    ).mock(return_value=httpx.Response(200, json=_merge_success_body()))

    result = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=1,
        head_sha="abc123",
        ticket_id="ACME-1",
        ticket_type="bug",
        trigger_event="review_approved",
    )

    # Outcome: merged.
    assert result["status"] == "merged", result

    # Every L1 + GitHub route fired exactly the expected number of times.
    assert profile_route.called
    assert mode_route.called
    assert toggle_route.called
    assert pr_route.called
    assert reviews_route.called
    assert checks_route.called
    assert merge_route.call_count == 1
    assert audit_route.called

    # Merge request shape — this is the load-bearing GitHub contract:
    # PUT /repos/{owner}/{repo}/pulls/{n}/merge with sha in the body
    # and "Authorization: token <pat>" on the headers.
    merge_request = merge_route.calls.last.request
    assert merge_request.method == "PUT"
    assert merge_request.url.path == "/repos/acme/repo/pulls/1/merge"
    auth_header = merge_request.headers.get("authorization", "")
    assert auth_header.lower() == "token ghp_test"
    body_bytes = merge_request.content
    # Body is JSON with {"sha": "abc123", "merge_method": "squash"}.
    import json as _json
    body = _json.loads(body_bytes)
    assert body["sha"] == "abc123"
    assert body.get("merge_method") == "squash"

    # Reviews/PR endpoints use the same Authorization header — a
    # silent drop to unauthenticated requests would crater reliability.
    pr_auth = pr_route.calls.last.request.headers.get("authorization", "")
    assert pr_auth.lower() == "token ghp_test"


@respx.mock  # type: ignore[misc]
async def test_evaluate_and_maybe_merge_records_sha_mismatch_on_409() -> None:
    """Failure contract: GitHub returning 409 yields decision=failed/sha_mismatch.

    A 409 on /merge means the head_sha we asked to merge no longer
    matches the current PR tip (optimistic-lock race after a force-push).
    ``merge_pr`` translates 409 → ``(False, "sha_mismatch")``; this
    test pins the end-to-end translation up to the final status dict.
    """
    respx.get(
        "http://l1.test/api/internal/autonomy/profile-by-repo"
    ).mock(return_value=httpx.Response(200, json=_profile_body()))
    respx.get("http://l1.test/api/autonomy").mock(
        return_value=httpx.Response(200, json=_autonomy_body())
    )
    respx.get(
        "http://l1.test/api/autonomy/auto-merge-toggle"
    ).mock(return_value=httpx.Response(200, json=_toggle_body()))
    respx.post(
        "http://l1.test/api/internal/autonomy/auto-merge-decisions"
    ).mock(return_value=httpx.Response(200, json={"ok": True}))

    respx.get(
        "https://api.github.com/repos/acme/repo/pulls/2"
    ).mock(return_value=httpx.Response(200, json=_pr_body(head_sha="newsha")))
    respx.get(
        "https://api.github.com/repos/acme/repo/pulls/2/reviews"
    ).mock(return_value=httpx.Response(200, json=_reviews_body("newsha")))
    respx.get(
        "https://api.github.com/repos/acme/repo/commits/newsha/check-suites"
    ).mock(return_value=httpx.Response(200, json=_check_suites_body()))
    # 409 — optimistic lock mismatch.
    merge_route = respx.put(
        "https://api.github.com/repos/acme/repo/pulls/2/merge",
    ).mock(return_value=httpx.Response(409, json={"message": "sha mismatch"}))

    result = await evaluate_and_maybe_merge(
        repo_full_name="acme/repo",
        pr_number=2,
        head_sha="newsha",
        ticket_id="ACME-2",
        ticket_type="bug",
        trigger_event="ci_passed",
    )

    # Merge was attempted (the PUT fired) but decision captured the failure.
    assert merge_route.called
    assert result["status"] == "failed"
    assert result["reason"] == "sha_mismatch"


@respx.mock  # type: ignore[misc]
async def test_evaluate_and_maybe_merge_honors_kill_switch_without_merging() -> None:
    """When AUTO_MERGE_ENABLED=false the PUT /merge is NEVER issued.

    This is the most critical kill-switch invariant: a misconfigured
    profile or a bug that flips a gate must not cause a merge while
    the global flag is off. Transport-level assertion so even a
    direct-httpx regression is caught.
    """
    import os
    os.environ["AUTO_MERGE_ENABLED"] = "false"
    try:
        respx.get(
            "http://l1.test/api/internal/autonomy/profile-by-repo"
        ).mock(return_value=httpx.Response(200, json=_profile_body()))
        respx.get("http://l1.test/api/autonomy").mock(
            return_value=httpx.Response(200, json=_autonomy_body())
        )
        respx.get(
            "http://l1.test/api/autonomy/auto-merge-toggle"
        ).mock(return_value=httpx.Response(200, json=_toggle_body()))
        respx.post(
            "http://l1.test/api/internal/autonomy/auto-merge-decisions"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))

        respx.get(
            "https://api.github.com/repos/acme/repo/pulls/3"
        ).mock(return_value=httpx.Response(200, json=_pr_body()))
        respx.get(
            "https://api.github.com/repos/acme/repo/pulls/3/reviews"
        ).mock(return_value=httpx.Response(200, json=_reviews_body("abc123")))
        respx.get(
            "https://api.github.com/repos/acme/repo/commits/abc123/check-suites"
        ).mock(return_value=httpx.Response(200, json=_check_suites_body()))
        merge_route = respx.put(
            "https://api.github.com/repos/acme/repo/pulls/3/merge",
        ).mock(return_value=httpx.Response(200, json=_merge_success_body()))

        result = await evaluate_and_maybe_merge(
            repo_full_name="acme/repo",
            pr_number=3,
            head_sha="abc123",
            ticket_id="ACME-3",
            ticket_type="bug",
            trigger_event="review_approved",
        )

        # Dry-run decision with the kill switch off.
        assert result["status"] == "dry_run"
        assert merge_route.called is False, (
            "PUT /merge must NOT fire when AUTO_MERGE_ENABLED=false"
        )
    finally:
        os.environ["AUTO_MERGE_ENABLED"] = "true"
