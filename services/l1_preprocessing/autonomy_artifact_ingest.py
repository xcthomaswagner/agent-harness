"""Sidecar artifact ingest orchestrator for autonomy metrics.

Reads the 3 L2 sidecar JSON files from a worktree (code-review.json,
judge-verdict.json, qa-matrix.json), parses them, applies judge verdicts
to code-review issues, and stages results into the ``pending_ai_issues``
staging table.

Safe: never raises on parse/IO errors. Logs degradation and returns a
partial ``SidecarIngestResult``.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from autonomy_sidecars import (
    parse_code_review_sidecar,
    parse_judge_sidecar,
    parse_qa_sidecar,
)
from autonomy_store import (
    ensure_schema,
    insert_pending_ai_issue,
    open_connection,
    resolve_db_path,
)
from config import settings

logger = structlog.get_logger()


_CODE_REVIEW_FILE = "code-review.json"
_JUDGE_FILE = "judge-verdict.json"
_QA_FILE = "qa-matrix.json"


@dataclass
class SidecarIngestResult:
    sidecars_present: list[str] = field(default_factory=list)
    code_review_issues_staged: int = 0
    qa_issues_staged: int = 0
    judge_validated: int = 0
    judge_rejected: int = 0
    parse_failures: list[str] = field(default_factory=list)


def _safe_read_bytes(path: Path) -> bytes | None:
    """Read a file's bytes. Returns None if missing or unreadable.

    Does not distinguish "missing" from "unreadable" — both yield None;
    the caller handles the distinction by checking ``path.exists()``.
    """
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.warning(
            "sidecar_read_failed", path=str(path), reason=str(exc)
        )
        return None


def ingest_worktree_sidecars(
    worktree_path: str | Path,
    *,
    ticket_id: str,
    repo_full_name: str,
    head_sha: str,
    db_path: Path | None = None,
) -> SidecarIngestResult:
    """Read sidecars from ``<worktree>/.harness/logs/``, parse them, and
    stage AI issues into ``pending_ai_issues`` keyed by
    ``(repo_full_name, head_sha, ticket_id)``.

    Judge verdicts adjust ``is_valid`` on code-review issues:

    - issue id in rejected -> is_valid=0
    - issue id in validated OR judge sidecar missing -> is_valid=1
    - otherwise -> is_valid=1 (permissive default)

    QA issues are always staged with ``is_valid=1``.

    Never raises on parse/IO errors. Logs degradation and returns a
    partial result. ``db_path=None`` resolves via
    ``settings.autonomy_db_path``.
    """
    result = SidecarIngestResult()
    logs_dir = Path(worktree_path) / ".harness" / "logs"

    code_review_path = logs_dir / _CODE_REVIEW_FILE
    judge_path = logs_dir / _JUDGE_FILE
    qa_path = logs_dir / _QA_FILE

    # --- Parse phase (pure functions, no DB) ---
    code_review_issues = None
    if code_review_path.exists():
        raw = _safe_read_bytes(code_review_path)
        if raw is None:
            result.parse_failures.append(_CODE_REVIEW_FILE)
        else:
            parsed = parse_code_review_sidecar(raw)
            if parsed is None:
                result.parse_failures.append(_CODE_REVIEW_FILE)
            else:
                code_review_issues = parsed
                result.sidecars_present.append(_CODE_REVIEW_FILE)

    judge = None
    if judge_path.exists():
        raw = _safe_read_bytes(judge_path)
        if raw is None:
            result.parse_failures.append(_JUDGE_FILE)
        else:
            parsed_judge = parse_judge_sidecar(raw)
            if parsed_judge is None:
                result.parse_failures.append(_JUDGE_FILE)
            else:
                judge = parsed_judge
                result.sidecars_present.append(_JUDGE_FILE)
                result.judge_validated = len(parsed_judge.validated)
                result.judge_rejected = len(parsed_judge.rejected)

    qa_issues = None
    if qa_path.exists():
        raw = _safe_read_bytes(qa_path)
        if raw is None:
            result.parse_failures.append(_QA_FILE)
        else:
            parsed_qa = parse_qa_sidecar(raw)
            if parsed_qa is None:
                result.parse_failures.append(_QA_FILE)
            else:
                qa_issues = parsed_qa
                result.sidecars_present.append(_QA_FILE)

    # --- Stage phase: open short-lived connection ---
    if code_review_issues is None and qa_issues is None:
        return result

    resolved_db = db_path if db_path is not None else resolve_db_path(
        settings.autonomy_db_path
    )

    try:
        conn = open_connection(resolved_db)
    except Exception:
        logger.exception(
            "autonomy_db_open_failed", db_path=str(resolved_db),
            ticket_id=ticket_id,
        )
        return result

    try:
        ensure_schema(conn)

        if code_review_issues is not None:
            for issue in code_review_issues:
                is_valid = 1
                if judge is not None:
                    if issue.external_id in judge.rejected:
                        is_valid = 0
                    elif issue.external_id in judge.validated:
                        is_valid = 1
                    else:
                        is_valid = 1  # permissive default
                try:
                    insert_pending_ai_issue(
                        conn,
                        repo_full_name=repo_full_name,
                        head_sha=head_sha,
                        ticket_id=ticket_id,
                        source="ai_review",
                        external_id=issue.external_id,
                        file_path=issue.file_path,
                        line_start=issue.line_start,
                        line_end=issue.line_end,
                        category=issue.category,
                        severity=issue.severity,
                        summary=issue.summary,
                        details=issue.details,
                        acceptance_criterion_ref=issue.acceptance_criterion_ref,
                        is_valid=is_valid,
                        is_code_change_request=issue.is_code_change_request,
                    )
                    result.code_review_issues_staged += 1
                except Exception:
                    logger.exception(
                        "pending_ai_issue_insert_failed",
                        source="ai_review",
                        external_id=issue.external_id,
                        ticket_id=ticket_id,
                    )

        if qa_issues is not None:
            for issue in qa_issues:
                try:
                    insert_pending_ai_issue(
                        conn,
                        repo_full_name=repo_full_name,
                        head_sha=head_sha,
                        ticket_id=ticket_id,
                        source="qa",
                        external_id=issue.external_id,
                        file_path=issue.file_path,
                        line_start=issue.line_start,
                        line_end=issue.line_end,
                        category=issue.category,
                        severity=issue.severity,
                        summary=issue.summary,
                        details=issue.details,
                        acceptance_criterion_ref=issue.acceptance_criterion_ref,
                        is_valid=1,
                        is_code_change_request=0,
                    )
                    result.qa_issues_staged += 1
                except Exception:
                    logger.exception(
                        "pending_ai_issue_insert_failed",
                        source="qa",
                        external_id=issue.external_id,
                        ticket_id=ticket_id,
                    )
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    return result


__all__ = ["SidecarIngestResult", "ingest_worktree_sidecars"]
