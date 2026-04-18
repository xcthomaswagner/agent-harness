"""Miner runner: orchestrates detectors, persists proposals.

Invariants worth knowing when reading callers:

- A detector that raises in ``scan`` cannot block the next
  detector — each runs in its own try/except.
- Reruns are idempotent. Candidates upsert on
  ``(detector_name, pattern_key, scope_key)``; evidence dedupes
  on ``(lesson_id, trace_id, source_ref)``.
- The runner's only non-DB side effect is redaction — drafting,
  PR opening, and dashboard rendering live elsewhere.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from autonomy_store import (
    LessonCandidateUpsert,
    insert_lesson_evidence,
    upsert_lesson_candidate,
)
from learning_miner.detectors.base import CandidateProposal, Detector
from redaction import redact

logger = structlog.get_logger()

# Name used for the reflector synthetic "detector" in per-run stats.
# Kept as a constant here so the integration test can reference it
# without importing the private name from retrospective_ingest.
REFLECTOR_STATS_NAME = "run_reflector"


@dataclass
class DetectorRunStats:
    """Per-detector accounting for one miner run."""

    detector_name: str
    proposals_emitted: int = 0
    candidates_inserted_or_updated: int = 0
    evidence_inserted: int = 0
    evidence_deduped: int = 0
    failed: bool = False
    error: str = ""
    duration_ms: int = 0


@dataclass
class MinerRunResult:
    """Aggregate result across all detectors."""

    window_days: int
    per_detector: list[DetectorRunStats] = field(default_factory=list)

    @property
    def total_candidates(self) -> int:
        return sum(
            d.candidates_inserted_or_updated
            for d in self.per_detector
        )

    @property
    def total_evidence(self) -> int:
        return sum(d.evidence_inserted for d in self.per_detector)

    @property
    def total_failures(self) -> int:
        return sum(1 for d in self.per_detector if d.failed)

    @property
    def total_duration_ms(self) -> int:
        return sum(d.duration_ms for d in self.per_detector)


def run_miner(
    conn: sqlite3.Connection,
    detectors: Iterable[Detector],
    *,
    window_days: int,
    retrospective_search_roots: Iterable[Path] | None = None,
) -> MinerRunResult:
    """Run every detector, persist proposals, return aggregate stats.

    Clears detector-owned caches up front so a long-lived L1
    process picks up profile YAML edits between scans without a
    restart.

    When ``retrospective_search_roots`` is provided, walk those paths
    for ``retrospective.json`` files, convert them to proposals via
    ``retrospective_ingest.ingest_retrospectives``, and persist them
    alongside the detector output under a synthetic per-run stats
    entry named ``run_reflector``. Passing ``None`` (the default)
    preserves the pre-reflector behavior exactly — existing tests
    and the nightly backfill script keep working without change.
    """
    _clear_per_run_caches()
    result = MinerRunResult(window_days=window_days)

    for detector in detectors:
        stats = DetectorRunStats(detector_name=detector.name)
        det_start = time.perf_counter()
        try:
            proposals = detector.scan(conn, window_days)
            stats.proposals_emitted = len(proposals)
            for proposal in proposals:
                _persist_proposal(conn, proposal, stats)
        except Exception as exc:
            stats.failed = True
            stats.error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "learning_detector_failed",
                detector_name=detector.name,
                detector_version=detector.version,
                error=stats.error,
            )
        finally:
            stats.duration_ms = int(
                (time.perf_counter() - det_start) * 1000
            )
            result.per_detector.append(stats)

    if retrospective_search_roots is not None:
        _ingest_reflector_proposals(
            conn, retrospective_search_roots, result
        )

    logger.info(
        "learning_miner_run",
        window_days=window_days,
        detectors=len(result.per_detector),
        candidates=result.total_candidates,
        evidence=result.total_evidence,
        failures=result.total_failures,
        duration_ms=result.total_duration_ms,
    )
    return result


def _ingest_reflector_proposals(
    conn: sqlite3.Connection,
    search_roots: Iterable[Path],
    result: MinerRunResult,
) -> None:
    """Load retrospectives + persist as synthetic detector output.

    The reflector isn't a Detector (it reads filesystem, not SQL), so
    we record its accounting under its own DetectorRunStats entry. A
    failure here is logged and collapsed into ``stats.failed`` so the
    overall run still completes — same contract as a crashing detector.
    """
    # Local import to avoid pulling retrospective_ingest (and its
    # transitive path / yaml dependencies) at package load time for the
    # many callers that never touch reflection.
    from learning_miner.retrospective_ingest import ingest_retrospectives

    stats = DetectorRunStats(detector_name=REFLECTOR_STATS_NAME)
    det_start = time.perf_counter()
    try:
        proposals = ingest_retrospectives(search_roots)
        stats.proposals_emitted = len(proposals)
        for proposal in proposals:
            _persist_proposal(conn, proposal, stats)
    except Exception as exc:
        stats.failed = True
        stats.error = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "learning_reflector_ingest_failed",
            error=stats.error,
        )
    finally:
        stats.duration_ms = int((time.perf_counter() - det_start) * 1000)
        result.per_detector.append(stats)


def _clear_per_run_caches() -> None:
    """Reset detector-owned caches so a long-lived L1 picks up profile edits."""
    # Local import: avoid circular import via detectors.base at module load.
    from learning_miner.detectors.human_issue_cluster import (
        _resolve_platform_profile,
    )

    _resolve_platform_profile.cache_clear()


def _persist_proposal(
    conn: sqlite3.Connection,
    proposal: CandidateProposal,
    stats: DetectorRunStats,
) -> None:
    """Upsert the candidate, then insert each evidence row.

    Per-proposal try/except so one malformed proposal doesn't
    prevent siblings from persisting.
    """
    lesson_id = proposal.lesson_id
    try:
        upsert_lesson_candidate(conn, _proposal_to_upsert(proposal))
        stats.candidates_inserted_or_updated += 1
    except Exception as exc:
        _log_learning_exception(
            "learning_candidate_upsert_failed",
            exc,
            detector_name=proposal.detector_name,
            lesson_id=lesson_id,
        )
        return

    for item in proposal.evidence:
        try:
            redacted_snippet, redaction_count = redact(item.snippet)
            if redaction_count > 0:
                # Observability: flag secret-like payloads caught by
                # the redactor. Detector evidence should be clean;
                # a non-zero count means upstream trace contained
                # credentials and the redactor scrubbed them.
                logger.info(
                    "learning_evidence_redacted",
                    detector_name=proposal.detector_name,
                    lesson_id=lesson_id,
                    trace_id=item.trace_id,
                    redaction_count=redaction_count,
                )
            inserted = insert_lesson_evidence(
                conn,
                lesson_id=lesson_id,
                trace_id=item.trace_id,
                source_ref=item.source_ref,
                observed_at=item.observed_at,
                snippet=redacted_snippet,
                pr_run_id=item.pr_run_id,
            )
            if inserted is None:
                stats.evidence_deduped += 1
            else:
                stats.evidence_inserted += 1
        except Exception as exc:
            _log_learning_exception(
                "learning_evidence_insert_failed",
                exc,
                detector_name=proposal.detector_name,
                lesson_id=lesson_id,
                trace_id=item.trace_id,
            )


def _proposal_to_upsert(proposal: CandidateProposal) -> LessonCandidateUpsert:
    return LessonCandidateUpsert(
        lesson_id=proposal.lesson_id,
        detector_name=proposal.detector_name,
        detector_version=proposal.detector_version,
        pattern_key=proposal.pattern_key,
        client_profile=proposal.client_profile,
        platform_profile=proposal.platform_profile,
        scope_key=proposal.scope_key,
        proposed_delta_json=proposal.proposed_delta_json,
        severity=proposal.severity,
        window_frequency=proposal.window_frequency,
    )


def _log_learning_exception(event: str, exc: Exception, **fields: object) -> None:
    """Uniform structured-log helper for miner-path failures."""
    logger.exception(
        event, error=f"{type(exc).__name__}: {exc}", **fields
    )
