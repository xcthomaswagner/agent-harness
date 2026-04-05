"""Sidecar parsers for autonomy metrics dashboard.

Pure functions that parse L2 sidecar JSON files (code-review.json,
judge-verdict.json, qa-matrix.json) into normalized Python objects.

No database, no side effects — just parse + validate. Malformed input
returns None (or drops the offending entry) and logs a warning; nothing
raises.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

_MAX_SUMMARY = 2000
_MAX_DETAILS = 4000
_MAX_FILE_PATH = 512


def _truncate(value: Any, limit: int) -> str:
    """Coerce to str and truncate to limit chars. None -> ''."""
    if value is None:
        return ""
    s = str(value)
    if len(s) > limit:
        return s[:limit]
    return s


def _coerce_int(value: Any) -> int:
    """Coerce to int, defaulting to 0 for None / invalid."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ParsedAiIssue(BaseModel):
    """Normalized AI issue from any of code-review / qa sidecars."""

    external_id: str
    source: str
    severity: str = ""
    category: str = ""
    file_path: str = ""
    line_start: int = 0
    line_end: int = 0
    summary: str
    details: str = ""
    acceptance_criterion_ref: str = ""
    is_code_change_request: int = 0
    is_valid: int = 1

    @field_validator("summary", mode="before")
    @classmethod
    def _truncate_summary(cls, v: Any) -> str:
        return _truncate(v, _MAX_SUMMARY)

    @field_validator("details", mode="before")
    @classmethod
    def _truncate_details(cls, v: Any) -> str:
        return _truncate(v, _MAX_DETAILS)

    @field_validator("file_path", mode="before")
    @classmethod
    def _truncate_file_path(cls, v: Any) -> str:
        return _truncate(v, _MAX_FILE_PATH)

    @field_validator("severity", "category", "acceptance_criterion_ref", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return "" if v is None else str(v)


class JudgeVerdicts(BaseModel):
    """Set of external_ids the judge validated vs rejected."""

    validated: set[str] = Field(default_factory=set)
    rejected: set[str] = Field(default_factory=set)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json_dict(raw: bytes | str, path_label: str) -> dict[str, Any] | None:
    """Parse raw JSON and require top-level dict. Returns None on failure."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "sidecar_parse_failed", path=path_label, reason=f"invalid_json: {exc}"
        )
        return None
    if not isinstance(parsed, dict):
        logger.warning(
            "sidecar_parse_failed",
            path=path_label,
            reason=f"top_level_not_dict: {type(parsed).__name__}",
        )
        return None
    return parsed


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_code_review_sidecar(raw: bytes | str) -> list[ParsedAiIssue] | None:
    """Parse code-review.json.

    Returns None on parse / top-level validation failure. Returns a
    (possibly empty) list of ParsedAiIssue otherwise. Individual malformed
    issues are dropped with a warning.
    """
    data = _load_json_dict(raw, "code-review.json")
    if data is None:
        return None

    issues_raw = data.get("issues", [])
    if not isinstance(issues_raw, list):
        logger.warning(
            "sidecar_parse_failed",
            path="code-review.json",
            reason="issues_not_list",
        )
        return None

    results: list[ParsedAiIssue] = []
    for idx, issue in enumerate(issues_raw):
        if not isinstance(issue, dict):
            logger.warning(
                "sidecar_parse_failed",
                path="code-review.json",
                reason=f"issue[{idx}]_not_dict",
            )
            continue

        ext_id = issue.get("id")
        summary = issue.get("summary")
        if not ext_id or not summary:
            logger.warning(
                "sidecar_parse_failed",
                path="code-review.json",
                reason=f"issue[{idx}]_missing_required_field",
            )
            continue

        # Spec §11.1: both fields are required per issue. Warn (but don't drop
        # the row) when absent so rollout gaps in agent prompts are observable.
        if "blocking" not in issue or "is_code_change_request" not in issue:
            logger.warning(
                "sidecar_missing_required_flag",
                path="code-review.json",
                issue_id=str(ext_id),
                has_blocking="blocking" in issue,
                has_ccr="is_code_change_request" in issue,
            )
        blocking = bool(issue.get("blocking"))
        flag = bool(issue.get("is_code_change_request"))
        is_ccr = 1 if (blocking or flag) else 0

        try:
            parsed_issue = ParsedAiIssue(
                external_id=str(ext_id),
                source="ai_review",
                severity=issue.get("severity", ""),
                category=issue.get("category", ""),
                file_path=issue.get("file_path", ""),
                line_start=_coerce_int(issue.get("line_start")),
                line_end=_coerce_int(issue.get("line_end")),
                summary=summary,
                details=issue.get("details", ""),
                acceptance_criterion_ref=issue.get("acceptance_criterion_ref", ""),
                is_code_change_request=is_ccr,
                is_valid=1,
            )
        except ValidationError as exc:
            logger.warning(
                "sidecar_parse_failed",
                path="code-review.json",
                reason=f"issue[{idx}]_validation: {exc}",
            )
            continue

        results.append(parsed_issue)

    return results


def parse_judge_sidecar(raw: bytes | str) -> JudgeVerdicts | None:
    """Parse judge-verdict.json. Returns None on parse failure."""
    data = _load_json_dict(raw, "judge-verdict.json")
    if data is None:
        return None

    validated: set[str] = set()
    rejected: set[str] = set()

    for key, bucket in (("validated_issues", validated), ("rejected_issues", rejected)):
        entries = data.get(key, [])
        if not isinstance(entries, list):
            logger.warning(
                "sidecar_parse_failed",
                path="judge-verdict.json",
                reason=f"{key}_not_list",
            )
            continue
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                logger.warning(
                    "sidecar_parse_failed",
                    path="judge-verdict.json",
                    reason=f"{key}[{idx}]_not_dict",
                )
                continue
            src_id = entry.get("source_issue_id")
            if not src_id:
                logger.warning(
                    "sidecar_parse_failed",
                    path="judge-verdict.json",
                    reason=f"{key}[{idx}]_missing_source_issue_id",
                )
                continue
            bucket.add(str(src_id))

    return JudgeVerdicts(validated=validated, rejected=rejected)


def parse_qa_sidecar(raw: bytes | str) -> list[ParsedAiIssue] | None:
    """Parse qa-matrix.json. Returns None on parse / top-level failure."""
    data = _load_json_dict(raw, "qa-matrix.json")
    if data is None:
        return None

    issues_raw = data.get("issues", [])
    if not isinstance(issues_raw, list):
        logger.warning(
            "sidecar_parse_failed",
            path="qa-matrix.json",
            reason="issues_not_list",
        )
        return None

    results: list[ParsedAiIssue] = []
    for idx, issue in enumerate(issues_raw):
        if not isinstance(issue, dict):
            logger.warning(
                "sidecar_parse_failed",
                path="qa-matrix.json",
                reason=f"issue[{idx}]_not_dict",
            )
            continue

        ext_id = issue.get("id")
        summary = issue.get("summary")
        if not ext_id or not summary:
            logger.warning(
                "sidecar_parse_failed",
                path="qa-matrix.json",
                reason=f"issue[{idx}]_missing_required_field",
            )
            continue

        try:
            parsed_issue = ParsedAiIssue(
                external_id=str(ext_id),
                source="qa",
                severity=issue.get("severity", ""),
                category=issue.get("category", ""),
                file_path=issue.get("file_path", ""),
                line_start=_coerce_int(issue.get("line_start")),
                line_end=_coerce_int(issue.get("line_end")),
                summary=summary,
                details=issue.get("details", ""),
                acceptance_criterion_ref=issue.get("acceptance_criterion_ref", ""),
                is_code_change_request=0,
                is_valid=1,
            )
        except ValidationError as exc:
            logger.warning(
                "sidecar_parse_failed",
                path="qa-matrix.json",
                reason=f"issue[{idx}]_validation: {exc}",
            )
            continue

        results.append(parsed_issue)

    return results


__all__ = [
    "JudgeVerdicts",
    "ParsedAiIssue",
    "parse_code_review_sidecar",
    "parse_judge_sidecar",
    "parse_qa_sidecar",
]
