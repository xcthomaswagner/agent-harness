"""Tests for ADO webhook routing in main.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app


@pytest.fixture
def _no_ado_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable ADO webhook token validation for testing."""
    monkeypatch.setattr("main.ADO_WEBHOOK_TOKEN", "")


def _ado_pr_payload(vote: int = 0, status: str = "active") -> dict:
    return {
        "eventType": "git.pullrequest.updated",
        "resource": {
            "pullRequestId": 42,
            "status": status,
            "sourceRefName": "refs/heads/ai/TICKET-123",
            "title": "Test PR",
            "repository": {
                "id": "repo-id",
                "name": "my-repo",
                "project": {"name": "my-project"},
            },
            "reviewers": [{"vote": vote}] if vote else [],
            "labels": [],
            "lastMergeSourceCommit": {"commitId": "abc123"},
        },
        "resourceContainers": {
            "collection": {"baseUrl": "https://dev.azure.com/org"},
        },
    }


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_webhook_review_approved_routes() -> None:
    """POST to /webhooks/ado-pr with vote=10 returns 202 + accepted."""
    payload = _ado_pr_payload(vote=10)

    with patch("main._handle_ado_review_approved", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/webhooks/ado-pr", json=payload)

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_webhook_changes_requested_routes() -> None:
    """POST with vote=-10 returns 202 + accepted."""
    payload = _ado_pr_payload(vote=-10)

    with patch("main._handle_ado_review_changes_requested", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/webhooks/ado-pr", json=payload)

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_webhook_review_comment_routes() -> None:
    """POST with vote=5 (approved with suggestions) returns 202 + accepted."""
    payload = _ado_pr_payload(vote=5)

    with patch("main._handle_ado_review_comment", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/webhooks/ado-pr", json=payload)

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_build_webhook_succeeded() -> None:
    """POST to /webhooks/ado-build with succeeded build returns 202."""
    payload = {
        "eventType": "build.complete",
        "resource": {
            "id": 123,
            "result": "succeeded",
            "reason": "pullRequest",
            "triggerInfo": {"pr.number": "42"},
            "repository": {"id": "repo-id"},
            "project": {"name": "my-project"},
            "sourceBranch": "refs/heads/ai/TICKET-123",
        },
        "resourceContainers": {
            "collection": {"baseUrl": "https://dev.azure.com/org"},
        },
    }

    with patch("main._handle_ado_build_complete", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/webhooks/ado-build", json=payload)

        assert resp.status_code == 202
        body = resp.json()
        assert body["event_type"] == "ci_passed"


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_build_complete_extracts_pr_number_flat_key() -> None:
    """Regression: PR number extraction from flat 'pr.number' key in triggerInfo
    must work (operator precedence bug fix)."""
    payload = {
        "eventType": "build.complete",
        "resource": {
            "id": 999,
            "result": "succeeded",
            "reason": "pullRequest",
            "triggerInfo": {"pr.number": "77"},
            "repository": {"id": "repo-id"},
            "project": {"name": "my-project"},
            "sourceBranch": "refs/heads/ai/TICKET-55",
        },
        "resourceContainers": {
            "collection": {"baseUrl": "https://dev.azure.com/org"},
        },
    }

    with patch(
        "main.evaluate_and_maybe_merge_ado", new_callable=AsyncMock
    ) as mock_eval:
        mock_eval.return_value = {"status": "dry_run"}
        # Call _handle_ado_build_complete directly to test extraction logic
        from main import _handle_ado_build_complete
        await _handle_ado_build_complete(payload)

        assert mock_eval.call_count == 1
        call_kwargs = mock_eval.call_args.kwargs
        assert call_kwargs["pr_id"] == 77, (
            f"Expected pr_id=77 from flat 'pr.number' key, got {call_kwargs['pr_id']}"
        )
        assert call_kwargs["ticket_id"] == "TICKET-55"
        assert call_kwargs["checks_passed"] is True


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_build_webhook_failed() -> None:
    """POST to /webhooks/ado-build with failed build returns 202."""
    payload = {
        "eventType": "build.complete",
        "resource": {"id": 123, "result": "failed"},
        "resourceContainers": {},
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/webhooks/ado-build", json=payload)

    assert resp.status_code == 202
    body = resp.json()
    assert body["event_type"] == "ci_failed"


# --- Task 3.2: ADO build webhook build_sha staleness check ---
#
# Before Phase 3 the ADO build webhook called evaluate_and_maybe_merge_ado
# with head_sha="" unconditionally. That meant a force-push that landed
# between CI start and CI complete silently merged stale code. Fix:
# extract sourceVersion from the build payload and pass it as head_sha;
# _evaluate_core skips with reason "build_sha_stale" if it no longer
# matches the PR's current head.


def _ado_build_payload(source_version: str = "") -> dict[str, object]:
    """ADO build.complete payload with optional sourceVersion."""
    resource: dict[str, object] = {
        "id": 999,
        "result": "succeeded",
        "reason": "pullRequest",
        "triggerInfo": {"pr.number": "42"},
        "repository": {"id": "repo-id"},
        "project": {"name": "my-project"},
        "sourceBranch": "refs/heads/ai/TICKET-55",
    }
    if source_version:
        resource["sourceVersion"] = source_version
    return {
        "eventType": "build.complete",
        "resource": resource,
        "resourceContainers": {
            "collection": {"baseUrl": "https://dev.azure.com/org"},
        },
    }


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_build_sha_matches_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the build's sourceVersion matches the PR's current head sha,
    _evaluate_core proceeds past the stale-sha guard and runs the full
    policy evaluation (which may then skip for other reasons like
    ticket_type not in low_risk, but crucially is reached).

    We assert by checking _record_decision received a decision that is
    NOT the build_sha_stale marker. The stale-sha skip short-circuits
    before reaching _record_decision, so hitting it is proof the sha
    check passed.
    """
    import auto_merge
    import autonomy_policy as ap

    auto_merge._clear_dedup()
    ap._cache_clear()
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "false")

    # Mock all downstream fetches so _evaluate_core actually runs
    monkeypatch.setattr(
        auto_merge, "fetch_profile_by_repo",
        AsyncMock(return_value={
            "client_profile": "acme",
            "low_risk_ticket_types": ["bug"],
            "auto_merge_enabled_yaml": True,
        }),
    )
    monkeypatch.setattr(
        auto_merge, "fetch_recommended_mode",
        AsyncMock(return_value=("semi_autonomous", "good")),
    )
    monkeypatch.setattr(
        auto_merge, "fetch_auto_merge_enabled", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        auto_merge, "get_ado_pr_state",
        AsyncMock(return_value={
            "author": "xcagentrockwell",
            "merged": False, "mergeable": True, "mergeable_state": "clean",
            "head_sha": "matching_sha",  # matches webhook build_sha below
            "approvals_count": 1, "human_approvals_count": 1,
            "changes_requested_count": 0, "checks_passed": True,
            "labels": ["bug"],
        }),
    )
    merger = AsyncMock(return_value=(True, "merged"))
    monkeypatch.setattr(auto_merge, "complete_ado_pr", merger)
    record_decision = AsyncMock()
    monkeypatch.setattr(auto_merge, "_record_decision", record_decision)

    payload = _ado_build_payload(source_version="matching_sha")
    from main import _handle_ado_build_complete
    await _handle_ado_build_complete(payload)

    # _record_decision being called at all means we made it past the
    # stale-sha check — the stale-sha branch returns before reaching
    # record_decision.
    assert record_decision.call_count >= 1, (
        "build_sha_stale short-circuited before policy evaluation — "
        "matching sha should NOT trigger stale skip"
    )
    call_reason = record_decision.call_args.kwargs.get("reason", "")
    assert call_reason != "build_sha_stale", (
        f"matching sha was treated as stale: reason={call_reason!r}"
    )


@pytest.mark.usefixtures("_no_ado_token")
async def test_ado_build_sha_stale_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When build's sourceVersion != PR head sha, _evaluate_core skips
    with reason 'build_sha_stale' and does NOT call the merger."""
    import auto_merge
    import autonomy_policy as ap

    auto_merge._clear_dedup()
    ap._cache_clear()
    monkeypatch.setenv("AUTO_MERGE_ENABLED", "true")

    monkeypatch.setattr(
        auto_merge, "fetch_profile_by_repo",
        AsyncMock(return_value={
            "client_profile": "acme",
            "low_risk_ticket_types": ["bug"],
            "auto_merge_enabled_yaml": True,
        }),
    )
    monkeypatch.setattr(
        auto_merge, "fetch_recommended_mode",
        AsyncMock(return_value=("semi_autonomous", "good")),
    )
    monkeypatch.setattr(
        auto_merge, "fetch_auto_merge_enabled", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        auto_merge, "get_ado_pr_state",
        AsyncMock(return_value={
            "author": "xcagentrockwell",
            "merged": False, "mergeable": True, "mergeable_state": "clean",
            "head_sha": "current_pr_head",  # PR got force-pushed
            "approvals_count": 1, "human_approvals_count": 1,
            "changes_requested_count": 0, "checks_passed": True,
            "labels": ["bug"],
        }),
    )
    merger = AsyncMock(return_value=(True, "merged"))
    monkeypatch.setattr(auto_merge, "complete_ado_pr", merger)
    monkeypatch.setattr(auto_merge, "_record_decision", AsyncMock())

    # Webhook's sourceVersion is the STALE sha (build ran on old head
    # before force-push).
    payload = _ado_build_payload(source_version="stale_build_sha")
    from main import _handle_ado_build_complete
    await _handle_ado_build_complete(payload)

    # Merger must NOT have been called
    assert merger.call_count == 0, (
        "complete_ado_pr called despite stale build_sha — sha-check bypassed"
    )

    # The dedup outcome should be "skipped" (build_sha_stale)
    dedup = auto_merge._dedup_key("my-project/repo-id", 42, "stale_build_sha")
    outcome = auto_merge._dedup_store.get_outcome(dedup)
    assert outcome == "skipped", (
        f"Expected skipped for stale build, got {outcome}"
    )
