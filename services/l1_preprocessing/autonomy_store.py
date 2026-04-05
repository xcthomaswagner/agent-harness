"""SQLite-backed store for autonomy metrics.

Owns the autonomy.db schema, migrations, and repository helpers used by the
L1 autonomy ingest and dashboard endpoints. Uses stdlib sqlite3 with a
connection-per-request pattern and hand-rolled, versioned migrations.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import structlog
from pydantic import BaseModel

from config import settings

logger = structlog.get_logger()


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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def get_autonomy_conn() -> Iterator[sqlite3.Connection]:
    """Yield a per-request sqlite3 connection; close in finally."""
    db_path = resolve_db_path(settings.autonomy_db_path)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        yield conn
    finally:
        conn.close()


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


def list_pr_runs(
    conn: sqlite3.Connection,
    *,
    client_profile: str | None = None,
    since_iso: str | None = None,
) -> list[sqlite3.Row]:
    """List pr_runs rows with optional client_profile and opened_at filters."""
    clauses: list[str] = []
    params: list[object] = []
    if client_profile is not None:
        clauses.append("client_profile = ?")
        params.append(client_profile)
    if since_iso is not None:
        clauses.append("opened_at >= ?")
        params.append(since_iso)
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
