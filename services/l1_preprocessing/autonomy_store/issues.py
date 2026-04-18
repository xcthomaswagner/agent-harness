"""review_issues / pending_ai_issues / issue_matches / pr_commits helpers.

Extracted from ``autonomy_store.py``. All ticket/PR-level review issue
manipulation lives here: staging, draining, insert, list, human-side
helpers, pr_commit tracking, issue_match basics.
"""

from __future__ import annotations

import sqlite3

from .schema import _now_iso


def insert_pending_ai_issue(
    conn: sqlite3.Connection,
    *,
    repo_full_name: str,
    head_sha: str,
    ticket_id: str,
    source: str,
    external_id: str,
    file_path: str,
    line_start: int,
    line_end: int,
    category: str,
    severity: str,
    summary: str,
    details: str,
    acceptance_criterion_ref: str,
    is_valid: int,
    is_code_change_request: int,
) -> int:
    """Insert a pending_ai_issues row. On unique-key conflict, updates the
    mutable fields (re-emitted sidecars may refresh content). Returns the
    row id.
    """
    now = _now_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO pending_ai_issues (
                repo_full_name, head_sha, ticket_id, source, external_id,
                file_path, line_start, line_end, category, severity,
                summary, details, acceptance_criterion_ref, is_valid,
                is_code_change_request, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (repo_full_name, head_sha, ticket_id, source, external_id)
            DO UPDATE SET
                file_path = excluded.file_path,
                line_start = excluded.line_start,
                line_end = excluded.line_end,
                category = excluded.category,
                severity = excluded.severity,
                summary = excluded.summary,
                details = excluded.details,
                acceptance_criterion_ref = excluded.acceptance_criterion_ref,
                is_valid = excluded.is_valid,
                is_code_change_request = excluded.is_code_change_request
            """,
            (
                repo_full_name,
                head_sha,
                ticket_id,
                source,
                external_id,
                file_path,
                line_start,
                line_end,
                category,
                severity,
                summary,
                details,
                acceptance_criterion_ref,
                int(is_valid),
                int(is_code_change_request),
                now,
            ),
        )
    # lastrowid is unreliable on ON CONFLICT DO UPDATE; look up explicitly
    row = conn.execute(
        "SELECT id FROM pending_ai_issues WHERE repo_full_name = ? "
        "AND head_sha = ? AND ticket_id = ? AND source = ? AND external_id = ?",
        (repo_full_name, head_sha, ticket_id, source, external_id),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    return int(cur.lastrowid or 0)


def drain_pending_ai_issues(
    conn: sqlite3.Connection,
    *,
    repo_full_name: str,
    head_sha: str,
    ticket_id: str,
    pr_run_id: int,
) -> int:
    """Move pending_ai_issues rows for (repo, head_sha, ticket_id) into
    review_issues under the given pr_run_id. Deletes the pending rows.
    Idempotent: if a review_issues row with the same (pr_run_id, source,
    external_id) already exists, skip the insert but still delete the
    pending row. Returns the count moved (newly inserted into
    review_issues).
    """
    pending = conn.execute(
        "SELECT * FROM pending_ai_issues WHERE repo_full_name = ? "
        "AND head_sha = ? AND ticket_id = ?",
        (repo_full_name, head_sha, ticket_id),
    ).fetchall()

    moved = 0
    with conn:
        for p in pending:
            existing = conn.execute(
                "SELECT id FROM review_issues WHERE pr_run_id = ? "
                "AND source = ? AND external_id = ?",
                (pr_run_id, p["source"], p["external_id"]),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO review_issues (
                        pr_run_id, source, external_id, created_at,
                        file_path, line_start, line_end, category, severity,
                        summary, details, acceptance_criterion_ref, status,
                        source_ref, is_valid, is_code_change_request
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        pr_run_id,
                        p["source"],
                        p["external_id"],
                        p["created_at"],
                        p["file_path"],
                        p["line_start"],
                        p["line_end"],
                        p["category"],
                        p["severity"],
                        p["summary"],
                        p["details"],
                        p["acceptance_criterion_ref"],
                        "open",
                        "",
                        int(p["is_valid"]),
                        int(p["is_code_change_request"]),
                    ),
                )
                moved += 1
            conn.execute(
                "DELETE FROM pending_ai_issues WHERE id = ?", (p["id"],)
            )
    return moved


def insert_review_issue(
    conn: sqlite3.Connection,
    *,
    pr_run_id: int,
    source: str,
    external_id: str = "",
    file_path: str = "",
    line_start: int = 0,
    line_end: int = 0,
    category: str = "",
    severity: str = "",
    summary: str,
    details: str = "",
    acceptance_criterion_ref: str = "",
    status: str = "open",
    source_ref: str = "",
    is_valid: int = 1,
    is_code_change_request: int = 0,
) -> int:
    """Insert a review_issues row. Returns the id."""
    now = _now_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO review_issues (
                pr_run_id, source, external_id, created_at,
                file_path, line_start, line_end, category, severity,
                summary, details, acceptance_criterion_ref, status,
                source_ref, is_valid, is_code_change_request
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pr_run_id,
                source,
                external_id,
                now,
                file_path,
                line_start,
                line_end,
                category,
                severity,
                summary,
                details,
                acceptance_criterion_ref,
                status,
                source_ref,
                int(is_valid),
                int(is_code_change_request),
            ),
        )
    return int(cur.lastrowid or 0)


def list_review_issues_by_pr_run(
    conn: sqlite3.Connection,
    pr_run_id: int,
    source: str | None = None,
) -> list[sqlite3.Row]:
    """Return review_issues rows for a pr_run, optionally filtered by source."""
    if source is None:
        rows = conn.execute(
            "SELECT * FROM review_issues WHERE pr_run_id = ? ORDER BY id",
            (pr_run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM review_issues WHERE pr_run_id = ? AND source = ? "
            "ORDER BY id",
            (pr_run_id, source),
        ).fetchall()
    return list(rows)


def insert_pr_commit(
    conn: sqlite3.Connection,
    *,
    pr_run_id: int,
    sha: str,
    committed_at: str,
) -> int:
    """Idempotent insert into pr_commits keyed on (pr_run_id, sha).

    Returns the row id (existing or new).
    """
    with conn:
        conn.execute(
            """
            INSERT INTO pr_commits (pr_run_id, sha, committed_at)
            VALUES (?, ?, ?)
            ON CONFLICT (pr_run_id, sha) DO NOTHING
            """,
            (pr_run_id, sha, committed_at),
        )
    row = conn.execute(
        "SELECT id FROM pr_commits WHERE pr_run_id = ? AND sha = ?",
        (pr_run_id, sha),
    ).fetchone()
    return int(row["id"]) if row is not None else 0


def list_pr_commits(
    conn: sqlite3.Connection, pr_run_id: int
) -> list[sqlite3.Row]:
    """Return pr_commits rows for a pr_run, ordered by committed_at ASC, id ASC."""
    rows = conn.execute(
        "SELECT * FROM pr_commits WHERE pr_run_id = ? "
        "ORDER BY committed_at ASC, id ASC",
        (pr_run_id,),
    ).fetchall()
    return list(rows)


def list_human_issues_for_pr_run(
    conn: sqlite3.Connection, pr_run_id: int
) -> list[sqlite3.Row]:
    """Return human_review review_issues rows for a pr_run, ordered by created_at ASC, id ASC."""
    rows = conn.execute(
        "SELECT * FROM review_issues WHERE pr_run_id = ? AND source = 'human_review' "
        "ORDER BY created_at ASC, id ASC",
        (pr_run_id,),
    ).fetchall()
    return list(rows)


def set_human_issue_code_change_flag(
    conn: sqlite3.Connection, issue_id: int, flag: int
) -> None:
    """Update is_code_change_request on a single review_issues row."""
    with conn:
        conn.execute(
            "UPDATE review_issues SET is_code_change_request = ? WHERE id = ?",
            (int(flag), issue_id),
        )


def insert_issue_match(
    conn: sqlite3.Connection,
    *,
    human_issue_id: int,
    ai_issue_id: int,
    match_type: str,
    confidence: float,
    matched_by: str = "system",
) -> int:
    """Insert an issue_matches row. Uses ON CONFLICT DO NOTHING keyed on
    (human_issue_id, ai_issue_id). Returns the new row id, or 0 if a row
    with the same pair already existed.
    """
    # issue_matches has no UNIQUE constraint at the schema level — emulate it
    # by checking first. Cheap enough given bounded issue counts.
    existing = conn.execute(
        "SELECT id FROM issue_matches WHERE human_issue_id = ? AND ai_issue_id = ?",
        (human_issue_id, ai_issue_id),
    ).fetchone()
    if existing is not None:
        return 0
    now = _now_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO issue_matches (
                human_issue_id, ai_issue_id, match_type, confidence,
                matched_at, matched_by
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                human_issue_id,
                ai_issue_id,
                match_type,
                float(confidence),
                now,
                matched_by,
            ),
        )
    return int(cur.lastrowid or 0)


def list_issue_matches_for_human(
    conn: sqlite3.Connection, human_issue_id: int
) -> list[sqlite3.Row]:
    """Return issue_matches rows for a given human issue."""
    rows = conn.execute(
        "SELECT * FROM issue_matches WHERE human_issue_id = ? ORDER BY id",
        (human_issue_id,),
    ).fetchall()
    return list(rows)
