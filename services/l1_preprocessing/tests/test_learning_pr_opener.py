"""Tests for learning_miner/pr_opener.py.

Uses an isolated git-backed fake repo that the PR opener clones as
its "harness" origin — exercises the real git codepath (clone,
checkout, apply, commit) end-to-end. ``gh pr create`` is patched so
no network call happens.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from learning_miner.pr_opener import (
    OpenPRInputs,
    PROpenerResult,
    _build_branch_name,
    _compose_pr_body,
    _edited_paths_from_diff,
    _parse_pr_url,
    _stamp_lesson_id,
    open_pr_for_lesson,
)

# ---- branch-name validation ------------------------------------------


class TestBuildBranchName:
    def test_valid_lesson_id_builds_clean_branch(self) -> None:
        assert _build_branch_name("LSN-a1b2c3d4") == "learning/lesson-LSN-a1b2c3d4"

    def test_unsafe_id_raises(self) -> None:
        with pytest.raises(ValueError, match="unsafe branch"):
            _build_branch_name("LSN-; rm -rf /")

    def test_double_dot_rejected(self) -> None:
        # `..` in a ref name would let an attacker reach sibling branches.
        with pytest.raises(ValueError):
            _build_branch_name("LSN-a..b")


# ---- diff parsing ----------------------------------------------------


class TestEditedPathsFromDiff:
    def test_single_file(self) -> None:
        diff = (
            "--- a/runtime/skills/x/SKILL.md\n"
            "+++ b/runtime/skills/x/SKILL.md\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        assert _edited_paths_from_diff(diff) == ["runtime/skills/x/SKILL.md"]

    def test_skips_dev_null(self) -> None:
        diff = "--- a/foo.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        assert _edited_paths_from_diff(diff) == []

    def test_dedupes(self) -> None:
        diff = (
            "--- a/foo.md\n+++ b/foo.md\n@@\n+x\n"
            "--- a/bar.md\n+++ b/bar.md\n@@\n+y\n"
            "--- a/foo.md\n+++ b/foo.md\n@@\n+z\n"
        )
        out = _edited_paths_from_diff(diff)
        assert out == ["foo.md", "bar.md"]


class TestRedactTokenUrls:
    def test_strips_user_token_from_github_url(self) -> None:
        from redaction import redact_token_urls

        raw = (
            "fatal: unable to access "
            "'https://x-access-token:ghp_SECRET123@github.com/x/y.git/': "
            "Could not resolve host"
        )
        assert "ghp_SECRET123" not in redact_token_urls(raw)
        assert "github.com/x/y.git" in redact_token_urls(raw)

    def test_handles_multiple_urls(self) -> None:
        from redaction import redact_token_urls

        raw = (
            "https://a:s1@github.com/x.git failed, "
            "https://b:s2@github.com/y.git also failed"
        )
        redacted = redact_token_urls(raw)
        assert "s1" not in redacted
        assert "s2" not in redacted


class TestParsePrUrl:
    def test_strips_preamble(self) -> None:
        text = (
            "Creating pull request for branch ...\n\n"
            "https://github.com/x/y/pull/42\n"
        )
        assert _parse_pr_url(text) == "https://github.com/x/y/pull/42"

    def test_empty_on_no_url(self) -> None:
        assert _parse_pr_url("something went wrong") == ""


# ---- frontmatter stamping --------------------------------------------


class TestStampLessonId:
    def test_stamps_file_without_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "x.md"
        f.write_text("# Title\n\nBody")
        assert _stamp_lesson_id(f, "LSN-abc12345") is True
        text = f.read_text()
        assert text.startswith("---\nlesson_id: LSN-abc12345\n---\n")

    def test_replaces_existing_lesson_id(self, tmp_path: Path) -> None:
        f = tmp_path / "x.md"
        f.write_text(
            "---\nname: foo\nlesson_id: LSN-old\n---\n\nbody"
        )
        _stamp_lesson_id(f, "LSN-new")
        text = f.read_text()
        assert "lesson_id: LSN-new" in text
        assert "lesson_id: LSN-old" not in text

    def test_skips_non_markdown(self, tmp_path: Path) -> None:
        f = tmp_path / "config.yaml"
        f.write_text("client: x")
        assert _stamp_lesson_id(f, "LSN-abc") is False
        assert f.read_text() == "client: x"


# ---- end-to-end (real git, mocked gh) --------------------------------


_REAL_SUBPROCESS_RUN = subprocess.run


@pytest.fixture(autouse=True)
def _restore_subprocess_run(monkeypatch: pytest.MonkeyPatch):
    """Restore the real ``subprocess.run`` at each test start.

    Needed because another test module (test_ensure_client_repo.py)
    patches ``pipeline.subprocess.run`` from background threads, and
    the patches can leak when threads outlive the main test. The
    ``_REAL_SUBPROCESS_RUN`` reference is captured at import time.
    """
    monkeypatch.setattr(subprocess, "run", _REAL_SUBPROCESS_RUN)
    yield


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    """Bare-ish origin repo the PR opener will clone from.

    We init a normal repo with one commit on `main`, then the PR
    opener can clone it via a file:// URL. Using `--bare` would be
    more realistic but less debuggable; a plain init suffices at
    this scale.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True, capture_output=True)
    skills = origin / "runtime" / "skills" / "code-review"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "# Code Review\n\n## Review Checklist\n\n"
        "- Check docstrings.\n"
        "- Verify tests.\n"
    )
    subprocess.run(["git", "add", "."], cwd=origin, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c", "user.email=init@test",
            "-c", "user.name=init",
            "commit", "-m", "init",
        ],
        cwd=origin, check=True, capture_output=True,
    )
    return origin


def _valid_diff() -> str:
    return (
        "--- a/runtime/skills/code-review/SKILL.md\n"
        "+++ b/runtime/skills/code-review/SKILL.md\n"
        "@@ -3,4 +3,5 @@\n"
        " ## Review Checklist\n"
        " \n"
        " - Check docstrings.\n"
        " - Verify tests.\n"
        "+- Check SOQL injection in *.cls files.\n"
    )


def _base_inputs(origin: Path, *, dry_run: bool = True) -> OpenPRInputs:
    return OpenPRInputs(
        lesson_id="LSN-a1b2c3d4",
        unified_diff=_valid_diff(),
        scope_key="xcsf30|salesforce|security|*.cls",
        detector_name="human_issue_cluster",
        rationale_md="Cluster of SOQL injection human-review issues",
        evidence_trace_ids=["SCRUM-42", "SCRUM-43"],
        harness_repo_url=str(origin),
        base_branch="main",
        dry_run=dry_run,
    )


class TestOpenPrDryRun:
    def test_dry_run_commits_without_pushing(
        self, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch gh so if the code ever tries, the test fails loud.
        monkeypatch.setattr(
            "learning_miner.pr_opener._gh",
            MagicMock(
                side_effect=AssertionError("gh must not run in dry-run")
            ),
        )
        result = open_pr_for_lesson(_base_inputs(origin_repo, dry_run=True))
        assert result.success is True
        assert result.dry_run is True
        assert result.pr_url == ""
        assert result.branch == "learning/lesson-LSN-a1b2c3d4"
        assert result.commit_sha  # some sha populated



class TestOpenPrFullFlow:
    def test_push_and_gh_succeed(
        self, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Local `origin` accepts pushes to arbitrary branches when its
        # HEAD is on main (we cloned `main`). Set receiveDenyCurrent
        # so push doesn't complain.
        subprocess.run(
            ["git", "config", "receive.denyCurrentBranch", "ignore"],
            cwd=origin_repo, check=True, capture_output=True,
        )
        monkeypatch.setenv("AGENT_GH_TOKEN", "fake-token-for-test")

        # `gh pr create` is patched to return a canned URL.
        def fake_gh(args, *, cwd, env, timeout):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "https://github.com/x/agent-harness/pull/42\n"
            proc.stderr = ""
            return proc

        monkeypatch.setattr("learning_miner.pr_opener._gh", fake_gh)
        result = open_pr_for_lesson(_base_inputs(origin_repo, dry_run=False))
        assert result.success is True, result.error
        assert result.dry_run is False
        assert result.pr_url == "https://github.com/x/agent-harness/pull/42"
        assert result.branch == "learning/lesson-LSN-a1b2c3d4"

    def test_missing_token_refuses_push(
        self, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENT_GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(
            "learning_miner.pr_opener._gh",
            MagicMock(side_effect=AssertionError("should not reach gh")),
        )
        result = open_pr_for_lesson(_base_inputs(origin_repo, dry_run=False))
        assert result.success is False
        assert "AGENT_GH_TOKEN" in result.error


class TestOpenPrFailurePaths:
    def test_unapplicable_diff_fails_at_apply(
        self, origin_repo: Path
    ) -> None:
        bad = OpenPRInputs(
            lesson_id="LSN-baddiff",
            unified_diff=(
                "--- a/runtime/skills/code-review/SKILL.md\n"
                "+++ b/runtime/skills/code-review/SKILL.md\n"
                "@@ -500,2 +500,3 @@\n"
                " context that does not exist\n"
                " nor this\n"
                "+new\n"
            ),
            scope_key="s",
            detector_name="d",
            rationale_md="r",
            evidence_trace_ids=[],
            harness_repo_url=str(origin_repo),
            base_branch="main",
            dry_run=True,
        )
        result = open_pr_for_lesson(bad)
        assert result.success is False
        assert "git apply" in result.error

    def test_unsafe_lesson_id_rejected_before_clone(
        self, origin_repo: Path
    ) -> None:
        bad = OpenPRInputs(
            lesson_id="LSN-;bad",
            unified_diff="--- a/x\n+++ b/x\n",
            scope_key="",
            detector_name="",
            rationale_md="",
            evidence_trace_ids=[],
            harness_repo_url=str(origin_repo),
            dry_run=True,
        )
        result = open_pr_for_lesson(bad)
        assert result.success is False
        assert "unsafe branch" in result.error


# ---- PR body content -------------------------------------------------


class TestComposePrBody:
    def test_body_has_summary_and_lesson_id(self) -> None:
        body = _compose_pr_body(
            lesson_id="LSN-42",
            scope_key="client|platform|scope",
            detector_name="det_a",
            rationale_md="because reasons",
            evidence_trace_ids=["T-1", "T-2"],
        )
        assert "LSN-42" in body
        assert "det_a" in body
        assert "client|platform|scope" in body
        assert "because reasons" in body
        assert "T-1" in body
        assert "T-2" in body
        # Bot-loop marker present so harness self-review skips this PR.
        assert "<!-- xcagent -->" in body

    def test_truncates_large_evidence_list(self) -> None:
        traces = [f"T-{i}" for i in range(50)]
        body = _compose_pr_body(
            lesson_id="LSN-x",
            scope_key="",
            detector_name="",
            rationale_md="",
            evidence_trace_ids=traces,
        )
        # Only first 20 trace IDs should appear (cap per module).
        assert body.count("`T-") == 20


# ---- approve-triggers-PR integration --------------------------------


class TestApproveTriggersPrOpener:
    def test_happy_path_real_pr_transitions_to_applied(
        self,
        monkeypatch: pytest.MonkeyPatch,
        learning_api_client,
    ) -> None:
        from config import settings

        monkeypatch.setattr(settings, "learning_pr_opener_enabled", True)
        monkeypatch.setattr(settings, "learning_pr_opener_dry_run", False)

        def fake_open_pr(inputs: OpenPRInputs) -> PROpenerResult:
            return PROpenerResult(
                success=True,
                pr_url="https://github.com/x/y/pull/1",
                branch="learning/lesson-LSN-abcd1234",
                commit_sha="deadbeefdead",
                dry_run=False,
            )

        monkeypatch.setattr("learning_api.open_pr_for_lesson", fake_open_pr)

        from tests.conftest import seed_draft_ready_candidate
        lid = seed_draft_ready_candidate()
        r = learning_api_client.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "ship it"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pr_opener_enabled"] is True
        assert body["pr_opener_success"] is True
        assert body["pr_url"] == "https://github.com/x/y/pull/1"

        # Lesson transitioned to applied.
        r2 = learning_api_client.get(f"/api/learning/candidates/{lid}")
        assert r2.json()["status"] == "applied"
        assert r2.json()["pr_url"] == "https://github.com/x/y/pull/1"

    def test_dry_run_keeps_lesson_at_approved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        learning_api_client,
    ) -> None:
        """Dry-run exercises the full local flow but does NOT transition
        to applied — that would be misleading for a no-network run.
        The operator sees status=approved with status_reason recording
        the dry-run branch/sha.
        """
        from config import settings

        monkeypatch.setattr(settings, "learning_pr_opener_enabled", True)
        monkeypatch.setattr(settings, "learning_pr_opener_dry_run", True)

        def fake_open_pr(inputs: OpenPRInputs) -> PROpenerResult:
            return PROpenerResult(
                success=True,
                branch="learning/lesson-LSN-abcd1234",
                commit_sha="cafef00dcafef00d",
                dry_run=True,
            )

        monkeypatch.setattr("learning_api.open_pr_for_lesson", fake_open_pr)

        from tests.conftest import seed_draft_ready_candidate
        lid = seed_draft_ready_candidate()
        r = learning_api_client.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "dry-run test"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["pr_opener_success"] is True
        assert body["pr_opener_dry_run"] is True
        assert not body.get("pr_url")

        r2 = learning_api_client.get(f"/api/learning/candidates/{lid}")
        row = r2.json()
        assert row["status"] == "approved"
        assert "dry-run ok" in row["status_reason"]

    def test_pr_opener_failure_leaves_lesson_at_approved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        learning_api_client,
    ) -> None:
        from config import settings

        monkeypatch.setattr(settings, "learning_pr_opener_enabled", True)
        monkeypatch.setattr(settings, "learning_pr_opener_dry_run", False)

        def fake_open_pr(inputs: OpenPRInputs) -> PROpenerResult:
            return PROpenerResult(
                success=False, error="gh pr create failed (exit 1): boom"
            )

        monkeypatch.setattr("learning_api.open_pr_for_lesson", fake_open_pr)

        from tests.conftest import seed_draft_ready_candidate
        lid = seed_draft_ready_candidate()
        r = learning_api_client.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "ship it"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["pr_opener_success"] is False
        assert "gh pr create" in body["error"]
        # Lesson stayed at approved so the operator can retry.
        r2 = learning_api_client.get(f"/api/learning/candidates/{lid}")
        assert r2.json()["status"] == "approved"
        assert "pr_opener" in r2.json()["status_reason"]

    def test_retry_after_pr_opener_failure_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        learning_api_client,
    ) -> None:
        """After an initial /approve fails at the PR-opener stage the
        lesson sits at ``approved``. Re-calling /approve must not 409
        — it should re-run the PR opener in place. This is the
        sanctioned recovery path; without it a transient gh failure
        would wedge the lesson permanently.
        """
        from config import settings

        monkeypatch.setattr(settings, "learning_pr_opener_enabled", True)
        monkeypatch.setattr(settings, "learning_pr_opener_dry_run", False)

        calls = {"n": 0}

        def fake_open_pr(inputs: OpenPRInputs) -> PROpenerResult:
            calls["n"] += 1
            if calls["n"] == 1:
                return PROpenerResult(success=False, error="transient blip")
            return PROpenerResult(
                success=True,
                pr_url="https://github.com/x/y/pull/2",
                branch=inputs.lesson_id,
                commit_sha="cafef00d",
                dry_run=False,
            )

        monkeypatch.setattr("learning_api.open_pr_for_lesson", fake_open_pr)

        from tests.conftest import seed_draft_ready_candidate
        lid = seed_draft_ready_candidate()

        # First call: PR opener fails. Lesson ends up at approved.
        r1 = learning_api_client.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "first"},
        )
        assert r1.status_code == 200
        assert r1.json()["pr_opener_success"] is False

        # Second call: retry, PR opener succeeds, lesson moves to applied.
        r2 = learning_api_client.post(
            f"/api/learning/candidates/{lid}/approve",
            json={"reason": "retry"},
        )
        assert r2.status_code == 200
        assert r2.json()["pr_opener_success"] is True
        assert r2.json()["pr_url"] == "https://github.com/x/y/pull/2"

        # Final state: applied.
        r3 = learning_api_client.get(f"/api/learning/candidates/{lid}")
        assert r3.json()["status"] == "applied"


# ---- dashboard PR link ------------------------------------------------


class TestDashboardRendersPrLink:
    def test_applied_row_shows_pr_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from autonomy_store import autonomy_conn, update_lesson_status
        from config import settings
        from learning_dashboard import router as dashboard_router
        from tests.conftest import seed_lesson_candidate

        monkeypatch.setattr(
            settings, "autonomy_db_path", str(tmp_path / "a.db")
        )
        lid = seed_lesson_candidate()
        with autonomy_conn() as conn:
            update_lesson_status(
                conn, lid, "draft_ready", reason="d"
            )
            update_lesson_status(
                conn,
                lid,
                "approved",
                reason="a",
                pr_url="https://github.com/x/y/pull/1",
            )
            update_lesson_status(
                conn,
                lid,
                "applied",
                reason="pr opened",
                pr_url="https://github.com/x/y/pull/1",
            )

        app = FastAPI()
        app.include_router(dashboard_router)
        r = TestClient(app).get("/autonomy/learning")
        assert r.status_code == 200
        assert 'href="https://github.com/x/y/pull/1"' in r.text
        assert "PR →" in r.text

    def test_non_pr_row_no_pr_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from config import settings
        from learning_dashboard import router as dashboard_router
        from tests.conftest import seed_lesson_candidate

        monkeypatch.setattr(
            settings, "autonomy_db_path", str(tmp_path / "a.db")
        )
        seed_lesson_candidate()
        app = FastAPI()
        app.include_router(dashboard_router)
        r = TestClient(app).get("/autonomy/learning")
        assert "PR →" not in r.text
