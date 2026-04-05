"""Tests for autonomy_artifact_ingest — sidecar ingest orchestrator."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from autonomy_artifact_ingest import ingest_worktree_sidecars
from autonomy_store import ensure_schema, open_connection

REPO = "acme/app"
SHA = "abc123"
TICKET = "SCRUM-42"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worktree(tmp_path: Path) -> Path:
    logs_dir = tmp_path / "wt" / ".harness" / "logs"
    logs_dir.mkdir(parents=True)
    return tmp_path / "wt"


def _write_sidecar(worktree: Path, name: str, data: dict | str) -> None:
    p = worktree / ".harness" / "logs" / name
    if isinstance(data, str):
        p.write_text(data)
    else:
        p.write_text(json.dumps(data))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Bootstrap schema
    path = tmp_path / "db.db"
    conn = open_connection(path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    return path


def _fetch_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = open_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM pending_ai_issues ORDER BY source, external_id"
        ).fetchall()
    finally:
        conn.close()
    return list(rows)


def _code_review_payload() -> dict:
    return {
        "issues": [
            {
                "id": "cr-1",
                "summary": "Null check missing",
                "severity": "high",
                "category": "bug",
                "file_path": "src/foo.py",
                "line_start": 10,
                "line_end": 12,
                "blocking": True,
            },
            {
                "id": "cr-2",
                "summary": "Style nit",
                "severity": "low",
                "category": "style",
                "file_path": "src/bar.py",
                "blocking": False,
            },
        ]
    }


def _qa_payload() -> dict:
    return {
        "issues": [
            {
                "id": "qa-1",
                "summary": "AC 1 not covered",
                "severity": "medium",
                "category": "coverage",
                "acceptance_criterion_ref": "AC-1",
            },
        ]
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ingests_all_three_sidecars(tmp_path: Path, db_path: Path) -> None:
    wt = _make_worktree(tmp_path)
    _write_sidecar(wt, "code-review.json", _code_review_payload())
    _write_sidecar(
        wt,
        "judge-verdict.json",
        {
            "validated_issues": [{"source_issue_id": "cr-1"}],
            "rejected_issues": [{"source_issue_id": "cr-2"}],
        },
    )
    _write_sidecar(wt, "qa-matrix.json", _qa_payload())

    result = ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )

    assert len(result.sidecars_present) == 3
    assert "code-review.json" in result.sidecars_present
    assert "judge-verdict.json" in result.sidecars_present
    assert "qa-matrix.json" in result.sidecars_present
    assert result.code_review_issues_staged == 2
    assert result.qa_issues_staged == 1
    assert result.judge_validated == 1
    assert result.judge_rejected == 1
    assert result.parse_failures == []

    rows = _fetch_rows(db_path)
    assert len(rows) == 3


def test_judge_rejected_issue_staged_as_invalid(
    tmp_path: Path, db_path: Path
) -> None:
    wt = _make_worktree(tmp_path)
    _write_sidecar(wt, "code-review.json", _code_review_payload())
    _write_sidecar(
        wt,
        "judge-verdict.json",
        {
            "validated_issues": [{"source_issue_id": "cr-1"}],
            "rejected_issues": [{"source_issue_id": "cr-2"}],
        },
    )

    ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )

    rows = _fetch_rows(db_path)
    by_id = {r["external_id"]: r for r in rows}
    assert by_id["cr-2"]["is_valid"] == 0


def test_judge_validated_issue_is_valid(
    tmp_path: Path, db_path: Path
) -> None:
    wt = _make_worktree(tmp_path)
    _write_sidecar(wt, "code-review.json", _code_review_payload())
    _write_sidecar(
        wt,
        "judge-verdict.json",
        {
            "validated_issues": [{"source_issue_id": "cr-1"}],
            "rejected_issues": [],
        },
    )

    ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )

    rows = _fetch_rows(db_path)
    by_id = {r["external_id"]: r for r in rows}
    assert by_id["cr-1"]["is_valid"] == 1


def test_missing_judge_defaults_permissive(
    tmp_path: Path, db_path: Path
) -> None:
    wt = _make_worktree(tmp_path)
    _write_sidecar(wt, "code-review.json", _code_review_payload())

    result = ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )

    assert "judge-verdict.json" not in result.sidecars_present
    assert result.code_review_issues_staged == 2

    rows = _fetch_rows(db_path)
    for r in rows:
        assert r["is_valid"] == 1


def test_missing_all_sidecars_returns_empty(
    tmp_path: Path, db_path: Path
) -> None:
    wt = _make_worktree(tmp_path)

    result = ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )

    assert result.sidecars_present == []
    assert result.code_review_issues_staged == 0
    assert result.qa_issues_staged == 0
    assert result.judge_validated == 0
    assert result.judge_rejected == 0
    assert result.parse_failures == []

    assert _fetch_rows(db_path) == []


def test_malformed_code_review_added_to_failures(
    tmp_path: Path, db_path: Path
) -> None:
    wt = _make_worktree(tmp_path)
    _write_sidecar(wt, "code-review.json", "not json {{{")

    result = ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )

    assert "code-review.json" in result.parse_failures
    assert "code-review.json" not in result.sidecars_present
    assert result.code_review_issues_staged == 0


def test_partial_malformed_continues(
    tmp_path: Path, db_path: Path
) -> None:
    wt = _make_worktree(tmp_path)
    _write_sidecar(wt, "code-review.json", _code_review_payload())
    _write_sidecar(wt, "qa-matrix.json", "not json at all")

    result = ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )

    assert "code-review.json" in result.sidecars_present
    assert "qa-matrix.json" in result.parse_failures
    assert result.code_review_issues_staged == 2
    assert result.qa_issues_staged == 0


def test_reingest_updates_existing_rows(
    tmp_path: Path, db_path: Path
) -> None:
    wt = _make_worktree(tmp_path)
    _write_sidecar(wt, "code-review.json", _code_review_payload())

    ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )
    first_rows = _fetch_rows(db_path)
    assert len(first_rows) == 2

    # Re-emit sidecar with updated content for cr-1
    updated = {
        "issues": [
            {
                "id": "cr-1",
                "summary": "Null check missing — UPDATED",
                "severity": "critical",
                "category": "bug",
                "file_path": "src/foo.py",
                "blocking": True,
            },
            {
                "id": "cr-2",
                "summary": "Style nit",
                "severity": "low",
                "category": "style",
                "file_path": "src/bar.py",
                "blocking": False,
            },
        ]
    }
    _write_sidecar(wt, "code-review.json", updated)

    ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )

    second_rows = _fetch_rows(db_path)
    assert len(second_rows) == 2
    by_id = {r["external_id"]: r for r in second_rows}
    assert "UPDATED" in by_id["cr-1"]["summary"]
    assert by_id["cr-1"]["severity"] == "critical"


def test_qa_issues_marked_as_qa_source(
    tmp_path: Path, db_path: Path
) -> None:
    wt = _make_worktree(tmp_path)
    _write_sidecar(wt, "qa-matrix.json", _qa_payload())

    ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )

    rows = _fetch_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["source"] == "qa"
    assert rows[0]["external_id"] == "qa-1"
    assert rows[0]["is_valid"] == 1


def test_is_code_change_request_flag_from_code_review(
    tmp_path: Path, db_path: Path
) -> None:
    wt = _make_worktree(tmp_path)
    _write_sidecar(wt, "code-review.json", _code_review_payload())

    ingest_worktree_sidecars(
        wt,
        ticket_id=TICKET,
        repo_full_name=REPO,
        head_sha=SHA,
        db_path=db_path,
    )

    rows = _fetch_rows(db_path)
    by_id = {r["external_id"]: r for r in rows}
    # cr-1 blocking=True -> is_code_change_request=1
    assert by_id["cr-1"]["is_code_change_request"] == 1
    # cr-2 blocking=False -> is_code_change_request=0
    assert by_id["cr-2"]["is_code_change_request"] == 0
