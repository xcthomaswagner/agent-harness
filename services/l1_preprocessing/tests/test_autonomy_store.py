"""Tests for autonomy_store — schema migrations, pragmas, and repo helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from autonomy_store import (
    PrRunUpsert,
    count_merged_pr_runs_with_escape,
    create_manual_match,
    drain_pending_ai_issues,
    ensure_schema,
    find_latest_merged_pr_run_by_ticket,
    get_defect_link,
    get_latest_defect_sweep_heartbeat,
    get_pr_run_by_unique,
    insert_defect_link,
    insert_issue_match,
    insert_pending_ai_issue,
    insert_review_issue,
    list_client_profiles,
    list_confirmed_escaped_defects,
    list_issue_matches_for_human,
    list_pr_runs,
    list_review_issues_by_pr_run,
    open_connection,
    promote_match_to_counted,
    record_defect_sweep_heartbeat,
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
            assert version >= 2  # v2+ migrations have run
            versions = [
                r["version"]
                for r in conn.execute(
                    "SELECT version FROM schema_version ORDER BY version"
                ).fetchall()
            ]
            assert 1 in versions
            assert 2 in versions
        finally:
            conn.close()

    def test_v2_migration_idempotent(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            first = ensure_schema(conn)
            assert ensure_schema(conn) == first
            rows = conn.execute(
                "SELECT version FROM schema_version"
            ).fetchall()
            assert len(rows) == first
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


def _merged_pr(
    conn: object,
    *,
    pr_number: int = 10,
    head_sha: str = "abc123",
    merged_at: str = "2026-03-01T12:00:00+00:00",
    client_profile: str = "default",
) -> int:
    """Create a merged pr_runs row and return its id."""
    return upsert_pr_run(
        conn,  # type: ignore[arg-type]
        _base_upsert(
            pr_number=pr_number,
            head_sha=head_sha,
            merged=1,
            merged_at=merged_at,
            client_profile=client_profile,
        ),
    )


class TestSchemaV3:
    def test_v3_migration_adds_category_column(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            version = ensure_schema(conn)
            assert version >= 3
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(defect_links)"
                ).fetchall()
            }
            assert "category" in cols
        finally:
            conn.close()

    def test_v3_migration_adds_defect_links_unique_index(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_defect_links_uniq'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_v3_migration_idempotent(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            first = ensure_schema(conn)
            assert first >= 3
            assert ensure_schema(conn) == first
            rows = conn.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ).fetchall()
            versions = [r["version"] for r in rows]
            assert 1 in versions and 2 in versions and 3 in versions
            assert len(versions) == first
        finally:
            conn.close()


class TestInsertDefectLink:
    def test_insert_defect_link_returns_id_and_row(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = _merged_pr(conn)
            dl_id = insert_defect_link(
                conn,
                pr_run_id=pr_id,
                defect_key="BUG-1",
                source="jira",
                reported_at="2026-03-05T09:00:00+00:00",
                severity="major",
                notes="customer report",
            )
            assert dl_id > 0
            row = get_defect_link(conn, pr_id, "BUG-1", "jira")
            assert row is not None
            assert row["severity"] == "major"
            assert row["notes"] == "customer report"
            assert row["category"] == "escaped"
            assert row["confirmed"] == 1
        finally:
            conn.close()

    def test_insert_defect_link_conflict_upserts(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = _merged_pr(conn)
            first = insert_defect_link(
                conn,
                pr_run_id=pr_id,
                defect_key="BUG-1",
                source="jira",
                reported_at="2026-03-05T09:00:00+00:00",
                severity="minor",
                notes="initial",
            )
            second = insert_defect_link(
                conn,
                pr_run_id=pr_id,
                defect_key="BUG-1",
                source="jira",
                reported_at="2026-03-06T10:00:00+00:00",
                severity="major",
                notes="updated",
                confirmed=1,
                category="pre_existing",
            )
            assert first == second
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM defect_links"
            ).fetchone()["c"]
            assert count == 1
            row = get_defect_link(conn, pr_id, "BUG-1", "jira")
            assert row is not None
            assert row["severity"] == "major"
            assert row["notes"] == "updated"
            assert row["reported_at"] == "2026-03-06T10:00:00+00:00"
            assert row["category"] == "pre_existing"
        finally:
            conn.close()

    def test_insert_defect_link_defaults_category_escaped(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = _merged_pr(conn)
            insert_defect_link(
                conn,
                pr_run_id=pr_id,
                defect_key="BUG-9",
                source="manual",
                reported_at="2026-03-05T09:00:00+00:00",
            )
            row = get_defect_link(conn, pr_id, "BUG-9", "manual")
            assert row is not None
            assert row["category"] == "escaped"
        finally:
            conn.close()

    def test_list_defect_links_for_profile_filters_before_limit(
        self, db_path: Path
    ) -> None:
        """Bug regression: before the fix, list_defect_links_for_profile
        applied LIMIT in SQL before any confirmed/category filter, so
        the Python-side ``[r for r in rows if confirmed=1 and
        category='escaped']`` in the dashboard could see an empty set
        if more-recent non-escaped rows filled the SQL LIMIT window.
        Fix pushes the filter into SQL via optional confirmed/category
        kwargs. This test plants 3 non-escaped rows that would have
        filled a LIMIT=2 plus 1 escaped row; the old code would have
        returned 0 escaped rows — the new code must return 1."""
        from autonomy_store import list_defect_links_for_profile

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = _merged_pr(conn, client_profile="rockwell")
            # Three most-recent non-escaped rows.
            for i, ts in enumerate(
                [
                    "2026-04-05T00:00:00+00:00",
                    "2026-04-04T00:00:00+00:00",
                    "2026-04-03T00:00:00+00:00",
                ]
            ):
                insert_defect_link(
                    conn,
                    pr_run_id=pr_id,
                    defect_key=f"PRE-{i}",
                    source="jira",
                    reported_at=ts,
                    category="pre_existing",
                )
            # One older escaped row.
            insert_defect_link(
                conn,
                pr_run_id=pr_id,
                defect_key="ESC-1",
                source="jira",
                reported_at="2026-04-01T00:00:00+00:00",
                category="escaped",
            )

            # With the pre-fix behavior (no filter), the top-2 rows
            # would both be pre_existing and the escaped row would be
            # dropped. With the fix, SQL filters first and LIMIT=2
            # still sees the escaped row.
            rows = list_defect_links_for_profile(
                conn,
                "rockwell",
                limit=2,
                confirmed=1,
                category="escaped",
            )
            assert len(rows) == 1
            assert rows[0]["defect_key"] == "ESC-1"
        finally:
            conn.close()


class TestListConfirmedEscapedDefects:
    def test_in_window_counts(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = _merged_pr(
                conn, merged_at="2026-03-01T00:00:00+00:00"
            )
            insert_defect_link(
                conn,
                pr_run_id=pr_id,
                defect_key="BUG-1",
                source="jira",
                reported_at="2026-03-15T00:00:00+00:00",
            )
            rows = list_confirmed_escaped_defects(
                conn, [pr_id], window_days=30
            )
            assert len(rows) == 1
            assert rows[0]["defect_key"] == "BUG-1"
            assert rows[0]["pr_number"] == 10
        finally:
            conn.close()

    def test_outside_window_excluded(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = _merged_pr(
                conn, merged_at="2026-03-01T00:00:00+00:00"
            )
            insert_defect_link(
                conn,
                pr_run_id=pr_id,
                defect_key="BUG-1",
                source="jira",
                reported_at="2026-04-15T00:00:00+00:00",
            )
            rows = list_confirmed_escaped_defects(
                conn, [pr_id], window_days=30
            )
            assert rows == []
        finally:
            conn.close()

    def test_unconfirmed_excluded(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = _merged_pr(
                conn, merged_at="2026-03-01T00:00:00+00:00"
            )
            insert_defect_link(
                conn,
                pr_run_id=pr_id,
                defect_key="BUG-1",
                source="jira",
                reported_at="2026-03-10T00:00:00+00:00",
                confirmed=0,
            )
            rows = list_confirmed_escaped_defects(
                conn, [pr_id], window_days=30
            )
            assert rows == []
        finally:
            conn.close()

    def test_non_escape_category_excluded(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = _merged_pr(
                conn, merged_at="2026-03-01T00:00:00+00:00"
            )
            insert_defect_link(
                conn,
                pr_run_id=pr_id,
                defect_key="BUG-1",
                source="jira",
                reported_at="2026-03-10T00:00:00+00:00",
                category="pre_existing",
            )
            rows = list_confirmed_escaped_defects(
                conn, [pr_id], window_days=30
            )
            assert rows == []
        finally:
            conn.close()

    def test_not_merged_excluded(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            # Create an unmerged PR run directly
            pr_id = upsert_pr_run(conn, _base_upsert())
            insert_defect_link(
                conn,
                pr_run_id=pr_id,
                defect_key="BUG-1",
                source="jira",
                reported_at="2026-04-10T00:00:00+00:00",
            )
            rows = list_confirmed_escaped_defects(
                conn, [pr_id], window_days=30
            )
            assert rows == []
        finally:
            conn.close()


class TestCountMergedPrRunsWithEscape:
    def test_counts_distinct_pr_runs(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr1 = _merged_pr(
                conn,
                pr_number=10,
                head_sha="sha1",
                merged_at="2026-03-01T00:00:00+00:00",
            )
            pr2 = _merged_pr(
                conn,
                pr_number=11,
                head_sha="sha2",
                merged_at="2026-03-01T00:00:00+00:00",
            )
            insert_defect_link(
                conn,
                pr_run_id=pr1,
                defect_key="BUG-1",
                source="jira",
                reported_at="2026-03-05T00:00:00+00:00",
            )
            insert_defect_link(
                conn,
                pr_run_id=pr1,
                defect_key="BUG-2",
                source="jira",
                reported_at="2026-03-07T00:00:00+00:00",
            )
            insert_defect_link(
                conn,
                pr_run_id=pr2,
                defect_key="BUG-3",
                source="jira",
                reported_at="2026-03-10T00:00:00+00:00",
            )
            count = count_merged_pr_runs_with_escape(
                conn, [pr1, pr2], window_days=30
            )
            assert count == 2
            # Only pr1
            count1 = count_merged_pr_runs_with_escape(
                conn, [pr1], window_days=30
            )
            assert count1 == 1
        finally:
            conn.close()


class TestPromoteMatch:
    def test_promote_flips_matched_by(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            human_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="human_review", summary="h"
            )
            ai_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="ai_review", summary="a"
            )
            match_id = insert_issue_match(
                conn,
                human_issue_id=human_id,
                ai_issue_id=ai_id,
                match_type="fuzzy",
                confidence=0.6,
                matched_by="suggested",
            )
            assert match_id > 0
            ok = promote_match_to_counted(conn, match_id=match_id)
            assert ok is True
            row = conn.execute(
                "SELECT matched_by, confidence FROM issue_matches WHERE id = ?",
                (match_id,),
            ).fetchone()
            assert row["matched_by"] == "manual"
            assert float(row["confidence"]) == 1.0
            # Audit row written
            audit = conn.execute(
                "SELECT * FROM manual_overrides WHERE override_type = 'promote_match'"
            ).fetchone()
            assert audit is not None
            assert audit["target_id"] == str(match_id)
        finally:
            conn.close()

    def test_promote_returns_false_for_nonexistent(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            assert promote_match_to_counted(conn, match_id=9999) is False
        finally:
            conn.close()

    def test_promote_returns_false_for_already_system(
        self, db_path: Path
    ) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            human_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="human_review", summary="h"
            )
            ai_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="ai_review", summary="a"
            )
            match_id = insert_issue_match(
                conn,
                human_issue_id=human_id,
                ai_issue_id=ai_id,
                match_type="exact",
                confidence=0.95,
                matched_by="system",
            )
            assert promote_match_to_counted(conn, match_id=match_id) is False
            row = conn.execute(
                "SELECT matched_by FROM issue_matches WHERE id = ?",
                (match_id,),
            ).fetchone()
            assert row["matched_by"] == "system"
        finally:
            conn.close()


class TestCreateManualMatch:
    def test_create_manual_match_inserts_row(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            human_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="human_review", summary="h"
            )
            ai_id = insert_review_issue(
                conn,
                pr_run_id=pr_id,
                source="ai_review",
                summary="a",
                is_valid=1,
            )
            match_id = create_manual_match(
                conn, human_issue_id=human_id, ai_issue_id=ai_id
            )
            assert match_id > 0
            row = conn.execute(
                "SELECT * FROM issue_matches WHERE id = ?", (match_id,)
            ).fetchone()
            assert row["match_type"] == "manual"
            assert float(row["confidence"]) == 1.0
            assert row["matched_by"] == "manual"
        finally:
            conn.close()

    def test_rejects_cross_pr_run(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr1 = upsert_pr_run(conn, _base_upsert(pr_number=1))
            pr2 = upsert_pr_run(conn, _base_upsert(pr_number=2))
            human_id = insert_review_issue(
                conn, pr_run_id=pr1, source="human_review", summary="h"
            )
            ai_id = insert_review_issue(
                conn, pr_run_id=pr2, source="ai_review", summary="a"
            )
            with pytest.raises(ValueError):
                create_manual_match(
                    conn, human_issue_id=human_id, ai_issue_id=ai_id
                )
        finally:
            conn.close()

    def test_rejects_invalid_ai_issue(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            human_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="human_review", summary="h"
            )
            ai_id = insert_review_issue(
                conn,
                pr_run_id=pr_id,
                source="ai_review",
                summary="a",
                is_valid=0,
            )
            with pytest.raises(ValueError):
                create_manual_match(
                    conn, human_issue_id=human_id, ai_issue_id=ai_id
                )
        finally:
            conn.close()

    def test_rejects_non_human_source(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            ai1 = insert_review_issue(
                conn, pr_run_id=pr_id, source="ai_review", summary="a1"
            )
            ai2 = insert_review_issue(
                conn, pr_run_id=pr_id, source="ai_review", summary="a2"
            )
            with pytest.raises(ValueError):
                create_manual_match(
                    conn, human_issue_id=ai1, ai_issue_id=ai2
                )
        finally:
            conn.close()

    def test_also_logs_manual_override(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            pr_id = upsert_pr_run(conn, _base_upsert())
            human_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="human_review", summary="h"
            )
            ai_id = insert_review_issue(
                conn, pr_run_id=pr_id, source="ai_review", summary="a"
            )
            match_id = create_manual_match(
                conn, human_issue_id=human_id, ai_issue_id=ai_id
            )
            audit = conn.execute(
                "SELECT * FROM manual_overrides "
                "WHERE override_type = 'create_manual_match'"
            ).fetchone()
            assert audit is not None
            assert audit["target_id"] == str(match_id)
        finally:
            conn.close()


class TestHeartbeat:
    def test_record_and_get_heartbeat_roundtrip(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            record_defect_sweep_heartbeat(
                conn,
                client_profile="acme",
                swept_through_iso="2026-04-01T00:00:00+00:00",
            )
            value = get_latest_defect_sweep_heartbeat(conn, "acme")
            assert value == "2026-04-01T00:00:00+00:00"
        finally:
            conn.close()

    def test_get_returns_latest_when_multiple(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            record_defect_sweep_heartbeat(
                conn,
                client_profile="acme",
                swept_through_iso="2026-03-01T00:00:00+00:00",
            )
            record_defect_sweep_heartbeat(
                conn,
                client_profile="acme",
                swept_through_iso="2026-04-01T00:00:00+00:00",
            )
            record_defect_sweep_heartbeat(
                conn,
                client_profile="beta",
                swept_through_iso="2026-05-01T00:00:00+00:00",
            )
            assert (
                get_latest_defect_sweep_heartbeat(conn, "acme")
                == "2026-04-01T00:00:00+00:00"
            )
            assert (
                get_latest_defect_sweep_heartbeat(conn, "beta")
                == "2026-05-01T00:00:00+00:00"
            )
        finally:
            conn.close()

    def test_get_returns_none_when_absent(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            assert get_latest_defect_sweep_heartbeat(conn, "acme") is None
        finally:
            conn.close()


class TestFindLatestMergedPrRunByTicket:
    def test_returns_none_when_no_rows(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            assert find_latest_merged_pr_run_by_ticket(conn, "PROJ-1") is None
        finally:
            conn.close()

    def test_returns_none_when_unmerged(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            upsert_pr_run(
                conn,
                _base_upsert(
                    ticket_id="PROJ-1",
                    pr_number=1,
                    head_sha="sha1",
                    merged=0,
                    opened_at="2026-03-01T12:00:00+00:00",
                ),
            )
            assert find_latest_merged_pr_run_by_ticket(conn, "PROJ-1") is None
        finally:
            conn.close()

    def test_picks_latest_by_merged_at(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            upsert_pr_run(
                conn,
                _base_upsert(
                    ticket_id="PROJ-1",
                    pr_number=1,
                    head_sha="sha_old",
                    merged=1,
                    merged_at="2026-02-01T12:00:00+00:00",
                ),
            )
            upsert_pr_run(
                conn,
                _base_upsert(
                    ticket_id="PROJ-1",
                    pr_number=2,
                    head_sha="sha_new",
                    merged=1,
                    merged_at="2026-03-10T12:00:00+00:00",
                ),
            )
            upsert_pr_run(
                conn,
                _base_upsert(
                    ticket_id="PROJ-1",
                    pr_number=3,
                    head_sha="sha_mid",
                    merged=1,
                    merged_at="2026-03-05T12:00:00+00:00",
                ),
            )
            row = find_latest_merged_pr_run_by_ticket(conn, "PROJ-1")
            assert row is not None
            assert row["head_sha"] == "sha_new"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Phase 4: auto-merge decisions + toggle
# ---------------------------------------------------------------------------

class TestAutoMergeDecisions:
    def test_record_auto_merge_decision_writes_row(self, db_path: Path) -> None:
        from autonomy_store import record_auto_merge_decision

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            decision_id = record_auto_merge_decision(
                conn,
                repo_full_name="acme/widgets",
                pr_number=42,
                decision="merged",
                reason="all gates passed",
                payload={"client_profile": "rockwell", "gates": {"ci": True}},
            )
            assert decision_id > 0
            row = conn.execute(
                "SELECT * FROM manual_overrides WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row["override_type"] == "auto_merge_decision"
            assert row["target_id"] == "acme/widgets#42"
            assert row["created_by"] == "l3_auto_merge"
            import json as _json
            payload = _json.loads(row["payload_json"])
            assert payload["decision"] == "merged"
            assert payload["reason"] == "all gates passed"
            assert payload["client_profile"] == "rockwell"
        finally:
            conn.close()

    def test_get_auto_merge_toggle_returns_none_when_absent(
        self, db_path: Path
    ) -> None:
        from autonomy_store import get_auto_merge_toggle

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            assert get_auto_merge_toggle(conn, "rockwell") is None
        finally:
            conn.close()

    def test_set_and_get_auto_merge_toggle_roundtrip(
        self, db_path: Path
    ) -> None:
        from autonomy_store import get_auto_merge_toggle, set_auto_merge_toggle

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            set_auto_merge_toggle(conn, client_profile="rockwell", enabled=True)
            assert get_auto_merge_toggle(conn, "rockwell") is True
        finally:
            conn.close()

    def test_toggle_latest_wins(self, db_path: Path) -> None:
        from autonomy_store import get_auto_merge_toggle, set_auto_merge_toggle

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            set_auto_merge_toggle(conn, client_profile="p", enabled=True)
            set_auto_merge_toggle(conn, client_profile="p", enabled=False)
            assert get_auto_merge_toggle(conn, "p") is False
        finally:
            conn.close()

    def test_list_recent_auto_merge_decisions_ordered_desc(
        self, db_path: Path
    ) -> None:
        from autonomy_store import (
            list_recent_auto_merge_decisions,
            record_auto_merge_decision,
        )

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            id1 = record_auto_merge_decision(
                conn,
                repo_full_name="a/b",
                pr_number=1,
                decision="merged",
                reason="ok",
                payload={},
            )
            id2 = record_auto_merge_decision(
                conn,
                repo_full_name="a/b",
                pr_number=2,
                decision="skipped",
                reason="gate failed",
                payload={},
            )
            rows = list_recent_auto_merge_decisions(conn, limit=10)
            assert len(rows) == 2
            assert rows[0]["id"] == id2
            assert rows[1]["id"] == id1
        finally:
            conn.close()

    def test_list_recent_filters_by_since_iso(self, db_path: Path) -> None:
        from autonomy_store import (
            list_recent_auto_merge_decisions,
            record_auto_merge_decision,
        )

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            record_auto_merge_decision(
                conn,
                repo_full_name="a/b",
                pr_number=1,
                decision="merged",
                reason="ok",
                payload={},
            )
            # since_iso far in the future → no rows
            future = "2099-01-01T00:00:00+00:00"
            rows = list_recent_auto_merge_decisions(conn, since_iso=future)
            assert rows == []
            # since_iso far in the past → row included
            past = "1999-01-01T00:00:00+00:00"
            rows = list_recent_auto_merge_decisions(conn, since_iso=past)
            assert len(rows) == 1
        finally:
            conn.close()

    def test_list_recent_filters_by_repo_full_name(self, db_path: Path) -> None:
        from autonomy_store import (
            list_recent_auto_merge_decisions,
            record_auto_merge_decision,
        )

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            record_auto_merge_decision(
                conn,
                repo_full_name="a/b",
                pr_number=1,
                decision="merged",
                reason="ok",
                payload={},
            )
            record_auto_merge_decision(
                conn,
                repo_full_name="c/d",
                pr_number=7,
                decision="skipped",
                reason="nope",
                payload={},
            )
            rows = list_recent_auto_merge_decisions(
                conn, repo_full_name="a/b"
            )
            assert len(rows) == 1
            assert rows[0]["target_id"] == "a/b#1"
        finally:
            conn.close()

    def test_list_recent_filters_by_client_profile(self, db_path: Path) -> None:
        from autonomy_store import (
            list_recent_auto_merge_decisions,
            record_auto_merge_decision,
        )

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            record_auto_merge_decision(
                conn,
                repo_full_name="a/b",
                pr_number=1,
                decision="merged",
                reason="ok",
                payload={"client_profile": "rockwell"},
            )
            record_auto_merge_decision(
                conn,
                repo_full_name="c/d",
                pr_number=2,
                decision="merged",
                reason="ok",
                payload={"client_profile": "harness-test"},
            )
            rows = list_recent_auto_merge_decisions(
                conn, client_profile="rockwell"
            )
            assert len(rows) == 1
            assert rows[0]["target_id"] == "a/b#1"
        finally:
            conn.close()

    def test_list_recent_client_profile_filter_escapes_like_wildcards(
        self, db_path: Path
    ) -> None:
        """Bug regression: ``client_profile`` was spliced into a LIKE
        pattern with no escaping. SQLite LIKE treats ``_`` as "any
        single character" and ``%`` as "any sequence", so a caller
        passing ``_`` or ``foo_bar`` would match across profiles —
        turning a per-profile filter into a data leak. Fixed by
        escaping ``%`` / ``_`` / ``\\`` and using
        ``LIKE ? ESCAPE '\\'``."""
        from autonomy_store import (
            list_recent_auto_merge_decisions,
            record_auto_merge_decision,
        )

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            # Two profiles with names that differ only by the character
            # a single ``_`` would match.
            record_auto_merge_decision(
                conn,
                repo_full_name="a/b",
                pr_number=1,
                decision="merged",
                reason="ok",
                payload={"client_profile": "acme"},
            )
            record_auto_merge_decision(
                conn,
                repo_full_name="c/d",
                pr_number=2,
                decision="merged",
                reason="ok",
                payload={"client_profile": "xcme"},
            )

            # Before the fix, ``_cme`` would match both profiles via
            # the LIKE wildcard. After the fix, it matches neither
            # (the underscore is escaped to a literal underscore).
            rows = list_recent_auto_merge_decisions(
                conn, client_profile="_cme"
            )
            assert rows == []

            # A single ``%`` used to match every row. After the fix,
            # it matches none.
            rows = list_recent_auto_merge_decisions(
                conn, client_profile="%"
            )
            assert rows == []

            # Legitimate literal names with underscores still match
            # correctly — seed one and fetch by its exact name.
            record_auto_merge_decision(
                conn,
                repo_full_name="e/f",
                pr_number=3,
                decision="merged",
                reason="ok",
                payload={"client_profile": "first_pass_acceptance"},
            )
            rows = list_recent_auto_merge_decisions(
                conn, client_profile="first_pass_acceptance"
            )
            assert len(rows) == 1
            assert rows[0]["target_id"] == "e/f#3"
        finally:
            conn.close()

    def test_list_recent_repo_full_name_filter_escapes_like_wildcards(
        self, db_path: Path
    ) -> None:
        """Bug regression: ``repo_full_name`` had the same LIKE-wildcard
        leak as ``client_profile`` — a caller passing ``o/r_po`` would
        match any repo whose 5th char was anything."""
        from autonomy_store import (
            list_recent_auto_merge_decisions,
            record_auto_merge_decision,
        )

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            record_auto_merge_decision(
                conn,
                repo_full_name="o/repo",
                pr_number=1,
                decision="merged",
                reason="ok",
                payload={"client_profile": "a"},
            )
            record_auto_merge_decision(
                conn,
                repo_full_name="o/rxpo",
                pr_number=2,
                decision="merged",
                reason="ok",
                payload={"client_profile": "a"},
            )

            # Before the fix, ``o/r_po`` would match both. After, it
            # matches neither.
            rows = list_recent_auto_merge_decisions(
                conn, repo_full_name="o/r_po"
            )
            assert rows == []

            # Exact literal still works.
            rows = list_recent_auto_merge_decisions(
                conn, repo_full_name="o/repo"
            )
            assert len(rows) == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# v4: pr_commits + helpers
# ---------------------------------------------------------------------------


class TestV4Migration:
    def test_v4_migration_creates_pr_commits(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            version = ensure_schema(conn)
            assert version >= 4
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='pr_commits'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_v4_migration_idempotent(self, db_path: Path) -> None:
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            ensure_schema(conn)
            rows = conn.execute(
                "SELECT version FROM schema_version WHERE version = 4"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()


class TestPrCommits:
    def _setup(self, db_path: Path):
        from autonomy_store import upsert_pr_run as _upsert
        conn = open_connection(db_path)
        ensure_schema(conn)
        pr_run_id = _upsert(conn, _base_upsert())
        return conn, pr_run_id

    def test_insert_pr_commit_idempotent_on_unique(
        self, db_path: Path
    ) -> None:
        from autonomy_store import insert_pr_commit, list_pr_commits
        conn, pr_run_id = self._setup(db_path)
        try:
            id1 = insert_pr_commit(
                conn,
                pr_run_id=pr_run_id,
                sha="abc",
                committed_at="2026-04-05T12:00:00+00:00",
            )
            id2 = insert_pr_commit(
                conn,
                pr_run_id=pr_run_id,
                sha="abc",
                committed_at="2026-04-05T12:30:00+00:00",
            )
            assert id1 == id2
            assert len(list_pr_commits(conn, pr_run_id)) == 1
        finally:
            conn.close()

    def test_list_pr_commits_ordered_by_committed_at(
        self, db_path: Path
    ) -> None:
        from autonomy_store import insert_pr_commit, list_pr_commits
        conn, pr_run_id = self._setup(db_path)
        try:
            insert_pr_commit(
                conn,
                pr_run_id=pr_run_id,
                sha="b",
                committed_at="2026-04-05T13:00:00+00:00",
            )
            insert_pr_commit(
                conn,
                pr_run_id=pr_run_id,
                sha="a",
                committed_at="2026-04-05T12:00:00+00:00",
            )
            insert_pr_commit(
                conn,
                pr_run_id=pr_run_id,
                sha="c",
                committed_at="2026-04-05T14:00:00+00:00",
            )
            rows = list_pr_commits(conn, pr_run_id)
            assert [r["sha"] for r in rows] == ["a", "b", "c"]
        finally:
            conn.close()

    def test_list_human_issues_for_pr_run_ordered(
        self, db_path: Path
    ) -> None:
        from autonomy_store import list_human_issues_for_pr_run
        conn, pr_run_id = self._setup(db_path)
        try:
            # Insert out-of-order human issues using direct SQL to control created_at.
            for ext, created_at in [
                ("c2", "2026-04-05T12:30:00+00:00"),
                ("c1", "2026-04-05T12:00:00+00:00"),
                ("c3", "2026-04-05T13:00:00+00:00"),
            ]:
                conn.execute(
                    "INSERT INTO review_issues (pr_run_id, source, external_id, "
                    "created_at, summary) VALUES (?, 'human_review', ?, ?, ?)",
                    (pr_run_id, ext, created_at, "s"),
                )
            conn.commit()
            # Also add a non-human issue to confirm filtering.
            conn.execute(
                "INSERT INTO review_issues (pr_run_id, source, external_id, "
                "created_at, summary) VALUES (?, 'ai_review', 'x', ?, ?)",
                (pr_run_id, "2026-04-05T11:00:00+00:00", "ai"),
            )
            conn.commit()
            rows = list_human_issues_for_pr_run(conn, pr_run_id)
            assert [r["external_id"] for r in rows] == ["c1", "c2", "c3"]
        finally:
            conn.close()

    def test_set_human_issue_code_change_flag(self, db_path: Path) -> None:
        from autonomy_store import set_human_issue_code_change_flag
        conn, pr_run_id = self._setup(db_path)
        try:
            issue_id = insert_review_issue(
                conn,
                pr_run_id=pr_run_id,
                source="human_review",
                external_id="c1",
                summary="comment",
                is_code_change_request=0,
            )
            set_human_issue_code_change_flag(conn, issue_id, 1)
            row = conn.execute(
                "SELECT is_code_change_request FROM review_issues WHERE id = ?",
                (issue_id,),
            ).fetchone()
            assert int(row["is_code_change_request"]) == 1
            set_human_issue_code_change_flag(conn, issue_id, 0)
            row = conn.execute(
                "SELECT is_code_change_request FROM review_issues WHERE id = ?",
                (issue_id,),
            ).fetchone()
            assert int(row["is_code_change_request"]) == 0
        finally:
            conn.close()
