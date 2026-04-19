"""Tests for Detector 5 — simplify_no_sidecar.

Covers:

- Positive fixture: trace has simplify phase log with changes_made=true
  but NO simplify_artifact entry → violation observed.
- Negative: trace has both the phase log and the simplify_artifact →
  not a violation (sidecar landed as expected).
- Negative: trace has phase log with changes_made=false → not eligible.
- Below-threshold clusters do not emit.
- Corrupt / missing trace file is skipped.
- Ticket retries (multiple pr_runs, same ticket) don't double-count.
- Non-resolvable client profiles are skipped.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from learning_miner.detectors.human_issue_cluster import (
    _resolve_platform_profile,
)
from learning_miner.detectors.simplify_no_sidecar import (
    MIN_CLUSTER_SIZE,
    SimplifyNoSidecarDetector,
    build,
)
from tests.conftest import seed_pr_run_for_learning


@pytest.fixture(autouse=True)
def clear_platform_cache():
    _resolve_platform_profile.cache_clear()
    yield
    _resolve_platform_profile.cache_clear()


@pytest.fixture
def trace_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import tracer

    monkeypatch.setattr(tracer, "LOGS_DIR", tmp_path)
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


def _write_trace(
    trace_dir: Path,
    ticket_id: str,
    *,
    simplify_changes_made: bool | None = None,
    include_simplify_artifact: bool = False,
) -> None:
    """Write a minimal trace file.

    ``simplify_changes_made``:
      - True  → emit simplify phase log with changes_made=true
      - False → emit simplify phase log with changes_made=false
      - None  → no simplify phase log at all

    ``include_simplify_artifact`` toggles the consolidator artifact row.
    """
    entries: list[dict] = [
        {
            "trace_id": ticket_id,
            "ticket_id": ticket_id,
            "timestamp": _days_ago_iso(1),
            "phase": "start",
            "event": "run_started",
        }
    ]
    if simplify_changes_made is not None:
        entries.append({
            "trace_id": ticket_id,
            "ticket_id": ticket_id,
            "timestamp": _days_ago_iso(1),
            "phase": "simplify",
            "event": "Simplification complete",
            "changes_made": simplify_changes_made,
        })
    if include_simplify_artifact:
        entries.append({
            "trace_id": ticket_id,
            "ticket_id": ticket_id,
            "timestamp": _days_ago_iso(1),
            "phase": "artifact",
            "event": "simplify_artifact",
            "content": "## Simplification\n\n### Changes Made\n- x",
        })
    path = trace_dir / f"{ticket_id}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


class TestPositiveFixture:
    def test_claimed_changes_without_sidecar_emits(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{100+i}"
            _seed_pr_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                simplify_changes_made=True,
                include_simplify_artifact=False,
            )
        out = SimplifyNoSidecarDetector().scan(conn, window_days=14)
        assert len(out) == 1
        prop = out[0]
        assert prop.pattern_key == "simplify_no_sidecar"
        assert prop.client_profile == "xcsf30"
        assert prop.platform_profile == "salesforce"


class TestNegativeFixtures:
    def test_sidecar_present_no_violation(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{200+i}"
            _seed_pr_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                simplify_changes_made=True,
                include_simplify_artifact=True,
            )
        out = SimplifyNoSidecarDetector().scan(conn, window_days=14)
        assert out == []

    def test_changes_made_false_not_eligible(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{300+i}"
            _seed_pr_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                simplify_changes_made=False,
                include_simplify_artifact=False,
            )
        out = SimplifyNoSidecarDetector().scan(conn, window_days=14)
        assert out == []

    def test_no_simplify_phase_not_eligible(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{400+i}"
            _seed_pr_run(conn, i + 1, tid)
            _write_trace(trace_dir, tid, simplify_changes_made=None)
        out = SimplifyNoSidecarDetector().scan(conn, window_days=14)
        assert out == []

    def test_below_threshold_no_emit(
        self, conn, trace_dir: Path
    ) -> None:
        tid = "XCSF30-500"
        _seed_pr_run(conn, 1, tid)
        _write_trace(trace_dir, tid, simplify_changes_made=True)
        out = SimplifyNoSidecarDetector().scan(conn, window_days=14)
        assert out == []


class TestRobustness:
    def test_missing_trace_is_skipped(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{600+i}"
            _seed_pr_run(conn, i + 1, tid)
            # intentionally no trace written
        out = SimplifyNoSidecarDetector().scan(conn, window_days=14)
        assert out == []

    def test_corrupt_trace_is_skipped(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{700+i}"
            _seed_pr_run(conn, i + 1, tid)
            (trace_dir / f"{tid}.jsonl").write_text("{not json\n")
        out = SimplifyNoSidecarDetector().scan(conn, window_days=14)
        assert out == []

    def test_non_resolvable_profile_is_skipped(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"UNK-{800+i}"
            _seed_pr_run(conn, i + 1, tid, profile="nonexistent-profile")
            _write_trace(trace_dir, tid, simplify_changes_made=True)
        out = SimplifyNoSidecarDetector().scan(conn, window_days=14)
        assert out == []

    def test_ticket_retries_do_not_double_count(
        self, conn, trace_dir: Path
    ) -> None:
        tid = "XCSF30-900"
        _seed_pr_run(conn, 1, tid)
        _seed_pr_run(conn, 2, tid)  # retry — shares one trace file
        _write_trace(trace_dir, tid, simplify_changes_made=True)
        out = SimplifyNoSidecarDetector().scan(conn, window_days=14)
        # Below cluster threshold — need MIN_CLUSTER_SIZE distinct tickets.
        assert out == []

    def test_changes_made_as_string_true(
        self, conn, trace_dir: Path
    ) -> None:
        # Team Lead sometimes serializes the bool as a string.
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"XCSF30-{1000+i}"
            _seed_pr_run(conn, i + 1, tid)
            path = trace_dir / f"{tid}.jsonl"
            path.write_text(
                json.dumps({
                    "trace_id": tid, "ticket_id": tid,
                    "phase": "simplify",
                    "event": "Simplification complete",
                    "changes_made": "true",
                }) + "\n"
            )
        out = SimplifyNoSidecarDetector().scan(conn, window_days=14)
        assert len(out) == 1


class TestRegistry:
    def test_build_returns_instance(self) -> None:
        det = build()
        assert det.name == "simplify_no_sidecar"
        assert det.version == 1

    def test_registered_in_package(self) -> None:
        from learning_miner import get_detector

        det = get_detector("simplify_no_sidecar")
        assert det is not None
        assert det.name == "simplify_no_sidecar"
