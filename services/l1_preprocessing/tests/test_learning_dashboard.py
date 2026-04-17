"""Tests for the self-learning dashboard (/autonomy/learning)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autonomy_store import (
    LessonCandidateUpsert,
    PrRunUpsert,
    autonomy_conn,
    insert_lesson_evidence,
    upsert_lesson_candidate,
    upsert_pr_run,
)
from config import settings
from learning_dashboard import router as learning_dashboard_router
from learning_miner.detectors.base import compute_lesson_id
from tests.conftest import seed_lesson_candidate


def _mk_app() -> FastAPI:
    app = FastAPI()
    app.include_router(learning_dashboard_router)
    return app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    return TestClient(_mk_app())


_DEFAULT_PROPOSED_DELTA = (
    '{"target_path":"runtime/foo.md",'
    '"edit_type":"append_section",'
    '"anchor":"## Review","after":"check SOQL injection"}'
)


def _seed(
    *,
    client_profile: str = "xcsf30",
    platform: str = "salesforce",
    detector: str = "human_issue_cluster",
    pattern: str = "security|*.cls",
    scope_suffix: str = "security|*.cls",
    frequency: int = 3,
    evidence_ticket: str = "SCRUM-42",
) -> str:
    """Seed a lesson candidate + one PR run + one evidence row."""
    scope = f"{client_profile}|{platform}|{scope_suffix}"
    lid = seed_lesson_candidate(
        scope=scope,
        detector=detector,
        pattern=pattern,
        client_profile=client_profile,
        platform_profile=platform,
        frequency=frequency,
        proposed_delta_json=_DEFAULT_PROPOSED_DELTA,
    )
    with autonomy_conn() as conn:
        pr_id = upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id=evidence_ticket,
                pr_number=1,
                repo_full_name="acme/app",
                head_sha="sha-1",
                client_profile=client_profile,
            ),
        )
        insert_lesson_evidence(
            conn,
            lesson_id=lid,
            trace_id=evidence_ticket,
            source_ref="review_issues#101",
            observed_at="2026-04-10T05:00:00+00:00",
            snippet="force-app/foo.cls: SOQL injection",
            pr_run_id=pr_id,
        )
    return lid


class TestEmptyState:
    def test_renders_200_html_when_no_candidates(
        self, client: TestClient
    ) -> None:
        r = client.get("/autonomy/learning")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "No lesson candidates match" in r.text

    def test_summary_shows_zero(self, client: TestClient) -> None:
        r = client.get("/autonomy/learning")
        assert "0 candidates shown" in r.text


class TestPopulated:
    def test_candidate_row_rendered(self, client: TestClient) -> None:
        lid = _seed()
        r = client.get("/autonomy/learning")
        assert r.status_code == 200
        assert lid in r.text
        assert "human_issue_cluster" in r.text
        assert "xcsf30" in r.text
        assert "salesforce" in r.text

    def test_proposed_delta_rendered_as_preformatted(
        self, client: TestClient
    ) -> None:
        _seed()
        r = client.get("/autonomy/learning")
        # Our pretty-printer lists target_path first.
        assert "target_path: runtime/foo.md" in r.text
        assert "edit_type: append_section" in r.text

    def test_evidence_details_block_present(
        self, client: TestClient
    ) -> None:
        _seed(evidence_ticket="TICK-EV-9")
        r = client.get("/autonomy/learning")
        assert "<details class='evidence'>" in r.text
        assert "Evidence (1)" in r.text
        assert 'href="/traces/TICK-EV-9"' in r.text
        assert "SOQL injection" in r.text

    def test_action_buttons_point_to_json_api(
        self, client: TestClient
    ) -> None:
        lid = _seed()
        r = client.get("/autonomy/learning")
        # Endpoints are surfaced on the disabled buttons' data-endpoint
        # attribute and in the tooltip — operators POST via curl until
        # Phase C adds form submission.
        assert f'data-endpoint="/api/learning/candidates/{lid}/approve"' in r.text
        assert f'data-endpoint="/api/learning/candidates/{lid}/reject"' in r.text
        assert f'data-endpoint="/api/learning/candidates/{lid}/snooze"' in r.text

    def test_buttons_rendered_as_disabled_elements(
        self, client: TestClient
    ) -> None:
        # Buttons are always disabled in Phase B — the form-submission
        # wiring lands in Phase C. Clicking an <a data-method="POST">
        # issues a GET and hits 405, so Phase B uses <button disabled>
        # instead.
        _seed()
        r = client.get("/autonomy/learning")
        assert "<button class=" in r.text
        assert "disabled" in r.text

    def test_terminal_status_disables_buttons_with_reason(
        self, client: TestClient
    ) -> None:
        lid = _seed()
        from autonomy_store import update_lesson_status

        with autonomy_conn() as conn:
            update_lesson_status(conn, lid, "rejected", reason="no")
        r = client.get("/autonomy/learning")
        assert "Disabled — current status is rejected" in r.text

    def test_approved_shows_reject_enabled(
        self, client: TestClient
    ) -> None:
        """Regression: approved used to be lumped into the terminal set
        so the Reject button was disabled. But the store transition
        table allows ``approved -> rejected`` — the dashboard now
        reflects that.
        """
        lid = _seed()
        from autonomy_store import update_lesson_status

        with autonomy_conn() as conn:
            update_lesson_status(conn, lid, "draft_ready", reason="d")
            update_lesson_status(conn, lid, "approved", reason="a")
        r = client.get("/autonomy/learning")
        # The Reject button's tooltip must NOT be the disabled-reason text
        # (which reads "Disabled — current status is X"). Instead it
        # should show the POST endpoint, signalling the action is live.
        # Locate the reject button specifically.
        import re
        m = re.search(
            r'<button[^>]*data-endpoint="[^"]*/reject"[^>]*title="([^"]*)"',
            r.text,
        )
        assert m is not None
        assert m.group(1).startswith("POST ")

    def test_approved_shows_approve_enabled_for_reentry(
        self, client: TestClient
    ) -> None:
        """Regression: /approve is re-entrable from approved (PR-opener
        retry path) — the dashboard must not disable the Approve button
        at that status.
        """
        lid = _seed()
        from autonomy_store import update_lesson_status

        with autonomy_conn() as conn:
            update_lesson_status(conn, lid, "draft_ready", reason="d")
            update_lesson_status(conn, lid, "approved", reason="a")
        r = client.get("/autonomy/learning")
        import re
        m = re.search(
            r'<button[^>]*data-endpoint="[^"]*/approve"[^>]*title="([^"]*)"',
            r.text,
        )
        assert m is not None
        assert m.group(1).startswith("POST ")


class TestProfileSelector:
    def test_only_profiles_with_candidates_listed(
        self, client: TestClient
    ) -> None:
        _seed(client_profile="xcsf30", scope_suffix="a", pattern="pa")
        _seed(client_profile="rockwell", scope_suffix="b", pattern="pb")
        r = client.get("/autonomy/learning")
        assert "xcsf30" in r.text
        assert "rockwell" in r.text

    def test_filters_narrow_rendered_rows(
        self, client: TestClient
    ) -> None:
        l1 = _seed(
            client_profile="xcsf30", scope_suffix="a", pattern="pa"
        )
        l2 = _seed(
            client_profile="rockwell",
            platform="salesforce",
            scope_suffix="b",
            pattern="pb",
        )
        r = client.get("/autonomy/learning?client_profile=xcsf30")
        assert l1 in r.text
        assert l2 not in r.text


class TestStatusFilter:
    def test_status_selector_shows_current_pill(
        self, client: TestClient
    ) -> None:
        _seed()
        r = client.get("/autonomy/learning?status=proposed")
        assert '<span class="current">proposed</span>' in r.text

    def test_filter_hides_non_matching(self, client: TestClient) -> None:
        _seed()
        r = client.get("/autonomy/learning?status=applied")
        assert "No lesson candidates match" in r.text


class TestNavigation:
    def test_home_autonomy_traces_links_present(
        self, client: TestClient
    ) -> None:
        r = client.get("/autonomy/learning")
        assert 'href="/dashboard"' in r.text
        assert 'href="/autonomy"' in r.text
        assert 'href="/traces"' in r.text


class TestProposedDeltaRendering:
    def test_empty_delta_renders_without_error(
        self, client: TestClient
    ) -> None:
        _seed()
        r = client.get("/autonomy/learning")
        assert r.status_code == 200

    def test_malformed_delta_falls_back_to_raw_pre(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from autonomy_store import autonomy_conn

        db_path = tmp_path / "autonomy.db"
        monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
        lid = compute_lesson_id("det", "pat", "xcsf30|salesforce|scope")
        with autonomy_conn() as conn:
            upsert_lesson_candidate(
                conn,
                LessonCandidateUpsert(
                    lesson_id=lid,
                    detector_name="det",
                    pattern_key="pat",
                    client_profile="xcsf30",
                    platform_profile="salesforce",
                    scope_key="xcsf30|salesforce|scope",
                    proposed_delta_json="this is not json",
                ),
            )
        c = TestClient(_mk_app())
        r = c.get("/autonomy/learning")
        # Falls back to escaped raw text inside a <pre> block — no 500.
        assert r.status_code == 200
        assert "this is not json" in r.text


class TestEvidenceOrdering:
    def test_newest_evidence_appears_first(
        self, client: TestClient
    ) -> None:
        from autonomy_store import autonomy_conn, insert_lesson_evidence

        lid = _seed(evidence_ticket="T-first")
        with autonomy_conn() as conn:
            insert_lesson_evidence(
                conn,
                lesson_id=lid,
                trace_id="T-second",
                source_ref="review_issues#202",
                observed_at="2026-04-11T05:00:00+00:00",
                snippet="second",
            )
        r = client.get("/autonomy/learning")
        # Newest (T-second, inserted second) renders before oldest.
        idx_second = r.text.find("T-second")
        idx_first = r.text.find("T-first")
        assert 0 < idx_second < idx_first
