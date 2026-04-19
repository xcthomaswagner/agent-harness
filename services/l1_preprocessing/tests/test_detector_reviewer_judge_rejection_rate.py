"""Tests for Detector 6 — reviewer_judge_rejection_rate.

Covers:

- Metrics capture phase: seeded judge-verdict.json files result in
  pipeline_metrics rows after scan.
- Positive fixture: 5 runs with 80%+ rejection → rolling mean > 0.7
  → one lesson emitted.
- Negative fixture: 5 runs with 40% rejection → no emit.
- Insufficient history: fewer than WINDOW_RUNS runs → no emit even
  if every observation is over threshold.
- Zero-issue verdicts (no validated, no rejected) contribute nothing
  to the rolling window.
- Malformed judge-verdict.json is skipped without crashing.
- Ticket retries don't create duplicate metric rows.
- Severity escalates at higher means.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autonomy_store import count_metrics, list_recent_metrics
from learning_miner.detectors.human_issue_cluster import (
    _resolve_platform_profile,
)
from learning_miner.detectors.reviewer_judge_rejection_rate import (
    METRIC_NAME,
    RATE_THRESHOLD,
    WINDOW_RUNS,
    ReviewerJudgeRejectionRateDetector,
    build,
)
from tests.conftest import seed_pr_run_for_learning


@pytest.fixture(autouse=True)
def clear_platform_cache():
    _resolve_platform_profile.cache_clear()
    yield
    _resolve_platform_profile.cache_clear()


@pytest.fixture
def archive_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import learning_miner.detectors.reviewer_judge_rejection_rate as det

    monkeypatch.setattr(det, "JUDGE_ARCHIVE_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def conn(learning_conn):
    return learning_conn


def _days_ago_iso(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _seed_pr_run(
    conn, pr_number: int, ticket_id: str, *,
    profile: str = "xcsf30", opened_days_ago: int = 1,
) -> int:
    return seed_pr_run_for_learning(
        conn,
        pr_number=pr_number,
        ticket_id=ticket_id,
        client_profile=profile,
        opened_at=_days_ago_iso(opened_days_ago),
    )


def _write_judge_verdict(
    archive_root: Path,
    ticket_id: str,
    *,
    validated: int,
    rejected: int,
) -> None:
    target = archive_root / ticket_id / "logs"
    target.mkdir(parents=True, exist_ok=True)
    doc = {
        "validated_issues": [
            {"source_issue_id": f"cr-{i+1}", "score": 92}
            for i in range(validated)
        ],
        "rejected_issues": [
            {"source_issue_id": f"cr-{validated + i + 1}", "score": 25}
            for i in range(rejected)
        ],
    }
    (target / "judge-verdict.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )


class TestMetricsCapture:
    def test_metrics_rows_populated_after_scan(
        self, conn, archive_root: Path
    ) -> None:
        # Two tickets: one 80% rejection, one 40%.
        _seed_pr_run(conn, 1, "XCSF30-1", opened_days_ago=2)
        _write_judge_verdict(
            archive_root, "XCSF30-1", validated=1, rejected=4
        )
        _seed_pr_run(conn, 2, "XCSF30-2", opened_days_ago=1)
        _write_judge_verdict(
            archive_root, "XCSF30-2", validated=3, rejected=2
        )

        ReviewerJudgeRejectionRateDetector().scan(conn, window_days=14)

        assert count_metrics(conn, metric_name=METRIC_NAME) == 2
        rows = list_recent_metrics(
            conn, metric_name=METRIC_NAME, limit=10
        )
        values = {m.ticket_id: m.metric_value for m in rows}
        assert abs(values["XCSF30-1"] - 0.8) < 1e-9
        assert abs(values["XCSF30-2"] - 0.4) < 1e-9

    def test_zero_issues_does_not_produce_metric(
        self, conn, archive_root: Path
    ) -> None:
        _seed_pr_run(conn, 1, "XCSF30-1")
        _write_judge_verdict(
            archive_root, "XCSF30-1", validated=0, rejected=0
        )
        ReviewerJudgeRejectionRateDetector().scan(conn, window_days=14)
        assert count_metrics(conn, metric_name=METRIC_NAME) == 0

    def test_malformed_verdict_is_skipped(
        self, conn, archive_root: Path
    ) -> None:
        _seed_pr_run(conn, 1, "XCSF30-1")
        target = archive_root / "XCSF30-1" / "logs"
        target.mkdir(parents=True)
        (target / "judge-verdict.json").write_text("{not json")
        ReviewerJudgeRejectionRateDetector().scan(conn, window_days=14)
        assert count_metrics(conn, metric_name=METRIC_NAME) == 0

    def test_retry_pr_runs_do_not_duplicate_rows(
        self, conn, archive_root: Path
    ) -> None:
        # Same ticket, two pr_runs, one verdict file → one metric row.
        _seed_pr_run(conn, 1, "XCSF30-1", opened_days_ago=3)
        _seed_pr_run(conn, 2, "XCSF30-1", opened_days_ago=2)
        _write_judge_verdict(
            archive_root, "XCSF30-1", validated=1, rejected=4
        )
        ReviewerJudgeRejectionRateDetector().scan(conn, window_days=14)
        assert count_metrics(conn, metric_name=METRIC_NAME) == 1


class TestRollingThresholdEmission:
    def test_five_runs_at_80_percent_emit_candidate(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(WINDOW_RUNS):
            tid = f"XCSF30-{100+i}"
            _seed_pr_run(conn, i + 1, tid, opened_days_ago=WINDOW_RUNS - i)
            _write_judge_verdict(
                archive_root, tid, validated=1, rejected=4
            )
        out = ReviewerJudgeRejectionRateDetector().scan(
            conn, window_days=14
        )
        assert len(out) == 1
        prop = out[0]
        assert prop.pattern_key == "reviewer_judge_rejection_rate"
        assert prop.client_profile == "xcsf30"
        assert prop.platform_profile == "salesforce"
        # Mean 0.8 lands at warn.
        assert prop.severity == "warn"

    def test_five_runs_at_40_percent_do_not_emit(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(WINDOW_RUNS):
            tid = f"XCSF30-{200+i}"
            _seed_pr_run(conn, i + 1, tid, opened_days_ago=WINDOW_RUNS - i)
            _write_judge_verdict(
                archive_root, tid, validated=3, rejected=2
            )
        out = ReviewerJudgeRejectionRateDetector().scan(
            conn, window_days=14
        )
        assert out == []

    def test_insufficient_history_no_emit(
        self, conn, archive_root: Path
    ) -> None:
        # Only 3 runs, all at 90% rejection — still below window size.
        for i in range(WINDOW_RUNS - 2):
            tid = f"XCSF30-{300+i}"
            _seed_pr_run(conn, i + 1, tid, opened_days_ago=WINDOW_RUNS - i)
            _write_judge_verdict(
                archive_root, tid, validated=1, rejected=9
            )
        out = ReviewerJudgeRejectionRateDetector().scan(
            conn, window_days=14
        )
        assert out == []

    def test_very_high_mean_bumps_to_critical(
        self, conn, archive_root: Path
    ) -> None:
        # 90% rejection across the window → critical.
        for i in range(WINDOW_RUNS):
            tid = f"XCSF30-{400+i}"
            _seed_pr_run(conn, i + 1, tid, opened_days_ago=WINDOW_RUNS - i)
            _write_judge_verdict(
                archive_root, tid, validated=1, rejected=9
            )
        out = ReviewerJudgeRejectionRateDetector().scan(
            conn, window_days=14
        )
        assert len(out) == 1
        assert out[0].severity == "critical"

    def test_threshold_is_strict(
        self, conn, archive_root: Path
    ) -> None:
        # Exactly at threshold (mean = 0.70) → NOT strictly greater →
        # no emit. Protects against emitting on "it's right at the line."
        # Mix 3 runs at 1.0 and 2 runs at 0.25 → mean = 0.7 exactly.
        # (3 * 1.0 + 2 * 0.25) / 5 = 3.5 / 5 = 0.7
        for i in range(3):
            tid = f"XCSF30-{500+i}"
            _seed_pr_run(conn, i + 1, tid, opened_days_ago=WINDOW_RUNS - i)
            _write_judge_verdict(
                archive_root, tid, validated=0, rejected=5
            )
        for i in range(2):
            tid = f"XCSF30-{510+i}"
            _seed_pr_run(
                conn, 100 + i, tid, opened_days_ago=2 - i
            )
            _write_judge_verdict(
                archive_root, tid, validated=3, rejected=1
            )
        out = ReviewerJudgeRejectionRateDetector().scan(
            conn, window_days=14
        )
        # Sanity: threshold is 0.7, our mean = 0.7, strict > → no emit.
        assert RATE_THRESHOLD == 0.7
        assert out == []


class TestMultiPlatformWindow:
    """Regression guard for the multi-platform window bug.

    With 2+ active platforms each emitting WINDOW_RUNS high-rejection
    runs in the window, ``_emit_if_rolling_threshold_met`` used to
    fetch only ``limit=WINDOW_RUNS`` rows globally. Split 5/5 across
    two platforms, neither platform's per-group check could satisfy
    ``>= WINDOW_RUNS``, so neither lesson emitted. Fix fetches
    ``WINDOW_RUNS * MAX_PLATFORMS`` rows so per-platform windows
    can be satisfied independently.
    """

    def test_two_platforms_each_emit_their_lesson(
        self,
        conn,
        archive_root: Path,
        tmp_path: Path,
    ) -> None:
        from unittest.mock import patch

        import client_profile as cp

        # Scratch profiles with two distinct non-empty platforms. Any
        # non-empty, distinct strings would do — values just flow
        # through _resolve_platform_profile into the scope key.
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "p-alpha.yaml").write_text(
            "client: alpha\nplatform_profile: \"alpha_stack\"\n"
        )
        (profiles_dir / "p-beta.yaml").write_text(
            "client: beta\nplatform_profile: \"beta_stack\"\n"
        )

        _resolve_platform_profile.cache_clear()
        with patch.object(cp, "PROFILES_DIR", profiles_dir):
            # Seed 5 high-rejection runs on each platform. opened_days_ago
            # interleaves across platforms so the global ORDER BY
            # observed_at DESC in list_recent_metrics would mix them.
            for i in range(WINDOW_RUNS):
                tid = f"ALPHA-{600+i}"
                _seed_pr_run(
                    conn,
                    i + 1,
                    tid,
                    profile="p-alpha",
                    opened_days_ago=2 * WINDOW_RUNS - 2 * i,
                )
                _write_judge_verdict(
                    archive_root, tid, validated=1, rejected=9
                )
            for i in range(WINDOW_RUNS):
                tid = f"BETA-{700+i}"
                _seed_pr_run(
                    conn,
                    100 + i + 1,
                    tid,
                    profile="p-beta",
                    opened_days_ago=2 * WINDOW_RUNS - 1 - 2 * i,
                )
                _write_judge_verdict(
                    archive_root, tid, validated=1, rejected=9
                )

            out = ReviewerJudgeRejectionRateDetector().scan(
                conn, window_days=14
            )

        # Each platform has 5 runs at 90% — each should emit one lesson.
        platforms = sorted(p.platform_profile for p in out)
        assert platforms == ["alpha_stack", "beta_stack"], (
            "Expected one lesson per platform with 5 rows each "
            f"(got {platforms})"
        )


class TestRegistry:
    def test_build_returns_instance(self) -> None:
        det = build()
        assert det.name == "reviewer_judge_rejection_rate"
        assert det.version == 1

    def test_registered_in_package(self) -> None:
        from learning_miner import get_detector

        det = get_detector("reviewer_judge_rejection_rate")
        assert det is not None
        assert det.name == "reviewer_judge_rejection_rate"
