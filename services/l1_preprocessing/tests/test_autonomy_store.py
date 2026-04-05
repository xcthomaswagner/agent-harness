"""Tests for autonomy_store — schema migrations, pragmas, and repo helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from autonomy_store import (
    PrRunUpsert,
    drain_pending_ai_issues,
    ensure_schema,
    get_pr_run_by_unique,
    insert_issue_match,
    insert_pending_ai_issue,
    insert_review_issue,
    list_client_profiles,
    list_issue_matches_for_human,
    list_pr_runs,
    list_review_issues_by_pr_run,
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
            # v1 tables must exist after migration chain runs
            assert version >= 1
            row = conn.execute(
                "SELECT version FROM schema_version WHERE version = 1"
            ).fetchone()
            assert row is not None
            assert row["version"] == 1
        finally:
            conn.close()

    def test_ensure_schema_is_idempotent(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            first = ensure_schema(conn)
            ensure_schema(conn)
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
            # One row per applied migration; second call must not add more
            assert len(rows) == first
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


def _stage_pending(
    conn: object,
    **overrides: object,
) -> int:
    defaults: dict[str, object] = {
        "repo_full_name": "acme/app",
        "head_sha": "abc123",
        "ticket_id": "SCRUM-1",
        "source": "ai_review",
        "external_id": "cr-1",
        "file_path": "src/foo.py",
        "line_start": 10,
        "line_end": 12,
        "category": "bug",
        "severity": "minor",
        "summary": "issue summary",
        "details": "details",
        "acceptance_criterion_ref": "",
        "is_valid": 1,
        "is_code_change_request": 1,
    }
    defaults.update(overrides)
    return insert_pending_ai_issue(conn, **defaults)  # type: ignore[arg-type]


class TestSchemaV2:
    def test_migration_runs_v2_after_v1(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            version = ensure_schema(conn)
            assert version == 2
            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
            assert row["v"] == 2
            versions = [
                r["version"]
                for r in conn.execute(
                    "SELECT version FROM schema_version ORDER BY version"
                ).fetchall()
            ]
            assert versions == [1, 2]
        finally:
            conn.close()

    def test_v2_migration_idempotent(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            assert ensure_schema(conn) == 2
            assert ensure_schema(conn) == 2
            rows = conn.execute(
                "SELECT version FROM schema_version"
            ).fetchall()
            assert len(rows) == 2
        finally:
            conn.close()

    def test_pending_ai_issues_table_exists(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(pending_ai_issues)"
                ).fetchall()
            }
            assert "repo_full_name" in cols
            assert "head_sha" in cols
            assert "ticket_id" in cols
            assert "source" in cols
            assert "external_id" in cols
            assert "summary" in cols
            assert "is_code_change_request" in cols
        finally:
            conn.close()

    def test_pending_ai_issues_unique_constraint(self, db_path: Path) -> None:
        import sqlite3
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO pending_ai_issues (repo_full_name, head_sha, "
                "ticket_id, source, external_id, summary, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("acme/app", "abc", "T-1", "ai_review", "cr-1", "s", "now"),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO pending_ai_issues (repo_full_name, head_sha, "
                    "ticket_id, source, external_id, summary, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ("acme/app", "abc", "T-1", "ai_review", "cr-1", "s2", "now"),
                )
                conn.commit()
        finally:
            conn.close()

    def test_pr_runs_backfilled_index_exists(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_pr_runs_backfilled'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()


class TestInsertPendingAiIssue:
    def test_insert_pending_ai_issue(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            row_id = _stage_pending(conn)
            assert row_id > 0
            row = conn.execute(
                "SELECT * FROM pending_ai_issues WHERE id = ?", (row_id,)
            ).fetchone()
            assert row is not None
            assert row["ticket_id"] == "SCRUM-1"
            assert row["source"] == "ai_review"
            assert row["external_id"] == "cr-1"
            assert row["summary"] == "issue summary"
        finally:
            conn.close()

    def test_insert_pending_ai_issue_conflict_updates(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            first = _stage_pending(conn, summary="original")
            second = _stage_pending(conn, summary="updated")
            assert first == second
            rows = conn.execute(
                "SELECT * FROM pending_ai_issues"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["summary"] == "updated"
        finally:
            conn.close()


class TestDrainPendingAiIssues:
    def test_drain_pending_ai_issues_moves_rows(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            _stage_pending(conn, external_id="cr-1")
            _stage_pending(conn, external_id="cr-2", source="qa")
            moved = drain_pending_ai_issues(
                conn,
                repo_full_name="acme/app",
                head_sha="abc123",
                ticket_id="SCRUM-1",
                pr_run_id=pr_id,
            )
            assert moved == 2
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS c FROM review_issues"
                ).fetchone()["c"]
                == 2
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS c FROM pending_ai_issues"
                ).fetchone()["c"]
                == 0
            )
        finally:
            conn.close()

    def test_drain_pending_ai_issues_idempotent(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            _stage_pending(conn, external_id="cr-1")
            first = drain_pending_ai_issues(
                conn,
                repo_full_name="acme/app",
                head_sha="abc123",
                ticket_id="SCRUM-1",
                pr_run_id=pr_id,
            )
            second = drain_pending_ai_issues(
                conn,
                repo_full_name="acme/app",
                head_sha="abc123",
                ticket_id="SCRUM-1",
                pr_run_id=pr_id,
            )
            assert first == 1
            assert second == 0
            # Re-stage same pending row; drain should skip re-insert into
            # review_issues (since one already exists for that key) but
            # still clear the pending row.
            _stage_pending(conn, external_id="cr-1")
            third = drain_pending_ai_issues(
                conn,
                repo_full_name="acme/app",
                head_sha="abc123",
                ticket_id="SCRUM-1",
                pr_run_id=pr_id,
            )
            assert third == 0
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS c FROM pending_ai_issues"
                ).fetchone()["c"]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS c FROM review_issues"
                ).fetchone()["c"]
                == 1
            )
        finally:
            conn.close()

    def test_drain_only_moves_matching_triple(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            _stage_pending(conn, head_sha="abc123", external_id="cr-1")
            _stage_pending(conn, head_sha="zzz999", external_id="cr-1")
            moved = drain_pending_ai_issues(
                conn,
                repo_full_name="acme/app",
                head_sha="abc123",
                ticket_id="SCRUM-1",
                pr_run_id=pr_id,
            )
            assert moved == 1
            remaining = conn.execute(
                "SELECT head_sha FROM pending_ai_issues"
            ).fetchall()
            assert len(remaining) == 1
            assert remaining[0]["head_sha"] == "zzz999"
        finally:
            conn.close()


class TestInsertReviewIssue:
    def test_insert_review_issue_returns_id(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            rid = insert_review_issue(
                conn,
                pr_run_id=pr_id,
                source="human",
                summary="reviewer comment",
            )
            assert rid > 0
            row = conn.execute(
                "SELECT * FROM review_issues WHERE id = ?", (rid,)
            ).fetchone()
            assert row is not None
            assert row["summary"] == "reviewer comment"
            assert row["status"] == "open"
        finally:
            conn.close()


class TestListReviewIssuesByPrRun:
    def test_list_review_issues_by_pr_run_filter_by_source(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            insert_review_issue(
                conn, pr_run_id=pr_id, source="human", summary="h1"
            )
            insert_review_issue(
                conn, pr_run_id=pr_id, source="ai_review", summary="a1"
            )
            insert_review_issue(
                conn, pr_run_id=pr_id, source="ai_review", summary="a2"
            )
            all_rows = list_review_issues_by_pr_run(conn, pr_id)
            assert len(all_rows) == 3
            ai_rows = list_review_issues_by_pr_run(
                conn, pr_id, source="ai_review"
            )
            assert len(ai_rows) == 2
            assert {r["summary"] for r in ai_rows} == {"a1", "a2"}
        finally:
            conn.close()


class TestIssueMatches:
    def test_insert_issue_match_unique(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            human_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="human", summary="h"
            )
            ai_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="ai_review", summary="a"
            )
            first = insert_issue_match(
                conn,
                human_issue_id=human_id,
                ai_issue_id=ai_id,
                match_type="exact",
                confidence=0.95,
            )
            assert first > 0
            second = insert_issue_match(
                conn,
                human_issue_id=human_id,
                ai_issue_id=ai_id,
                match_type="exact",
                confidence=0.95,
            )
            assert second == 0
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM issue_matches"
            ).fetchone()["c"]
            assert count == 1
        finally:
            conn.close()

    def test_list_issue_matches_for_human(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            human_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="human", summary="h"
            )
            ai1 = insert_review_issue(
                conn, pr_run_id=pr_id, source="ai_review", summary="a1"
            )
            ai2 = insert_review_issue(
                conn, pr_run_id=pr_id, source="ai_review", summary="a2"
            )
            other_human = insert_review_issue(
                conn, pr_run_id=pr_id, source="human", summary="h2"
            )
            insert_issue_match(
                conn,
                human_issue_id=human_id,
                ai_issue_id=ai1,
                match_type="exact",
                confidence=0.9,
            )
            insert_issue_match(
                conn,
                human_issue_id=human_id,
                ai_issue_id=ai2,
                match_type="fuzzy",
                confidence=0.7,
            )
            insert_issue_match(
                conn,
                human_issue_id=other_human,
                ai_issue_id=ai1,
                match_type="exact",
                confidence=0.8,
            )
            matches = list_issue_matches_for_human(conn, human_id)
            assert len(matches) == 2
            assert {m["ai_issue_id"] for m in matches} == {ai1, ai2}
        finally:
            conn.close()
