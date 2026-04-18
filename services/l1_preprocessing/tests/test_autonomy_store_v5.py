"""Tests for the v5 self-learning schema migration.

Covers:

- v5 migration applies on a fresh DB (full chain v1..v5).
- v5 migration applies on top of an existing v4 DB (upgrade path —
  this is the one an in-place deployment will actually execute).
- Three new tables exist with the expected columns.
- Expected indexes exist, including the hot-read index
  ``idx_lesson_candidates_profile_status_seen``.
- UNIQUE constraints behave as designed:
    * ``lesson_candidates (detector_name, pattern_key, scope_key)``
      rejects a duplicate insert.
    * ``lesson_evidence (lesson_id, trace_id, source_ref)`` rejects
      a duplicate insert.
- ``ON DELETE CASCADE`` on ``lesson_evidence.lesson_id`` removes
  evidence rows when their parent candidate is deleted.

Stays at the schema level — no repository helpers are exercised
here; those get their own tests in Phase A.2.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autonomy_store import ensure_schema, open_connection


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "autonomy.db"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        r["name"]
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None


class TestMigrationApplies:
    def test_ensure_schema_reaches_v5_on_fresh_db(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            version = ensure_schema(conn)
            # ensure_schema reaches the current latest (v6 after
            # pipeline_metrics migration), but the v5 tables must still
            # exist. Assert presence of v5 rather than equality.
            assert version >= 5
            row = conn.execute(
                "SELECT version FROM schema_version WHERE version = 5"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_v5_migration_idempotent(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            ensure_schema(conn)
            # One row per applied migration. Second call must not add more.
            rows = conn.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ).fetchall()
            versions = [int(r["version"]) for r in rows]
            # Must include the full v1..v5 chain in order. Trailing
            # versions (v6+) are allowed — they're independent migrations.
            assert versions[:5] == [1, 2, 3, 4, 5]
        finally:
            conn.close()


class TestLessonCandidatesSchema:
    def test_lesson_candidates_table_exists_with_expected_columns(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            cols = _table_columns(conn, "lesson_candidates")
            expected = {
                "id",
                "lesson_id",
                "detector_name",
                "detector_version",
                "pattern_key",
                "client_profile",
                "platform_profile",
                "scope_key",
                "frequency",
                "severity",
                "detected_at",
                "last_seen_at",
                "proposed_delta_json",
                "status",
                "status_reason",
                "next_review_at",
                "pr_url",
                "merged_commit_sha",
                "created_at",
                "updated_at",
            }
            assert expected.issubset(cols), f"missing: {expected - cols}"
        finally:
            conn.close()

    def test_lesson_candidates_hot_read_index_exists(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            assert _index_exists(
                conn, "idx_lesson_candidates_profile_status_seen"
            )
        finally:
            conn.close()

    def test_lesson_candidates_other_indexes_exist(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            assert _index_exists(conn, "idx_lesson_candidates_status")
            assert _index_exists(conn, "idx_lesson_candidates_profile")
            assert _index_exists(conn, "idx_lesson_candidates_detector")
        finally:
            conn.close()

    def test_lesson_candidates_unique_on_detector_pattern_scope(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO lesson_candidates "
                "(lesson_id, detector_name, pattern_key, scope_key, "
                " detected_at, last_seen_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "LSN-aaaaaaaa",
                    "human_issue_cluster",
                    "pk-1",
                    "xcsf30|salesforce|security|foo.cls",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                ),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO lesson_candidates "
                    "(lesson_id, detector_name, pattern_key, scope_key, "
                    " detected_at, last_seen_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "LSN-bbbbbbbb",
                        "human_issue_cluster",
                        "pk-1",
                        "xcsf30|salesforce|security|foo.cls",
                        "2026-04-02T00:00:00+00:00",
                        "2026-04-02T00:00:00+00:00",
                        "2026-04-02T00:00:00+00:00",
                        "2026-04-02T00:00:00+00:00",
                    ),
                )
        finally:
            conn.close()

    def test_lesson_id_is_unique(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO lesson_candidates "
                "(lesson_id, detector_name, pattern_key, scope_key, "
                " detected_at, last_seen_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "LSN-shared",
                    "det_a",
                    "pk-1",
                    "scope-1",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                ),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO lesson_candidates "
                    "(lesson_id, detector_name, pattern_key, scope_key, "
                    " detected_at, last_seen_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "LSN-shared",
                        "det_b",
                        "pk-2",
                        "scope-2",
                        "2026-04-02T00:00:00+00:00",
                        "2026-04-02T00:00:00+00:00",
                        "2026-04-02T00:00:00+00:00",
                        "2026-04-02T00:00:00+00:00",
                    ),
                )
        finally:
            conn.close()


class TestLessonEvidenceSchema:
    def test_lesson_evidence_table_exists_with_expected_columns(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            cols = _table_columns(conn, "lesson_evidence")
            expected = {
                "id",
                "lesson_id",
                "pr_run_id",
                "trace_id",
                "observed_at",
                "source_ref",
                "snippet",
            }
            assert expected.issubset(cols), f"missing: {expected - cols}"
        finally:
            conn.close()

    def test_lesson_evidence_indexes_exist(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            assert _index_exists(conn, "idx_lesson_evidence_lesson_id")
            assert _index_exists(conn, "idx_lesson_evidence_trace_id")
            assert _index_exists(conn, "idx_lesson_evidence_pr_run_id")
        finally:
            conn.close()

    def test_lesson_evidence_unique_on_lesson_trace_source(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO lesson_candidates "
                "(lesson_id, detector_name, pattern_key, scope_key, "
                " detected_at, last_seen_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "LSN-ev1",
                    "det",
                    "pk",
                    "scope",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                ),
            )
            conn.execute(
                "INSERT INTO lesson_evidence "
                "(lesson_id, trace_id, observed_at, source_ref, snippet) "
                "VALUES (?, ?, ?, ?, ?)",
                ("LSN-ev1", "SCRUM-1", "2026-04-01T01:00:00+00:00", "x.json#a", "s"),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO lesson_evidence "
                    "(lesson_id, trace_id, observed_at, source_ref, snippet) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        "LSN-ev1",
                        "SCRUM-1",
                        "2026-04-01T02:00:00+00:00",
                        "x.json#a",
                        "s2",
                    ),
                )
        finally:
            conn.close()

    def test_lesson_evidence_cascade_on_parent_delete(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO lesson_candidates "
                "(lesson_id, detector_name, pattern_key, scope_key, "
                " detected_at, last_seen_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "LSN-cascade",
                    "det",
                    "pk",
                    "scope",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                    "2026-04-01T00:00:00+00:00",
                ),
            )
            conn.execute(
                "INSERT INTO lesson_evidence "
                "(lesson_id, trace_id, observed_at, source_ref, snippet) "
                "VALUES (?, ?, ?, ?, ?)",
                ("LSN-cascade", "T-1", "2026-04-01T01:00:00+00:00", "r", "s"),
            )
            conn.commit()
            conn.execute(
                "DELETE FROM lesson_candidates WHERE lesson_id = ?",
                ("LSN-cascade",),
            )
            conn.commit()
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM lesson_evidence "
                "WHERE lesson_id = ?",
                ("LSN-cascade",),
            ).fetchone()
            assert int(row["n"]) == 0
        finally:
            conn.close()


class TestLessonOutcomesSchema:
    def test_lesson_outcomes_table_exists_with_expected_columns(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            cols = _table_columns(conn, "lesson_outcomes")
            expected = {
                "id",
                "lesson_id",
                "measured_at",
                "window_days",
                "pre_fpa",
                "post_fpa",
                "pre_escape_rate",
                "post_escape_rate",
                "pre_catch_rate",
                "post_catch_rate",
                "pattern_recurrence_count",
                "human_reedit_count",
                "human_reedit_refs",
                "verdict",
                "notes",
            }
            assert expected.issubset(cols), f"missing: {expected - cols}"
        finally:
            conn.close()

    def test_lesson_outcomes_index_exists(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            assert _index_exists(conn, "idx_lesson_outcomes_lesson_id")
        finally:
            conn.close()


class TestIncrementalUpgradeFromV4:
    """Simulate the in-place upgrade path: DB already at v4, migrate to v5.

    The fresh-DB path is covered above; this case exercises the branch
    that will actually run on the deployed L1 host.
    """

    def test_v5_migration_applies_on_top_of_v4(self, db_path: Path) -> None:
        from unittest.mock import patch

        import autonomy_store as store

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            # Rewind to v4 by removing the v5+v6 marker rows + tables, so
            # the next ensure_schema() exercises the incremental-upgrade
            # path. The test targets the v4→v5 branch; any post-v5
            # migration needs the same rewind treatment so we don't skew
            # the starting version.
            conn.execute("DELETE FROM schema_version WHERE version >= 5")
            conn.execute("DROP TABLE IF EXISTS lesson_outcomes")
            conn.execute("DROP TABLE IF EXISTS lesson_evidence")
            conn.execute("DROP TABLE IF EXISTS lesson_candidates")
            conn.execute("DROP TABLE IF EXISTS pipeline_metrics")
            conn.commit()

            assert store._current_schema_version(conn) == 4

            with patch.object(store.logger, "info"):
                version = ensure_schema(conn)
            # The full migration chain continues past v5 now (v6 adds
            # pipeline_metrics). Just assert we reached at least v5.
            assert version >= 5
            for t in ("lesson_candidates", "lesson_evidence", "lesson_outcomes"):
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (t,),
                ).fetchone()
                assert row is not None, f"table {t} not recreated"
        finally:
            conn.close()
