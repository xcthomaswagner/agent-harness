"""Tests for Detector 4 — cross_unit_object_pivot.

Covers:

- Positive fixture: MIN_CLUSTER_SIZE tickets have multiple plan
  versions targeting objects but no permset unit → one candidate.
- Negative: single plan version (no pivot) → no candidate.
- Negative: plan has a permset unit → no gap.
- Negative: plan has no SObject paths at all → not eligible.
- Below-threshold clusters do not emit.
- Malformed plan JSON is skipped.
- Missing plans directory is skipped.
- Non-Salesforce profiles are skipped without error.
- Ticket retries do not double-count.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from learning_miner.detectors.cross_unit_object_pivot import (
    MIN_CLUSTER_SIZE,
    CrossUnitObjectPivotDetector,
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
    import learning_miner.detectors.cross_unit_object_pivot as det

    monkeypatch.setattr(det, "PLAN_ARCHIVE_ROOT", tmp_path)
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


def _write_plan_version(
    archive_root: Path,
    ticket_id: str,
    version: int,
    *,
    units: list[dict],
) -> None:
    target = archive_root / ticket_id / "plans"
    target.mkdir(parents=True, exist_ok=True)
    doc = {"units": units}
    (target / f"plan-v{version}.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )


def _pivot_unit(object_name: str) -> dict:
    return {
        "id": f"unit-for-{object_name}",
        "description": f"Update {object_name}",
        "affected_files": [
            f"force-app/main/default/objects/{object_name}/"
            f"{object_name}.object-meta.xml",
        ],
    }


def _permset_unit() -> dict:
    return {
        "id": "unit-permsets",
        "description": "Update permission sets",
        "affected_files": [
            "force-app/main/default/permissionsets/ReadAll.permissionset-meta.xml",
        ],
    }


class TestPositiveFixture:
    def test_pivot_without_permset_emits_candidate(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{100+i}"
            _seed_pr_run(conn, i + 1, tid)
            _write_plan_version(
                archive_root, tid, 1,
                units=[_pivot_unit("Alpha")],
            )
            _write_plan_version(
                archive_root, tid, 2,
                units=[_pivot_unit("Beta")],  # pivot!
            )
        out = CrossUnitObjectPivotDetector().scan(conn, window_days=14)
        assert len(out) == 1
        assert out[0].client_profile == "xcsf30"
        assert out[0].platform_profile == "salesforce"
        assert out[0].pattern_key == "object_pivot_no_permset"

    def test_severity_bump_at_high_cluster(
        self, conn, archive_root: Path
    ) -> None:
        count = MIN_CLUSTER_SIZE * 2
        for i in range(count):
            tid = f"XCSF30-{200+i}"
            _seed_pr_run(conn, i + 1, tid)
            _write_plan_version(
                archive_root, tid, 1,
                units=[_pivot_unit("Alpha")],
            )
            _write_plan_version(
                archive_root, tid, 2,
                units=[_pivot_unit("Beta")],
            )
        out = CrossUnitObjectPivotDetector().scan(conn, window_days=14)
        assert len(out) == 1
        assert out[0].severity == "warn"


class TestNegativeFixtures:
    def test_single_plan_version_not_eligible(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{300+i}"
            _seed_pr_run(conn, i + 1, tid)
            _write_plan_version(
                archive_root, tid, 1,
                units=[_pivot_unit("Alpha")],
            )
        out = CrossUnitObjectPivotDetector().scan(conn, window_days=14)
        assert out == []

    def test_plan_with_permset_unit_not_a_gap(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{400+i}"
            _seed_pr_run(conn, i + 1, tid)
            _write_plan_version(
                archive_root, tid, 1,
                units=[_pivot_unit("Alpha")],
            )
            _write_plan_version(
                archive_root, tid, 2,
                units=[_pivot_unit("Beta"), _permset_unit()],
            )
        out = CrossUnitObjectPivotDetector().scan(conn, window_days=14)
        assert out == []

    def test_plan_with_no_sobject_paths_not_eligible(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{500+i}"
            _seed_pr_run(conn, i + 1, tid)
            _write_plan_version(
                archive_root, tid, 1,
                units=[{"affected_files": ["src/util.ts"]}],
            )
            _write_plan_version(
                archive_root, tid, 2,
                units=[{"affected_files": ["src/util.ts", "src/y.ts"]}],
            )
        out = CrossUnitObjectPivotDetector().scan(conn, window_days=14)
        assert out == []

    def test_below_threshold_no_emit(
        self, conn, archive_root: Path
    ) -> None:
        # Just one ticket with full pivot pattern.
        tid = "XCSF30-600"
        _seed_pr_run(conn, 1, tid)
        _write_plan_version(
            archive_root, tid, 1, units=[_pivot_unit("A")]
        )
        _write_plan_version(
            archive_root, tid, 2, units=[_pivot_unit("B")]
        )
        out = CrossUnitObjectPivotDetector().scan(conn, window_days=14)
        assert out == []


class TestRobustness:
    def test_missing_plans_dir_is_skipped(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{700+i}"
            _seed_pr_run(conn, i + 1, tid)
            # No plans written.
        out = CrossUnitObjectPivotDetector().scan(conn, window_days=14)
        assert out == []

    def test_malformed_plan_is_skipped(
        self, conn, archive_root: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{800+i}"
            _seed_pr_run(conn, i + 1, tid)
            target = archive_root / tid / "plans"
            target.mkdir(parents=True, exist_ok=True)
            (target / "plan-v1.json").write_text("{")
            (target / "plan-v2.json").write_text("{")
        out = CrossUnitObjectPivotDetector().scan(conn, window_days=14)
        assert out == []

    def test_non_salesforce_profile_skipped(
        self, conn, archive_root: Path
    ) -> None:
        # Unknown profile → resolver returns None → skipped.
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"UNK-{900+i}"
            _seed_pr_run(conn, i + 1, tid, profile="nonexistent-profile")
            _write_plan_version(
                archive_root, tid, 1, units=[_pivot_unit("A")]
            )
            _write_plan_version(
                archive_root, tid, 2, units=[_pivot_unit("B")]
            )
        out = CrossUnitObjectPivotDetector().scan(conn, window_days=14)
        assert out == []

    def test_ticket_retries_do_not_double_count(
        self, conn, archive_root: Path
    ) -> None:
        # One ticket with two pr_runs (retry); threshold needs 2
        # distinct tickets.
        tid = "XCSF30-1000"
        _seed_pr_run(conn, 1, tid)
        _seed_pr_run(conn, 2, tid)
        _write_plan_version(
            archive_root, tid, 1, units=[_pivot_unit("A")]
        )
        _write_plan_version(
            archive_root, tid, 2, units=[_pivot_unit("B")]
        )
        out = CrossUnitObjectPivotDetector().scan(conn, window_days=14)
        assert out == []  # 1 unique ticket, below threshold


class TestRegistry:
    def test_build_returns_instance(self) -> None:
        det = build()
        assert det.name == "cross_unit_object_pivot"
        assert det.version == 1

    def test_registered_in_package(self) -> None:
        from learning_miner import get_detector

        det = get_detector("cross_unit_object_pivot")
        assert det is not None
        assert det.name == "cross_unit_object_pivot"
