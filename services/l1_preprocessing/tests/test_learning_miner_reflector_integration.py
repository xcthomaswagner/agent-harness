"""End-to-end test: run_miner(retrospective_search_roots=...) persists rows.

Seeds a minimal valid retrospective.json under a fake archive root,
invokes ``run_miner`` with no detectors (just the reflector path),
and asserts that the ingested proposals land in ``lesson_candidates``
under ``detector_name='run_reflector'`` with matching pattern/scope
and at least one evidence row.

Also:

- Passing ``retrospective_search_roots=None`` (the default) does not
  introduce a new per-detector stats entry — backfill callers that
  don't care about reflection see unchanged behavior.
- Malformed retrospectives are skipped without failing the run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autonomy_store import list_lesson_candidates, list_lesson_evidence
from learning_miner import ingest_retrospectives, run_miner
from learning_miner.detectors.human_issue_cluster import (
    _resolve_platform_profile,
)
from learning_miner.runner import REFLECTOR_STATS_NAME


@pytest.fixture(autouse=True)
def clear_platform_cache():
    _resolve_platform_profile.cache_clear()
    yield
    _resolve_platform_profile.cache_clear()


@pytest.fixture
def conn(learning_conn):
    return learning_conn


def _write_valid_retrospective(
    root: Path,
    ticket_id: str,
    *,
    pattern_key: str = "judge_rejected_most_findings",
    severity: str = "warning",
    candidate_count: int = 1,
) -> None:
    target = root / ticket_id
    target.mkdir(parents=True, exist_ok=True)
    candidates = []
    for i in range(candidate_count):
        candidates.append({
            "pattern_key": f"{pattern_key}-{i}" if i else pattern_key,
            "scope_key": (
                f"xcsf30|salesforce|"
                f"{pattern_key}{'-' + str(i) if i else ''}|{ticket_id}"
            ),
            "severity": severity,
            "client_profile": "xcsf30",
            "platform_profile": "salesforce",
            "proposed_delta_json": json.dumps(
                {"rule": "tighten reviewer rubric"}
            ),
            "evidence_refs": [
                {
                    "source_ref": "judge-verdict.json",
                    "snippet": "12 of 14 rejected",
                }
            ],
        })
    doc = {
        "schema_version": 1,
        "status": "ok",
        "ticket_id": ticket_id,
        "trace_id": f"trace-{ticket_id}",
        "generated_at": "2026-04-17T16:30:00Z",
        "markdown_summary": "Run summary.",
        "error": None,
        "lesson_candidates": candidates,
    }
    (target / "retrospective.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )


class TestRunMinerReflectorPath:
    def test_ingest_lands_candidates_and_evidence(
        self, conn, tmp_path: Path
    ) -> None:
        _write_valid_retrospective(tmp_path, "XCSF30-1")
        result = run_miner(
            conn,
            detectors=[],
            window_days=14,
            retrospective_search_roots=[tmp_path],
        )
        # One synthetic "detector" for the reflector path.
        names = [s.detector_name for s in result.per_detector]
        assert REFLECTOR_STATS_NAME in names

        # Lesson landed in the DB with the right detector_name.
        rows = list_lesson_candidates(conn)
        assert len(rows) == 1
        assert rows[0]["detector_name"] == "run_reflector"
        assert rows[0]["pattern_key"] == "judge_rejected_most_findings"
        assert rows[0]["client_profile"] == "xcsf30"
        assert rows[0]["platform_profile"] == "salesforce"

        # Evidence attached.
        lesson_id = rows[0]["lesson_id"]
        evidence = list_lesson_evidence(conn, lesson_id)
        assert len(evidence) == 1
        # snippet redacted through the runner's redaction path.
        assert "12 of 14 rejected" in evidence[0]["snippet"]

    def test_multi_candidate_retrospective(
        self, conn, tmp_path: Path
    ) -> None:
        _write_valid_retrospective(
            tmp_path, "XCSF30-1", candidate_count=3
        )
        run_miner(
            conn,
            detectors=[],
            window_days=14,
            retrospective_search_roots=[tmp_path],
        )
        rows = list_lesson_candidates(conn)
        assert len(rows) == 3
        for r in rows:
            assert r["detector_name"] == "run_reflector"

    def test_failed_retrospective_not_ingested(
        self, conn, tmp_path: Path
    ) -> None:
        target = tmp_path / "XCSF30-1"
        target.mkdir(parents=True)
        (target / "retrospective.json").write_text(
            json.dumps({
                "schema_version": 1,
                "status": "failed",
                "ticket_id": "XCSF30-1",
                "trace_id": "t",
                "generated_at": "2026-04-17T16:30:00Z",
                "markdown_summary": "",
                "error": "broken",
                "lesson_candidates": [],
            })
        )
        run_miner(
            conn,
            detectors=[],
            window_days=14,
            retrospective_search_roots=[tmp_path],
        )
        assert list_lesson_candidates(conn) == []

    def test_malformed_retrospective_does_not_fail_run(
        self, conn, tmp_path: Path
    ) -> None:
        target = tmp_path / "XCSF30-1"
        target.mkdir(parents=True)
        (target / "retrospective.json").write_text("{not json")
        result = run_miner(
            conn,
            detectors=[],
            window_days=14,
            retrospective_search_roots=[tmp_path],
        )
        # Reflector stats entry exists; run didn't error.
        names = [s.detector_name for s in result.per_detector]
        assert REFLECTOR_STATS_NAME in names
        reflector_stats = next(
            s for s in result.per_detector
            if s.detector_name == REFLECTOR_STATS_NAME
        )
        # Reflector ingest swallows the per-file malformed JSON and
        # logs — the overall stats entry is NOT ``failed``.
        assert not reflector_stats.failed
        assert reflector_stats.proposals_emitted == 0

    def test_none_search_roots_does_not_add_reflector_stats(
        self, conn, tmp_path: Path
    ) -> None:
        """Backfill callers that don't care about reflection see zero
        overhead — no reflector stats entry, no work done."""
        result = run_miner(
            conn,
            detectors=[],
            window_days=14,
        )
        names = [s.detector_name for s in result.per_detector]
        assert REFLECTOR_STATS_NAME not in names

    def test_reruns_are_idempotent(
        self, conn, tmp_path: Path
    ) -> None:
        _write_valid_retrospective(tmp_path, "XCSF30-1")
        run_miner(
            conn, detectors=[], window_days=14,
            retrospective_search_roots=[tmp_path],
        )
        run_miner(
            conn, detectors=[], window_days=14,
            retrospective_search_roots=[tmp_path],
        )
        # Second run upserts into the same row — no duplication.
        rows = list_lesson_candidates(conn)
        assert len(rows) == 1


class TestIngestRetrospectivesExported:
    """The package re-export must be usable."""

    def test_import_from_package(self) -> None:
        # Trivially proves the symbol is reachable at the documented path.
        assert ingest_retrospectives is not None
