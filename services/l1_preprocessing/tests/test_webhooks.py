"""Tests for the webhook receiver endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

import main
from main import app

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


async def _make_client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# --- Jira Webhook ---


async def test_jira_webhook_accepts_valid_payload() -> None:
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())
    async with await _make_client() as client:
        response = await client.post("/webhooks/jira", json=payload)
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["ticket_id"] == "ACME-42"


async def test_jira_webhook_bug_payload() -> None:
    payload = json.loads((FIXTURES / "jira_webhook_bug.json").read_text())
    async with await _make_client() as client:
        response = await client.post("/webhooks/jira", json=payload)
        assert response.status_code == 202
        assert response.json()["ticket_id"] == "ACME-99"


async def test_jira_webhook_validates_signature_when_secret_set() -> None:
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())
    body = json.dumps(payload).encode()
    secret = "test-secret-123"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = secret
        mock_settings.jira_base_url = "https://test.atlassian.net"
        mock_settings.jira_api_token = "token"
        mock_settings.jira_user_email = "bot@test.com"
        mock_settings.jira_ac_field_id = "customfield_10429"

        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/jira",
                content=body,
                headers={"Content-Type": "application/json", "x-hub-signature": signature},
            )
            assert response.status_code == 202


async def test_jira_webhook_rejects_invalid_signature() -> None:
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = "real-secret"

        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/jira",
                json=payload,
                headers={"x-hub-signature": "sha256=bad"},
            )
            assert response.status_code == 401


async def test_jira_webhook_rejects_missing_signature_when_secret_set() -> None:
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = "real-secret"

        async with await _make_client() as client:
            response = await client.post("/webhooks/jira", json=payload)
            assert response.status_code == 401


async def test_jira_webhook_skips_signature_when_no_secret() -> None:
    """When webhook_secret is empty, signature validation is skipped."""
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())
    async with await _make_client() as client:
        response = await client.post("/webhooks/jira", json=payload)
        assert response.status_code == 202


# --- ADO Webhook ---


def _ado_payload(
    *,
    work_item_id: int = 10,
    title: str = "ADO test task",
    project: str = "TestProject",
    tags: str = "ai-implement",
    work_item_type: str = "Task",
) -> dict[str, object]:
    """Build a minimal ADO Service Hook payload for tests."""
    return {
        "eventType": "workitem.updated",
        "resource": {
            "workItemId": work_item_id,
            "revision": {
                "id": work_item_id,
                "fields": {
                    "System.WorkItemType": work_item_type,
                    "System.Title": title,
                    "System.TeamProject": project,
                    "System.Tags": tags,
                },
            },
        },
    }


async def test_ado_webhook_accepts_valid_payload() -> None:
    """When no auth secrets are configured, webhook is accepted (local dev)."""
    payload = _ado_payload(tags="ai-implement")
    async with await _make_client() as client:
        response = await client.post("/webhooks/ado", json=payload)
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"


async def test_ado_webhook_rejects_non_json() -> None:
    async with await _make_client() as client:
        response = await client.post(
            "/webhooks/ado",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422


async def test_ado_webhook_accepts_token_header() -> None:
    """ADO webhook accepts X-ADO-Webhook-Token header."""
    payload = _ado_payload(tags="ai-implement")
    body = json.dumps(payload).encode()

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = ""
        mock_settings.ado_webhook_token = "ado-secret-abc"
        mock_settings.ado_org_url = ""
        mock_settings.ado_pat = ""

        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/ado",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "x-ado-webhook-token": "ado-secret-abc",
                },
            )
            assert response.status_code == 202


async def test_ado_webhook_rejects_without_auth() -> None:
    """When ado_webhook_token is set, requests without auth are rejected."""
    payload = _ado_payload(tags="ai-implement")

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = ""
        mock_settings.ado_webhook_token = "ado-secret-abc"

        async with await _make_client() as client:
            response = await client.post("/webhooks/ado", json=payload)
            assert response.status_code == 401


async def test_ado_webhook_rejects_wrong_token() -> None:
    """Wrong token value is rejected."""
    payload = _ado_payload(tags="ai-implement")

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = ""
        mock_settings.ado_webhook_token = "ado-secret-abc"

        async with await _make_client() as client:
            response = await client.post(
                "/webhooks/ado",
                json=payload,
                headers={"x-ado-webhook-token": "wrong-token"},
            )
            assert response.status_code == 401


async def test_ado_webhook_skips_when_no_ai_tag() -> None:
    """Work items without the ai-implement tag are skipped."""
    payload = _ado_payload(tags="sprint-7; enhancement")
    async with await _make_client() as client:
        response = await client.post("/webhooks/ado", json=payload)
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "skipped"
        assert "ai-implement" in data["reason"]


async def test_ado_webhook_processes_when_ai_tag_present() -> None:
    """Work items with the ai-implement tag are accepted."""
    payload = _ado_payload(tags="ai-implement; sprint-7")
    async with await _make_client() as client:
        response = await client.post("/webhooks/ado", json=payload)
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"


def _mock_process_ticket_that_releases() -> AsyncMock:
    """Build an AsyncMock replacement for _process_ticket that releases the
    active ticket claim (matching the real function's no-spawn behavior) but
    does NOT clear trigger state. Lets webhook-level tests exercise edge
    detection without the real pipeline or the no-spawn state-clear path.
    """
    async def _release(ticket, trace_id=""):
        main._release_ticket(ticket.id)
    return AsyncMock(side_effect=_release)


async def test_ado_webhook_second_call_with_same_tag_is_not_a_new_edge() -> None:
    """Regression test for Finding 4 from session 2026-04-10 post-mortem.

    The trigger tag is a one-shot edge, not a level. Two consecutive webhooks
    with the tag present (common when ADO fires workitem.updated for a merge
    commit, comment, or field edit after the pipeline already dispatched) must
    NOT re-trigger the pipeline. The first call edges absent→present and
    accepts; the second sees present→present and skips.

    _process_ticket is mocked out so the no-spawn state-clear path from Bug 1's
    fix doesn't reset the edge memory between the two webhooks — we're testing
    the webhook-handler level edge detection in isolation.
    """
    payload = _ado_payload(work_item_id=7777, tags="ai-implement")
    with patch.object(main, "_process_ticket", _mock_process_ticket_that_releases()):
        async with await _make_client() as client:
            first = await client.post("/webhooks/ado", json=payload)
            assert first.status_code == 202
            assert first.json()["status"] == "accepted"

            second = await client.post("/webhooks/ado", json=payload)
            assert second.status_code == 202
            assert second.json()["status"] == "skipped"
            assert "not a new edge" in second.json()["reason"]


async def test_ado_webhook_tag_removed_then_readded_retriggers() -> None:
    """If the tag is removed and then re-added, that IS a new edge and the
    pipeline should fire again. Without clearing state on tag-absent webhooks
    a re-add after the first cascade would be silently skipped forever."""
    with patch.object(main, "_process_ticket", _mock_process_ticket_that_releases()):
        async with await _make_client() as client:
            # First fire: absent → present. Accepted.
            p1 = _ado_payload(work_item_id=8888, tags="ai-implement")
            r1 = await client.post("/webhooks/ado", json=p1)
            assert r1.json()["status"] == "accepted"

            # Tag removed. Webhook arrives with no trigger tags. State cleared.
            p2 = _ado_payload(work_item_id=8888, tags="sprint-7")
            r2 = await client.post("/webhooks/ado", json=p2)
            assert r2.json()["status"] == "skipped"
            assert "ai-implement" in r2.json()["reason"]  # reason is "tag not found"

            # Tag re-added. Should be treated as a fresh edge, accepted again.
            p3 = _ado_payload(work_item_id=8888, tags="ai-implement")
            r3 = await client.post("/webhooks/ado", json=p3)
            assert r3.json()["status"] == "accepted"


def _seed_ticket(ticket_id: str):
    """Seed a ticket as mid-flight (tag observed, claim held) and return a
    TicketPayload matching what the ADO adapter would normalize. Shared by
    the _process_ticket direct regression tests so each one doesn't repeat
    the four-line setup for _last_trigger_state + _active_tickets + payload
    construction.
    """
    from models import TicketPayload

    main._last_trigger_state[ticket_id] = True
    main._active_tickets.add(ticket_id)
    return TicketPayload(
        source="ado", id=ticket_id, ticket_type="story",
        title="t", description="d", acceptance_criteria=["a"],
    )


async def test_process_ticket_clears_trigger_state_on_exception() -> None:
    """Regression: if _process_ticket raises, the trigger-state must be
    cleared so a subsequent webhook for the same ticket can re-trigger.

    Without this, _check_trigger_edge has already set the state to True at
    webhook receipt, and the next webhook (with the same tag still present)
    is silently dropped as "not a new edge" — permanently wedging the ticket
    until the tag is removed-and-readded or the service restarts.
    """
    ticket = _seed_ticket("FAIL-1")

    failing_pipeline = AsyncMock()
    failing_pipeline.process = AsyncMock(side_effect=RuntimeError("pipeline boom"))

    with patch("main._get_pipeline", return_value=failing_pipeline):
        await main._process_ticket(ticket)

    assert "FAIL-1" not in main._last_trigger_state, \
        "trigger state must be cleared on pipeline failure"
    assert "FAIL-1" not in main._active_tickets, \
        "ticket should also be released from _active_tickets on failure"


async def test_process_ticket_clears_trigger_state_on_no_spawn() -> None:
    """Regression: if the pipeline returns without spawning L2 (e.g. analyst
    decided the ticket needs clarification), trigger-state must be cleared
    so the user can re-trigger after addressing the clarification request.
    """
    ticket = _seed_ticket("NOSPAWN-1")

    # Pipeline returns without spawn_triggered (analyst flagged as
    # needs-clarification, or enrichment failed, or any non-L2 path).
    no_spawn_pipeline = AsyncMock()
    no_spawn_pipeline.process = AsyncMock(
        return_value={"ticket_id": "NOSPAWN-1", "status": "needs_clarification"}
    )

    with patch("main._get_pipeline", return_value=no_spawn_pipeline):
        await main._process_ticket(ticket)

    assert "NOSPAWN-1" not in main._last_trigger_state, \
        "trigger state must be cleared on no-spawn completion"
    assert "NOSPAWN-1" not in main._active_tickets


async def test_process_ticket_keeps_trigger_state_on_successful_spawn() -> None:
    """When L2 is spawned successfully, _process_ticket must NOT clear the
    trigger state (agent-complete's _delayed_release owns cleanup). Clearing
    early would allow cascading webhooks to re-dispatch during the agent run.
    """
    ticket = _seed_ticket("SPAWN-1")

    spawn_pipeline = AsyncMock()
    spawn_pipeline.process = AsyncMock(
        return_value={"ticket_id": "SPAWN-1", "spawn_triggered": True}
    )

    with patch("main._get_pipeline", return_value=spawn_pipeline):
        await main._process_ticket(ticket)

    assert main._last_trigger_state.get("SPAWN-1") is True, \
        "trigger state must persist while L2 is running — cleared on agent_complete"
    assert "SPAWN-1" in main._active_tickets, \
        "active ticket must also persist until agent_complete"


async def test_ado_webhook_remaps_ticket_id(tmp_path: Path) -> None:
    """When a matching ADO profile exists, ticket ID is remapped to project_key prefix."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "xcsf30.yaml").write_text(
        "client: XCSF\n"
        "ticket_source:\n"
        "  type: ado\n"
        "  project_key: XCSF30\n"
        "  ado_project_name: XC-SF-30in30\n"
        "  ai_label: ai-implement\n"
    )

    payload = _ado_payload(work_item_id=123, project="XC-SF-30in30", tags="ai-implement")

    with patch("main.find_profile_by_ado_project") as mock_find:
        from client_profile import ClientProfile

        profile_data = {
            "client": "XCSF",
            "ticket_source": {
                "type": "ado",
                "project_key": "XCSF30",
                "ado_project_name": "XC-SF-30in30",
                "ai_label": "ai-implement",
            },
        }
        mock_find.return_value = ClientProfile(profile_data, name="xcsf30")

        async with await _make_client() as client:
            response = await client.post("/webhooks/ado", json=payload)
            assert response.status_code == 202
            data = response.json()
            assert data["status"] == "accepted"
            assert data["ticket_id"] == "XCSF30-123"


# --- Manual Process Ticket ---


async def test_manual_process_ticket() -> None:
    ticket = {
        "source": "jira",
        "id": "TEST-1",
        "ticket_type": "story",
        "title": "Test ticket",
        "description": "A test",
        "acceptance_criteria": ["It works"],
    }
    async with await _make_client() as client:
        response = await client.post("/api/process-ticket", json=ticket)
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["ticket_id"] == "TEST-1"


async def test_manual_process_ticket_validation_error() -> None:
    """Missing required fields should return 422."""
    async with await _make_client() as client:
        response = await client.post("/api/process-ticket", json={"title": "incomplete"})
        assert response.status_code == 422


# --- Agent Completion Callback ---


async def test_agent_complete_updates_jira() -> None:
    completion = {
        "ticket_id": "SCRUM-1",
        "status": "complete",
        "pr_url": "https://github.com/org/repo/pull/1",
        "branch": "ai/SCRUM-1",
    }
    with patch.object(main, "_get_jira_adapter") as mock_get:
        mock_adapter = AsyncMock()
        mock_get.return_value = mock_adapter
        async with await _make_client() as client:
            response = await client.post("/api/agent-complete", json=completion)
        assert response.status_code == 200
        mock_adapter.write_comment.assert_called_once()
        mock_adapter.transition_status.assert_called_once_with("SCRUM-1", "Done")


async def test_agent_complete_partial_adds_label() -> None:
    completion = {
        "ticket_id": "SCRUM-2",
        "status": "partial",
        "pr_url": "https://github.com/org/repo/pull/2",
        "branch": "ai/SCRUM-2",
    }
    with patch.object(main, "_get_jira_adapter") as mock_get:
        mock_adapter = AsyncMock()
        mock_get.return_value = mock_adapter
        async with await _make_client() as client:
            response = await client.post("/api/agent-complete", json=completion)
        assert response.status_code == 200
        mock_adapter.add_label.assert_called_once_with("SCRUM-2", "partial-implementation")
        mock_adapter.transition_status.assert_not_called()


async def test_agent_complete_escalated_adds_label() -> None:
    completion = {
        "ticket_id": "SCRUM-3",
        "status": "escalated",
        "pr_url": "",
        "branch": "ai/SCRUM-3",
    }
    with patch.object(main, "_get_jira_adapter") as mock_get:
        mock_adapter = AsyncMock()
        mock_get.return_value = mock_adapter
        async with await _make_client() as client:
            response = await client.post("/api/agent-complete", json=completion)
        assert response.status_code == 200
        mock_adapter.add_label.assert_called_once_with("SCRUM-3", "needs-human")


async def test_webhook_stats_counters() -> None:
    """The /stats/webhooks endpoint exposes cumulative counters for each
    ADO webhook outcome (accepted edge, skipped not-edge, skipped no-tag)
    plus the current release-delay setting and live-state sizes. This is
    the operator-facing visibility for the cascade-prevention fix.

    _process_ticket is patched out so the real analyst pipeline doesn't
    run — we're testing the webhook-handler counter wiring, not the
    pipeline. Without the patch the no-spawn-state-clear behavior from
    Bug 1's fix would reset _last_trigger_state between calls and the
    second webhook would hit the accepted path instead of not-edge.
    """
    with patch.object(main, "_process_ticket", new_callable=AsyncMock):
        async with await _make_client() as client:
            # Accepted edge: fresh ticket, tag present.
            accepted = _ado_payload(work_item_id=9001, tags="ai-implement")
            r = await client.post("/webhooks/ado", json=accepted)
            assert r.json()["status"] == "accepted"

            # Skipped not-edge: same ticket, tag still present.
            r = await client.post("/webhooks/ado", json=accepted)
            assert r.json()["status"] == "skipped"
            assert "not a new edge" in r.json()["reason"]

            # Skipped no-tag: different ticket, no trigger tag.
            no_tag = _ado_payload(work_item_id=9002, tags="sprint-7")
            r = await client.post("/webhooks/ado", json=no_tag)
            assert r.json()["status"] == "skipped"

            stats = await client.get("/stats/webhooks")
            assert stats.status_code == 200
            body = stats.json()

    counters = body["counters"]
    assert counters[main.COUNTER_ACCEPTED_EDGE] == 1
    assert counters[main.COUNTER_SKIPPED_NOT_EDGE] == 1
    assert counters[main.COUNTER_SKIPPED_NO_TAG] == 1
    assert body["release_delay_sec"] == main.settings.agent_complete_release_delay_sec


async def test_agent_complete_clears_trigger_state_after_delay() -> None:
    """After agent-complete fires, the delayed-release background task must
    clear both _active_tickets and _last_trigger_state once the cooldown
    window expires. This path was previously untested because the default
    60-second delay makes tests impractical — now configurable via
    settings.agent_complete_release_delay_sec which we monkeypatch to 0.
    """
    import asyncio

    # Seed state as if a pipeline had dispatched this ticket and was now
    # completing. _check_trigger_edge would have set state=True at webhook
    # receipt; _process_ticket keeps it around because spawn_triggered=True.
    main._active_tickets.add("SCRUM-9")
    main._last_trigger_state["SCRUM-9"] = True

    completion = {
        "ticket_id": "SCRUM-9",
        "status": "complete",
        "pr_url": "https://github.com/org/repo/pull/9",
        "branch": "ai/SCRUM-9",
    }

    # Snapshot background tasks BEFORE the POST so we can identify the new
    # _delayed_release task created by agent_complete.
    before = set(main._BACKGROUND_TASKS)

    with (
        patch.object(main.settings, "agent_complete_release_delay_sec", 0),
        patch.object(main, "_get_jira_adapter") as mock_get,
    ):
        mock_adapter = AsyncMock()
        mock_get.return_value = mock_adapter

        async with await _make_client() as client:
            response = await client.post("/api/agent-complete", json=completion)
        assert response.status_code == 200

        # Grab the delayed-release task that agent_complete just created and
        # await it directly instead of guessing the right number of event-loop
        # yields. This keeps the test deterministic if _delayed_release grows
        # new await points in the future.
        new_tasks = main._BACKGROUND_TASKS - before
        assert new_tasks, "agent_complete should have scheduled a delayed_release task"
        await asyncio.gather(*new_tasks)

    assert "SCRUM-9" not in main._active_tickets, \
        "ticket should be released from _active_tickets after delay"
    assert "SCRUM-9" not in main._last_trigger_state, \
        "trigger state should be cleared after delay so future re-tag retriggers"


# --- Jira Webhook: malformed payloads ---


async def test_jira_webhook_rejects_non_json_body() -> None:
    """Non-JSON body should return 422."""
    async with await _make_client() as client:
        response = await client.post(
            "/webhooks/jira",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422


# --- Background task enqueuing ---


async def test_jira_webhook_enqueues_background_task() -> None:
    """Verify that _process_ticket is scheduled as a background task."""
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())

    with patch.object(main, "_process_ticket", new_callable=AsyncMock) as mock_process:
        async with await _make_client() as client:
            response = await client.post("/webhooks/jira", json=payload)
            assert response.status_code == 202

        # Background tasks run after response in test transport
        mock_process.assert_called_once()
        ticket = mock_process.call_args[0][0]
        assert ticket.id == "ACME-42"
        assert ticket.source == "jira"


async def test_manual_process_enqueues_background_task() -> None:
    """Verify that manual endpoint also enqueues the background task."""
    ticket_data = {
        "source": "jira",
        "id": "TEST-99",
        "ticket_type": "bug",
        "title": "Background task test",
        "description": "Verify enqueuing",
        "acceptance_criteria": [],
    }

    with patch.object(main, "_process_ticket", new_callable=AsyncMock) as mock_process:
        async with await _make_client() as client:
            response = await client.post("/api/process-ticket", json=ticket_data)
            assert response.status_code == 202

        mock_process.assert_called_once()
        ticket = mock_process.call_args[0][0]
        assert ticket.id == "TEST-99"


# --- HMAC edge cases ---


async def test_jira_webhook_accepts_signature_without_prefix() -> None:
    """A raw hex signature (no sha256= prefix) is accepted via removeprefix fallback."""
    payload = json.loads((FIXTURES / "jira_webhook_story.json").read_text())
    body = json.dumps(payload).encode()
    secret = "test-secret-456"
    raw_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    with patch("main.settings") as mock_settings:
        mock_settings.webhook_secret = secret
        mock_settings.jira_base_url = "https://test.atlassian.net"
        mock_settings.jira_api_token = "token"
        mock_settings.jira_user_email = "bot@test.com"
        mock_settings.jira_ac_field_id = "customfield_10429"

        async with await _make_client() as client:
            # Send signature without "sha256=" prefix -- should still pass
            # because removeprefix is a no-op when prefix is absent
            response = await client.post(
                "/webhooks/jira",
                content=body,
                headers={"Content-Type": "application/json", "x-hub-signature": raw_hex},
            )
            assert response.status_code == 202


# --- Jira Bug Webhook ---


class TestJiraBugWebhook:
    @staticmethod
    def _bug_payload(
        parent_key: str = "PROJ-1", bug_key: str = "BUG-1"
    ) -> dict[str, object]:
        return {
            "issue": {
                "key": bug_key,
                "fields": {
                    "issuetype": {"name": "Bug"},
                    "created": "2026-04-03T10:00:00.000+0000",
                    "priority": {"name": "High"},
                    "labels": [],
                    "summary": "Checkout fails",
                    "description": "Details",
                    "parent": {
                        "key": parent_key,
                        "fields": {"issuetype": {"name": "Task"}},
                    },
                    "issuelinks": [],
                },
            },
        }

    async def test_webhook_auth_rejects_without_secret_or_token(self) -> None:
        # Both unset -> 503
        with patch("main.settings") as mock_settings:
            mock_settings.webhook_secret = ""
            mock_settings.jira_bug_webhook_token = ""
            async with await _make_client() as client:
                r = await client.post(
                    "/webhooks/jira-bug", json=self._bug_payload()
                )
            assert r.status_code == 503

    async def test_webhook_accepts_valid_bearer_token(self, tmp_path: Path) -> None:
        db_path = tmp_path / "autonomy.db"
        with patch("main.settings") as mock_settings:
            mock_settings.webhook_secret = ""
            mock_settings.jira_bug_webhook_token = "tok-abc"
            mock_settings.autonomy_db_path = str(db_path)
            mock_settings.jira_implemented_ticket_field_id = ""
            mock_settings.jira_bug_link_types = "is caused by,relates to,is blocked by"
            mock_settings.jira_qa_confirmed_field_id = ""
            async with await _make_client() as client:
                r = await client.post(
                    "/webhooks/jira-bug",
                    json=self._bug_payload(),
                    headers={"x-jira-bug-token": "tok-abc"},
                )
            assert r.status_code == 202

    async def test_webhook_rejects_wrong_bearer_token(self, tmp_path: Path) -> None:
        db_path = tmp_path / "autonomy.db"
        with patch("main.settings") as mock_settings:
            mock_settings.webhook_secret = ""
            mock_settings.jira_bug_webhook_token = "tok-abc"
            mock_settings.autonomy_db_path = str(db_path)
            async with await _make_client() as client:
                r = await client.post(
                    "/webhooks/jira-bug",
                    json=self._bug_payload(),
                    headers={"x-jira-bug-token": "nope"},
                )
            assert r.status_code == 401

    async def test_webhook_accepts_valid_hmac(self, tmp_path: Path) -> None:
        db_path = tmp_path / "autonomy.db"
        payload = self._bug_payload()
        body = json.dumps(payload).encode()
        secret = "whsec"
        sig = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        with patch("main.settings") as mock_settings:
            mock_settings.webhook_secret = secret
            mock_settings.jira_bug_webhook_token = ""
            mock_settings.autonomy_db_path = str(db_path)
            mock_settings.jira_implemented_ticket_field_id = ""
            mock_settings.jira_bug_link_types = "is caused by,relates to,is blocked by"
            mock_settings.jira_qa_confirmed_field_id = ""
            async with await _make_client() as client:
                r = await client.post(
                    "/webhooks/jira-bug",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "x-hub-signature": sig,
                    },
                )
            assert r.status_code == 202

    async def test_webhook_happy_path_creates_defect(self, tmp_path: Path) -> None:
        from autonomy_store import (
            PrRunUpsert,
            ensure_schema,
            open_connection,
            upsert_pr_run,
        )

        db_path = tmp_path / "autonomy.db"
        # Seed a merged pr_run for PROJ-1
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id="PROJ-1",
                    pr_number=1,
                    repo_full_name="acme/app",
                    head_sha="sha1",
                    client_profile="default",
                    opened_at="2026-03-01T00:00:00+00:00",
                    merged=1,
                    merged_at="2026-03-01T00:00:00+00:00",
                ),
            )
        finally:
            conn.close()

        with patch("main.settings") as mock_settings:
            mock_settings.webhook_secret = ""
            mock_settings.jira_bug_webhook_token = "tok"
            mock_settings.autonomy_db_path = str(db_path)
            mock_settings.jira_implemented_ticket_field_id = ""
            mock_settings.jira_bug_link_types = "is caused by,relates to,is blocked by"
            mock_settings.jira_qa_confirmed_field_id = ""
            async with await _make_client() as client:
                r = await client.post(
                    "/webhooks/jira-bug",
                    json=self._bug_payload(parent_key="PROJ-1"),
                    headers={"x-jira-bug-token": "tok"},
                )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "accepted"
        assert body["parent_ticket_id"] == "PROJ-1"

        conn = open_connection(db_path)
        try:
            rows = conn.execute("SELECT * FROM defect_links").fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0]["defect_key"] == "BUG-1"
        assert rows[0]["source"] == "jira"

    async def test_webhook_bug_with_no_parent_returns_ignored(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "autonomy.db"
        payload = {
            "issue": {
                "key": "BUG-2",
                "fields": {
                    "issuetype": {"name": "Bug"},
                    "created": "2026-04-03T10:00:00.000+0000",
                    "priority": {"name": "Low"},
                    "summary": "Unlinked bug",
                    "issuelinks": [],
                },
            },
        }
        with patch("main.settings") as mock_settings:
            mock_settings.webhook_secret = ""
            mock_settings.jira_bug_webhook_token = "tok"
            mock_settings.autonomy_db_path = str(db_path)
            mock_settings.jira_implemented_ticket_field_id = ""
            mock_settings.jira_bug_link_types = "is caused by,relates to,is blocked by"
            mock_settings.jira_qa_confirmed_field_id = ""
            async with await _make_client() as client:
                r = await client.post(
                    "/webhooks/jira-bug",
                    json=payload,
                    headers={"x-jira-bug-token": "tok"},
                )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "ignored"
        assert body["reason"] == "no_parent_link"

    async def test_webhook_issuetype_story_returns_ignored(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "autonomy.db"
        payload = self._bug_payload()
        # mutate to Story
        payload["issue"]["fields"]["issuetype"] = {"name": "Story"}  # type: ignore[index]
        with patch("main.settings") as mock_settings:
            mock_settings.webhook_secret = ""
            mock_settings.jira_bug_webhook_token = "tok"
            mock_settings.autonomy_db_path = str(db_path)
            mock_settings.jira_implemented_ticket_field_id = ""
            mock_settings.jira_bug_link_types = "is caused by,relates to,is blocked by"
            mock_settings.jira_qa_confirmed_field_id = ""
            async with await _make_client() as client:
                r = await client.post(
                    "/webhooks/jira-bug",
                    json=payload,
                    headers={"x-jira-bug-token": "tok"},
                )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "ignored"
        assert body["reason"] == "not_a_defect_type"


# --- /api/retest path-traversal guard ---
#
# Bug regression: ``_BRANCH_PATTERN`` used to permit ``..`` because the
# regex charset included ``.``, and the worktree-containment check used
# ``str.startswith`` on resolved paths. A sibling-prefix directory like
# ``worktrees-evil`` satisfied both checks — "../worktrees-evil/x"
# resolved to ``/repo/worktrees-evil/x`` which literally starts with
# ``/repo/worktrees``. Fixed by rejecting ``..`` in the branch regex
# and switching the containment check to ``Path.relative_to``.


async def test_retest_rejects_branch_with_dotdot(tmp_path: Path) -> None:
    """Bug regression: ``..`` in the branch must be rejected at the
    regex layer, before the containment check even runs."""
    import re as _re

    fake_client = tmp_path / "client-repo"
    fake_client.mkdir()
    (fake_client / ".git").mkdir()
    # Create a sibling-prefix directory that would have fooled the old
    # startswith check. It must never be used as cwd.
    sibling = tmp_path / "worktrees-evil"
    sibling.mkdir()
    (sibling / "x").mkdir()

    with patch("main.settings") as mock_settings:
        mock_settings.api_key = ""  # Open local dev auth
        mock_settings.default_client_repo = str(fake_client)
        async with await _make_client() as client:
            # Attempt 1: explicit traversal via ``..``
            r = await client.post(
                "/api/retest",
                json={
                    "ticket_id": "PROJ-1",
                    "phase": "qa",
                    "branch": "../worktrees-evil/x",
                },
            )
    # Rejected at regex layer — 400 with the branch-name detail.
    assert r.status_code == 400
    assert "branch" in r.json()["detail"].lower()

    # The bug-1 regex must also reject the subtler forms.
    for evil in ("../../outside", "..", "..foo"):
        assert not _re.match(main._BRANCH_PATTERN, evil), (
            f"{evil!r} must not match _BRANCH_PATTERN"
        )


async def test_retest_rejects_sibling_prefix_via_relative_to(tmp_path: Path) -> None:
    """Bug regression: before the fix, the containment check was
    ``str(resolved).startswith(str(worktrees_parent))`` — so a branch
    that resolved to ``/tmp/x/worktrees-evil/foo`` passed because the
    string happened to start with ``/tmp/x/worktrees``. This test
    verifies that even a crafted branch name that survives the regex
    (no ``..``) still fails the relative_to containment check.

    The only way to produce such a path without ``..`` is to plant a
    sibling symlink inside worktrees that resolves out. Simulate that
    here."""
    fake_client = tmp_path / "client-repo"
    fake_client.mkdir()
    (fake_client / ".git").mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    # Plant a sibling-prefix directory the symlink will escape to.
    sibling = tmp_path / "worktrees-evil" / "target"
    sibling.mkdir(parents=True)
    (sibling / "trap").write_text("planted")
    # Symlink inside worktrees that escapes to the sibling.
    (worktrees / "evil-link").symlink_to(sibling, target_is_directory=True)

    with patch("main.settings") as mock_settings:
        mock_settings.api_key = ""
        mock_settings.default_client_repo = str(fake_client)
        async with await _make_client() as client:
            r = await client.post(
                "/api/retest",
                json={
                    "ticket_id": "PROJ-1",
                    "phase": "qa",
                    "branch": "evil-link",
                },
            )
    # Must be rejected — the symlink resolves outside worktrees/.
    assert r.status_code == 400
    assert "outside" in r.json()["detail"].lower()
