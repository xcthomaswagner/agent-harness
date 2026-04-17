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


class TestRowProposedDelta:
    """Regression coverage for the drafter-input sanitizer.

    A ``draft_ready -> proposed`` bounce leaves the previous drafter
    output on the row. /draft reads that row, parses the JSON, and
    feeds it to the next LLM prompt — without stripping, the stale
    unified_diff muddies the starter proposal the model sees.
    """

    def test_strips_unified_diff_and_drafter_origin(self) -> None:
        import json as _json
        from unittest.mock import MagicMock

        from learning_api import _row_proposed_delta

        row = MagicMock()
        row.__getitem__ = lambda self, k: _json.dumps({
            "target_path": "runtime/skills/x/SKILL.md",
            "anchor": "## Checklist",
            "unified_diff": "--- a/x\n+++ b/x\n@@\n+rule",
            "drafter_origin": "markdown_drafter",
        }) if k == "proposed_delta_json" else None
        out = _row_proposed_delta(row)
        assert "unified_diff" not in out
        assert "drafter_origin" not in out
        assert out["target_path"] == "runtime/skills/x/SKILL.md"
        assert out["anchor"] == "## Checklist"

    def test_empty_delta_returns_empty(self) -> None:
        from unittest.mock import MagicMock

        from learning_api import _row_proposed_delta

        row = MagicMock()
        row.__getitem__ = lambda self, k: None
        assert _row_proposed_delta(row) == {}

    def test_malformed_delta_returns_empty(self) -> None:
        from unittest.mock import MagicMock

        from learning_api import _row_proposed_delta

        row = MagicMock()
        row.__getitem__ = lambda self, k: "not json"
        assert _row_proposed_delta(row) == {}


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

    def test_second_approve_is_idempotent_when_pr_opener_off(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # With the PR opener disabled, re-calling /approve on an
        # already-approved lesson is a no-op (the operator is
        # effectively retrying but there's no PR to open). This
        # matches the "re-entrable from approved" contract that
        # enables the retry-after-PR-opener-failure recovery path.
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
        assert r2.status_code == 200
        assert r2.json()["status"] == "approved"

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

    def test_absolute_target_path_rejected_before_read(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """Absolute target_path must be refused BEFORE any file read.

        Regression: pathlib's `/` operator discards the repo_root when
        the RHS is absolute, so `_repo_root() / "/etc/passwd"` reads
        the absolute path. The drafter's internal precheck would
        eventually reject, but only after the file has already been
        slurped into a Claude prompt.
        """
        lid = self._seed_with_delta(target_path="/etc/passwd")
        run_drafter = AsyncMock()
        with (
            patch("learning_api._run_drafter", run_drafter),
            patch("pathlib.Path.read_text") as mock_read,
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
        assert "absolute" in body["error"]
        # Crucially: the file read must not have happened, and the
        # drafter must not have been invoked with a prompt containing
        # the file contents.
        mock_read.assert_not_called()
        run_drafter.assert_not_called()

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


class TestRevert:
    """POST /api/learning/candidates/{id}/revert

    Gated on: (1) status==applied, (2) merged_commit_sha present,
    (3) latest outcome verdict ∈ {regressed, human_reedit}. Dry-run
    keeps lesson at applied; real run transitions to reverted.
    """

    def _seed_outcome(self, lesson_id: str, verdict: str) -> None:
        from autonomy_store import (
            LessonOutcomeInsert,
            autonomy_conn,
            insert_lesson_outcome,
        )
        with autonomy_conn() as conn:
            insert_lesson_outcome(
                conn,
                LessonOutcomeInsert(
                    lesson_id=lesson_id,
                    measured_at="2026-04-17T00:00:00+00:00",
                    window_days=14,
                    verdict=verdict,
                ),
            )

    def test_unknown_lesson_returns_404(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        r = client.post(
            "/api/learning/candidates/LSN-missing/revert",
            json={"reason": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 404

    def test_wrong_status_returns_409(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        # Freshly seeded candidate is at 'proposed', not applied.
        lid = _seed_candidate()
        r = client.post(
            f"/api/learning/candidates/{lid}/revert",
            json={"reason": "regressed"},
            headers=admin_headers,
        )
        assert r.status_code == 409
        assert "applied" in r.json()["detail"]

    def test_missing_outcome_returns_409(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        from tests.conftest import seed_applied_candidate
        lid = seed_applied_candidate()
        # No outcome written — verdict lookup returns "".
        r = client.post(
            f"/api/learning/candidates/{lid}/revert",
            json={"reason": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 409
        assert "revertable" in r.json()["detail"]

    def test_confirmed_verdict_is_not_revertable(
        self, client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        from tests.conftest import seed_applied_candidate
        lid = seed_applied_candidate()
        self._seed_outcome(lid, "confirmed")
        r = client.post(
            f"/api/learning/candidates/{lid}/revert",
            json={"reason": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 409

    def test_regressed_dry_run_stays_applied(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.conftest import seed_applied_candidate
        lid = seed_applied_candidate()
        self._seed_outcome(lid, "regressed")

        from learning_miner.pr_opener import PROpenerResult
        monkeypatch.setattr(settings, "learning_pr_opener_dry_run", True)

        def fake_open(inputs):
            return PROpenerResult(
                success=True,
                branch=f"learning/revert-{inputs.lesson_id}",
                commit_sha="revsha01",
                dry_run=True,
            )

        monkeypatch.setattr(
            "learning_api.open_revert_pr_for_lesson", fake_open
        )
        r = client.post(
            f"/api/learning/candidates/{lid}/revert",
            json={"reason": "regressed"},
            headers=admin_headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["revert_success"] is True
        assert body["revert_dry_run"] is True
        # Lesson stays at applied on dry-run.
        from autonomy_store import autonomy_conn, get_lesson_by_id
        with autonomy_conn() as conn:
            row = get_lesson_by_id(conn, lid)
        assert row["status"] == "applied"

    def test_human_reedit_real_run_transitions_to_reverted(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.conftest import seed_applied_candidate
        lid = seed_applied_candidate()
        self._seed_outcome(lid, "human_reedit")

        from learning_miner.pr_opener import PROpenerResult
        monkeypatch.setattr(settings, "learning_pr_opener_dry_run", False)

        def fake_open(inputs):
            return PROpenerResult(
                success=True,
                pr_url="https://github.com/x/y/pull/42",
                branch=f"learning/revert-{inputs.lesson_id}",
                commit_sha="revsha02",
            )

        monkeypatch.setattr(
            "learning_api.open_revert_pr_for_lesson", fake_open
        )
        r = client.post(
            f"/api/learning/candidates/{lid}/revert",
            json={"reason": "human found issue"},
            headers=admin_headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["revert_success"] is True
        assert body["pr_url"] == "https://github.com/x/y/pull/42"
        from autonomy_store import autonomy_conn, get_lesson_by_id
        with autonomy_conn() as conn:
            row = get_lesson_by_id(conn, lid)
        assert row["status"] == "reverted"
        assert row["pr_url"] == "https://github.com/x/y/pull/42"

    def test_opener_failure_keeps_applied_status(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.conftest import seed_applied_candidate
        lid = seed_applied_candidate()
        self._seed_outcome(lid, "regressed")

        from learning_miner.pr_opener import PROpenerResult
        monkeypatch.setattr(settings, "learning_pr_opener_dry_run", False)

        def fake_open(inputs):
            return PROpenerResult(success=False, error="git revert conflict")

        monkeypatch.setattr(
            "learning_api.open_revert_pr_for_lesson", fake_open
        )
        r = client.post(
            f"/api/learning/candidates/{lid}/revert",
            json={"reason": "x"},
            headers=admin_headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["revert_success"] is False
        assert "git revert conflict" in body["error"]
        from autonomy_store import autonomy_conn, get_lesson_by_id
        with autonomy_conn() as conn:
            row = get_lesson_by_id(conn, lid)
        assert row["status"] == "applied"
        assert "revert_pr_opener" in (row["status_reason"] or "")
