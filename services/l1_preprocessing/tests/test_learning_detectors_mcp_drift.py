"""Tests for Detector 1 — mcp_drift.

Covers:

- Bash count below MIN_BASH_COUNT does not trigger.
- MCP server not in ``mcp_servers_available`` does not trigger.
- Ratio gate: mcp_count of 1 keeps the detector quiet if bash_count
  is only 2x that (below RATIO_THRESHOLD).
- Cluster threshold: fewer than MIN_CLUSTER_SIZE distinct tickets
  do not emit a candidate.
- Severity bump when MCP usage is zero.
- Alias folding: ``sfdx`` is counted alongside ``sf``.
- Platform-profile resolution: unknown profile is dropped.
- Window filter: pr_runs before the cutoff are excluded.
- Malformed / missing trace files don't crash the scan.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from learning_miner import run_miner
from learning_miner.detectors.human_issue_cluster import (
    _resolve_platform_profile,
)
from learning_miner.detectors.mcp_drift import (
    MIN_BASH_COUNT,
    MIN_CLUSTER_SIZE,
    McpDriftDetector,
    _canonical_verb,
    build,
)
from tests.conftest import seed_pr_run_for_learning


@pytest.fixture(autouse=True)
def clear_platform_cache():
    _resolve_platform_profile.cache_clear()
    yield
    _resolve_platform_profile.cache_clear()


@pytest.fixture
def conn(learning_conn):
    return learning_conn


@pytest.fixture
def trace_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point tracer.LOGS_DIR at tmp_path so tests can write trace files."""
    import tracer

    monkeypatch.setattr(tracer, "LOGS_DIR", tmp_path)
    return tmp_path


def _days_ago_iso(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _write_trace(
    trace_dir: Path,
    ticket_id: str,
    *,
    bash_verb_counts: dict[str, int],
    mcp_servers_available: list[str],
    tool_counts: dict[str, int] | None = None,
) -> None:
    """Write a minimal trace file with one tool_index artifact."""
    path = trace_dir / f"{ticket_id}.jsonl"
    entries = [
        {
            "trace_id": ticket_id,
            "ticket_id": ticket_id,
            "timestamp": _days_ago_iso(1),
            "phase": "start",
            "event": "run_started",
        },
        {
            "trace_id": ticket_id,
            "ticket_id": ticket_id,
            "timestamp": _days_ago_iso(1),
            "phase": "artifact",
            "event": "tool_index",
            "index": {
                "tool_counts": tool_counts or {},
                "tool_errors": {},
                "bash_verb_counts": bash_verb_counts,
                "mcp_servers_used": [
                    s for s, _ in (tool_counts or {}).items()
                ],
                "mcp_servers_available": mcp_servers_available,
                "mcp_servers_unused": [],
                "first_tool_error": None,
                "assistant_turns": 1,
                "tool_call_count": sum(bash_verb_counts.values()),
            },
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _seed_run(
    conn, pr_number: int, ticket_id: str, *, profile: str = "xcsf30"
) -> int:
    return seed_pr_run_for_learning(
        conn,
        pr_number=pr_number,
        ticket_id=ticket_id,
        client_profile=profile,
        opened_at=_days_ago_iso(1),
    )


# ---- canonicalization ------------------------------------------------


class TestCanonicalVerb:
    def test_alias_folded(self) -> None:
        assert _canonical_verb("sfdx") == "sf"

    def test_known_verb_passes_through(self) -> None:
        assert _canonical_verb("sf") == "sf"

    def test_unknown_passes_through(self) -> None:
        assert _canonical_verb("tar") == "tar"

    def test_empty(self) -> None:
        assert _canonical_verb("") == ""
        assert _canonical_verb(None) == ""  # type: ignore[arg-type]


# ---- threshold + ratio gates ----------------------------------------


class TestThresholds:
    def test_below_min_bash_count_does_not_trigger(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": MIN_BASH_COUNT - 1},
                mcp_servers_available=["salesforce_capability_mcp"],
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert out == []

    def test_ratio_gate_keeps_quiet_when_mcp_used(
        self, conn, trace_dir: Path
    ) -> None:
        """bash=3 and mcp=1 → ratio 3/(1+1)=1.5 < threshold 3 → no proposal."""
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": 3},
                mcp_servers_available=["salesforce_capability_mcp"],
                tool_counts={"mcp__salesforce_capability_mcp__sf_deploy": 1},
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert out == []

    def test_ratio_gate_triggers_when_bash_dominates(
        self, conn, trace_dir: Path
    ) -> None:
        """bash=9, mcp=1 → ratio 9/2=4.5 > 3 → triggers."""
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": 9},
                mcp_servers_available=["salesforce_capability_mcp"],
                tool_counts={"mcp__salesforce_capability_mcp__sf_deploy": 1},
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert len(out) == 1
        assert out[0].pattern_key == "sf|salesforce_capability_mcp"

    def test_below_cluster_size_does_not_emit(
        self, conn, trace_dir: Path
    ) -> None:
        # 2 tickets, threshold is 3.
        for i in range(MIN_CLUSTER_SIZE - 1):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": 10},
                mcp_servers_available=["salesforce_capability_mcp"],
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert out == []


# ---- availability + platform resolution ------------------------------


class TestAvailabilityAndResolution:
    def test_mcp_not_available_does_not_trigger(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": 10},
                mcp_servers_available=[],  # no MCPs at all
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert out == []

    def test_unknown_client_profile_dropped(
        self, conn, trace_dir: Path
    ) -> None:
        # Seed pr_runs with an unknown client_profile — platform
        # resolution returns None, detector skips the row.
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid, profile="nonexistent-profile")
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": 10},
                mcp_servers_available=["salesforce_capability_mcp"],
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert out == []


# ---- severity bumps --------------------------------------------------


class TestRetryDedup:
    def test_multiple_pr_runs_per_ticket_dedupe(
        self, conn, trace_dir: Path
    ) -> None:
        """Regression: a ticket with multiple pr_runs (retries) used
        to contribute one observation per pr_run — each counting the
        SAME bash/mcp totals from the shared trace. That inflated
        rationale's ``total_bash`` and could flip severity. Now we
        count one observation per ticket+verb.
        """
        # Seed MIN_CLUSTER_SIZE tickets, each with a retry (2 pr_runs).
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, 100 + i * 2, tid)
            _seed_run(conn, 101 + i * 2, tid)  # retry — same ticket
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": 10},
                mcp_servers_available=["salesforce_capability_mcp"],
                tool_counts={},
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert len(out) == 1
        proposal = out[0]
        # Rationale references the unique-ticket count, and total_bash
        # should be the raw 10 per ticket * 3 tickets = 30, NOT 60.
        delta = json.loads(proposal.proposed_delta_json)
        assert "30x" in delta["rationale_md"]
        assert "60x" not in delta["rationale_md"]
        # window_frequency tracks unique tickets.
        assert proposal.window_frequency == MIN_CLUSTER_SIZE


class TestSeverity:
    def test_zero_mcp_bumps_severity(self, conn, trace_dir: Path) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": 10},
                mcp_servers_available=["salesforce_capability_mcp"],
                tool_counts={},  # zero MCP usage
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert len(out) == 1
        # 3 tickets exactly + zero MCP → "warn"; 6+ would be "critical".
        assert out[0].severity == "warn"

    def test_zero_mcp_at_large_cluster_is_critical(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE * 2):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": 10},
                mcp_servers_available=["salesforce_capability_mcp"],
                tool_counts={},
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert len(out) == 1
        assert out[0].severity == "critical"


# ---- alias folding ---------------------------------------------------


class TestAliases:
    def test_sfdx_counted_alongside_sf(
        self, conn, trace_dir: Path
    ) -> None:
        # Mix sfdx + sf calls across the cluster; detector should
        # fold both under ``sf``.
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            verbs = {"sfdx": 5, "sf": 5} if i % 2 == 0 else {"sf": 10}
            _write_trace(
                trace_dir, tid,
                bash_verb_counts=verbs,
                mcp_servers_available=["salesforce_capability_mcp"],
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert len(out) == 1
        assert out[0].pattern_key == "sf|salesforce_capability_mcp"


# ---- window filter + error resilience --------------------------------


class TestWindowAndResilience:
    def test_window_filter_excludes_old_runs(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            # Seed with an opened_at 30 days ago — well outside a 14-day window.
            seed_pr_run_for_learning(
                conn,
                pr_number=i + 1,
                ticket_id=tid,
                client_profile="xcsf30",
                opened_at=_days_ago_iso(30),
            )
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": 10},
                mcp_servers_available=["salesforce_capability_mcp"],
            )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert out == []

    def test_missing_trace_file_skipped(
        self, conn, trace_dir: Path
    ) -> None:
        # Seed 3 runs but only write trace files for 2 of them.
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            if i < 2:
                _write_trace(
                    trace_dir, tid,
                    bash_verb_counts={"sf": 10},
                    mcp_servers_available=["salesforce_capability_mcp"],
                )
        out = McpDriftDetector().scan(conn, window_days=14)
        assert out == []  # only 2 tickets → below cluster size

    def test_malformed_trace_does_not_crash(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            if i == 0:
                # malformed JSON
                (trace_dir / f"{tid}.jsonl").write_text("not json\n")
            else:
                _write_trace(
                    trace_dir, tid,
                    bash_verb_counts={"sf": 10},
                    mcp_servers_available=["salesforce_capability_mcp"],
                )
        # Must not raise; just skip the bad trace.
        out = McpDriftDetector().scan(conn, window_days=14)
        assert out == []  # only 2 usable → below cluster size


# ---- factory + end-to-end --------------------------------------------


class TestFactoryAndRunner:
    def test_build_returns_detector(self) -> None:
        det = build()
        assert det.name == "mcp_drift"
        assert det.version == 1

    def test_run_miner_persists_mcp_drift_candidate(
        self, conn, trace_dir: Path
    ) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            tid = f"TKT-{i}"
            _seed_run(conn, i + 1, tid)
            _write_trace(
                trace_dir, tid,
                bash_verb_counts={"sf": 10},
                mcp_servers_available=["salesforce_capability_mcp"],
            )
        detector = build()
        result = run_miner(conn, [detector], window_days=14)
        assert result.total_failures == 0
        assert result.total_candidates == 1
