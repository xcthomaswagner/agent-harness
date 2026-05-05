"""Tests for operator_api_data — /api/operator JSON endpoints."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    open_connection,
    record_auto_merge_decision,
    upsert_lesson_candidate,
    upsert_pr_run,
)
from autonomy_store.lessons import LessonCandidateUpsert
from config import settings
from operator_api_data import router


def _mk_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _write_profile(
    profiles_dir: Path,
    name: str,
    platform: str = "salesforce",
    project_key: str = "TEST",
    local_path: str = "/tmp/x",
) -> None:
    """Write a minimal client-profile YAML the loader can parse."""
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "client": name,
                "platform_profile": platform,
                "ticket_source": {
                    "kind": "jira",
                    "instance": "example.atlassian.net",
                    "project_key": project_key,
                    "ai_label": "ai-implement",
                    "quick_label": "ai-quick",
                },
                "source_control": {"kind": "github", "owner": "x", "repo": "y"},
                "client_repo": {"local_path": local_path},
            }
        )
    )


def _seed_pr_run(
    db_path: Path,
    *,
    ticket_id: str,
    pr_number: int,
    client_profile: str,
    merged: int = 0,
    first_pass_accepted: int = 0,
    opened_at: str = "2026-04-18T12:00:00+00:00",
    merged_at: str | None = None,
    closed_at: str | None = None,
    state: str | None = None,
    excluded_from_metrics: int | None = None,
) -> int:
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        return upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id=ticket_id,
                pr_number=pr_number,
                repo_full_name="acme/widgets",
                pr_url=f"https://example.test/pr/{pr_number}",
                head_sha=f"sha{pr_number}",
                client_profile=client_profile,
                opened_at=opened_at,
                merged_at=merged_at,
                closed_at=closed_at,
                first_pass_accepted=first_pass_accepted,
                merged=merged,
                state=state,
                excluded_from_metrics=excluded_from_metrics,
            ),
        )
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    # Point client_profile loader at an empty scratch dir by default so
    # repo-real profiles don't leak into the test.
    profiles_dir = tmp_path / "client-profiles"
    profiles_dir.mkdir()
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    return TestClient(_mk_app())


# ---------- /api/operator/profiles ----------


def test_profiles_empty(client: TestClient, tmp_path: Path) -> None:
    r = client.get("/api/operator/profiles")
    assert r.status_code == 200
    assert r.json() == {"profiles": []}


def test_profiles_lists_yaml_profiles_with_zero_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed YAML profile but no PR runs — endpoint returns the profile
    # with zeroed metrics, not nothing.
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha", platform="salesforce")
    _write_profile(profiles_dir, "bravo", platform="sitecore")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    c = TestClient(_mk_app())
    r = c.get("/api/operator/profiles")
    assert r.status_code == 200
    profiles = r.json()["profiles"]
    assert len(profiles) == 2
    names = {p["id"] for p in profiles}
    assert names == {"alpha", "bravo"}
    # Every profile populated with zeroed counts.
    for p in profiles:
        assert p["in_flight"] == 0
        assert p["completed_24h"] == 0
        assert p["auto_merge"] == 0.0


def test_profiles_counts_in_flight_and_completed_24h(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    # Use NOW-1h so the row falls inside the 24h window.
    from datetime import UTC, datetime, timedelta

    recent_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    _seed_pr_run(
        db_path,
        ticket_id="T-1",
        pr_number=1,
        client_profile="alpha",
        merged=0,
        opened_at=recent_iso,
    )
    _seed_pr_run(
        db_path,
        ticket_id="T-2",
        pr_number=2,
        client_profile="alpha",
        merged=1,
        first_pass_accepted=1,
        opened_at=recent_iso,
        merged_at=recent_iso,
    )

    c = TestClient(_mk_app())
    r = c.get("/api/operator/profiles")
    assert r.status_code == 200
    [p] = r.json()["profiles"]
    assert p["id"] == "alpha"
    assert p["in_flight"] == 1
    assert p["completed_24h"] == 1


def test_profiles_count_active_pre_pr_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home in-flight count includes dispatched agents before a PR exists."""
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha", platform="contentstack", project_key="ALPHA")
    import client_profile as cp_module
    import tracer as tracer_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_trace(
        logs_dir,
        "ALPHA-1",
        events=[
            ("webhook", "ado_webhook_received"),
            ("pipeline", "processing_started"),
            ("pipeline", "l2_dispatched"),
            ("webhook", "ado_webhook_skipped_no_tag"),
        ],
    )

    c = TestClient(_mk_app())
    [profile] = c.get("/api/operator/profiles").json()["profiles"]
    assert profile["id"] == "alpha"
    assert profile["in_flight"] == 1


def test_profiles_do_not_double_count_trace_with_active_pr_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha", project_key="ALPHA")
    import client_profile as cp_module
    import tracer as tracer_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    recent_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _seed_pr_run(
        db_path,
        ticket_id="ALPHA-1",
        pr_number=20,
        client_profile="alpha",
        opened_at=recent_iso,
        state="open",
    )
    _write_trace(
        logs_dir,
        "ALPHA-1",
        events=[
            ("webhook", "webhook_received"),
            ("pipeline", "processing_started"),
            ("pipeline", "l2_dispatched"),
        ],
    )

    c = TestClient(_mk_app())
    [profile] = c.get("/api/operator/profiles").json()["profiles"]
    assert profile["in_flight"] == 1


def test_profiles_exclude_closed_suppressed_and_misfire_from_inflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    recent_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _seed_pr_run(
        db_path,
        ticket_id="T-ACTIVE",
        pr_number=10,
        client_profile="alpha",
        opened_at=recent_iso,
        state="open",
    )
    _seed_pr_run(
        db_path,
        ticket_id="T-CLOSED",
        pr_number=11,
        client_profile="alpha",
        opened_at=recent_iso,
        closed_at=recent_iso,
        state="closed",
    )
    _seed_pr_run(
        db_path,
        ticket_id="T-MISFIRE",
        pr_number=12,
        client_profile="alpha",
        opened_at=recent_iso,
        state="misfire",
        excluded_from_metrics=1,
    )
    _seed_pr_run(
        db_path,
        ticket_id="T-OLD",
        pr_number=13,
        client_profile="alpha",
        opened_at=(datetime.now(UTC) - timedelta(days=10)).isoformat(),
        state="open",
    )

    c = TestClient(_mk_app())
    [profile] = c.get("/api/operator/profiles").json()["profiles"]
    assert profile["in_flight"] == 1


def test_profiles_counts_long_running_and_recently_merged_by_correct_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    from datetime import UTC, datetime, timedelta

    old_opened = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    recent_merged = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    # Still-open PRs should count as in-flight even when opened before
    # the 24h activity window.
    _seed_pr_run(
        db_path,
        ticket_id="T-LONG",
        pr_number=10,
        client_profile="alpha",
        merged=0,
        opened_at=old_opened,
    )
    # Recently merged PRs should count by merged_at, not opened_at.
    _seed_pr_run(
        db_path,
        ticket_id="T-MERGED",
        pr_number=11,
        client_profile="alpha",
        merged=1,
        opened_at=old_opened,
        merged_at=recent_merged,
    )

    c = TestClient(_mk_app())
    [p] = c.get("/api/operator/profiles").json()["profiles"]
    assert p["in_flight"] == 1
    assert p["completed_24h"] == 1


def test_profiles_sorted_by_in_flight_desc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    _write_profile(profiles_dir, "bravo")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    from datetime import UTC, datetime, timedelta

    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    # bravo has 2 in-flight, alpha has 1
    _seed_pr_run(
        db_path,
        ticket_id="T-1",
        pr_number=1,
        client_profile="alpha",
        opened_at=recent,
    )
    _seed_pr_run(
        db_path,
        ticket_id="T-2",
        pr_number=2,
        client_profile="bravo",
        opened_at=recent,
    )
    _seed_pr_run(
        db_path,
        ticket_id="T-3",
        pr_number=3,
        client_profile="bravo",
        opened_at=recent,
    )

    c = TestClient(_mk_app())
    profiles = c.get("/api/operator/profiles").json()["profiles"]
    assert [p["id"] for p in profiles] == ["bravo", "alpha"]


def test_profiles_auto_merge_rate_computed_from_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        # 3 merged, 1 blocked, 1 skipped → 3/4 eligible = 0.75
        for i, decision in enumerate(["merged", "merged", "merged", "blocked", "skipped"]):
            record_auto_merge_decision(
                conn,
                repo_full_name="acme/widgets",
                pr_number=i + 1,
                decision=decision,
                reason="test",
                payload={"client_profile": "alpha"},
            )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    [p] = c.get("/api/operator/profiles").json()["profiles"]
    assert p["auto_merge"] == pytest.approx(0.75, abs=1e-3)


@pytest.mark.parametrize(
    "path",
    [
        "/api/operator/profiles",
        "/api/operator/traces",
        "/api/operator/traces/HARN-1",
        "/api/operator/autonomy/xcsf30",
        "/api/operator/lessons/counts",
        "/api/operator/model-policy",
        "/api/operator/tickets/HARN-1/agents",
        "/api/operator/pr/1",
    ],
)
def test_all_operator_endpoints_require_auth_when_key_set(
    path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router-level Depends(_require_dashboard_auth) covers every route."""
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "secret")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", False)

    profiles_dir = tmp_path / "client-profiles"
    profiles_dir.mkdir()
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    c = TestClient(_mk_app())
    # Missing key → 401.
    assert c.get(path).status_code == 401
    # Correct key → not 401 (may be 200/404 depending on fixture state).
    r = c.get(path, headers={"X-API-Key": "secret"})
    assert r.status_code != 401


def test_profiles_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", False)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    c = TestClient(_mk_app())
    assert c.get("/api/operator/profiles").status_code == 401
    ok = c.get("/api/operator/profiles", headers={"X-API-Key": "secret-key"})
    assert ok.status_code == 200


# ---------- /api/operator/lessons/counts ----------


# ---------- /api/operator/model-policy ----------


def test_model_policy_returns_defaults(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import operator_api_data as oad

    monkeypatch.setattr(oad, "_MODEL_POLICY_PATH", tmp_path / "policy.json")
    r = client.get("/api/operator/model-policy")
    assert r.status_code == 200
    data = r.json()
    assert data["source"] == "default"
    roles = {row["role"]: row for row in data["roles"]}
    assert roles["analyst"]["model"] == "claude-opus-4-20250514"
    assert roles["team_lead"]["reasoning"] == "high"
    assert roles["developer"]["model"] == "opus"
    assert roles["run_reflector"]["model"] == "opus"
    assert "sonnet" in data["model_options"]


def test_model_policy_put_persists_operator_choices(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import operator_api_data as oad

    policy_path = tmp_path / "policy.json"
    monkeypatch.setattr(oad, "_MODEL_POLICY_PATH", policy_path)
    current = client.get("/api/operator/model-policy").json()
    roles = current["roles"]
    for row in roles:
        if row["role"] == "developer":
            row["model"] = "sonnet"
            row["reasoning"] = "standard"

    r = client.put("/api/operator/model-policy", json={"roles": roles})
    assert r.status_code == 200
    data = r.json()
    assert data["source"] == "local"
    saved = {row["role"]: row for row in data["roles"]}
    assert saved["developer"]["model"] == "sonnet"
    assert saved["developer"]["reasoning"] == "standard"
    assert policy_path.is_file()

    round_trip = client.get("/api/operator/model-policy").json()
    assert {row["role"]: row for row in round_trip["roles"]}["developer"]["model"] == "sonnet"


def test_model_policy_write_is_atomic_on_replace_failure(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import operator_api_data as oad

    policy_path = tmp_path / "policy.json"
    original = '{"version":1,"roles":[],"updated_at":"old"}\n'
    policy_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(oad, "_MODEL_POLICY_PATH", policy_path)
    current = client.get("/api/operator/model-policy").json()
    roles = current["roles"]
    for row in roles:
        if row["role"] == "developer":
            row["model"] = "sonnet"

    def fail_replace(src: str, dst: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(oad.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        oad._write_model_policy_file(oad.ModelPolicyUpdate(roles=roles))

    assert policy_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".policy.json.*.tmp")) == []


def test_model_policy_rejects_unknown_role(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import operator_api_data as oad

    monkeypatch.setattr(oad, "_MODEL_POLICY_PATH", tmp_path / "policy.json")
    r = client.put(
        "/api/operator/model-policy",
        json={"roles": [{"role": "other_user", "model": "opus", "reasoning": "high"}]},
    )
    assert r.status_code == 400


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_repo_workflow_options_include_profile_repo_state(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "client"
    _init_git_repo(repo)
    (repo / "WORKFLOW.md").write_text("# WORKFLOW.md\n", encoding="utf-8")
    profiles_dir = tmp_path / "client-profiles"
    _write_profile(
        profiles_dir,
        "alpha",
        platform="contentstack",
        project_key="ALPHA",
        local_path=str(repo),
    )
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    r = client.get("/api/operator/repo-workflow/options")
    assert r.status_code == 200
    [profile] = r.json()["profiles"]
    assert profile["client_profile"] == "alpha"
    assert profile["platform_profile"] == "contentstack"
    assert profile["repo_exists"] is True
    assert profile["workflow_exists"] is True


def test_repo_workflow_draft_scans_next_repo(client: TestClient, tmp_path: Path) -> None:
    repo = tmp_path / "next-client"
    _init_git_repo(repo)
    (repo / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "typecheck": "tsc --noEmit",
                    "lint": "eslint .",
                    "test": "vitest run",
                    "build": "next build",
                },
                "dependencies": {"next": "15.0.0", "react": "19.0.0"},
                "devDependencies": {"@playwright/test": "1.0.0", "vitest": "3.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (repo / "package-lock.json").write_text("{}", encoding="utf-8")
    (repo / "README.md").write_text("# Client\n", encoding="utf-8")
    workflow_dir = repo / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(
        "name: ci\njobs:\n  test:\n    steps:\n      - run: npm run build\n",
        encoding="utf-8",
    )

    r = client.post(
        "/api/operator/repo-workflow/draft",
        json={"repo_path": str(repo), "client_profile": ""},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["repo_path"] == str(repo)
    assert data["workflow_exists"] is False
    assert "Next.js" in data["detected"]["frameworks"]
    assert "npm run typecheck" in data["detected"]["validation_commands"]
    assert "## Next.js Rules" in data["draft_text"]
    assert any(row["source"] == "package.json" for row in data["evidence"])
    assert any(item["id"] == "workflow_missing" for item in data["validation"])


def test_repo_workflow_draft_uses_client_profile_path(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "contentstack-client"
    _init_git_repo(repo)
    (repo / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.0.0"}}),
        encoding="utf-8",
    )
    profiles_dir = tmp_path / "client-profiles"
    _write_profile(
        profiles_dir,
        "alpha",
        platform="contentstack",
        project_key="ALPHA",
        local_path=str(repo),
    )
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    r = client.post(
        "/api/operator/repo-workflow/draft",
        json={"client_profile": "alpha", "repo_path": ""},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["client_profile"] == "alpha"
    assert data["platform_profile"] == "contentstack"
    assert "ContentStack" in data["detected"]["frameworks"]
    assert "## ContentStack Rules" in data["draft_text"]


def test_repo_workflow_save_writes_workflow_md(client: TestClient, tmp_path: Path) -> None:
    repo = tmp_path / "save-client"
    _init_git_repo(repo)
    body = "# WORKFLOW.md\n\n## Repository Context\n\n- Repository: save-client\n"

    r = client.put(
        "/api/operator/repo-workflow",
        json={"repo_path": str(repo), "client_profile": "", "content": body},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["saved"] is True
    assert data["workflow_path"] == str(repo / "WORKFLOW.md")
    assert (repo / "WORKFLOW.md").read_text(encoding="utf-8") == body


def test_system_endpoint_reports_runtime_metadata(client: TestClient) -> None:
    r = client.get("/api/operator/system")
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "l1_preprocessing"
    assert isinstance(data["pid"], int)
    assert data["started_at"]
    assert data["uptime_seconds"] >= 0
    assert data["code_path"].endswith("operator_api_data.py")
    assert data["db_path"]
    assert set(data["operator_bundle"]) == {"rev", "built_at"}


# ---------- /api/operator/traces ----------


def _write_trace(
    logs_dir: Path,
    ticket_id: str,
    *,
    events: list[tuple[str, str]],
    title: str = "",
) -> None:
    """Seed a JSONL trace file the tracer reads.

    events: list of (phase, event) pairs. Timestamps auto-generated.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"{ticket_id}.jsonl"
    base_ts = datetime.now(UTC)
    with path.open("w") as f:
        for i, (phase, ev) in enumerate(events):
            entry = {
                "ticket_id": ticket_id,
                "trace_id": f"t-{ticket_id}",
                "phase": phase,
                "event": ev,
                "timestamp": (base_ts + timedelta(seconds=i)).isoformat(),
                "source": "agent",
            }
            if i == 0 and title:
                entry["ticket_title"] = title
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def traces_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Test client with isolated data/logs and autonomy.db."""
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    return TestClient(_mk_app())


def test_traces_empty(traces_client: TestClient) -> None:
    r = traces_client.get("/api/operator/traces")
    assert r.status_code == 200
    data = r.json()
    assert data == {
        "traces": [],
        "count": 0,
        "status_counts": {
            "all": 0,
            "in-flight": 0,
            "stuck": 0,
            "queued": 0,
            "done": 0,
            "hidden": 0,
        },
        "offset": 0,
        "limit": 100,
        "include_hidden": False,
    }


def test_traces_returns_shaped_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_trace(
        logs_dir,
        "HARN-100",
        events=[
            ("webhook", "webhook_received"),
            ("pipeline", "processing_completed"),
            ("pipeline", "Pipeline complete"),
        ],
        title="Ship the thing",
    )

    c = TestClient(_mk_app())
    rows = c.get("/api/operator/traces").json()["traces"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "HARN-100"
    assert row["status"] == "done"
    assert row["raw_status"] == "Complete"
    assert "elapsed" in row


def test_traces_status_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    # One "done" trace, one "queued" trace.
    _write_trace(
        logs_dir,
        "HARN-DONE",
        events=[
            ("webhook", "webhook_received"),
            ("pipeline", "Pipeline complete"),
        ],
    )
    _write_trace(
        logs_dir,
        "HARN-Q",
        events=[("webhook", "webhook_received")],
    )

    c = TestClient(_mk_app())
    done = c.get("/api/operator/traces?status=done").json()["traces"]
    queued = c.get("/api/operator/traces?status=queued").json()["traces"]
    assert [r["id"] for r in done] == ["HARN-DONE"]
    assert [r["id"] for r in queued] == ["HARN-Q"]


def test_manual_ticket_with_l2_dispatch_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_trace(
        logs_dir,
        "HARN-MANUAL",
        events=[
            ("manual", "manual_ticket_submitted"),
            ("pipeline", "processing_started"),
            ("pipeline", "l2_dispatched"),
            ("pipeline", "processing_completed"),
        ],
        title="Manual ticket under active L2 work",
    )

    c = TestClient(_mk_app())
    body = c.get("/api/operator/traces").json()
    row = next(r for r in body["traces"] if r["id"] == "HARN-MANUAL")
    assert row["raw_status"] == "Dispatched"
    assert row["status"] == "in-flight"
    assert body["status_counts"]["in-flight"] == 1


def test_traces_status_filter_applies_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_trace(
        logs_dir,
        "HARN-DONE",
        events=[("webhook", "webhook_received"), ("pipeline", "Pipeline complete")],
    )
    _write_trace(
        logs_dir,
        "HARN-Q",
        events=[("webhook", "webhook_received")],
    )

    c = TestClient(_mk_app())
    body = c.get("/api/operator/traces?status=done&limit=1").json()
    assert body["count"] == 1
    assert body["status_counts"] == {
        "all": 2,
        "in-flight": 0,
        "stuck": 0,
        "queued": 1,
        "done": 1,
        "hidden": 0,
    }
    assert [r["id"] for r in body["traces"]] == ["HARN-DONE"]


def test_traces_limit_caps_at_500(traces_client: TestClient) -> None:
    r = traces_client.get("/api/operator/traces?limit=9999")
    assert r.status_code == 200
    assert r.json()["limit"] == 500


def test_traces_pr_created_bucket_is_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PR Created / Review Done / QA Done should all land in 'done', not 'in-flight'."""
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    # Trace with a pr_url but no "Pipeline complete" → derive_trace_status = "PR Created"
    logs_dir.mkdir(parents=True, exist_ok=True)
    pr_path = logs_dir / "HARN-PR.jsonl"
    pr_path.write_text(
        json.dumps(
            {
                "ticket_id": "HARN-PR",
                "trace_id": "t-pr",
                "phase": "pr",
                "event": "pr_opened",
                "pr_url": "https://github.com/org/repo/pull/1",
                "timestamp": "2026-04-18T12:00:00+00:00",
                "source": "pipeline",
            }
        )
        + "\n"
    )

    c = TestClient(_mk_app())
    rows = c.get("/api/operator/traces").json()["traces"]
    pr_row = next(r for r in rows if r["id"] == "HARN-PR")
    assert pr_row["raw_status"] == "PR Created"
    assert pr_row["status"] == "done", "PR Created should bucket as 'done' — agent work is finished"


def test_traces_stale_active_or_queued_reclassified_as_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Active/queued tickets with last activity > 2 hours ago should show as 'stuck'."""
    import operator_api_data as oad_module

    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    monkeypatch.setattr(oad_module, "_STALE_INFLIGHT_HOURS", 2)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    # Dispatched ticket whose last event was 3 hours ago — should become stuck.
    stale_ts = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    logs_dir.mkdir(parents=True, exist_ok=True)
    stale_path = logs_dir / "HARN-STALE.jsonl"
    stale_path.write_text(
        json.dumps(
            {
                "ticket_id": "HARN-STALE",
                "trace_id": "t-stale",
                "phase": "implement",
                "event": "l2_dispatched",
                "timestamp": stale_ts,
                "source": "pipeline",
            }
        )
        + "\n"
    )

    queued_path = logs_dir / "HARN-QUEUED-STALE.jsonl"
    queued_path.write_text(
        json.dumps(
            {
                "ticket_id": "HARN-QUEUED-STALE",
                "trace_id": "t-queued-stale",
                "phase": "webhook",
                "event": "webhook_received",
                "timestamp": stale_ts,
                "source": "pipeline",
            }
        )
        + "\n"
    )

    # Fresh ticket dispatched 30 minutes ago — should stay in-flight.
    fresh_ts = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    fresh_path = logs_dir / "HARN-FRESH.jsonl"
    fresh_path.write_text(
        json.dumps(
            {
                "ticket_id": "HARN-FRESH",
                "trace_id": "t-fresh",
                "phase": "implement",
                "event": "l2_dispatched",
                "timestamp": fresh_ts,
                "source": "pipeline",
            }
        )
        + "\n"
    )

    c = TestClient(_mk_app())
    rows = {r["id"]: r for r in c.get("/api/operator/traces").json()["traces"]}

    assert rows["HARN-STALE"]["status"] == "stuck", (
        "Dispatched ticket silent for 3h should be reclassified as stuck"
    )
    assert rows["HARN-QUEUED-STALE"]["status"] == "stuck", (
        "Queued ticket silent for 3h should be reclassified as stuck"
    )
    assert rows["HARN-FRESH"]["status"] == "in-flight", (
        "Dispatched ticket only 30min old should remain in-flight"
    )


def test_trace_detail_reuses_stale_status_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trace list and detail should agree when silent work is stale."""
    import operator_api_data as oad_module

    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    monkeypatch.setattr(oad_module, "_STALE_INFLIGHT_HOURS", 2)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    stale_ts = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "HARN-DETAIL-STALE.jsonl").write_text(
        json.dumps(
            {
                "ticket_id": "HARN-DETAIL-STALE",
                "trace_id": "t-detail-stale",
                "phase": "implement",
                "event": "l2_dispatched",
                "timestamp": stale_ts,
                "source": "pipeline",
            }
        )
        + "\n"
    )

    c = TestClient(_mk_app())
    list_rows = c.get("/api/operator/traces").json()["traces"]
    detail = c.get("/api/operator/traces/HARN-DETAIL-STALE").json()

    assert next(r for r in list_rows if r["id"] == "HARN-DETAIL-STALE")["status"] == "stuck"
    assert detail["status"] == "stuck"


def test_trace_state_mark_misfire_hides_trace_and_excludes_pr_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_trace(
        logs_dir,
        "HARN-MIS",
        events=[
            ("webhook", "webhook_received"),
            ("pipeline", "l2_dispatched"),
        ],
    )
    _seed_pr_run(
        db_path,
        ticket_id="HARN-MIS",
        pr_number=77,
        client_profile="alpha",
        state="open",
    )

    c = TestClient(_mk_app())
    r = c.post(
        "/api/operator/traces/HARN-MIS/state",
        json={
            "state": "misfire",
            "reason": "duplicate test run",
            "exclude_metrics": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["affected_pr_runs"] == 1

    visible = c.get("/api/operator/traces").json()["traces"]
    assert [row["id"] for row in visible] == []

    hidden = c.get("/api/operator/traces?include_hidden=true").json()["traces"]
    assert hidden[0]["id"] == "HARN-MIS"
    assert hidden[0]["status"] == "hidden"
    assert hidden[0]["raw_status"] == "Misfire"

    conn = open_connection(db_path)
    try:
        row = conn.execute(
            "SELECT state, excluded_from_metrics FROM pr_runs WHERE ticket_id = ?",
            ("HARN-MIS",),
        ).fetchone()
        assert row["state"] == "misfire"
        assert row["excluded_from_metrics"] == 1
    finally:
        conn.close()


def test_trace_state_open_restores_stale_pr_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_trace(
        logs_dir,
        "HARN-STALE-OPEN",
        events=[
            ("webhook", "webhook_received"),
            ("pipeline", "l2_dispatched"),
        ],
    )
    _seed_pr_run(
        db_path,
        ticket_id="HARN-STALE-OPEN",
        pr_number=78,
        client_profile="alpha",
        state="stale",
    )

    c = TestClient(_mk_app())
    r = c.post(
        "/api/operator/traces/HARN-STALE-OPEN/state",
        json={
            "state": "open",
            "reason": "operator confirmed still active",
            "exclude_metrics": False,
        },
    )

    assert r.status_code == 200
    assert r.json()["affected_pr_runs"] == 1
    conn = open_connection(db_path)
    try:
        row = conn.execute(
            "SELECT state, state_reason FROM pr_runs WHERE ticket_id = ?",
            ("HARN-STALE-OPEN",),
        ).fetchone()
        assert row["state"] == "open"
        assert row["state_reason"] == "operator confirmed still active"
    finally:
        conn.close()


def test_reconcile_stale_marks_old_active_pr_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    old_iso = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    recent_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _seed_pr_run(
        db_path,
        ticket_id="OLD-1",
        pr_number=81,
        client_profile="alpha",
        opened_at=old_iso,
        state="open",
    )
    _seed_pr_run(
        db_path,
        ticket_id="NEW-1",
        pr_number=82,
        client_profile="alpha",
        opened_at=recent_iso,
        state="open",
    )

    c = TestClient(_mk_app())
    dry = c.post(
        "/api/operator/dashboard/reconcile-stale",
        json={"stale_after_hours": 24, "dry_run": True},
    )
    assert dry.status_code == 200
    assert dry.json()["matched"] == 1

    applied = c.post(
        "/api/operator/dashboard/reconcile-stale",
        json={"stale_after_hours": 24, "dry_run": False},
    )
    assert applied.status_code == 200
    assert applied.json()["matched"] == 1

    conn = open_connection(db_path)
    try:
        rows = {
            row["ticket_id"]: row["state"]
            for row in conn.execute("SELECT ticket_id, state FROM pr_runs")
        }
        assert rows == {"OLD-1": "stale", "NEW-1": "open"}
    finally:
        conn.close()


def test_reconcile_stale_backfills_closed_trace_before_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)

    opened_at = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    closed_at = (datetime.now(UTC) - timedelta(days=9)).isoformat()
    _seed_pr_run(
        db_path,
        ticket_id="SCRUM-16",
        pr_number=42,
        client_profile="alpha",
        opened_at=opened_at,
        state="open",
    )
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "SCRUM-16.jsonl").write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {
                    "ticket_id": "SCRUM-16",
                    "trace_id": "trace-1",
                    "phase": "l3_approval",
                    "event": "review_approved",
                    "pr_number": 42,
                    "timestamp": opened_at,
                    "source": "l3",
                },
                {
                    "ticket_id": "SCRUM-16",
                    "trace_id": "trace-1",
                    "phase": "l3_pr_review",
                    "event": "pr_closed",
                    "pr_number": 42,
                    "timestamp": closed_at,
                    "source": "l3",
                },
                {
                    "ticket_id": "SCRUM-16",
                    "trace_id": "trace-1",
                    "phase": "l3_approval",
                    "event": "review_approved",
                    "pr_number": 42,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "source": "l3",
                },
            ]
        )
        + "\n"
    )

    c = TestClient(_mk_app())
    dry = c.post(
        "/api/operator/dashboard/reconcile-stale",
        json={"stale_after_hours": 24, "dry_run": True},
    )
    assert dry.status_code == 200
    assert dry.json()["lifecycle_reconciled"] == 1
    assert dry.json()["matched"] == 0

    applied = c.post(
        "/api/operator/dashboard/reconcile-stale",
        json={"stale_after_hours": 24, "dry_run": False},
    )
    assert applied.status_code == 200
    assert applied.json()["lifecycle_reconciled"] == 1
    assert applied.json()["matched"] == 0

    trace_row = c.get("/api/operator/traces?include_hidden=true").json()["traces"][0]
    assert trace_row["status"] == "done"
    assert trace_row["raw_status"] == "Closed"
    assert trace_row["lifecycle_state"] == "closed"

    conn = open_connection(db_path)
    try:
        row = conn.execute(
            "SELECT state, closed_at, state_reason FROM pr_runs WHERE ticket_id = ?",
            ("SCRUM-16",),
        ).fetchone()
        assert row["state"] == "closed"
        assert row["closed_at"] == closed_at
        assert row["state_reason"] == "reconciled from trace event pr_closed"
    finally:
        conn.close()


# ---------- /api/operator/traces/{id} (trace detail) ----------


def _write_rich_trace(
    logs_dir: Path,
    ticket_id: str,
    phases_events: list[tuple[str, str, str]],
) -> None:
    """Write a JSONL trace with agent-phase entries.

    phases_events: list of (phase, event, message). Timestamps
    auto-incremented by 30s per entry so compute_phase_durations has
    non-zero deltas.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"{ticket_id}.jsonl"
    with path.open("w") as f:
        # A webhook entry to anchor the run start.
        f.write(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "trace_id": f"t-{ticket_id}",
                    "phase": "webhook",
                    "event": "webhook_received",
                    "timestamp": "2026-04-18T12:00:00+00:00",
                    "source": "pipeline",
                }
            )
            + "\n"
        )
        for i, (phase, ev, msg) in enumerate(phases_events):
            ts_sec = 30 + i * 30
            f.write(
                json.dumps(
                    {
                        "ticket_id": ticket_id,
                        "trace_id": f"t-{ticket_id}",
                        "phase": phase,
                        "event": ev,
                        "message": msg,
                        "timestamp": f"2026-04-18T12:{ts_sec // 60:02d}:{ts_sec % 60:02d}+00:00",
                        "source": "agent",
                    }
                )
                + "\n"
            )


def test_trace_detail_404_when_missing(traces_client: TestClient) -> None:
    r = traces_client.get("/api/operator/traces/HARN-MISSING")
    assert r.status_code == 404


def test_trace_detail_shapes_phases_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_rich_trace(
        logs_dir,
        "HARN-777",
        phases_events=[
            ("planning", "plan_drafted", "Plan drafted"),
            ("planning", "plan_approved", "Plan approved"),
            ("scaffolding", "worktree_ready", "Worktree ready"),
            ("implementing", "unit_01_spawn", "unit-01 spawned"),
            ("implementing", "unit_01_done", "unit-01 done"),
            ("reviewing", "review_complete", "Review complete"),
        ],
    )

    c = TestClient(_mk_app())
    r = c.get("/api/operator/traces/HARN-777")
    assert r.status_code == 200
    data = r.json()

    # Core fields present.
    assert data["id"] == "HARN-777"
    assert "phases" in data and "events" in data
    assert len(data["phases"]) == 5

    phase_by_key = {p["key"]: p for p in data["phases"]}
    # Planning + scaffolding + implementing have events -> not pending.
    # The current phase (last agent-written) is reviewing -> active.
    assert phase_by_key["planning"]["state"] in ("done", "active")
    assert phase_by_key["planning"]["event_count"] >= 2
    assert phase_by_key["scaffolding"]["state"] in ("done", "active")
    assert phase_by_key["implementing"]["state"] in ("done", "active")
    assert phase_by_key["reviewing"]["state"] == "active"
    assert phase_by_key["merging"]["state"] == "pending"

    # Event stream preserves message text.
    messages = [e["msg"] for e in data["events"]]
    assert any("Plan drafted" in m for m in messages)
    assert any("Review complete" in m for m in messages)


def test_trace_detail_marks_failed_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_rich_trace(
        logs_dir,
        "HARN-FAIL",
        phases_events=[
            ("planning", "plan_drafted", "Plan drafted"),
            ("implementing", "unit_01_spawn", "unit-01 spawned"),
            ("implementing", "unit_01_error", "Unit failed with error"),
        ],
    )

    c = TestClient(_mk_app())
    data = c.get("/api/operator/traces/HARN-FAIL").json()
    impl = next(p for p in data["phases"] if p["key"] == "implementing")
    assert impl["state"] == "fail"


def test_trace_detail_maps_l3_only_phases_to_reviewing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_rich_trace(
        logs_dir,
        "HARN-L3",
        phases_events=[
            ("pr_review_spawned", "review_spawned", "L3 review spawned"),
            ("l3_approval", "review_approved", "Review approved"),
        ],
    )

    c = TestClient(_mk_app())
    data = c.get("/api/operator/traces/HARN-L3").json()
    reviewing = next(p for p in data["phases"] if p["key"] == "reviewing")
    assert reviewing["event_count"] >= 2
    assert reviewing["state"] == "done"


def test_trace_detail_maps_runtime_phase_names_to_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    _write_rich_trace(
        logs_dir,
        "HARN-RUNTIME",
        phases_events=[
            ("analyst", "analyst_completed", "Analyst completed"),
            ("pipeline", "l2_dispatched", "L2 dispatched"),
            ("implementation", "Implementation complete", "Implementation complete"),
            ("security_scan", "Security scan complete", "Security scan complete"),
            ("judge", "Judge complete", "Judge complete"),
            ("code_review", "Review complete", "Review complete"),
            ("qa_validation", "QA complete", "QA complete"),
            ("pr_created", "PR created", "PR created"),
            ("complete", "Pipeline complete", "Pipeline complete"),
        ],
    )

    c = TestClient(_mk_app())
    data = c.get("/api/operator/traces/HARN-RUNTIME").json()
    phases = {p["key"]: p for p in data["phases"]}

    assert phases["planning"]["event_count"] >= 1
    assert phases["scaffolding"]["event_count"] >= 1
    assert phases["implementing"]["event_count"] >= 1
    assert phases["reviewing"]["event_count"] >= 4
    assert phases["merging"]["event_count"] >= 2
    assert all(p["state"] == "done" for p in phases.values())


def test_trace_detail_keeps_manual_l1_prelude_in_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    ticket_id = "HARN-MANUAL"
    trace_id = "trace-manual"
    path = logs_dir / f"{ticket_id}.jsonl"
    rows = [
        ("operator", "manual_ticket_submitted", "Manual ticket submitted"),
        ("pipeline", "processing_started", "Processing started"),
        ("analyst", "analyst_completed", "Analyst completed"),
        ("pipeline", "l2_dispatched", "L2 dispatched"),
        ("pipeline", "processing_completed", "Processing completed"),
        ("pipeline", "Pipeline started", "Team lead started"),
        ("implementation", "Implementation complete", "Implementation complete"),
    ]
    with path.open("w") as f:
        for idx, (phase, event, message) in enumerate(rows):
            f.write(
                json.dumps(
                    {
                        "ticket_id": ticket_id,
                        "trace_id": trace_id,
                        "phase": phase,
                        "event": event,
                        "message": message,
                        "timestamp": f"2026-04-18T12:0{idx}:00+00:00",
                        "source": "agent" if idx >= 5 else "pipeline",
                    }
                )
                + "\n"
            )

    c = TestClient(_mk_app())
    data = c.get(f"/api/operator/traces/{ticket_id}").json()
    phases = {p["key"]: p for p in data["phases"]}
    assert phases["planning"]["event_count"] >= 1
    assert phases["scaffolding"]["event_count"] >= 1
    assert phases["implementing"]["event_count"] >= 1


# ---------- /api/operator/autonomy/{profile} ----------


def test_autonomy_returns_empty_shape_for_unknown_profile(
    client: TestClient,
) -> None:
    r = client.get("/api/operator/autonomy/does-not-exist")
    assert r.status_code == 200
    data = r.json()
    assert data["profile"] == "does-not-exist"
    assert "metrics" in data
    assert "trends" in data
    assert data["by_type"] == []
    assert data["escaped"] == []
    # 30 days x 4 trends, empty arrays with all-None values
    assert len(data["trends"]["fpa"]) == 30
    assert all(d["value"] is None for d in data["trends"]["fpa"])


def test_autonomy_includes_auto_merge_trend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        for i, decision in enumerate(["merged", "blocked"]):
            record_auto_merge_decision(
                conn,
                repo_full_name="acme/widgets",
                pr_number=i + 1,
                decision=decision,
                reason="test",
                payload={"client_profile": "alpha"},
            )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    data = c.get("/api/operator/autonomy/alpha").json()
    trend = data["trends"]["auto_merge"]
    today_entry = next(d for d in trend if d["sample"] > 0)
    # 1 merged / 2 eligible
    assert today_entry["value"] == 0.5
    assert today_entry["sample"] == 2


def test_autonomy_ticket_type_breakdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    from datetime import UTC, datetime, timedelta

    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        for i, tt in enumerate(["bug", "feature", "chore"]):
            upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id=f"T-{i}",
                    pr_number=i + 10,
                    repo_full_name="acme/widgets",
                    pr_url=f"https://example.test/pr/{i + 10}",
                    head_sha=f"sha{i}",
                    client_profile="alpha",
                    opened_at=recent,
                    ticket_type=tt,
                    first_pass_accepted=1,
                    merged=1,
                ),
            )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    data = c.get("/api/operator/autonomy/alpha").json()
    types = sorted({row["ticket_type"] for row in data["by_type"]})
    assert types == ["bug", "chore", "feature"]


# ---------- /api/operator/pr/{pr_run_id} ----------


def test_pr_detail_404_when_missing(client: TestClient) -> None:
    r = client.get("/api/operator/pr/999999")
    assert r.status_code == 404


def test_pr_detail_shapes_pr_row_and_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autonomy_store import insert_review_issue

    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        pr_run_id = upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id="T-42",
                pr_number=100,
                repo_full_name="acme/widgets",
                pr_url="https://example.test/pr/100",
                head_sha="sha100",
                client_profile="alpha",
                opened_at="2026-04-18T12:00:00+00:00",
                first_pass_accepted=0,
                merged=0,
            ),
        )
        insert_review_issue(
            conn,
            pr_run_id=pr_run_id,
            source="ai_review",
            file_path="src/foo.py",
            line_start=10,
            category="correctness",
            severity="major",
            summary="Unbounded loop risk",
            is_valid=1,
        )
        insert_review_issue(
            conn,
            pr_run_id=pr_run_id,
            source="ai_review",
            file_path="src/bar.py",
            category="style",
            severity="minor",
            summary="Missing trailing newline",
            is_valid=1,
        )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    r = c.get(f"/api/operator/pr/{pr_run_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["pr_run_id"] == pr_run_id
    assert data["ticket_id"] == "T-42"
    assert data["pr_number"] == 100
    # Issues sorted with major before minor.
    assert [i["severity"] for i in data["issues"]] == ["major", "minor"]
    assert data["issues"][0]["summary"] == "Unbounded loop risk"
    assert data["ci_checks_available"] is False


# ---------- /api/operator/tickets/{id}/agents ----------


def test_agents_empty_when_no_worktree(client: TestClient) -> None:
    r = client.get("/api/operator/tickets/HARN-NONE/agents")
    assert r.status_code == 200
    assert r.json() == {"agents": []}


def test_agents_lists_teammates_with_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    # Build a fake worktree tree with two session-stream.jsonl files:
    # one recent (running), one old (stale).
    worktree = tmp_path / "wt" / "HARN-ROSTER"
    main_stream = worktree / ".harness" / "logs" / "session-stream.jsonl"
    main_stream.parent.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    main_stream.write_text(json.dumps({"timestamp": now, "type": "system"}) + "\n")

    sub_stream = (
        worktree / ".claude" / "worktrees" / "dev-01" / ".harness" / "logs" / "session-stream.jsonl"
    )
    sub_stream.parent.mkdir(parents=True)
    old = "2024-01-01T00:00:00+00:00"
    sub_stream.write_text(json.dumps({"timestamp": old, "type": "system"}) + "\n")

    import live_stream as ls

    monkeypatch.setattr(ls, "_worktree_root_for_ticket", lambda _tid: worktree)
    # operator_api_data imports _worktree_root_for_ticket from live_stream
    # at module load — patch both sites.
    import operator_api_data as oad

    monkeypatch.setattr(oad, "_worktree_root_for_ticket", lambda _tid: worktree)

    c = TestClient(_mk_app())
    data = c.get("/api/operator/tickets/HARN-ROSTER/agents").json()
    agents = data["agents"]
    assert len(agents) == 2
    states = {a["teammate"]: a["state"] for a in agents}
    # At least one running (from recent), one stale (from 2024 timestamp).
    assert any(v == "running" for v in states.values())
    assert any(v == "stale" for v in states.values())
    main = next(a for a in agents if a["teammate"] == "team-lead")
    assert main["role"] == "team_lead"
    assert main["role_group"] == "team_lead"
    assert main["display_name"] == "Team Lead"
    assert main["stream_path_present"] is True
    assert "current_activity" in main
    assert "latest_events" in main


def test_agents_handles_naive_fallback_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    worktree = tmp_path / "wt" / "HARN-NAIVE"
    stream = worktree / ".harness" / "logs" / "session-stream.jsonl"
    stream.parent.mkdir(parents=True)
    # A system-only stream falls back to _last_event_time; pin that
    # timezone-naive ISO values don't crash UTC age comparison.
    stream.write_text(json.dumps({"timestamp": "2026-05-04T12:00:00", "type": "system"}) + "\n")

    import operator_api_data as oad

    monkeypatch.setattr(oad, "_worktree_root_for_ticket", lambda _tid: worktree)

    c = TestClient(_mk_app())
    r = c.get("/api/operator/tickets/HARN-NAIVE/agents")
    assert r.status_code == 200
    assert r.json()["agents"][0]["last_at"] == "2026-05-04T12:00:00+00:00"


def test_agents_extracts_embedded_claude_subagents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    worktree = tmp_path / "wt" / "HARN-SUBAGENTS"
    stream = worktree / ".harness" / "logs" / "session-stream.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-05-04T12:00:00+00:00",
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_dev",
                                    "name": "Agent",
                                    "input": {
                                        "subagent_type": "developer",
                                        "description": "Implement hero",
                                    },
                                }
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-05-04T12:00:01+00:00",
                        "type": "system",
                        "subtype": "task_started",
                        "tool_use_id": "toolu_dev",
                        "description": "Developer implementing hero",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-05-04T12:00:02+00:00",
                        "type": "assistant",
                        "parent_tool_use_id": "toolu_dev",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Edit",
                                    "input": {"file_path": "src/Hero.tsx"},
                                }
                            ]
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    import operator_api_data as oad

    monkeypatch.setattr(oad, "_worktree_root_for_ticket", lambda _tid: worktree)

    c = TestClient(_mk_app())
    data = c.get("/api/operator/tickets/HARN-SUBAGENTS/agents").json()
    agents = {agent["teammate"]: agent for agent in data["agents"]}
    assert agents["team-lead"]["role"] == "team_lead"
    assert agents["developer"]["role"] == "developer"
    assert agents["developer"]["current_activity"] == "Edit: src/Hero.tsx"


def test_activity_summary_returns_deduped_ticket_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    worktree = tmp_path / "wt" / "HARN-SUMMARY"
    stream = worktree / ".harness" / "logs" / "session-stream.jsonl"
    stream.parent.mkdir(parents=True)
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "src/Hero.tsx"},
                }
            ]
        },
    }
    stream.write_text(json.dumps(event) + "\n" + json.dumps(event) + "\n")

    import operator_api_data as oad

    monkeypatch.setattr(oad, "_worktree_root_for_ticket", lambda _tid: worktree)

    c = TestClient(_mk_app())
    data = c.get("/api/operator/tickets/HARN-SUMMARY/activity-summary").json()
    assert data["ticket_id"] == "HARN-SUMMARY"
    assert data["raw_event_count"] == 2
    assert data["deduped_event_count"] == 1
    assert data["teammates"][0]["actions"][0]["count"] == 2


def test_activity_summary_extracts_embedded_subagent_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    worktree = tmp_path / "wt" / "HARN-SUBSUMMARY"
    stream = worktree / ".harness" / "logs" / "session-stream.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_qa",
                                    "name": "Agent",
                                    "input": {"subagent_type": "qa"},
                                }
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "parent_tool_use_id": "toolu_qa",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "QA found the missing Tailwind dependency.",
                                }
                            ]
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    import operator_api_data as oad

    monkeypatch.setattr(oad, "_worktree_root_for_ticket", lambda _tid: worktree)

    c = TestClient(_mk_app())
    data = c.get("/api/operator/tickets/HARN-SUBSUMMARY/activity-summary").json()
    teammates = {teammate["role"] for teammate in data["teammates"]}
    assert "qa" in teammates
    assert any("missing Tailwind" in item["message"] for item in data["highlights"])


def test_activity_summary_includes_finished_phase_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    worktree = tmp_path / "wt" / "HARN-FINISHED"
    stream = worktree / ".harness" / "logs" / "session-stream.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "package.json"},
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    (stream.parent / "pipeline.jsonl").write_text(
        json.dumps(
            {
                "phase": "qa_validation",
                "timestamp": "2026-05-03T22:21:56Z",
                "event": "QA complete",
                "overall": "PASS",
                "criteria_passed": 23,
                "criteria_total": 23,
            }
        )
        + "\n"
    )
    (stream.parent / "qa-matrix.json").write_text(
        json.dumps(
            {
                "overall": "PASS",
                "issues": [
                    {"criterion": "Hero renders", "status": "PASS"},
                    {"criterion": "CTA sanitized", "status": "PASS_WITH_CAVEAT"},
                ],
            }
        )
    )

    import operator_api_data as oad

    monkeypatch.setattr(oad, "_worktree_root_for_ticket", lambda _tid: worktree)

    c = TestClient(_mk_app())
    data = c.get("/api/operator/tickets/HARN-FINISHED/activity-summary").json()
    roles = {teammate["role"] for teammate in data["teammates"]}
    assert "team_lead" in roles
    assert "qa" in roles
    assert any("QA complete" in item["message"] for item in data["highlights"])


def test_activity_summary_uses_finished_artifacts_without_session_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    worktree = tmp_path / "wt" / "HARN-NOSTREAM"
    logs = worktree / ".harness" / "logs"
    logs.mkdir(parents=True)
    (logs / "pipeline.jsonl").write_text(
        json.dumps(
            {
                "phase": "qa_validation",
                "timestamp": "2026-05-03T22:21:56Z",
                "event": "QA complete",
                "overall": "PASS",
                "criteria_passed": 2,
                "criteria_total": 2,
            }
        )
        + "\n"
    )
    (logs / "qa-matrix.json").write_text(
        json.dumps(
            {
                "overall": "PASS",
                "issues": [{"criterion": "Hero renders", "status": "PASS"}],
            }
        )
    )

    import operator_api_data as oad

    monkeypatch.setattr(oad, "_worktree_root_for_ticket", lambda _tid: worktree)

    c = TestClient(_mk_app())
    data = c.get("/api/operator/tickets/HARN-NOSTREAM/activity-summary").json()
    assert data["raw_event_count"] > 0
    roles = {teammate["role"] for teammate in data["teammates"]}
    assert "qa" in roles
    assert any("QA complete" in item["message"] for item in data["highlights"])


def test_ticket_readiness_returns_spawn_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    worktree = tmp_path / "wt" / "HARN-READY"
    readiness = worktree / ".harness" / "client-readiness.json"
    readiness.parent.mkdir(parents=True)
    readiness.write_text(
        json.dumps(
            {
                "generated_by": "spawn_team.client_readiness",
                "client_profile": "harness-test-client",
                "is_next": True,
                "warning_count": 1,
                "warnings": [
                    {
                        "id": "github_actions_missing",
                        "area": "ci",
                        "severity": "warning",
                        "message": "No GitHub Actions workflow was found.",
                        "recommendation": "Add CI.",
                    }
                ],
            }
        )
    )

    import operator_api_data as oad

    monkeypatch.setattr(oad, "_worktree_root_for_ticket", lambda _tid: worktree)

    c = TestClient(_mk_app())
    data = c.get("/api/operator/tickets/HARN-READY/readiness").json()
    assert data["available"] is True
    assert data["source"] == "worktree"
    assert data["client_profile"] == "harness-test-client"
    assert data["warnings"][0]["id"] == "github_actions_missing"


def test_ticket_readiness_empty_when_report_missing(client: TestClient) -> None:
    data = client.get("/api/operator/tickets/HARN-NOREADY/readiness").json()
    assert data["available"] is False
    assert data["warning_count"] == 0


def test_operator_trigger_label_removal_clears_ai_and_quick_labels(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "HARN", project_key="HARN")
    import client_profile as cp_module
    import main

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)
    adapter = type("FakeAdoAdapter", (), {})()
    adapter.remove_label = AsyncMock()
    adapter.write_comment = AsyncMock()

    with patch.object(main, "_get_ado_adapter", return_value=adapter):
        response = client.delete("/api/operator/tickets/HARN-123/trigger-label")

    assert response.status_code == 200
    assert response.json()["labels"] == ["ai-implement", "ai-quick"]
    adapter.remove_label.assert_any_await("HARN-123", "ai-implement")
    adapter.remove_label.assert_any_await("HARN-123", "ai-quick")
    adapter.write_comment.assert_awaited_once()


def test_pr_detail_surfaces_auto_merge_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "autonomy.db"
    monkeypatch.setattr(settings, "autonomy_db_path", str(db_path))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "dashboard_allow_anonymous", True)

    profiles_dir = tmp_path / "client-profiles"
    _write_profile(profiles_dir, "alpha")
    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles_dir)

    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        pr_run_id = upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id="T-1",
                pr_number=7,
                repo_full_name="acme/widgets",
                pr_url="https://example.test/pr/7",
                head_sha="sha7",
                client_profile="alpha",
                opened_at="2026-04-18T12:00:00+00:00",
            ),
        )
        record_auto_merge_decision(
            conn,
            repo_full_name="acme/widgets",
            pr_number=7,
            decision="hold",
            reason="pending playwright smoke",
            payload={
                "client_profile": "alpha",
                "confidence": 0.43,
                "gates": {
                    "ci_passed": False,
                    "review_clean": True,
                    "is_mergeable": True,
                },
            },
        )
    finally:
        conn.close()

    c = TestClient(_mk_app())
    data = c.get(f"/api/operator/pr/{pr_run_id}").json()
    am = data["auto_merge"]
    assert am["decision"] == "hold"
    assert am["reason"] == "pending playwright smoke"
    assert am["confidence"] == 0.43
    assert am["gates"]["ci_passed"] is False


# ---------- /api/operator/lessons/counts ----------


def test_lesson_counts_empty(client: TestClient) -> None:
    r = client.get("/api/operator/lessons/counts")
    assert r.status_code == 200
    counts = r.json()["counts"]
    expected_keys = {
        "proposed",
        "draft_ready",
        "approved",
        "applied",
        "snoozed",
        "rejected",
        "reverted",
        "stale",
    }
    assert set(counts.keys()) == expected_keys
    assert all(v == 0 for v in counts.values())


def test_lesson_counts_tallies_every_state(client: TestClient, tmp_path: Path) -> None:
    from autonomy_store.lessons import update_lesson_status

    db_path = Path(settings.autonomy_db_path)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        # Seed 4 lessons as "proposed" (only entry state).
        for i in range(4):
            upsert_lesson_candidate(
                conn,
                LessonCandidateUpsert(
                    lesson_id=f"LSN-{i:04x}",
                    client_profile="alpha",
                    platform_profile="salesforce",
                    detector_name="test-detector",
                    pattern_key=f"test|{i}",
                    scope_key=f"scope|{i}",
                ),
            )
        # Transition 2 through the valid state machine:
        #   proposed → draft_ready → approved → applied
        update_lesson_status(conn, "LSN-0002", "draft_ready")
        update_lesson_status(conn, "LSN-0002", "approved")
        update_lesson_status(conn, "LSN-0002", "applied")
        update_lesson_status(conn, "LSN-0003", "rejected")
    finally:
        conn.close()

    counts = client.get("/api/operator/lessons/counts").json()["counts"]
    # 2 still in proposed (0 and 1), 1 applied (2), 1 rejected (3).
    assert counts["proposed"] == 2
    assert counts["applied"] == 1
    assert counts["rejected"] == 1
    assert counts["approved"] == 0
    assert counts["draft_ready"] == 0
