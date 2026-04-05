"""Tests for autonomy sidecar parsers."""

from __future__ import annotations

import json

from autonomy_sidecars import (
    parse_code_review_sidecar,
    parse_judge_sidecar,
    parse_qa_sidecar,
)

# ---------------------------------------------------------------------------
# TestParseCodeReviewSidecar
# ---------------------------------------------------------------------------


class TestParseCodeReviewSidecar:
    def test_parses_valid_sidecar(self) -> None:
        payload = {
            "verdict": "APPROVED",
            "issues": [
                {
                    "id": "cr-1",
                    "severity": "warning",
                    "category": "correctness",
                    "file_path": "src/foo.ts",
                    "line_start": 14,
                    "line_end": 14,
                    "summary": "Null handling is missing",
                    "details": "Explanation",
                    "acceptance_criterion_ref": "AC-2",
                    "blocking": True,
                    "is_code_change_request": True,
                }
            ],
        }
        result = parse_code_review_sidecar(json.dumps(payload).encode())
        assert result is not None
        assert len(result) == 1
        issue = result[0]
        assert issue.external_id == "cr-1"
        assert issue.source == "ai_review"
        assert issue.severity == "warning"
        assert issue.category == "correctness"
        assert issue.file_path == "src/foo.ts"
        assert issue.line_start == 14
        assert issue.line_end == 14
        assert issue.summary == "Null handling is missing"
        assert issue.details == "Explanation"
        assert issue.acceptance_criterion_ref == "AC-2"
        assert issue.is_code_change_request == 1
        assert issue.is_valid == 1

    def test_blocking_false_but_flag_true_still_code_change(self) -> None:
        payload = {
            "issues": [
                {
                    "id": "cr-9",
                    "summary": "x",
                    "blocking": False,
                    "is_code_change_request": True,
                }
            ]
        }
        result = parse_code_review_sidecar(json.dumps(payload))
        assert result is not None
        assert len(result) == 1
        assert result[0].is_code_change_request == 1

    def test_blocking_true_flag_absent_is_code_change(self) -> None:
        payload = {"issues": [{"id": "cr-1", "summary": "x", "blocking": True}]}
        result = parse_code_review_sidecar(json.dumps(payload))
        assert result is not None
        assert result[0].is_code_change_request == 1

    def test_neither_blocking_nor_flag_is_zero(self) -> None:
        payload = {"issues": [{"id": "cr-1", "summary": "x"}]}
        result = parse_code_review_sidecar(json.dumps(payload))
        assert result is not None
        assert result[0].is_code_change_request == 0

    def test_empty_issues_list_returns_empty(self) -> None:
        result = parse_code_review_sidecar(b'{"verdict": "APPROVED", "issues": []}')
        assert result == []

    def test_missing_issues_key_returns_empty_list(self) -> None:
        result = parse_code_review_sidecar(b'{"verdict": "APPROVED"}')
        assert result == []

    def test_malformed_json_returns_none(self) -> None:
        assert parse_code_review_sidecar(b"not json") is None

    def test_non_dict_top_level_returns_none(self) -> None:
        assert parse_code_review_sidecar(b"[]") is None
        assert parse_code_review_sidecar(b'"string"') is None
        assert parse_code_review_sidecar(b"42") is None

    def test_issue_missing_required_field_dropped(self) -> None:
        payload = {
            "issues": [
                {"id": "cr-1"},  # missing summary
                {"id": "cr-2", "summary": "valid"},
                {"summary": "no id"},  # missing id
            ]
        }
        result = parse_code_review_sidecar(json.dumps(payload))
        assert result is not None
        assert len(result) == 1
        assert result[0].external_id == "cr-2"

    def test_summary_truncated_to_2000(self) -> None:
        long_summary = "a" * 3000
        payload = {"issues": [{"id": "cr-1", "summary": long_summary}]}
        result = parse_code_review_sidecar(json.dumps(payload))
        assert result is not None
        assert len(result) == 1
        assert len(result[0].summary) == 2000

    def test_details_truncated_to_4000(self) -> None:
        long_details = "b" * 5000
        payload = {
            "issues": [{"id": "cr-1", "summary": "x", "details": long_details}]
        }
        result = parse_code_review_sidecar(json.dumps(payload))
        assert result is not None
        assert len(result[0].details) == 4000

    def test_file_path_truncated_to_512(self) -> None:
        long_path = "p" * 1000
        payload = {
            "issues": [{"id": "cr-1", "summary": "x", "file_path": long_path}]
        }
        result = parse_code_review_sidecar(json.dumps(payload))
        assert result is not None
        assert len(result[0].file_path) == 512

    def test_str_input_accepted(self) -> None:
        payload_str = json.dumps({"issues": [{"id": "cr-1", "summary": "x"}]})
        result = parse_code_review_sidecar(payload_str)
        assert result is not None
        assert len(result) == 1

    def test_null_line_numbers_default_to_zero(self) -> None:
        payload = {
            "issues": [
                {
                    "id": "cr-1",
                    "summary": "x",
                    "line_start": None,
                    "line_end": None,
                }
            ]
        }
        result = parse_code_review_sidecar(json.dumps(payload))
        assert result is not None
        assert result[0].line_start == 0
        assert result[0].line_end == 0


# ---------------------------------------------------------------------------
# TestParseJudgeSidecar
# ---------------------------------------------------------------------------


class TestParseJudgeSidecar:
    def test_parses_valid_judge_verdict(self) -> None:
        payload = {
            "validated_issues": [
                {"source_issue_id": "cr-1", "score": 92, "summary": "Null handling"}
            ],
            "rejected_issues": [
                {"source_issue_id": "cr-2", "score": 25, "summary": "Style-only"}
            ],
        }
        result = parse_judge_sidecar(json.dumps(payload).encode())
        assert result is not None
        assert result.validated == {"cr-1"}
        assert result.rejected == {"cr-2"}

    def test_missing_validated_key_returns_empty_set(self) -> None:
        payload = {"rejected_issues": [{"source_issue_id": "cr-2"}]}
        result = parse_judge_sidecar(json.dumps(payload))
        assert result is not None
        assert result.validated == set()
        assert result.rejected == {"cr-2"}

    def test_missing_both_keys_returns_empty_sets(self) -> None:
        result = parse_judge_sidecar(b"{}")
        assert result is not None
        assert result.validated == set()
        assert result.rejected == set()

    def test_missing_source_issue_id_dropped(self) -> None:
        payload = {
            "validated_issues": [
                {"score": 50, "summary": "no id"},
                {"source_issue_id": "cr-3"},
            ]
        }
        result = parse_judge_sidecar(json.dumps(payload))
        assert result is not None
        assert result.validated == {"cr-3"}

    def test_malformed_json_returns_none(self) -> None:
        assert parse_judge_sidecar(b"nope") is None

    def test_non_dict_top_level_returns_none(self) -> None:
        assert parse_judge_sidecar(b"[]") is None

    def test_str_input_accepted(self) -> None:
        result = parse_judge_sidecar('{"validated_issues": []}')
        assert result is not None


# ---------------------------------------------------------------------------
# TestParseQaSidecar
# ---------------------------------------------------------------------------


class TestParseQaSidecar:
    def test_parses_valid_qa_matrix(self) -> None:
        payload = {
            "acceptance_criteria": [
                {"id": "AC-1", "status": "PASS", "evidence": "unit test xyz"}
            ],
            "issues": [
                {
                    "id": "qa-1",
                    "severity": "critical",
                    "category": "behavior",
                    "file_path": "",
                    "line_start": 0,
                    "line_end": 0,
                    "summary": "Checkout button does not submit",
                    "details": "Observed in E2E",
                }
            ],
        }
        result = parse_qa_sidecar(json.dumps(payload).encode())
        assert result is not None
        assert len(result) == 1
        issue = result[0]
        assert issue.external_id == "qa-1"
        assert issue.source == "qa"
        assert issue.severity == "critical"
        assert issue.category == "behavior"
        assert issue.summary == "Checkout button does not submit"
        assert issue.details == "Observed in E2E"
        assert issue.is_code_change_request == 0

    def test_empty_file_path_allowed(self) -> None:
        payload = {
            "issues": [
                {
                    "id": "qa-1",
                    "summary": "x",
                    "file_path": "",
                    "line_start": 0,
                    "line_end": 0,
                }
            ]
        }
        result = parse_qa_sidecar(json.dumps(payload))
        assert result is not None
        assert len(result) == 1
        assert result[0].file_path == ""
        assert result[0].line_start == 0
        assert result[0].line_end == 0

    def test_malformed_json_returns_none(self) -> None:
        assert parse_qa_sidecar(b"{bad") is None

    def test_qa_issue_source_is_qa(self) -> None:
        payload = {"issues": [{"id": "qa-1", "summary": "x"}]}
        result = parse_qa_sidecar(json.dumps(payload))
        assert result is not None
        assert len(result) == 1
        assert result[0].source == "qa"

    def test_qa_issue_never_code_change_request(self) -> None:
        payload = {
            "issues": [
                {
                    "id": "qa-1",
                    "summary": "x",
                    "blocking": True,
                    "is_code_change_request": True,
                }
            ]
        }
        result = parse_qa_sidecar(json.dumps(payload))
        assert result is not None
        assert result[0].is_code_change_request == 0

    def test_empty_issues_list_returns_empty(self) -> None:
        result = parse_qa_sidecar(b'{"issues": []}')
        assert result == []

    def test_missing_issues_key_returns_empty_list(self) -> None:
        result = parse_qa_sidecar(b'{"acceptance_criteria": []}')
        assert result == []
