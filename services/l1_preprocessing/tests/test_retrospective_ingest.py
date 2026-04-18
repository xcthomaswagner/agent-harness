"""Tests for learning_miner/retrospective_ingest.py.

Covers:

- Happy path: a well-formed retrospective.json with 2 candidates →
  2 CandidateProposals with detector_name=run_reflector.
- status="failed" is skipped entirely (zero proposals).
- Malformed JSON is skipped with a warning log.
- Unresolvable client_profile → platform rejects the candidate.
- Mismatched platform_profile claim (candidate says "sitecore" but
  client_profile maps to "salesforce") is rejected.
- Missing required fields (schema_version, ticket_id, pattern_key)
  are skipped with warning logs.
- Nested search roots are walked recursively; duplicate paths (via
  symlinks or overlapping roots) are visited once.
- Missing search root is handled gracefully.
- evidence_refs are attached with unique source_refs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from learning_miner.detectors.human_issue_cluster import (
    _resolve_platform_profile,
)
from learning_miner.retrospective_ingest import (
    DETECTOR_NAME,
    DETECTOR_VERSION,
    ingest_retrospectives,
)


@pytest.fixture(autouse=True)
def clear_platform_cache():
    _resolve_platform_profile.cache_clear()
    yield
    _resolve_platform_profile.cache_clear()


def _write_retrospective(
    base: Path, ticket_id: str, doc: dict
) -> Path:
    """Place retrospective.json under base/ticket_id/."""
    target = base / ticket_id
    target.mkdir(parents=True, exist_ok=True)
    f = target / "retrospective.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    return f


def _valid_doc(
    *,
    ticket_id: str = "XCSF30-1",
    client_profile: str = "xcsf30",
    platform_profile: str = "salesforce",
    candidates: list[dict] | None = None,
    status: str = "ok",
    schema_version: int = 1,
) -> dict:
    if candidates is None:
        candidates = [
            {
                "pattern_key": "judge_rejected_most_findings",
                "scope_key": (
                    f"{client_profile}|{platform_profile}|"
                    f"judge_rejected_most_findings|{ticket_id}"
                ),
                "severity": "warning",
                "client_profile": client_profile,
                "platform_profile": platform_profile,
                "proposed_delta_json": json.dumps(
                    {"rule": "tighten reviewer rubric"}
                ),
                "evidence_refs": [
                    {
                        "source_ref": "judge-verdict.json",
                        "snippet": "12 of 14 rejected",
                    },
                ],
            },
        ]
    return {
        "schema_version": schema_version,
        "status": status,
        "ticket_id": ticket_id,
        "trace_id": "trace-abc",
        "generated_at": "2026-04-17T16:30:00Z",
        "markdown_summary": "Run summary here.",
        "error": None,
        "lesson_candidates": candidates,
    }


class TestHappyPath:
    def test_two_candidates_produce_two_proposals(self, tmp_path: Path) -> None:
        doc = _valid_doc(
            candidates=[
                {
                    "pattern_key": "judge_rejected_most_findings",
                    "scope_key": "xcsf30|salesforce|judge_rejected_most_findings|XCSF30-1",
                    "severity": "warning",
                    "client_profile": "xcsf30",
                    "platform_profile": "salesforce",
                    "proposed_delta_json": json.dumps(
                        {"rule": "tighten reviewer rubric"}
                    ),
                    "evidence_refs": [
                        {
                            "source_ref": "judge-verdict.json",
                            "snippet": "12/14",
                        }
                    ],
                },
                {
                    "pattern_key": "qa_caught_missed_ac",
                    "scope_key": "xcsf30|salesforce|qa_caught_missed_ac|XCSF30-1",
                    "severity": "info",
                    "client_profile": "xcsf30",
                    "platform_profile": "salesforce",
                    "proposed_delta_json": json.dumps(
                        {"rule": "add AC check"}
                    ),
                    "evidence_refs": [],
                },
            ]
        )
        _write_retrospective(tmp_path, "XCSF30-1", doc)
        out = ingest_retrospectives([tmp_path])
        assert len(out) == 2
        for p in out:
            assert p.detector_name == DETECTOR_NAME
            assert p.detector_version == DETECTOR_VERSION
            assert p.client_profile == "xcsf30"
            assert p.platform_profile == "salesforce"
        assert {p.pattern_key for p in out} == {
            "judge_rejected_most_findings",
            "qa_caught_missed_ac",
        }

    def test_evidence_refs_attached_with_unique_source_refs(
        self, tmp_path: Path
    ) -> None:
        doc = _valid_doc(
            candidates=[
                {
                    "pattern_key": "a",
                    "scope_key": "xcsf30|salesforce|a|XCSF30-1",
                    "severity": "info",
                    "client_profile": "xcsf30",
                    "platform_profile": "salesforce",
                    "proposed_delta_json": "{}",
                    "evidence_refs": [
                        {"source_ref": "judge-verdict.json", "snippet": "x"},
                        {"source_ref": "judge-verdict.json", "snippet": "y"},
                    ],
                },
            ]
        )
        _write_retrospective(tmp_path, "XCSF30-1", doc)
        out = ingest_retrospectives([tmp_path])
        assert len(out) == 1
        evid = out[0].evidence
        assert len(evid) == 2
        # Source refs must be distinct — otherwise the UNIQUE constraint
        # (lesson_id, trace_id, source_ref) rejects the second one.
        assert evid[0].source_ref != evid[1].source_ref

    def test_string_proposed_delta_is_passed_through(
        self, tmp_path: Path
    ) -> None:
        delta_str = json.dumps({"rule": "x"}, sort_keys=True)
        doc = _valid_doc(
            candidates=[
                {
                    "pattern_key": "p",
                    "scope_key": "xcsf30|salesforce|p|XCSF30-1",
                    "severity": "info",
                    "client_profile": "xcsf30",
                    "platform_profile": "salesforce",
                    "proposed_delta_json": delta_str,
                    "evidence_refs": [],
                }
            ]
        )
        _write_retrospective(tmp_path, "XCSF30-1", doc)
        out = ingest_retrospectives([tmp_path])
        assert out[0].proposed_delta_json == delta_str

    def test_object_proposed_delta_is_stringified(
        self, tmp_path: Path
    ) -> None:
        doc = _valid_doc(
            candidates=[
                {
                    "pattern_key": "p",
                    "scope_key": "xcsf30|salesforce|p|XCSF30-1",
                    "severity": "info",
                    "client_profile": "xcsf30",
                    "platform_profile": "salesforce",
                    "proposed_delta_json": {"rule": "x"},
                    "evidence_refs": [],
                }
            ]
        )
        _write_retrospective(tmp_path, "XCSF30-1", doc)
        out = ingest_retrospectives([tmp_path])
        # Should be a string containing the serialized object.
        assert isinstance(out[0].proposed_delta_json, str)
        assert json.loads(out[0].proposed_delta_json) == {"rule": "x"}


class TestStatusFiltering:
    def test_status_failed_is_skipped(self, tmp_path: Path) -> None:
        doc = _valid_doc(status="failed")
        _write_retrospective(tmp_path, "XCSF30-1", doc)
        out = ingest_retrospectives([tmp_path])
        assert out == []

    def test_status_missing_is_skipped(self, tmp_path: Path) -> None:
        doc = _valid_doc()
        del doc["status"]
        _write_retrospective(tmp_path, "XCSF30-1", doc)
        assert ingest_retrospectives([tmp_path]) == []


class TestMalformed:
    def test_malformed_json_is_skipped(self, tmp_path: Path) -> None:
        target = tmp_path / "XCSF30-1"
        target.mkdir()
        (target / "retrospective.json").write_text("{not valid json", encoding="utf-8")
        assert ingest_retrospectives([tmp_path]) == []

    def test_non_object_root_is_skipped(self, tmp_path: Path) -> None:
        target = tmp_path / "XCSF30-1"
        target.mkdir()
        (target / "retrospective.json").write_text(
            json.dumps(["not", "an", "object"]), encoding="utf-8"
        )
        assert ingest_retrospectives([tmp_path]) == []

    def test_unknown_schema_version_is_skipped(self, tmp_path: Path) -> None:
        doc = _valid_doc(schema_version=99)
        _write_retrospective(tmp_path, "XCSF30-1", doc)
        assert ingest_retrospectives([tmp_path]) == []

    def test_missing_ticket_id_is_skipped(self, tmp_path: Path) -> None:
        doc = _valid_doc()
        del doc["ticket_id"]
        _write_retrospective(tmp_path, "XCSF30-1", doc)
        assert ingest_retrospectives([tmp_path]) == []

    def test_candidates_not_list_is_skipped(self, tmp_path: Path) -> None:
        doc = _valid_doc()
        doc["lesson_candidates"] = {"not": "a list"}
        _write_retrospective(tmp_path, "XCSF30-1", doc)
        assert ingest_retrospectives([tmp_path]) == []


class TestCandidateValidation:
    def test_candidate_missing_pattern_key_is_dropped(self, tmp_path: Path) -> None:
        doc = _valid_doc(
            candidates=[
                {
                    "pattern_key": "",
                    "scope_key": "xcsf30|salesforce|x|X-1",
                    "severity": "info",
                    "client_profile": "xcsf30",
                    "platform_profile": "salesforce",
                    "proposed_delta_json": "{}",
                    "evidence_refs": [],
                },
                {
                    "pattern_key": "kept",
                    "scope_key": "xcsf30|salesforce|kept|X-1",
                    "severity": "info",
                    "client_profile": "xcsf30",
                    "platform_profile": "salesforce",
                    "proposed_delta_json": "{}",
                    "evidence_refs": [],
                },
            ]
        )
        _write_retrospective(tmp_path, "X-1", doc)
        out = ingest_retrospectives([tmp_path])
        assert len(out) == 1
        assert out[0].pattern_key == "kept"

    def test_unknown_client_profile_drops_candidate(self, tmp_path: Path) -> None:
        # 'nonexistent-client' doesn't resolve via load_profile, so the
        # candidate is dropped per _resolve_platform_profile rules.
        doc = _valid_doc(client_profile="nonexistent-client")
        doc["lesson_candidates"][0]["client_profile"] = "nonexistent-client"
        _write_retrospective(tmp_path, "X-1", doc)
        assert ingest_retrospectives([tmp_path]) == []

    def test_mismatched_platform_profile_drops_candidate(
        self, tmp_path: Path
    ) -> None:
        # xcsf30 resolves to salesforce; if the candidate claims sitecore
        # we reject it rather than silently accept the contradiction.
        doc = _valid_doc()
        doc["lesson_candidates"][0]["platform_profile"] = "sitecore"
        _write_retrospective(tmp_path, "X-1", doc)
        assert ingest_retrospectives([tmp_path]) == []

    def test_empty_scope_key_is_auto_generated(self, tmp_path: Path) -> None:
        doc = _valid_doc(
            ticket_id="XCSF30-42",
            candidates=[
                {
                    "pattern_key": "p",
                    "scope_key": "",
                    "severity": "info",
                    "client_profile": "xcsf30",
                    "platform_profile": "salesforce",
                    "proposed_delta_json": "{}",
                    "evidence_refs": [],
                }
            ],
        )
        _write_retrospective(tmp_path, "XCSF30-42", doc)
        out = ingest_retrospectives([tmp_path])
        assert len(out) == 1
        assert "XCSF30-42" in out[0].scope_key

    def test_severity_variants_normalized(self, tmp_path: Path) -> None:
        raw_severities = [
            "warning",
            "warn",
            "critical",
            "info",
            "unknown-word",
        ]
        for raw in raw_severities:
            target = tmp_path / f"sev-{raw}"
            target.mkdir(exist_ok=True, parents=True)
            doc = _valid_doc(ticket_id=f"T-{raw}")
            doc["lesson_candidates"][0]["severity"] = raw
            doc["lesson_candidates"][0]["pattern_key"] = f"p-{raw}"
            doc["lesson_candidates"][0]["scope_key"] = (
                f"xcsf30|salesforce|p-{raw}|T-{raw}"
            )
            _write_retrospective(target, f"T-{raw}", doc)
        out = ingest_retrospectives([tmp_path])
        # 5 candidates, one per severity variant
        assert len(out) == 5
        sev_map = {p.pattern_key: p.severity for p in out}
        assert sev_map["p-warning"] == "warn"
        assert sev_map["p-warn"] == "warn"
        assert sev_map["p-critical"] == "critical"
        assert sev_map["p-info"] == "info"
        assert sev_map["p-unknown-word"] == "info"


class TestSearchRoots:
    def test_missing_search_root_is_skipped(self, tmp_path: Path) -> None:
        assert ingest_retrospectives([tmp_path / "does-not-exist"]) == []

    def test_walks_nested_paths(self, tmp_path: Path) -> None:
        nested = tmp_path / "archive" / "2026" / "04" / "XCSF30-1"
        nested.mkdir(parents=True)
        doc = _valid_doc()
        (nested / "retrospective.json").write_text(json.dumps(doc))
        out = ingest_retrospectives([tmp_path])
        assert len(out) == 1

    def test_duplicate_paths_visited_once(self, tmp_path: Path) -> None:
        # Same dir under two roots (overlap). Dedup by canonical path.
        doc = _valid_doc()
        _write_retrospective(tmp_path, "X-1", doc)
        out = ingest_retrospectives([tmp_path, tmp_path])
        assert len(out) == 1  # NOT 2

    def test_multiple_retrospectives_under_one_root(
        self, tmp_path: Path
    ) -> None:
        for ticket in ("XCSF30-1", "XCSF30-2"):
            doc = _valid_doc(ticket_id=ticket)
            doc["lesson_candidates"][0]["pattern_key"] = f"p-{ticket}"
            doc["lesson_candidates"][0]["scope_key"] = (
                f"xcsf30|salesforce|p-{ticket}|{ticket}"
            )
            _write_retrospective(tmp_path, ticket, doc)
        out = ingest_retrospectives([tmp_path])
        assert len(out) == 2
