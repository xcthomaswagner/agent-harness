"""Tests for Detector 3 — form_controls_ac_gaps.

Covers:

- Positive fixture: MIN_CLUSTER_SIZE tickets where AC calls out a
  taxonomy category and AI review produced no matching finding →
  one candidate emitted.
- Negative fixture: AC mentions the category but AI review DID file
  a matching issue → no candidate (not a gap).
- Negative fixture: AC does not mention any taxonomy category → no
  candidate regardless of review content.
- Below-threshold: only 2 eligible tickets (< MIN_CLUSTER_SIZE) → no
  candidate.
- Missing ticket.json → detector skips without crashing.
- Unknown client_profile (no platform resolution) → run is dropped.
- Ticket retries (multiple pr_runs for same ticket) do not
  double-count against the cluster threshold.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from autonomy_store import insert_review_issue
from learning_miner.detectors.form_controls_ac_gaps import (
    MIN_CLUSTER_SIZE,
    FormControlsAcGapsDetector,
    build,
)
from learning_miner.detectors.human_issue_cluster import (
    _resolve_platform_profile,
)
from tests.conftest import seed_pr_run_for_learning


@pytest.fixture(autouse=True)
def clear_platform_cache():
    _resolve_platform_profile.cache_clear()
    yield
    _resolve_platform_profile.cache_clear()


@pytest.fixture
def archive_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the detector's TICKET_ARCHIVE_ROOT at tmp_path."""
    import learning_miner.detectors.form_controls_ac_gaps as det

    monkeypatch.setattr(det, "TICKET_ARCHIVE_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def conn(learning_conn):
    return learning_conn


def _days_ago_iso(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _seed_pr_run(
    conn, pr_number: int, ticket_id: str, *, profile: str = "xcsf30"
) -> int:
    return seed_pr_run_for_learning(
        conn,
        pr_number=pr_number,
        ticket_id=ticket_id,
        client_profile=profile,
        opened_at=_days_ago_iso(1),
    )


def _write_ticket_json(
    archive_root: Path,
    ticket_id: str,
    *,
    authored_ac: list[str] | None = None,
    generated_ac: list[str] | list[dict[str, Any]] | None = None,
) -> None:
    target = archive_root / ticket_id
    target.mkdir(parents=True, exist_ok=True)
    doc = {
        "id": ticket_id,
        "acceptance_criteria": authored_ac or [],
        "generated_acceptance_criteria": generated_ac or [],
    }
    (target / "ticket.json").write_text(json.dumps(doc), encoding="utf-8")


def _seed_ai_issue(
    conn,
    pr_run_id: int,
    *,
    category: str = "",
    summary: str = "",
) -> int:
    return insert_review_issue(
        conn,
        pr_run_id=pr_run_id,
        source="ai_review",
        file_path="",
        category=category,
        summary=summary,
        is_valid=1,
    )


class TestPositiveFixture:
    def test_three_tickets_with_gap_emit_one_candidate(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{i+100}"
            _seed_pr_run(conn, i + 1, tid)
            _write_ticket_json(
                archive_root, tid,
                generated_ac=[
                    "The form must handle cross-field validation "
                    "across email and password.",
                ],
            )
            # AI review filed unrelated issues — NOT in the category.
            _seed_ai_issue(
                conn, pr_run_id=i + 1,
                category="style", summary="extra whitespace",
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert len(out) == 1
        prop = out[0]
        assert prop.client_profile == "xcsf30"
        assert prop.platform_profile == "salesforce"
        assert prop.pattern_key == "form_controls_gap|cross_field_validation"
        assert "cross_field_validation" in prop.scope_key
        assert prop.severity == "info"  # exactly at threshold

    def test_high_cluster_bumps_severity(
        self, conn, archive_root: Path
    ) -> None:
        count = MIN_CLUSTER_SIZE * 2
        for i in range(count):
            tid = f"XCSF30-{i+200}"
            _seed_pr_run(conn, i + 1, tid)
            _write_ticket_json(
                archive_root, tid,
                generated_ac=["Session timeout must redirect to login."],
            )
            _seed_ai_issue(
                conn, pr_run_id=i + 1,
                category="format", summary="minor",
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert len(out) == 1
        assert out[0].severity == "warn"
        assert "session_timeout" in out[0].scope_key


class TestNegativeFixtures:
    def test_ai_review_catches_it_no_gap(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{i+300}"
            _seed_pr_run(conn, i + 1, tid)
            _write_ticket_json(
                archive_root, tid,
                generated_ac=["Cross-field validation is required."],
            )
            # AI review DID flag a cross-field issue on this run.
            _seed_ai_issue(
                conn, pr_run_id=i + 1,
                category="correctness",
                summary="cross-field validation missing between A and B",
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert out == []

    def test_ac_does_not_mention_taxonomy_no_eligibility(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE * 2):
            tid = f"XCSF30-{i+400}"
            _seed_pr_run(conn, i + 1, tid)
            _write_ticket_json(
                archive_root, tid,
                generated_ac=[
                    "Button must be blue.",
                    "Text must be centered.",
                ],
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert out == []

    def test_below_threshold_no_emit(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE - 1):
            tid = f"XCSF30-{i+500}"
            _seed_pr_run(conn, i + 1, tid)
            _write_ticket_json(
                archive_root, tid,
                generated_ac=["Prevent double-submit race conditions."],
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert out == []


class TestRobustness:
    def test_missing_ticket_json_is_skipped(
        self, conn, archive_root: Path
    ) -> None:
        # Seed pr_runs without writing ticket.json files.
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{i+600}"
            _seed_pr_run(conn, i + 1, tid)
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert out == []

    def test_malformed_ticket_json_is_skipped(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{i+700}"
            _seed_pr_run(conn, i + 1, tid)
            target = archive_root / tid
            target.mkdir(parents=True)
            (target / "ticket.json").write_text(
                "{not json", encoding="utf-8"
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert out == []

    def test_unknown_client_profile_is_dropped(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"UNK-{i+800}"
            _seed_pr_run(conn, i + 1, tid, profile="nonexistent-profile")
            _write_ticket_json(
                archive_root, tid,
                generated_ac=["Cross-field validation is required."],
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert out == []

    def test_ticket_retries_do_not_double_count(
        self, conn, archive_root: Path
    ) -> None:
        """Multiple pr_runs for the SAME ticket ID must count once."""
        # Write ONE ticket.json shared by two pr_runs (retry case).
        tid = "XCSF30-900"
        _seed_pr_run(conn, 1, tid)
        _seed_pr_run(conn, 2, tid)  # retry
        _write_ticket_json(
            archive_root, tid,
            generated_ac=["Cross-field validation is required."],
        )
        # Add two more DISTINCT tickets — cluster size across distinct
        # ticket_ids = 3 (threshold).
        for i, ticket_id in enumerate(["XCSF30-901", "XCSF30-902"]):
            _seed_pr_run(conn, 10 + i, ticket_id)
            _write_ticket_json(
                archive_root, ticket_id,
                generated_ac=["Cross-field validation is required."],
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert len(out) == 1  # 3 distinct tickets, not 4 pr_runs

    def test_authored_ac_also_counts(
        self, conn, archive_root: Path
    ) -> None:
        # Some tickets land with acceptance_criteria (authored) only —
        # generated may be empty when the analyst doesn't run.
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{i+1000}"
            _seed_pr_run(conn, i + 1, tid)
            _write_ticket_json(
                archive_root, tid,
                authored_ac=[
                    "URL state (back button) must restore form values."
                ],
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert len(out) == 1
        assert "url_state" in out[0].scope_key


class TestRegistry:
    def test_build_returns_instance(self) -> None:
        det = build()
        assert det.name == "form_controls_ac_gaps"
        assert det.version == 1

    def test_registered_in_package(self) -> None:
        from learning_miner import get_detector

        det = get_detector("form_controls_ac_gaps")
        assert det is not None
        assert det.name == "form_controls_ac_gaps"


class TestStructuredAcShape:
    """Post implicit-requirements migration, ticket.json carries
    ``generated_acceptance_criteria`` as a list of dicts with ``text``
    and ``category`` keys. The detector must still fire on those."""

    def test_structured_ac_is_extracted_and_fires_gap(
        self, conn: sqlite3.Connection, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{i + 400}"
            _seed_pr_run(conn, i + 1, tid)
            _write_ticket_json(
                archive_root,
                tid,
                generated_ac=[
                    {
                        "id": "AC-001",
                        "category": "implicit",
                        "text": (
                            "Invalid start>end date range shows inline "
                            "validation (cross-field validation)."
                        ),
                        "feature_type": "form_controls",
                    },
                ],
            )
            _seed_ai_issue(
                conn, pr_run_id=i + 1,
                category="style", summary="extra whitespace",
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert len(out) == 1
        assert "cross_field_validation" in out[0].pattern_key

    def test_extract_ac_list_handles_mixed_shapes(self) -> None:
        """_extract_ac_list must accept both legacy and structured shape
        on disk. Unit-test the helper directly — SQLite fixtures are
        unnecessary overhead for this path."""
        from learning_miner.detectors.form_controls_ac_gaps import _extract_ac_list

        mixed = {
            "id": "X-1",
            "acceptance_criteria": ["legacy string ac"],
            "generated_acceptance_criteria": [
                {"id": "AC-001", "category": "ticket", "text": "structured ac"},
                "surviving legacy string in generated list",
                {"id": "AC-002", "category": "implicit", "text": ""},
            ],
        }
        extracted = _extract_ac_list(mixed)
        assert "legacy string ac" in extracted
        assert "structured ac" in extracted
        assert "surviving legacy string in generated list" in extracted
        assert "" not in extracted

    def test_extract_ac_list_handles_non_string_text_values(self) -> None:
        """Non-string text values in structured AC dicts are coerced to
        string and stripped; empty results are skipped."""
        from learning_miner.detectors.form_controls_ac_gaps import _extract_ac_list

        ticket = {
            "id": "X-2",
            "acceptance_criteria": [],
            "generated_acceptance_criteria": [
                {"id": "AC-001", "category": "ticket", "text": None},
                {"id": "AC-002", "category": "ticket", "text": 123},
            ],
        }
        extracted = _extract_ac_list(ticket)
        # None skipped (empty); 123 coerced to "123" (still truthy).
        assert "123" in extracted
        assert "None" not in extracted

    def test_detector_does_not_emit_when_ai_review_covers_the_category(
        self, conn: sqlite3.Connection, archive_root: Path
    ) -> None:
        """If the AI reviewer already filed an issue in a matching category
        for the same run, the detector does not treat the run as a gap.
        This is the negative backstop guarding against the detector over-firing
        after implicit ACs tighten the eligibility set."""
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{i + 500}"
            _seed_pr_run(conn, i + 1, tid)
            _write_ticket_json(
                archive_root,
                tid,
                generated_ac=[
                    {
                        "id": "AC-001",
                        "category": "implicit",
                        "text": "Invalid start>end date range shows cross-field validation.",
                        "feature_type": "form_controls",
                    },
                ],
            )
            # AI reviewer filed a matching cross-field issue — gap is closed.
            _seed_ai_issue(
                conn,
                pr_run_id=i + 1,
                category="validation",
                summary="cross-field date range not enforced",
            )
        out = FormControlsAcGapsDetector().scan(conn, window_days=14)
        assert out == []
