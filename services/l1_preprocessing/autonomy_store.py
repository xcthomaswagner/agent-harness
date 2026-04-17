"""SQLite-backed store for autonomy metrics.

Owns the autonomy.db schema, migrations, and repository helpers used by the
L1 autonomy ingest and dashboard endpoints. Uses stdlib sqlite3 with a
connection-per-request pattern and hand-rolled, versioned migrations.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from config import settings

logger = structlog.get_logger()

# AI-origin sources (defined locally to avoid import cycle with autonomy_matching).
AI_SOURCES = ("ai_review", "judge", "qa")


# ---------------------------------------------------------------------------
# Connection + schema management
# ---------------------------------------------------------------------------

def resolve_db_path(settings_path: str) -> Path:
    """Resolve DB path.

    If settings_path is empty, default to <repo_root>/data/autonomy.db where
    repo_root is two levels up from this file (services/l1_preprocessing/ →
    repo root).
    """
    if settings_path:
        return Path(settings_path)
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "data" / "autonomy.db"


def open_connection(db_path: Path) -> sqlite3.Connection:
    """Open a sqlite3 connection with required pragmas.

    Creates parent directory if missing. Sets WAL journaling, NORMAL
    synchronous, foreign_keys ON, and a 5s busy timeout. Returns rows as
    sqlite3.Row.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _current_schema_version(conn: sqlite3.Connection) -> int:
    """Return highest applied migration version, or 0 if none applied."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    if row is None or row["v"] is None:
        return 0
    return int(row["v"])


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Ensure the DB schema is at the latest version.

    Runs any missing migrations in transactions. Returns current version.
    """
    version = _current_schema_version(conn)
    if version < 1:
        with conn:
            _migrate_to_v1(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (1, _now_iso()),
            )
        version = 1
        logger.info("autonomy_schema_migrated", version=version)
    if version < 2:
        with conn:
            _migrate_to_v2(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (2, _now_iso()),
            )
        version = 2
        logger.info("autonomy_schema_migrated", version=version)
    if version < 3:
        with conn:
            _migrate_to_v3(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (3, _now_iso()),
            )
        version = 3
        logger.info("autonomy_schema_migrated", version=version)
    if version < 4:
        with conn:
            _migrate_to_v4(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (4, _now_iso()),
            )
        version = 4
        logger.info("autonomy_schema_migrated", version=version)
    if version < 5:
        with conn:
            _migrate_to_v5(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (5, _now_iso()),
            )
        version = 5
        logger.info("autonomy_schema_migrated", version=version)
    return version


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """Create all v1 tables + indexes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE pr_runs (
            id INTEGER PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            repo_full_name TEXT NOT NULL,
            pr_url TEXT NOT NULL DEFAULT '',
            ticket_type TEXT NOT NULL DEFAULT '',
            pipeline_mode TEXT NOT NULL DEFAULT '',
            head_sha TEXT NOT NULL,
            base_sha TEXT NOT NULL DEFAULT '',
            client_profile TEXT NOT NULL DEFAULT '',
            opened_at TEXT NOT NULL DEFAULT '',
            approved_at TEXT NOT NULL DEFAULT '',
            merged_at TEXT NOT NULL DEFAULT '',
            closed_at TEXT NOT NULL DEFAULT '',
            first_pass_accepted INTEGER NOT NULL DEFAULT 0,
            merged INTEGER NOT NULL DEFAULT 0,
            escalated INTEGER NOT NULL DEFAULT 0,
            backfilled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (repo_full_name, pr_number, head_sha)
        )
        """
    )
    conn.execute("CREATE INDEX idx_pr_runs_ticket_id ON pr_runs (ticket_id)")
    conn.execute("CREATE INDEX idx_pr_runs_merged_at ON pr_runs (merged_at)")
    conn.execute("CREATE INDEX idx_pr_runs_opened_at ON pr_runs (opened_at)")
    conn.execute(
        "CREATE INDEX idx_pr_runs_client_profile_opened_at "
        "ON pr_runs (client_profile, opened_at)"
    )

    conn.execute(
        """
        CREATE TABLE review_issues (
            id INTEGER PRIMARY KEY,
            pr_run_id INTEGER NOT NULL REFERENCES pr_runs(id),
            source TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            file_path TEXT NOT NULL DEFAULT '',
            line_start INTEGER NOT NULL DEFAULT 0,
            line_end INTEGER NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            acceptance_criterion_ref TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            source_ref TEXT NOT NULL DEFAULT '',
            is_valid INTEGER NOT NULL DEFAULT 1,
            is_code_change_request INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_review_issues_pr_run_source "
        "ON review_issues (pr_run_id, source)"
    )
    conn.execute(
        "CREATE INDEX idx_review_issues_file_path ON review_issues (file_path)"
    )

    conn.execute(
        """
        CREATE TABLE issue_matches (
            id INTEGER PRIMARY KEY,
            human_issue_id INTEGER NOT NULL REFERENCES review_issues(id),
            ai_issue_id INTEGER NOT NULL REFERENCES review_issues(id),
            match_type TEXT NOT NULL,
            confidence REAL NOT NULL,
            matched_at TEXT NOT NULL,
            matched_by TEXT NOT NULL DEFAULT 'system'
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_issue_matches_human ON issue_matches (human_issue_id)"
    )
    conn.execute(
        "CREATE INDEX idx_issue_matches_ai ON issue_matches (ai_issue_id)"
    )

    conn.execute(
        """
        CREATE TABLE defect_links (
            id INTEGER PRIMARY KEY,
            pr_run_id INTEGER NOT NULL REFERENCES pr_runs(id),
            defect_key TEXT NOT NULL,
            source TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT '',
            reported_at TEXT NOT NULL,
            confirmed INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_defect_links_pr_run ON defect_links (pr_run_id)"
    )
    conn.execute(
        "CREATE INDEX idx_defect_links_reported_at ON defect_links (reported_at)"
    )

    conn.execute(
        """
        CREATE TABLE manual_overrides (
            id INTEGER PRIMARY KEY,
            override_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL
        )
        """
    )


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Create v2 additions: pending_ai_issues staging table + new indexes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_ai_issues (
            id INTEGER PRIMARY KEY,
            repo_full_name TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL DEFAULT '',
            line_start INTEGER NOT NULL DEFAULT 0,
            line_end INTEGER NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            acceptance_criterion_ref TEXT NOT NULL DEFAULT '',
            is_valid INTEGER NOT NULL DEFAULT 1,
            is_code_change_request INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE (repo_full_name, head_sha, ticket_id, source, external_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_ai_issues_lookup "
        "ON pending_ai_issues (repo_full_name, head_sha, ticket_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pr_runs_backfilled "
        "ON pr_runs (backfilled)"
    )


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v3: categorize defect_links + unique key on (pr_run_id, defect_key, source)."""
    # Add category column. Values: 'escaped'|'feature_request'|'pre_existing'|'infra'.
    # Default to 'escaped' so existing rows remain counted as escapes.
    conn.execute(
        "ALTER TABLE defect_links ADD COLUMN category TEXT NOT NULL "
        "DEFAULT 'escaped'"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_defect_links_uniq "
        "ON defect_links (pr_run_id, defect_key, source)"
    )


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """v4: record PR commit history for follow-up-commit acceptance signal."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pr_commits (
            id INTEGER PRIMARY KEY,
            pr_run_id INTEGER NOT NULL REFERENCES pr_runs(id),
            sha TEXT NOT NULL,
            committed_at TEXT NOT NULL,
            UNIQUE (pr_run_id, sha)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pr_commits_pr_run_committed_at "
        "ON pr_commits (pr_run_id, committed_at)"
    )


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    """v5: self-learning lesson tables.

    Adds three tables for the self-learning miner: ``lesson_candidates``
    (one row per detected pattern, upserted on detector+pattern+scope),
    ``lesson_evidence`` (pointers to the traces/pr_runs supporting each
    candidate), and ``lesson_outcomes`` (pre/post metrics + human-reedit
    signals written by the outcomes job after merge).

    The hot-read index ``(client_profile, status, detected_at DESC)``
    supports the dashboard's main query. ``status`` enumerates
    ``proposed|draft_ready|approved|rejected|applied|reverted|stale|snoozed``.
    See ``docs/self-learning-plan.md`` §3 for the full design.
    """
    conn.execute(
        """
        CREATE TABLE lesson_candidates (
            id INTEGER PRIMARY KEY,
            lesson_id TEXT NOT NULL UNIQUE,
            detector_name TEXT NOT NULL,
            detector_version INTEGER NOT NULL DEFAULT 1,
            pattern_key TEXT NOT NULL,
            client_profile TEXT NOT NULL DEFAULT '',
            platform_profile TEXT NOT NULL DEFAULT '',
            scope_key TEXT NOT NULL DEFAULT '',
            frequency INTEGER NOT NULL DEFAULT 1,
            severity TEXT NOT NULL DEFAULT 'info',
            detected_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            proposed_delta_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'proposed',
            status_reason TEXT NOT NULL DEFAULT '',
            next_review_at TEXT NOT NULL DEFAULT '',
            pr_url TEXT NOT NULL DEFAULT '',
            merged_commit_sha TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (detector_name, pattern_key, scope_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_lesson_candidates_status "
        "ON lesson_candidates (status)"
    )
    conn.execute(
        "CREATE INDEX idx_lesson_candidates_profile "
        "ON lesson_candidates (client_profile, platform_profile)"
    )
    conn.execute(
        "CREATE INDEX idx_lesson_candidates_detector "
        "ON lesson_candidates (detector_name)"
    )
    conn.execute(
        "CREATE INDEX idx_lesson_candidates_profile_status_seen "
        "ON lesson_candidates (client_profile, status, detected_at DESC)"
    )

    conn.execute(
        """
        CREATE TABLE lesson_evidence (
            id INTEGER PRIMARY KEY,
            lesson_id TEXT NOT NULL REFERENCES lesson_candidates(lesson_id)
                ON DELETE CASCADE,
            pr_run_id INTEGER REFERENCES pr_runs(id),
            trace_id TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL,
            source_ref TEXT NOT NULL DEFAULT '',
            snippet TEXT NOT NULL DEFAULT '',
            UNIQUE (lesson_id, trace_id, source_ref)
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_lesson_evidence_lesson_id "
        "ON lesson_evidence (lesson_id)"
    )
    conn.execute(
        "CREATE INDEX idx_lesson_evidence_trace_id "
        "ON lesson_evidence (trace_id)"
    )
    conn.execute(
        "CREATE INDEX idx_lesson_evidence_pr_run_id "
        "ON lesson_evidence (pr_run_id)"
    )

    conn.execute(
        """
        CREATE TABLE lesson_outcomes (
            id INTEGER PRIMARY KEY,
            lesson_id TEXT NOT NULL REFERENCES lesson_candidates(lesson_id),
            measured_at TEXT NOT NULL,
            window_days INTEGER NOT NULL,
            pre_fpa REAL,
            post_fpa REAL,
            pre_escape_rate REAL,
            post_escape_rate REAL,
            pre_catch_rate REAL,
            post_catch_rate REAL,
            pattern_recurrence_count INTEGER NOT NULL DEFAULT 0,
            human_reedit_count INTEGER NOT NULL DEFAULT 0,
            human_reedit_refs TEXT NOT NULL DEFAULT '[]',
            verdict TEXT NOT NULL DEFAULT 'pending',
            notes TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_lesson_outcomes_lesson_id "
        "ON lesson_outcomes (lesson_id)"
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def autonomy_conn() -> Iterator[sqlite3.Connection]:
    """Open an autonomy.db connection, run ``ensure_schema``, close it.

    Single helper replacing the ``_open_conn() / try: ... / finally:
    conn.close()`` boilerplate that was copy-pasted into every
    autonomy_ingest endpoint (and a couple of sites in main.py /
    unified_dashboard.py). Call sites become::

        with autonomy_conn() as conn:
            do_thing(conn)

    Centralising the lifecycle here guarantees ``ensure_schema`` runs
    on every code path — previously a handful of sites opened the
    connection without calling it, risking a crash on the first
    write against a fresh DB.
    """
    db_path = resolve_db_path(settings.autonomy_db_path)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        yield conn
    finally:
        conn.close()


# Back-compat alias — FastAPI dependency form. The generator shape
# still works with ``Depends`` because FastAPI unwraps generators
# into per-request dependencies; ``autonomy_conn`` is the preferred
# form for direct ``with`` usage in handler bodies.
def get_autonomy_conn() -> Iterator[sqlite3.Connection]:
    """Yield a per-request sqlite3 connection; close in finally."""
    with autonomy_conn() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Repository helpers
# ---------------------------------------------------------------------------

class PrRunUpsert(BaseModel):
    """Patch-style upsert payload for pr_runs.

    On update, only non-None fields are applied, and text fields are only
    overwritten when non-empty. Integer boolean flags (first_pass_accepted,
    merged, escalated) use None to indicate "do not touch".
    """

    ticket_id: str
    pr_number: int
    repo_full_name: str
    pr_url: str = ""
    ticket_type: str = ""
    pipeline_mode: str = ""
    head_sha: str
    base_sha: str = ""
    client_profile: str = ""
    opened_at: str = ""
    approved_at: str | None = None
    merged_at: str | None = None
    closed_at: str | None = None
    first_pass_accepted: int | None = None
    merged: int | None = None
    escalated: int | None = None
    backfilled: int = 0


_TEXT_FIELDS = (
    "ticket_id",
    "pr_url",
    "ticket_type",
    "pipeline_mode",
    "base_sha",
    "client_profile",
    "opened_at",
    "approved_at",
    "merged_at",
    "closed_at",
)
_INT_FIELDS = ("first_pass_accepted", "merged", "escalated")


def upsert_pr_run(conn: sqlite3.Connection, row: PrRunUpsert) -> int:
    """Insert or patch-update a pr_runs row keyed on (repo, pr_number, head_sha).

    Returns the pr_run id. On update, text fields are only overwritten when
    the new value is non-empty; nullable integer flags are only overwritten
    when not None. `backfilled` and `pr_number`/`ticket_id` always get written
    on insert. `updated_at` is bumped on every write.
    """
    now = _now_iso()
    existing = get_pr_run_by_unique(
        conn, row.repo_full_name, row.pr_number, row.head_sha
    )

    if existing is None:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO pr_runs (
                    ticket_id, pr_number, repo_full_name, pr_url, ticket_type,
                    pipeline_mode, head_sha, base_sha, client_profile,
                    opened_at, approved_at, merged_at, closed_at,
                    first_pass_accepted, merged, escalated, backfilled,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.ticket_id,
                    row.pr_number,
                    row.repo_full_name,
                    row.pr_url,
                    row.ticket_type,
                    row.pipeline_mode,
                    row.head_sha,
                    row.base_sha,
                    row.client_profile,
                    row.opened_at,
                    row.approved_at or "",
                    row.merged_at or "",
                    row.closed_at or "",
                    row.first_pass_accepted if row.first_pass_accepted is not None else 0,
                    row.merged if row.merged is not None else 0,
                    row.escalated if row.escalated is not None else 0,
                    row.backfilled,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    # Update path: patch-style
    sets: list[str] = []
    params: list[object] = []

    for field in _TEXT_FIELDS:
        value = getattr(row, field)
        # Nullable text fields may be None
        if value is None:
            continue
        if value == "":
            continue
        sets.append(f"{field} = ?")
        params.append(value)

    for field in _INT_FIELDS:
        value = getattr(row, field)
        if value is None:
            continue
        sets.append(f"{field} = ?")
        params.append(int(value))

    # backfilled is always defined (int); only update if caller set it to 1
    if row.backfilled:
        sets.append("backfilled = ?")
        params.append(row.backfilled)

    sets.append("updated_at = ?")
    params.append(now)
    params.append(existing["id"])

    with conn:
        conn.execute(
            f"UPDATE pr_runs SET {', '.join(sets)} WHERE id = ?",
            params,
        )
    return int(existing["id"])


def get_pr_run_by_unique(
    conn: sqlite3.Connection,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
) -> sqlite3.Row | None:
    """Fetch a pr_runs row by its unique key, or None if not found."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM pr_runs WHERE repo_full_name = ? AND pr_number = ? "
        "AND head_sha = ?",
        (repo_full_name, pr_number, head_sha),
    ).fetchone()
    return row


def find_latest_merged_pr_run_by_ticket(
    conn: sqlite3.Connection, ticket_id: str
) -> sqlite3.Row | None:
    """Return the most recently merged pr_run for this ticket, or None."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM pr_runs WHERE ticket_id = ? AND merged = 1 "
        "ORDER BY datetime(merged_at) DESC, id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    return row


def list_pr_runs(
    conn: sqlite3.Connection,
    *,
    client_profile: str | None = None,
    since_iso: str | None = None,
    until_iso: str | None = None,
) -> list[sqlite3.Row]:
    """List pr_runs rows with optional client_profile + opened_at filters.

    ``since_iso`` / ``until_iso`` are inclusive/exclusive respectively —
    matching the half-open window convention the outcomes job uses
    to avoid double-counting PRs that open at midnight between
    pre/post windows.
    """
    clauses: list[str] = []
    params: list[object] = []
    if client_profile is not None:
        clauses.append("client_profile = ?")
        params.append(client_profile)
    if since_iso is not None:
        clauses.append("opened_at >= ?")
        params.append(since_iso)
    if until_iso is not None:
        clauses.append("opened_at < ?")
        params.append(until_iso)
    sql = "SELECT * FROM pr_runs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY opened_at DESC, id DESC"
    return list(conn.execute(sql, params).fetchall())


def list_client_profiles(conn: sqlite3.Connection) -> list[str]:
    """Return distinct non-empty client_profile values from pr_runs."""
    rows = conn.execute(
        "SELECT DISTINCT client_profile FROM pr_runs "
        "WHERE client_profile != '' ORDER BY client_profile"
    ).fetchall()
    return [r["client_profile"] for r in rows]


# ---------------------------------------------------------------------------
# v2 helpers: pending_ai_issues, review_issues, issue_matches
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# v3 helpers: defect_links, manual_overrides, match promotion
# ---------------------------------------------------------------------------

def insert_defect_link(
    conn: sqlite3.Connection,
    *,
    pr_run_id: int,
    defect_key: str,
    source: str,
    reported_at: str,
    severity: str = "",
    confirmed: int = 1,
    notes: str = "",
    category: str = "escaped",
) -> int:
    """Insert a defect_links row, upserting on (pr_run_id, defect_key, source).

    On conflict, updates severity/reported_at/confirmed/notes/category.
    Returns the row id.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO defect_links (
                pr_run_id, defect_key, source, severity, reported_at,
                confirmed, notes, category
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT (pr_run_id, defect_key, source) DO UPDATE SET
                severity = excluded.severity,
                reported_at = excluded.reported_at,
                confirmed = excluded.confirmed,
                notes = excluded.notes,
                category = excluded.category
            """,
            (
                pr_run_id,
                defect_key,
                source,
                severity,
                reported_at,
                int(confirmed),
                notes,
                category,
            ),
        )
    row = conn.execute(
        "SELECT id FROM defect_links WHERE pr_run_id = ? AND defect_key = ? "
        "AND source = ?",
        (pr_run_id, defect_key, source),
    ).fetchone()
    return int(row["id"]) if row is not None else 0


def get_defect_link(
    conn: sqlite3.Connection,
    pr_run_id: int,
    defect_key: str,
    source: str,
) -> sqlite3.Row | None:
    """Lookup a defect_link by unique triple."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM defect_links WHERE pr_run_id = ? AND defect_key = ? "
        "AND source = ?",
        (pr_run_id, defect_key, source),
    ).fetchone()
    return row


def _parse_iso(value: str) -> datetime | None:
    """Parse ISO-8601 string; return None on empty/malformed input.

    Always returns a UTC-aware datetime so callers can safely subtract
    any two parsed values without a naive-vs-aware TypeError.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def list_confirmed_escaped_defects(
    conn: sqlite3.Connection,
    pr_run_ids: list[int],
    *,
    window_days: int = 30,
) -> list[sqlite3.Row]:
    """Return confirmed escaped defect_links joined to pr_runs.

    Filters:
    - pr_run_id in pr_run_ids
    - pr_run.merged=1 and merged_at non-empty
    - defect_links.confirmed=1
    - defect_links.category='escaped'
    - reported_at >= merged_at
    - reported_at < merged_at + window_days

    Date-window filter is applied in Python over ISO strings.
    """
    if not pr_run_ids:
        return []
    # Chunk to stay under SQLite's SQLITE_MAX_VARIABLE_NUMBER (default 999).
    chunk_size = 900
    rows: list[sqlite3.Row] = []
    for i in range(0, len(pr_run_ids), chunk_size):
        chunk = pr_run_ids[i : i + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            "SELECT dl.*, pr.pr_number, pr.pr_url, pr.client_profile, "
            "pr.ticket_id, pr.merged_at "
            "FROM defect_links dl "
            "JOIN pr_runs pr ON pr.id = dl.pr_run_id "
            f"WHERE dl.pr_run_id IN ({placeholders}) "
            "AND pr.merged = 1 "
            "AND pr.merged_at != '' "
            "AND dl.confirmed = 1 "
            "AND dl.category = 'escaped'"
        )
        rows.extend(conn.execute(sql, tuple(chunk)).fetchall())

    out: list[sqlite3.Row] = []
    for r in rows:
        merged_dt = _parse_iso(r["merged_at"])
        reported_dt = _parse_iso(r["reported_at"])
        if merged_dt is None or reported_dt is None:
            continue
        delta_days = (reported_dt - merged_dt).total_seconds() / 86400.0
        if delta_days < 0:
            continue
        if delta_days >= window_days:
            continue
        out.append(r)
    return out


def count_merged_pr_runs_with_escape(
    conn: sqlite3.Connection,
    pr_run_ids: list[int],
    *,
    window_days: int = 30,
) -> int:
    """Return count of distinct pr_run_ids with at least one escaped defect
    in the post-merge window.
    """
    rows = list_confirmed_escaped_defects(
        conn, pr_run_ids, window_days=window_days
    )
    return len({int(r["pr_run_id"]) for r in rows})


def insert_manual_override(
    conn: sqlite3.Connection,
    *,
    override_type: str,
    target_id: str,
    payload_json: str,
    created_by: str = "admin",
) -> int:
    """Insert a manual_overrides audit row. Returns the new row id."""
    now = _now_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO manual_overrides (
                override_type, target_id, payload_json, created_at, created_by
            ) VALUES (?,?,?,?,?)
            """,
            (override_type, target_id, payload_json, now, created_by),
        )
    return int(cur.lastrowid or 0)


def promote_match_to_counted(
    conn: sqlite3.Connection,
    *,
    match_id: int,
    created_by: str = "admin",
) -> bool:
    """Promote a 'suggested' issue_matches row to 'manual' with confidence=1.0.

    Returns True if promoted. Returns False if the match does not exist or
    is not currently matched_by='suggested'.
    """
    row = conn.execute(
        "SELECT id, matched_by FROM issue_matches WHERE id = ?",
        (match_id,),
    ).fetchone()
    if row is None:
        return False
    if row["matched_by"] != "suggested":
        return False
    with conn:
        conn.execute(
            "UPDATE issue_matches SET matched_by = 'manual', confidence = 1.0 "
            "WHERE id = ?",
            (match_id,),
        )
    insert_manual_override(
        conn,
        override_type="promote_match",
        target_id=str(match_id),
        payload_json=json.dumps({"match_id": match_id}),
        created_by=created_by,
    )
    return True


def create_manual_match(
    conn: sqlite3.Connection,
    *,
    human_issue_id: int,
    ai_issue_id: int,
    created_by: str = "admin",
) -> int:
    """Create a manual issue_matches row linking a human-review issue to an
    AI-origin issue on the same PR run.

    Validates that:
    - both review_issues rows exist
    - they share pr_run_id
    - human source == 'human_review'
    - ai source in AI_SOURCES
    - ai.is_valid == 1

    Also writes a manual_overrides audit row. Returns the new match id.
    """
    human = conn.execute(
        "SELECT id, pr_run_id, source, is_valid FROM review_issues WHERE id = ?",
        (human_issue_id,),
    ).fetchone()
    if human is None:
        raise ValueError(f"human_issue_id {human_issue_id} not found")
    ai = conn.execute(
        "SELECT id, pr_run_id, source, is_valid FROM review_issues WHERE id = ?",
        (ai_issue_id,),
    ).fetchone()
    if ai is None:
        raise ValueError(f"ai_issue_id {ai_issue_id} not found")
    if human["pr_run_id"] != ai["pr_run_id"]:
        raise ValueError(
            "human and ai issues must belong to the same pr_run"
        )
    if human["source"] != "human_review":
        raise ValueError(
            f"human_issue_id source must be 'human_review', got "
            f"{human['source']!r}"
        )
    if ai["source"] not in AI_SOURCES:
        raise ValueError(
            f"ai_issue_id source must be one of {AI_SOURCES}, got "
            f"{ai['source']!r}"
        )
    if int(ai["is_valid"]) != 1:
        raise ValueError("ai_issue_id must have is_valid=1")

    # Emulated uniqueness guard — issue_matches has no UNIQUE constraint
    # in v1 schema so we check here to prevent duplicate manual matches.
    existing = conn.execute(
        "SELECT id FROM issue_matches WHERE human_issue_id = ? "
        "AND ai_issue_id = ?",
        (human_issue_id, ai_issue_id),
    ).fetchone()
    if existing is not None:
        raise ValueError(
            f"match already exists between human_issue_id={human_issue_id} "
            f"and ai_issue_id={ai_issue_id}"
        )

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
                "manual",
                1.0,
                now,
                "manual",
            ),
        )
    match_id = int(cur.lastrowid or 0)
    insert_manual_override(
        conn,
        override_type="create_manual_match",
        target_id=str(match_id),
        payload_json=json.dumps(
            {
                "match_id": match_id,
                "human_issue_id": human_issue_id,
                "ai_issue_id": ai_issue_id,
            }
        ),
        created_by=created_by,
    )
    return match_id


def list_defect_links_for_profile(
    conn: sqlite3.Connection,
    client_profile: str,
    *,
    since_iso: str | None = None,
    limit: int = 50,
    confirmed: int | None = None,
    category: str | None = None,
) -> list[sqlite3.Row]:
    """Return defect_links for PRs in the given profile, most recent first.

    Joins defect_links to pr_runs on pr_runs.client_profile. Optional
    since_iso filters on defect_links.reported_at. ``confirmed`` and
    ``category`` are optional SQL-side filters — push them in here
    rather than filtering in Python after LIMIT. Without this, the
    escaped-defects dashboard panel could silently drop escaped rows
    whenever a profile had more recent non-escaped/unconfirmed rows
    within the same window: SQL LIMIT applies first, and the Python
    ``[r for r in rows if confirmed==1 and category=='escaped']``
    filter then saw an empty set.
    """
    clauses = ["pr.client_profile = ?"]
    params: list[object] = [client_profile]
    if since_iso is not None:
        clauses.append("dl.reported_at >= ?")
        params.append(since_iso)
    if confirmed is not None:
        clauses.append("dl.confirmed = ?")
        params.append(int(confirmed))
    if category is not None:
        clauses.append("dl.category = ?")
        params.append(category)
    sql = (
        "SELECT dl.*, pr.pr_number, pr.pr_url, pr.client_profile, "
        "pr.ticket_id, pr.merged_at "
        "FROM defect_links dl "
        "JOIN pr_runs pr ON pr.id = dl.pr_run_id "
        "WHERE " + " AND ".join(clauses) + " "
        "ORDER BY dl.reported_at DESC, dl.id DESC LIMIT ?"
    )
    params.append(int(limit))
    return list(conn.execute(sql, tuple(params)).fetchall())


def record_defect_sweep_heartbeat(
    conn: sqlite3.Connection,
    *,
    client_profile: str,
    swept_through_iso: str,
    created_by: str = "admin",
) -> int:
    """Record a 'defect sweep heartbeat' for a client profile.

    Stored as a manual_overrides row with override_type='defect_sweep_heartbeat',
    target_id=client_profile, payload_json={'swept_through': swept_through_iso}.
    """
    return insert_manual_override(
        conn,
        override_type="defect_sweep_heartbeat",
        target_id=client_profile,
        payload_json=json.dumps({"swept_through": swept_through_iso}),
        created_by=created_by,
    )


def get_latest_defect_sweep_heartbeat(
    conn: sqlite3.Connection,
    client_profile: str,
) -> str | None:
    """Return the most recent swept_through timestamp for the profile."""
    rows = conn.execute(
        "SELECT payload_json FROM manual_overrides "
        "WHERE override_type = 'defect_sweep_heartbeat' AND target_id = ? "
        "ORDER BY id DESC",
        (client_profile,),
    ).fetchall()
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            continue
        value = payload.get("swept_through")
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# Phase 4: auto-merge decisions + kill-switch toggle
# ---------------------------------------------------------------------------

def record_auto_merge_decision(
    conn: sqlite3.Connection,
    *,
    repo_full_name: str,
    pr_number: int,
    decision: str,
    reason: str,
    payload: dict[str, Any],
    created_by: str = "l3_auto_merge",
) -> int:
    """Log an auto-merge decision via manual_overrides.

    override_type='auto_merge_decision', target_id='{repo}#{pr_number}'.
    payload_json merges {decision, reason} with the caller-supplied payload.
    """
    merged = {"decision": decision, "reason": reason, **payload}
    return insert_manual_override(
        conn,
        override_type="auto_merge_decision",
        target_id=f"{repo_full_name}#{pr_number}",
        payload_json=json.dumps(merged),
        created_by=created_by,
    )


def get_auto_merge_toggle(
    conn: sqlite3.Connection, client_profile: str
) -> bool | None:
    """Read the latest per-profile auto-merge runtime toggle.

    Returns None if no toggle has ever been set (caller falls back to YAML).
    """
    row = conn.execute(
        "SELECT payload_json FROM manual_overrides "
        "WHERE override_type = 'auto_merge_toggle' AND target_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (client_profile,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return bool(payload.get("enabled"))


def set_auto_merge_toggle(
    conn: sqlite3.Connection,
    *,
    client_profile: str,
    enabled: bool,
    created_by: str = "admin",
) -> int:
    """Insert a new toggle row for `client_profile`. Latest wins."""
    return insert_manual_override(
        conn,
        override_type="auto_merge_toggle",
        target_id=client_profile,
        payload_json=json.dumps({"enabled": bool(enabled)}),
        created_by=created_by,
    )


def _escape_like(value: str) -> str:
    """Escape SQLite ``LIKE`` wildcards in user-supplied values.

    Callers must pair the returned value with ``LIKE ? ESCAPE '\\'``
    (see ``list_recent_auto_merge_decisions``). Without escaping,
    ``_`` matches any single character and ``%`` matches any sequence,
    so user input like ``foo_bar`` would match ``fooXbar`` and a
    single ``_`` would match every row — turning a per-profile filter
    into a data leak across profiles.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_recent_auto_merge_decisions(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    since_iso: str | None = None,
    repo_full_name: str | None = None,
    client_profile: str | None = None,
) -> list[sqlite3.Row]:
    """Return recent auto-merge decision rows from manual_overrides.

    Filters:
      * since_iso — created_at >= value
      * repo_full_name — target_id starts with "{repo}#"
      * client_profile — payload JSON contains this profile name

    User-supplied values in ``repo_full_name`` and ``client_profile``
    are LIKE-escaped before being spliced into the pattern so
    underscores and percent signs in legitimate profile/repo names
    (``first_pass_acceptance``, ``my_repo``) match literally instead
    of acting as wildcards. Uses ``LIKE ? ESCAPE '\\'`` to tell SQLite
    how to interpret the backslash escape.

    Ordered by id DESC, capped at `limit`.
    """
    clauses = ["override_type = 'auto_merge_decision'"]
    params: list[Any] = []
    if since_iso:
        clauses.append("created_at >= ?")
        params.append(since_iso)
    if repo_full_name:
        clauses.append("target_id LIKE ? ESCAPE '\\'")
        params.append(f"{_escape_like(repo_full_name)}#%")
    if client_profile:
        # Crude substring match on serialized JSON — sufficient for dashboard.
        clauses.append("payload_json LIKE ? ESCAPE '\\'")
        params.append(
            f'%"client_profile": "{_escape_like(client_profile)}"%'
        )
    sql = (
        "SELECT * FROM manual_overrides WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT ?"
    )
    params.append(limit)
    return list(conn.execute(sql, params).fetchall())


# ---------------------------------------------------------------------------
# Self-learning lesson tables (v5)
# ---------------------------------------------------------------------------

# Cap per lesson. Evidence rows past this are trimmed oldest-first on insert
# so the dashboard stays responsive and the ``evidence`` JOIN stays bounded.
LESSON_EVIDENCE_CAP = 20

# Max length of a ``lesson_evidence.snippet``. Enforced here (not by the
# detector) so truncation happens AFTER redaction — which prevents
# splitting a redaction marker in half.
LESSON_SNIPPET_MAX_LEN = 500


class LessonCandidateUpsert(BaseModel):
    """Patch-style upsert for a detected lesson candidate.

    Keyed on ``(detector_name, pattern_key, scope_key)`` so rescans
    refresh the existing row instead of duplicating. See
    ``upsert_lesson_candidate`` for MAX-frequency semantics and
    fields that are preserved across rescans.
    """

    lesson_id: str
    detector_name: str
    detector_version: int = 1
    pattern_key: str
    client_profile: str = ""
    platform_profile: str = ""
    scope_key: str = ""
    proposed_delta_json: str = "{}"
    severity: str = "info"
    window_frequency: int = 1


def upsert_lesson_candidate(
    conn: sqlite3.Connection,
    row: LessonCandidateUpsert,
    *,
    now: str | None = None,
) -> int:
    """Insert or refresh a lesson_candidates row.

    On first detection: inserts with ``status='proposed'`` and
    ``frequency=window_frequency``.

    On repeat detection: refreshes ``severity``,
    ``proposed_delta_json``, ``detector_version``, and
    ``last_seen_at``; sets ``frequency = MAX(current,
    window_frequency)`` so a narrower later scan doesn't regress
    the count. Does NOT touch ``status``, ``status_reason``,
    ``next_review_at``, ``pr_url``, or ``merged_commit_sha`` —
    those are driven by the approval flow.

    Returns the candidate id.
    """
    ts = now or _now_iso()
    initial_freq = max(1, int(row.window_frequency))
    existing = conn.execute(
        "SELECT id FROM lesson_candidates "
        "WHERE detector_name = ? AND pattern_key = ? AND scope_key = ?",
        (row.detector_name, row.pattern_key, row.scope_key),
    ).fetchone()

    if existing is None:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO lesson_candidates (
                    lesson_id, detector_name, detector_version, pattern_key,
                    client_profile, platform_profile, scope_key,
                    frequency, severity, detected_at, last_seen_at,
                    proposed_delta_json, status, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.lesson_id,
                    row.detector_name,
                    row.detector_version,
                    row.pattern_key,
                    row.client_profile,
                    row.platform_profile,
                    row.scope_key,
                    initial_freq,
                    row.severity,
                    ts,
                    ts,
                    row.proposed_delta_json,
                    "proposed",
                    ts,
                    ts,
                ),
            )
        return int(cur.lastrowid or 0)

    with conn:
        conn.execute(
            """
            UPDATE lesson_candidates
            SET frequency = MAX(frequency, ?),
                last_seen_at = ?,
                updated_at = ?,
                severity = ?,
                proposed_delta_json = ?,
                detector_version = ?
            WHERE id = ?
            """,
            (
                initial_freq,
                ts,
                ts,
                row.severity,
                row.proposed_delta_json,
                row.detector_version,
                int(existing["id"]),
            ),
        )
    return int(existing["id"])


def insert_lesson_evidence(
    conn: sqlite3.Connection,
    *,
    lesson_id: str,
    trace_id: str,
    source_ref: str,
    observed_at: str,
    snippet: str = "",
    pr_run_id: int | None = None,
    cap: int | None = None,
) -> int | None:
    """Insert an evidence row, trimming oldest beyond ``cap``.

    Returns the new id, or ``None`` on UNIQUE collision (which is
    the normal no-op path when a rescan revisits an existing
    trace). Snippets longer than ``LESSON_SNIPPET_MAX_LEN`` are
    truncated with an ellipsis.
    """
    effective_cap = cap if cap is not None else LESSON_EVIDENCE_CAP
    if effective_cap < 1:
        raise ValueError(
            f"insert_lesson_evidence: cap must be >= 1, got {effective_cap}"
        )
    if len(snippet) > LESSON_SNIPPET_MAX_LEN:
        snippet = snippet[: LESSON_SNIPPET_MAX_LEN - 3] + "..."
    try:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO lesson_evidence
                    (lesson_id, pr_run_id, trace_id, observed_at,
                     source_ref, snippet)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lesson_id,
                    pr_run_id,
                    trace_id,
                    observed_at,
                    source_ref,
                    snippet,
                ),
            )
    except sqlite3.IntegrityError:
        return None
    new_id = int(cur.lastrowid or 0)

    with conn:
        conn.execute(
            """
            DELETE FROM lesson_evidence
            WHERE lesson_id = ?
              AND id NOT IN (
                  SELECT id FROM lesson_evidence
                  WHERE lesson_id = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
            """,
            (lesson_id, lesson_id, effective_cap),
        )
    return new_id


def list_lesson_evidence(
    conn: sqlite3.Connection,
    lesson_id: str,
    *,
    limit: int = LESSON_EVIDENCE_CAP,
) -> list[sqlite3.Row]:
    """Return evidence rows for a lesson, newest first."""
    rows = conn.execute(
        "SELECT * FROM lesson_evidence WHERE lesson_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (lesson_id, limit),
    ).fetchall()
    return list(rows)


def get_lesson_by_id(
    conn: sqlite3.Connection, lesson_id: str
) -> sqlite3.Row | None:
    """Fetch a lesson_candidates row by its ``LSN-<hex>`` id."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM lesson_candidates WHERE lesson_id = ?",
        (lesson_id,),
    ).fetchone()
    return row


def list_lesson_candidates(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    client_profile: str | None = None,
    detector_name: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """List lesson_candidates with optional filters.

    Ordered by ``detected_at DESC`` so the most recent patterns
    appear first — matches the dashboard's default sort order and
    rides the ``idx_lesson_candidates_profile_status_seen`` index
    when ``status`` and ``client_profile`` are both specified.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if client_profile is not None:
        clauses.append("client_profile = ?")
        params.append(client_profile)
    if detector_name is not None:
        clauses.append("detector_name = ?")
        params.append(detector_name)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT * FROM lesson_candidates {where} "
        "ORDER BY detected_at DESC LIMIT ?"
    )
    params.append(limit)
    return list(conn.execute(sql, params).fetchall())


# Allowed (current -> {next}) transitions. Linear approval flow
# (proposed -> draft_ready -> approved -> applied) with snooze
# cycles and rejected/reverted/stale as terminal states.
_LESSON_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"draft_ready", "rejected", "snoozed", "stale"},
    "draft_ready": {"approved", "proposed", "rejected"},
    "snoozed": {"proposed", "rejected", "stale"},
    "approved": {"applied", "rejected"},
    "applied": {"reverted"},
    "rejected": set(),
    "reverted": set(),
    "stale": set(),
}


def update_lesson_status(
    conn: sqlite3.Connection,
    lesson_id: str,
    new_status: str,
    *,
    reason: str = "",
    pr_url: str | None = None,
    merged_commit_sha: str | None = None,
    next_review_at: str | None = None,
    proposed_delta_json: str | None = None,
    now: str | None = None,
) -> sqlite3.Row:
    """Transition a lesson's status, validating against the allowed
    transition table. Raises ``ValueError`` on unknown lesson or
    disallowed transition. Side-channel fields (``pr_url``,
    ``merged_commit_sha``, ``next_review_at``, ``proposed_delta_json``)
    only get written when the caller supplies a non-None value.
    """
    ts = now or _now_iso()
    row = get_lesson_by_id(conn, lesson_id)
    if row is None:
        raise ValueError(f"unknown lesson_id: {lesson_id}")
    current = str(row["status"])
    allowed = _LESSON_STATUS_TRANSITIONS.get(current)
    if allowed is None:
        raise ValueError(f"unknown current status: {current}")
    if new_status not in allowed:
        raise ValueError(
            f"invalid transition {current} -> {new_status} "
            f"for lesson {lesson_id}"
        )

    sets = ["status = ?", "status_reason = ?", "updated_at = ?"]
    params: list[Any] = [new_status, reason, ts]
    if pr_url is not None:
        sets.append("pr_url = ?")
        params.append(pr_url)
    if merged_commit_sha is not None:
        sets.append("merged_commit_sha = ?")
        params.append(merged_commit_sha)
    if next_review_at is not None:
        sets.append("next_review_at = ?")
        params.append(next_review_at)
    if proposed_delta_json is not None:
        sets.append("proposed_delta_json = ?")
        params.append(proposed_delta_json)
    params.append(lesson_id)

    with conn:
        conn.execute(
            f"UPDATE lesson_candidates SET {', '.join(sets)} "
            "WHERE lesson_id = ?",
            params,
        )
    updated = get_lesson_by_id(conn, lesson_id)
    assert updated is not None  # just updated
    return updated


def set_lesson_status_reason(
    conn: sqlite3.Connection,
    lesson_id: str,
    reason: str,
    *,
    now: str | None = None,
) -> None:
    """Update ``status_reason`` without changing ``status``.

    Used when a drafter run fails — the operator needs to see what
    went wrong, but the lesson remains at its existing status
    (usually ``proposed``). The transition validator does not apply
    since no transition is happening.
    """
    ts = now or _now_iso()
    with conn:
        conn.execute(
            "UPDATE lesson_candidates SET status_reason = ?, updated_at = ? "
            "WHERE lesson_id = ?",
            (reason[:500], ts, lesson_id),
        )


def set_lesson_merged_commit_sha(
    conn: sqlite3.Connection,
    lesson_id: str,
    merged_commit_sha: str,
    *,
    now: str | None = None,
) -> None:
    """Record the merge commit sha for an applied lesson.

    Side-channel writer that sits alongside ``set_lesson_status_reason``
    so outcomes measurement doesn't need raw SQL against
    ``lesson_candidates``. Applied→applied is not a legal
    ``update_lesson_status`` transition, and we only ever need to
    stamp this column once per lesson.
    """
    ts = now or _now_iso()
    with conn:
        conn.execute(
            "UPDATE lesson_candidates SET merged_commit_sha = ?, "
            "updated_at = ? WHERE lesson_id = ?",
            (merged_commit_sha, ts, lesson_id),
        )


# ---------------------------------------------------------------------------
# Lesson outcomes (v5 — pre/post metrics + human-reedit signals)
# ---------------------------------------------------------------------------


_TERMINAL_VERDICTS: tuple[str, ...] = ("regressed", "human_reedit", "confirmed")


def list_applied_lessons(
    conn: sqlite3.Connection,
    *,
    exclude_terminal_verdicts: bool = False,
) -> list[sqlite3.Row]:
    """Return applied lessons in detected_at DESC order.

    With ``exclude_terminal_verdicts=True`` the query LEFT JOINs the
    latest ``lesson_outcomes`` row per lesson and drops any whose
    verdict is terminal (regressed / human_reedit / confirmed). The
    outcomes job uses that filter to avoid re-measuring lessons whose
    verdict is already final.
    """
    if exclude_terminal_verdicts:
        placeholders = ",".join("?" for _ in _TERMINAL_VERDICTS)
        sql = f"""
            SELECT c.* FROM lesson_candidates c
            LEFT JOIN (
                SELECT lesson_id, verdict
                FROM lesson_outcomes
                WHERE id IN (
                    SELECT MAX(id) FROM lesson_outcomes GROUP BY lesson_id
                )
            ) o ON o.lesson_id = c.lesson_id
            WHERE c.status = 'applied'
              AND (o.verdict IS NULL OR o.verdict NOT IN ({placeholders}))
            ORDER BY c.detected_at DESC
        """
        rows = conn.execute(sql, _TERMINAL_VERDICTS).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM lesson_candidates WHERE status = 'applied' "
            "ORDER BY detected_at DESC"
        ).fetchall()
    return list(rows)


def get_latest_outcome(
    conn: sqlite3.Connection, lesson_id: str
) -> sqlite3.Row | None:
    """Return the most recent ``lesson_outcomes`` row for a lesson, or None."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM lesson_outcomes WHERE lesson_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (lesson_id,),
    ).fetchone()
    return row


def list_latest_outcomes(
    conn: sqlite3.Connection, lesson_ids: list[str]
) -> dict[str, sqlite3.Row]:
    """Return ``{lesson_id: latest outcome row}`` for a batch of ids.

    Dashboard row rendering calls this once instead of issuing one
    SELECT per row (N+1). Lesson ids with no outcome are simply
    absent from the result dict.
    """
    if not lesson_ids:
        return {}
    placeholders = ",".join("?" for _ in lesson_ids)
    sql = f"""
        SELECT * FROM lesson_outcomes
        WHERE id IN (
            SELECT MAX(id) FROM lesson_outcomes
            WHERE lesson_id IN ({placeholders})
            GROUP BY lesson_id
        )
    """
    rows = conn.execute(sql, lesson_ids).fetchall()
    return {str(r["lesson_id"]): r for r in rows}


class LessonOutcomeInsert(BaseModel):
    """Write payload for a ``lesson_outcomes`` row.

    ``verdict`` is one of:
      - ``pending`` — measurement still within window
      - ``confirmed`` — post metrics better or equal + no pattern recurrence
      - ``no_change`` — metrics didn't move meaningfully
      - ``regressed`` — post metrics worse than pre
      - ``human_reedit`` — the lesson's anchor was re-edited by a human
        (direct "this lesson was wrong" signal; trumps metric verdict)
    """

    lesson_id: str
    measured_at: str
    window_days: int
    pre_fpa: float | None = None
    post_fpa: float | None = None
    pre_escape_rate: float | None = None
    post_escape_rate: float | None = None
    pre_catch_rate: float | None = None
    post_catch_rate: float | None = None
    pattern_recurrence_count: int = 0
    human_reedit_count: int = 0
    human_reedit_refs: str = "[]"
    verdict: str = "pending"
    notes: str = ""


def insert_lesson_outcome(
    conn: sqlite3.Connection, row: LessonOutcomeInsert
) -> int:
    """Insert a ``lesson_outcomes`` row; return the new id."""
    with conn:
        cur = conn.execute(
            """
            INSERT INTO lesson_outcomes (
                lesson_id, measured_at, window_days,
                pre_fpa, post_fpa,
                pre_escape_rate, post_escape_rate,
                pre_catch_rate, post_catch_rate,
                pattern_recurrence_count,
                human_reedit_count, human_reedit_refs,
                verdict, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row.lesson_id,
                row.measured_at,
                row.window_days,
                row.pre_fpa,
                row.post_fpa,
                row.pre_escape_rate,
                row.post_escape_rate,
                row.pre_catch_rate,
                row.post_catch_rate,
                row.pattern_recurrence_count,
                row.human_reedit_count,
                row.human_reedit_refs,
                row.verdict,
                row.notes,
            ),
        )
    return int(cur.lastrowid or 0)
