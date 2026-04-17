"""Tests for the learning_miner runner.

Covers the per-detector isolation contract: a detector that
raises must not prevent the next detector from running, and its
failure must be recorded as structured stats.

Also covers:
- Idempotent runs (same proposals upsert cleanly on re-run).
- Evidence redaction before insert.
- Evidence dedup counter.
"""

from __future__ import annotations

import pytest

from autonomy_store import get_lesson_by_id, list_lesson_evidence
from learning_miner import MinerRunResult, run_miner
from learning_miner.detectors.base import CandidateProposal, EvidenceItem


@pytest.fixture
def conn(learning_conn):
    return learning_conn


class _StubDetector:
    """Minimal Detector stub for runner tests."""

    def __init__(
        self,
        name: str,
        version: int = 1,
        proposals: list[CandidateProposal] | None = None,
        raise_on_scan: Exception | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self._proposals = proposals or []
        self._raise = raise_on_scan

    def scan(self, conn, window_days):  # type: ignore[no-untyped-def]
        if self._raise is not None:
            raise self._raise
        return self._proposals


def _sample_proposal(
    *, detector: str = "det_a", scope: str = "xcsf30|sf|a"
) -> CandidateProposal:
    return CandidateProposal(
        detector_name=detector,
        detector_version=1,
        pattern_key="pk-1",
        client_profile="xcsf30",
        platform_profile="salesforce",
        scope_key=scope,
        severity="warn",
        proposed_delta_json='{"edit_type": "append_section"}',
        evidence=(
            EvidenceItem(
                trace_id="T-1",
                observed_at="2026-04-10T05:00:00+00:00",
                source_ref="review_issues#1",
                snippet="example snippet",
            ),
        ),
    )


class TestPerDetectorIsolation:
    def test_failing_detector_does_not_block_next_detector(self, conn) -> None:
        good_before = _StubDetector(
            "good_before", proposals=[_sample_proposal(detector="good_before")]
        )
        bad = _StubDetector(
            "bad", raise_on_scan=RuntimeError("boom")
        )
        good_after = _StubDetector(
            "good_after", proposals=[_sample_proposal(detector="good_after")]
        )

        result = run_miner(
            conn, [good_before, bad, good_after], window_days=14
        )

        assert isinstance(result, MinerRunResult)
        by_name = {d.detector_name: d for d in result.per_detector}
        assert by_name["good_before"].failed is False
        assert by_name["bad"].failed is True
        assert "RuntimeError" in by_name["bad"].error
        assert by_name["good_after"].failed is False
        # Both good detectors persisted their candidates.
        assert by_name["good_before"].candidates_inserted_or_updated == 1
        assert by_name["good_after"].candidates_inserted_or_updated == 1

    def test_no_candidates_persisted_when_detector_fails(self, conn) -> None:
        bad = _StubDetector("bad", raise_on_scan=ValueError("nope"))
        result = run_miner(conn, [bad], window_days=14)
        assert result.total_candidates == 0
        assert result.total_failures == 1


class TestPersistence:
    def test_proposal_upserts_candidate_and_evidence(self, conn) -> None:
        det = _StubDetector("det_a", proposals=[_sample_proposal()])
        result = run_miner(conn, [det], window_days=14)
        assert result.total_candidates == 1
        assert result.total_evidence == 1

        lid = _sample_proposal().lesson_id
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        assert row["frequency"] == 1
        assert row["detector_name"] == "det_a"
        ev = list_lesson_evidence(conn, lid)
        assert len(ev) == 1
        assert ev[0]["trace_id"] == "T-1"

    def test_rerun_upserts_without_duplicating(self, conn) -> None:
        det = _StubDetector("det_a", proposals=[_sample_proposal()])
        run_miner(conn, [det], window_days=14)
        run_miner(conn, [det], window_days=14)

        lid = _sample_proposal().lesson_id
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        # Same cluster re-detected → frequency is MAX(1, 1) = 1.
        # The upsert must not accumulate scan counts as frequency.
        assert row["frequency"] == 1
        # One candidate row only.
        from autonomy_store import list_lesson_candidates
        assert len(list_lesson_candidates(conn)) == 1
        # Evidence stayed at one row — UNIQUE collision is a no-op.
        ev = list_lesson_evidence(conn, lid)
        assert len(ev) == 1

    def test_evidence_dedup_counted_in_stats(self, conn) -> None:
        det = _StubDetector("det_a", proposals=[_sample_proposal()])
        run_miner(conn, [det], window_days=14)
        result = run_miner(conn, [det], window_days=14)
        stats = result.per_detector[0]
        assert stats.evidence_inserted == 0
        assert stats.evidence_deduped == 1


class TestRedaction:
    def test_snippet_redacted_before_insert(self, conn) -> None:
        # redaction.py's line-pass catches ``Bearer <token>`` patterns.
        leaky = CandidateProposal(
            detector_name="det_a",
            detector_version=1,
            pattern_key="pk",
            client_profile="xcsf30",
            platform_profile="salesforce",
            scope_key="scope",
            severity="warn",
            proposed_delta_json="{}",
            evidence=(
                EvidenceItem(
                    trace_id="T-1",
                    observed_at="2026-04-10T05:00:00+00:00",
                    source_ref="ref",
                    snippet="Authorization: Bearer sk-ant-abc123deadbeef",
                ),
            ),
        )
        det = _StubDetector("det_a", proposals=[leaky])
        run_miner(conn, [det], window_days=14)
        ev = list_lesson_evidence(conn, leaky.lesson_id)
        assert len(ev) == 1
        # Token string must not appear in the stored snippet.
        assert "sk-ant-abc123deadbeef" not in ev[0]["snippet"]


class TestNoDetectors:
    def test_empty_detector_list_is_valid(self, conn) -> None:
        result = run_miner(conn, [], window_days=14)
        assert result.total_candidates == 0
        assert result.total_failures == 0
        assert result.per_detector == []


class TestPerRunCacheClear:
    """Long-running L1 picks up profile YAML edits between scans.

    The runner must clear detector-owned caches at the start of
    each scan. If it doesn't, a profile whose platform_profile
    was changed after L1 started up will keep routing to the
    wrong supplement until restart.
    """

    def test_run_miner_clears_platform_profile_cache(self, conn) -> None:
        from learning_miner.detectors.human_issue_cluster import (
            _resolve_platform_profile,
        )

        # Prime the cache with a bogus entry so we can detect the clear.
        _resolve_platform_profile.cache_clear()
        _resolve_platform_profile("no-such-profile")
        assert _resolve_platform_profile.cache_info().currsize == 1

        run_miner(conn, [], window_days=14)
        assert _resolve_platform_profile.cache_info().currsize == 0
