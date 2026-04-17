"""Tests for learning_api.py. /draft uses mocked drafter + checker."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import settings
from learning_api import router as learning_api_router
from tests.conftest import seed_lesson_candidate as _seed_candidate


@pytest.fixture
def admin_app(configure_admin_auth: str) -> FastAPI:
    app = FastAPI()
    app.include_router(learning_api_router)
    return app


@pytest.fixture
def client(admin_app: FastAPI) -> TestClient:
    return TestClient(admin_app)


@pytest.fixture
def admin_headers(configure_admin_auth: str) -> dict[str, str]:
    return {"X-Autonomy-Admin-Token": configure_admin_auth}


class TestListCandidates:
    def test_returns_empty_list_when_no_candidates(
        self, client: TestClient
    ) -> None:
        r = client.get("/api/learning/candidates")
        assert r.status_code == 200
        body = r.json()
        assert body == {"candidates": [], "count": 0}

    def test_returns_candidates_with_parsed_delta(
        self, client: TestClient
    ) -> None:
        _seed_candidate()
        r = client.get("/api/learning/candidates")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        candidate = body["candidates"][0]
        assert isinstance(candidate["proposed_delta"], dict)
        assert candidate["client_profile"] == "xcsf30"
        assert candidate["status"] == "proposed"

    def test_filters_by_status(self, client: TestClient) -> None:
        _seed_candidate(scope="s1", pattern="p1")
        _seed_candidate(scope="s2", pattern="p2")
        r = client.get("/api/learning/candidates?status=proposed")
        assert r.json()["count"] == 2
        r = client.get("/api/learning/candidates?status=applied")
        assert r.json()["count"] == 0

    def test_filters_by_client_profile(self, client: TestClient) -> None:
        _seed_candidate()
        r = client.get(
            "/api/learning/candidates?client_profile=xcsf30"
        )
        assert r.json()["count"] == 1
        r = client.get(
            "/api/learning/candidates?client_profile=rockwell"
        )
        assert r.json()["count"] == 0

    def test_include_evidence_returns_empty_list_for_seeded_candidate(
        self, client: TestClient
    ) -> None:
        # Candidate seeded without explicit evidence rows — the field
        # must still be present as an empty list, not missing.
        _seed_candidate()
        r = client.get(
            "/api/learning/candidates?include_evidence=true"
        )
        candidate = r.json()["candidates"][0]
        assert candidate["evidence"] == []


class TestGetCandidate:
    def test_404_on_unknown(self, client: TestClient) -> None:
        r = client.get("/api/learning/candidates/LSN-deadbeef")
        assert r.status_code == 404

    def test_200_on_existing(self, client: TestClient) -> None:
        lid = _seed_candidate()
        r = client.get(f"/api/learning/candidates/{lid}")
        assert r.status_code == 200
        body = r.json()
        assert body["lesson_id"] == lid
        assert body["evidence"] == []


class TestApprove:
    def test_proposed_cannot_be_directly_approved(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # Phase C gates approval behind /draft. Bypassing the drafter
        # would defeat the consistency check, so the transition table
        # rejects it with 409.
        lid = _seed_candidate()
        r = client.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "looks good"},
            headers=admin_headers,
        )
        assert r.status_code == 409

    def test_happy_path_from_draft_ready(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        from autonomy_store import autonomy_conn, update_lesson_status

        lid = _seed_candidate()
        with autonomy_conn() as conn:
            update_lesson_status(
                conn, lid, "draft_ready", reason="drafter ok"
            )
        r = client.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "reviewed"},
            headers=admin_headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "approved"
        assert body["status_reason"] == "reviewed"

    def test_second_approve_409(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        from autonomy_store import autonomy_conn, update_lesson_status

        lid = _seed_candidate()
        with autonomy_conn() as conn:
            update_lesson_status(
                conn, lid, "draft_ready", reason="drafter ok"
            )
        client.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "first"},
            headers=admin_headers,
        )
        r2 = client.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "second"},
            headers=admin_headers,
        )
        assert r2.status_code == 409

    def test_404_on_unknown(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        r = client.post(
            "/api/learning/candidates/LSN-nope/approve",
            json={"reason": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 404


class TestReject:
    def test_happy_path(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        lid = _seed_candidate()
        r = client.post(
            f"/api/learning/candidates/{lid}/reject",
            json={"reason": "not actionable"},
            headers=admin_headers,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_reject_then_approve_409(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        lid = _seed_candidate()
        client.post(
            f"/api/learning/candidates/{lid}/reject",
            json={"reason": "no"},
            headers=admin_headers,
        )
        r = client.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "?"},
            headers=admin_headers,
        )
        assert r.status_code == 409


class TestSnooze:
    def test_happy_path(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        lid = _seed_candidate()
        r = client.post(
            f"/api/learning/candidates/{lid}/snooze",
            json={
                "reason": "need more data",
                "next_review_at": "2026-05-01T00:00:00+00:00",
            },
            headers=admin_headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "snoozed"
        assert body["next_review_at"] == "2026-05-01T00:00:00+00:00"

    def test_snooze_requires_next_review_at(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        lid = _seed_candidate()
        r = client.post(
            f"/api/learning/candidates/{lid}/snooze",
            json={"reason": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 422


class TestAuth:
    def test_503_when_admin_token_unset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            settings, "autonomy_db_path", str(tmp_path / "a.db")
        )
        monkeypatch.setattr(settings, "autonomy_admin_token", "")
        app = FastAPI()
        app.include_router(learning_api_router)
        c = TestClient(app)
        r = c.post(
            "/api/learning/candidates/LSN-x/approve",
            json={"reason": "x"},
            headers={"X-Autonomy-Admin-Token": "anything"},
        )
        assert r.status_code == 503

    def test_401_on_wrong_token(self, client: TestClient) -> None:
        r = client.post(
            "/api/learning/candidates/LSN-x/approve",
            json={"reason": "x"},
            headers={"X-Autonomy-Admin-Token": "wrong"},
        )
        assert r.status_code == 401

    def test_401_without_header(self, client: TestClient) -> None:
        r = client.post(
            "/api/learning/candidates/LSN-x/approve",
            json={"reason": "x"},
        )
        assert r.status_code == 401

    def test_read_endpoints_do_not_require_token(
        self, client: TestClient
    ) -> None:
        # GETs are observability-only — no auth gate.
        r = client.get("/api/learning/candidates")
        assert r.status_code == 200
        r = client.get("/api/learning/candidates/LSN-x")
        # 404 because unknown, but not 401 / 503.
        assert r.status_code == 404


class TestAbuseGuards:
    def test_413_on_oversized_payload(
        self,
        admin_app: FastAPI,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            settings, "autonomy_internal_max_body_bytes", 128
        )
        c = TestClient(admin_app)
        lid = _seed_candidate()
        r = c.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "x" * 256},
            headers=admin_headers,
        )
        assert r.status_code == 413

    def test_429_when_bucket_exhausted(
        self,
        admin_app: FastAPI,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import autonomy_ingest
        from autonomy_ingest import TokenBucket

        monkeypatch.setattr(
            autonomy_ingest,
            "_bucket",
            TokenBucket(capacity=0, refill_per_sec=0.0),
        )
        c = TestClient(admin_app)
        lid = _seed_candidate()
        r = c.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 429


# ---- /draft endpoint -------------------------------------------------

from unittest.mock import AsyncMock, patch  # noqa: E402

from learning_miner.drafter_consistency_check import (  # noqa: E402
    ConsistencyVerdict,
)
from learning_miner.drafter_markdown import DrafterResult  # noqa: E402


class TestDraftEndpoint:
    def _seed_with_delta(self, target_path: str = "runtime/skills/code-review/SKILL.md") -> str:
        """Seed a candidate whose proposed_delta points at a real file."""
        import json as _json

        return _seed_candidate(
            scope="xcsf30|salesforce|security|*.cls",
            detector="human_issue_cluster",
            pattern="security|*.cls",
            proposed_delta_json=_json.dumps(
                {
                    "target_path": target_path,
                    "anchor": "## Review Checklist",
                    "rationale_md": "cluster of SOQL injections",
                }
            ),
        )

    def test_drafter_success_promotes_to_draft_ready(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        lid = self._seed_with_delta()
        drafter_result = DrafterResult(
            success=True,
            unified_diff="--- a/x\n+++ b/x\n@@\n+rule",
            tokens_in=100,
            tokens_out=50,
        )
        consistency_verdict = ConsistencyVerdict(
            contradicts=False, reasoning="ok"
        )
        with (
            patch(
                "learning_api._run_drafter",
                AsyncMock(return_value=drafter_result),
            ),
            patch(
                "learning_api._run_consistency_check",
                AsyncMock(return_value=consistency_verdict),
            ),
            # The handler reads the target file; stub it.
            patch("pathlib.Path.read_text", return_value="current content"),
        ):
            r = client.post(
                f"/api/learning/candidates/{lid}/draft",
                json={},
                headers=admin_headers,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "draft_ready"
        assert body["drafter_success"] is True
        assert body["consistency_contradicts"] is False
        # Unified diff got merged into the stored delta.
        assert (
            "unified_diff"
            in body["candidate"]["proposed_delta"]
        )

    def test_drafter_failure_keeps_proposed(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        lid = self._seed_with_delta()
        drafter_result = DrafterResult(
            success=False, error="git apply --check failed"
        )
        with patch(
            "learning_api._run_drafter",
            AsyncMock(return_value=drafter_result),
        ):
            r = client.post(
                f"/api/learning/candidates/{lid}/draft",
                json={},
                headers=admin_headers,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "proposed"
        assert body["drafter_success"] is False
        assert "git apply" in body["error"]

    def test_consistency_contradiction_blocks_promotion(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        lid = self._seed_with_delta()
        drafter_result = DrafterResult(
            success=True, unified_diff="--- a/x\n+++ b/x\n@@\n+rule"
        )
        verdict = ConsistencyVerdict(
            contradicts=True,
            contradicts_with="Use X tool",
            reasoning="new rule conflicts with X",
        )
        with (
            patch(
                "learning_api._run_drafter",
                AsyncMock(return_value=drafter_result),
            ),
            patch(
                "learning_api._run_consistency_check",
                AsyncMock(return_value=verdict),
            ),
            patch("pathlib.Path.read_text", return_value="existing"),
        ):
            r = client.post(
                f"/api/learning/candidates/{lid}/draft",
                json={},
                headers=admin_headers,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "proposed"
        assert body["drafter_success"] is True
        assert body["consistency_contradicts"] is True
        assert body["contradicts_with"] == "Use X tool"

    def test_non_proposed_409(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        from autonomy_store import autonomy_conn, update_lesson_status

        lid = self._seed_with_delta()
        with autonomy_conn() as conn:
            update_lesson_status(
                conn, lid, "rejected", reason="no"
            )
        r = client.post(
            f"/api/learning/candidates/{lid}/draft",
            json={},
            headers=admin_headers,
        )
        assert r.status_code == 409

    def test_404_on_unknown(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        r = client.post(
            "/api/learning/candidates/LSN-nope/draft",
            json={},
            headers=admin_headers,
        )
        assert r.status_code == 404

    def test_requires_admin_token(self, client: TestClient) -> None:
        lid = self._seed_with_delta()
        r = client.post(
            f"/api/learning/candidates/{lid}/draft", json={}
        )
        assert r.status_code == 401

    def test_drafter_failure_records_status_reason_on_candidate(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # The dashboard surfaces status_reason to the operator, so the
        # failure branch of /draft must actually persist the reason —
        # not just echo it in the response.
        lid = self._seed_with_delta()
        drafter_result = DrafterResult(
            success=False, error="git apply --check failed"
        )
        with patch(
            "learning_api._run_drafter",
            AsyncMock(return_value=drafter_result),
        ):
            client.post(
                f"/api/learning/candidates/{lid}/draft",
                json={},
                headers=admin_headers,
            )
        r = client.get(f"/api/learning/candidates/{lid}")
        assert r.status_code == 200
        assert "git apply" in r.json()["status_reason"]

    def test_consistency_check_killswitch_skips_second_llm_call(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When the kill switch is off, the handler must still succeed
        # end-to-end — the real ConsistencyChecker short-circuits to
        # contradicts=False when enabled=False. We verify by letting
        # the REAL consistency checker run (not mocked) with a mocked
        # client that would blow up if called.
        monkeypatch.setattr(
            settings, "learning_consistency_check_enabled", False
        )
        lid = self._seed_with_delta()
        drafter_result = DrafterResult(
            success=True,
            unified_diff="--- a/x\n+++ b/x\n@@\n+rule",
        )
        fake_client = AsyncMock()
        fake_client.messages.create.side_effect = AssertionError(
            "consistency check must not be called when killswitch is off"
        )

        def _fake_checker_factory(**_kw: object) -> object:
            from learning_miner.drafter_consistency_check import (
                ConsistencyChecker,
            )

            return ConsistencyChecker(
                api_key="test", client=fake_client, enabled=False
            )

        with (
            patch(
                "learning_api._run_drafter",
                AsyncMock(return_value=drafter_result),
            ),
            patch(
                "learning_api.ConsistencyChecker",
                side_effect=_fake_checker_factory,
            ),
            patch("pathlib.Path.read_text", return_value="current"),
        ):
            r = client.post(
                f"/api/learning/candidates/{lid}/draft",
                json={},
                headers=admin_headers,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "draft_ready"
        fake_client.messages.create.assert_not_called()
