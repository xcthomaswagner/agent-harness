"""Tests for autonomy_store — schema migrations, pragmas, and repo helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    get_pr_run_by_unique,
    list_client_profiles,
    list_pr_runs,
    open_connection,
    upsert_pr_run,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "autonomy.db"


def _base_upsert(**overrides: object) -> PrRunUpsert:
    data: dict[str, object] = {
        "ticket_id": "SCRUM-1",
        "pr_number": 10,
        "repo_full_name": "acme/app",
        "head_sha": "abc123",
        "pr_url": "https://github.com/acme/app/pull/10",
        "ticket_type": "story",
        "pipeline_mode": "simple",
        "base_sha": "def456",
        "client_profile": "default",
        "opened_at": "2026-04-01T10:00:00+00:00",
    }
    data.update(overrides)
    return PrRunUpsert(**data)  # type: ignore[arg-type]


class TestEnsureSchema:
    def test_ensure_schema_creates_v1_on_fresh_db(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            version = ensure_schema(conn)
            assert version == 1
            row = conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            assert row["version"] == 1
        finally:
            conn.close()

    def test_ensure_schema_is_idempotent(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            ensure_schema(conn)
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
            assert len(rows) == 1
        finally:
            conn.close()


class TestPragmas:
    def test_wal_mode_enabled(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() == "wal"
        finally:
            conn.close()

    def test_foreign_keys_enabled(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert int(fk) == 1
        finally:
            conn.close()

    def test_synchronous_normal(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            # synchronous=NORMAL → integer value 1
            s = conn.execute("PRAGMA synchronous").fetchone()[0]
            assert int(s) == 1
        finally:
            conn.close()

    def test_busy_timeout_5000(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            t = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert int(t) == 5000
        finally:
            conn.close()


class TestSchemaShape:
    def test_pr_runs_has_client_profile_column(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(pr_runs)").fetchall()
            }
            assert "client_profile" in cols
        finally:
            conn.close()

    def test_client_profile_opened_at_index_exists(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_pr_runs_client_profile_opened_at'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()


class TestUpsertPrRun:
    def test_upsert_pr_run_inserts_new_row(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            assert pr_id > 0
            row = get_pr_run_by_unique(conn, "acme/app", 10, "abc123")
            assert row is not None
            assert row["ticket_id"] == "SCRUM-1"
            assert row["client_profile"] == "default"
        finally:
            conn.close()

    def test_upsert_pr_run_updates_existing_row(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            first_id = upsert_pr_run(conn, _base_upsert())
            second_id = upsert_pr_run(
                conn,
                _base_upsert(merged=1, merged_at="2026-04-02T11:00:00+00:00"),
            )
            assert first_id == second_id
            rows = conn.execute("SELECT * FROM pr_runs").fetchall()
            assert len(rows) == 1
            assert rows[0]["merged"] == 1
            assert rows[0]["merged_at"] == "2026-04-02T11:00:00+00:00"
        finally:
            conn.close()

    def test_upsert_pr_run_only_updates_provided_fields(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            upsert_pr_run(conn, _base_upsert(pipeline_mode="simple"))
            # Second upsert with empty pipeline_mode should NOT overwrite
            upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id="SCRUM-1",
                    pr_number=10,
                    repo_full_name="acme/app",
                    head_sha="abc123",
                    merged=1,
                ),
            )
            row = get_pr_run_by_unique(conn, "acme/app", 10, "abc123")
            assert row is not None
            assert row["pipeline_mode"] == "simple"
            assert row["merged"] == 1
            assert row["client_profile"] == "default"
        finally:
            conn.close()


class TestUniqueConstraint:
    def test_unique_triple_rejects_direct_duplicate_insert(self, db_path: Path) -> None:
        import sqlite3
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            upsert_pr_run(conn, _base_upsert())
            # Raw INSERT bypasses upsert — must hit UNIQUE constraint
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO pr_runs (ticket_id, pr_number, repo_full_name, "
                    "head_sha, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    ("SCRUM-1", 10, "acme/app", "abc123", "now", "now"),
                )
                conn.commit()
        finally:
            conn.close()


class TestPatchSemanticsAllTextFields:
    def test_empty_text_fields_never_overwrite(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            upsert_pr_run(
                conn,
                _base_upsert(
                    pr_url="https://x/1",
                    ticket_type="story",
                    pipeline_mode="simple",
                    base_sha="def456",
                    client_profile="acme",
                ),
            )
            # Second upsert with all text fields empty — none should clobber
            upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id="SCRUM-1",
                    pr_number=10,
                    repo_full_name="acme/app",
                    head_sha="abc123",
                    merged=1,
                ),
            )
            row = get_pr_run_by_unique(conn, "acme/app", 10, "abc123")
            assert row is not None
            assert row["pr_url"] == "https://x/1"
            assert row["ticket_type"] == "story"
            assert row["pipeline_mode"] == "simple"
            assert row["base_sha"] == "def456"
            assert row["client_profile"] == "acme"
            assert row["merged"] == 1
        finally:
            conn.close()


class TestListClientProfiles:
    def test_list_client_profiles_returns_distinct_non_empty(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            upsert_pr_run(conn, _base_upsert(pr_number=1, client_profile="acme"))
            upsert_pr_run(conn, _base_upsert(pr_number=2, client_profile="acme"))
            upsert_pr_run(conn, _base_upsert(pr_number=3, client_profile=""))
            upsert_pr_run(conn, _base_upsert(pr_number=4, client_profile="beta"))
            profiles = list_client_profiles(conn)
            assert profiles == ["acme", "beta"]
        finally:
            conn.close()


class TestListPrRuns:
    def test_list_pr_runs_filter_by_client_profile(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            upsert_pr_run(conn, _base_upsert(pr_number=1, client_profile="acme"))
            upsert_pr_run(conn, _base_upsert(pr_number=2, client_profile="beta"))
            rows = list_pr_runs(conn, client_profile="acme")
            assert len(rows) == 1
            assert rows[0]["pr_number"] == 1
        finally:
            conn.close()

    def test_list_pr_runs_filter_by_since_iso(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            upsert_pr_run(
                conn,
                _base_upsert(pr_number=1, opened_at="2026-03-01T10:00:00+00:00"),
            )
            upsert_pr_run(
                conn,
                _base_upsert(pr_number=2, opened_at="2026-04-02T10:00:00+00:00"),
            )
            rows = list_pr_runs(conn, since_iso="2026-04-01T00:00:00+00:00")
            assert len(rows) == 1
            assert rows[0]["pr_number"] == 2
        finally:
            conn.close()
