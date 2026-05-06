"""Tests for scripts/run_learning_backfill.py — ``--dry-run`` must not
write to the live DB."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run_learning_backfill.py"
SERVICE_SRC = REPO_ROOT / "services" / "l1_preprocessing"

sys.path.insert(0, str(SERVICE_SRC))


def _load_backfill_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_learning_backfill_under_test", str(SCRIPT)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _seed_live_cluster(db_path: Path) -> None:
    """Seed 3 PR runs + 3 human issues on the xcsf30 profile.

    Mirrors the shape that Detector 2 keys off.
    """
    from autonomy_store import (
        PrRunUpsert,
        ensure_schema,
        insert_review_issue,
        open_connection,
        upsert_pr_run,
    )

    conn = open_connection(db_path)
    opened_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    try:
        ensure_schema(conn)
        for i in range(3):
            pr_id = upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id=f"LB-{i}",
                    pr_number=100 + i,
                    repo_full_name="acme/app",
                    head_sha=f"sha-{i}",
                    client_profile="xcsf30",
                    opened_at=opened_at,
                ),
            )
            insert_review_issue(
                conn,
                pr_run_id=pr_id,
                source="human_review",
                file_path=f"force-app/foo{i}.cls",
                category="security",
                summary=f"issue {i}",
                is_valid=1,
            )
    finally:
        conn.close()


@pytest.fixture
def live_db(tmp_path: Path) -> Path:
    """A DB seeded with a real cluster for the script to find."""
    path = tmp_path / "autonomy.db"
    _seed_live_cluster(path)
    return path


class TestDetectorRegistration:
    """Backfill script must load every registered production detector.

    Prior versions hard-coded a single detector — a silent regression
    every time a new detector shipped. Tying the backfill to
    ``all_production_detectors`` guards against drift.
    """

    def test_all_production_detectors_are_loaded(self) -> None:
        from learning_miner import all_production_detectors

        mod = _load_backfill_module()

        loaded = mod._load_detectors()
        loaded_names = {d.name for d in loaded}
        expected_names = {d.name for d in all_production_detectors()}
        assert loaded_names == expected_names

    def test_expected_detector_count(self) -> None:
        """Count check so adding a detector forces a test update."""
        from learning_miner import all_production_detectors

        names = {d.name for d in all_production_detectors()}
        assert names == {
            "human_issue_cluster",
            "mcp_drift",
            "form_controls_ac_gaps",
            "cross_unit_object_pivot",
            "simplify_no_sidecar",
            "reviewer_judge_rejection_rate",
        }


class TestRetrospectiveRoots:
    def test_default_root_uses_default_client_repo_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import config

        mod = _load_backfill_module()
        client_repo = tmp_path / "client"
        monkeypatch.setattr(config.settings, "default_client_repo", str(client_repo))
        args = SimpleNamespace(no_retrospectives=False, retrospective_root=[])

        assert mod._retrospective_search_roots(args) == [
            tmp_path / "trace-archive"
        ]

    def test_no_retrospectives_disables_ingest(self) -> None:
        mod = _load_backfill_module()
        args = SimpleNamespace(
            no_retrospectives=True,
            retrospective_root=["/tmp/trace-archive"],
        )

        assert mod._retrospective_search_roots(args) is None


class TestDryRunPersistence:
    def test_dry_run_does_not_write_to_live_db(self, live_db: Path) -> None:
        from autonomy_store import (
            ensure_schema,
            list_lesson_candidates,
            open_connection,
        )

        result = _run_script(
            "--dry-run",
            "--db-path",
            str(live_db),
            "--window-days",
            "14",
            "--json",
        )
        assert result.returncode == 0, (
            f"script failed: {result.stderr}"
        )
        payload = json.loads(result.stdout)
        # Dry-run found the cluster inside its scratch DB.
        assert payload["total_candidates"] == 1
        assert payload["dry_run"] is True

        # Live DB is unchanged.
        conn = open_connection(live_db)
        try:
            ensure_schema(conn)
            candidates = list_lesson_candidates(conn)
        finally:
            conn.close()
        assert candidates == []

    def test_real_run_does_persist(self, live_db: Path) -> None:
        from autonomy_store import (
            ensure_schema,
            list_lesson_candidates,
            open_connection,
        )

        result = _run_script(
            "--db-path", str(live_db), "--window-days", "14", "--json"
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["total_candidates"] == 1
        assert payload["dry_run"] is False

        conn = open_connection(live_db)
        try:
            ensure_schema(conn)
            candidates = list_lesson_candidates(conn)
        finally:
            conn.close()
        assert len(candidates) == 1
        assert candidates[0]["detector_name"] == "human_issue_cluster"
