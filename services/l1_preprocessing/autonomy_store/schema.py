"""Connection + schema management for autonomy.db.

Extracted from ``autonomy_store.py`` as part of the Phase 4
structural refactor. Owns:

* ``resolve_db_path``, ``open_connection``
* ``ensure_schema`` + the v1..v5 migration helpers
* ``_now_iso`` helper
* ``autonomy_conn`` / ``get_autonomy_conn`` FastAPI dependency
* ``AI_SOURCES`` tuple (used by defects.py for cross-source validation)

Every public symbol is re-exported from ``autonomy_store.__init__``
so ``from autonomy_store import X`` continues to work for every
existing caller.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import structlog

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
    # Subpackage dir sits at services/l1_preprocessing/autonomy_store/ so
    # repo_root is three levels up rather than two.
    repo_root = Path(__file__).resolve().parents[3]
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
    if version < 6:
        with conn:
            _migrate_to_v6(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (6, _now_iso()),
            )
        version = 6
        logger.info("autonomy_schema_migrated", version=version)
    if version < 7:
        with conn:
            _migrate_to_v7(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (7, _now_iso()),
            )
        version = 7
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


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    """v6: pipeline_metrics table — per-run scalar observations.

    One row per (trace_id, metric_name) tuple. Populated by the
    learning miner's reviewer_judge_rejection_rate detector when it
    scans worktree artifacts (code-review.json + judge-verdict.json)
    to compute rejection rates. Kept generic so future detectors can
    reuse it for rolling-window trends.

    The UNIQUE (trace_id, metric_name) constraint supports idempotent
    upserts: a repeat scan of the same trace replaces rather than
    accumulates. ``observed_at`` is the timestamp of the underlying
    observation (typically the pr_run open_at or the artifact mtime),
    not the time the metric row was inserted.
    """
    conn.execute(
        """
        CREATE TABLE pipeline_metrics (
            id INTEGER PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE (trace_id, metric_name)
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_pipeline_metrics_name_observed_at "
        "ON pipeline_metrics (metric_name, observed_at DESC)"
    )
    conn.execute(
        "CREATE INDEX idx_pipeline_metrics_ticket_id "
        "ON pipeline_metrics (ticket_id)"
    )


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    """v7: trigger_state table — persists edge-detection across restarts.

    Stores the last-known tag state per ticket so an L1 restart doesn't
    re-dispatch tickets that already had the trigger label before the
    restart. Replaces the in-process _last_trigger_state dict in
    claim_store.py for the cross-restart case.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trigger_state (
            ticket_id TEXT PRIMARY KEY,
            tag_present INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
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
