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
    RevertPRInputs,
    _build_branch_name,
    _build_revert_branch_name,
    _compose_pr_body,
    _edited_paths_from_diff,
    _parse_pr_url,
    _stamp_lesson_id,
    open_pr_for_lesson,
    open_revert_pr_for_lesson,
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

    def test_deletion_uses_pre_image_path(self) -> None:
        """Previously /dev/null post-images were dropped entirely,
        so the caller's ``git add -- *edited`` missed the deletion
        and the commit silently lost it. Now the ``--- a/<path>``
        pre-image is surfaced so ``git add`` stages the delete.
        """
        diff = "--- a/foo.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        assert _edited_paths_from_diff(diff) == ["foo.md"]

    def test_dedupes(self) -> None:
        diff = (
            "--- a/foo.md\n+++ b/foo.md\n@@\n+x\n"
            "--- a/bar.md\n+++ b/bar.md\n@@\n+y\n"
            "--- a/foo.md\n+++ b/foo.md\n@@\n+z\n"
        )
        out = _edited_paths_from_diff(diff)
        assert out == ["foo.md", "bar.md"]

    def test_mixed_modify_delete_add(self) -> None:
        """A diff with modify + delete + add must surface all three paths
        so the caller's ``git add -- *edited`` stages each change.
        """
        diff = (
            "--- a/foo.md\n+++ b/foo.md\n@@\n+x\n"
            "--- a/baz.md\n+++ /dev/null\n@@\n-gone\n"
            "--- /dev/null\n+++ b/new.md\n@@\n+fresh\n"
        )
        assert _edited_paths_from_diff(diff) == ["foo.md", "baz.md", "new.md"]

    def test_rename_surfaces_both_old_and_new_path(self) -> None:
        """Regression: a rename diff used to surface only the
        ``+++ b/new.md`` post-image. ``git add -- new.md`` staged the
        add but left the deletion of ``old.md`` unstaged — the commit
        carried the new file but not the rename. Both paths now
        surface so ``git add`` stages both sides.
        """
        diff = (
            "diff --git a/runtime/skills/old.md b/runtime/skills/new.md\n"
            "similarity index 95%\n"
            "rename from runtime/skills/old.md\n"
            "rename to runtime/skills/new.md\n"
            "--- a/runtime/skills/old.md\n"
            "+++ b/runtime/skills/new.md\n"
            "@@\n-x\n+y\n"
        )
        paths = _edited_paths_from_diff(diff)
        assert "runtime/skills/old.md" in paths
        assert "runtime/skills/new.md" in paths


class TestResolveAuthToken:
    """resolve_auth_token precedence + whitespace handling."""

    def test_agent_token_preferred_over_github_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from learning_miner._subprocess import resolve_auth_token

        monkeypatch.setenv("AGENT_GH_TOKEN", "agent-tok")
        monkeypatch.setenv("GITHUB_TOKEN", "github-tok")
        assert resolve_auth_token() == "agent-tok"

    def test_falls_back_to_github_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from learning_miner._subprocess import resolve_auth_token

        monkeypatch.delenv("AGENT_GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "github-tok")
        assert resolve_auth_token() == "github-tok"

    def test_whitespace_agent_token_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: a .env with ``AGENT_GH_TOKEN=" "`` used to win
        precedence over GITHUB_TOKEN because whitespace is truthy.
        gh would then reject the bogus token with a cryptic error and
        the ``if not token:`` push guard let it through.
        """
        from learning_miner._subprocess import resolve_auth_token

        monkeypatch.setenv("AGENT_GH_TOKEN", "   ")
        monkeypatch.setenv("GITHUB_TOKEN", "github-tok")
        assert resolve_auth_token() == "github-tok"

    def test_both_empty_or_whitespace_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from learning_miner._subprocess import resolve_auth_token

        monkeypatch.setenv("AGENT_GH_TOKEN", "   ")
        monkeypatch.setenv("GITHUB_TOKEN", "")
        assert resolve_auth_token() == ""

    def test_strips_surrounding_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from learning_miner._subprocess import resolve_auth_token

        monkeypatch.setenv("AGENT_GH_TOKEN", "  ghp_real\n")
        assert resolve_auth_token() == "ghp_real"


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

    def test_extracts_embedded_url(self) -> None:
        """Some gh versions emit ``Opened: <url>`` on a single line.

        Regression: the previous implementation required the URL to
        start the line, so an embedded URL was missed and callers fell
        back to the pr-list recovery path.
        """
        text = "Opened: https://github.com/x/y/pull/99 successfully.\n"
        assert _parse_pr_url(text) == "https://github.com/x/y/pull/99"

    def test_picks_last_url_when_multiple(self) -> None:
        """Multiple URLs — take the final one (the newly-created PR)."""
        text = (
            "Referencing previous: https://github.com/x/y/pull/5\n"
            "Created: https://github.com/x/y/pull/42\n"
        )
        assert _parse_pr_url(text) == "https://github.com/x/y/pull/42"

    def test_comma_joined_urls_match_separately(self) -> None:
        """Regression: ``[^\\s]+`` was too greedy and matched across
        commas — two comma-joined URLs parsed as ONE concatenated
        "URL". Tighten the char class so adjacent URLs separate.
        """
        text = "https://github.com/x/y/pull/1,https://github.com/x/z/pull/2"
        # After the tightening, should pick the LAST discrete URL.
        assert _parse_pr_url(text) == "https://github.com/x/z/pull/2"


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

    def test_collapses_duplicate_lesson_id_lines(self, tmp_path: Path) -> None:
        """Regression: a file whose frontmatter already had two
        ``lesson_id:`` lines (from a prior stamping bug) used to end up
        with two ``lesson_id: LSN-NEW`` lines after re-stamping.
        Collapse to one.
        """
        f = tmp_path / "x.md"
        f.write_text(
            "---\nname: foo\n"
            "lesson_id: LSN-OLD1\n"
            "lesson_id: LSN-OLD2\n"
            "---\n\nbody"
        )
        _stamp_lesson_id(f, "LSN-NEW")
        text = f.read_text()
        # Exactly one lesson_id line — not two.
        assert text.count("lesson_id:") == 1
        assert "lesson_id: LSN-NEW" in text

    def test_skips_non_markdown(self, tmp_path: Path) -> None:
        f = tmp_path / "config.yaml"
        f.write_text("client: x")
        assert _stamp_lesson_id(f, "LSN-abc") is False
        assert f.read_text() == "client: x"


# ---- end-to-end (real git, mocked gh) --------------------------------


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


class TestBaseBranchForking:
    """Regression: approve + revert flows used to ``git checkout -b
    <branch>`` without naming a start point, so the new branch forked
    off the remote default HEAD regardless of inputs.base_branch.
    A non-default base produced PR diffs that included unrelated
    default-branch commits.
    """

    def test_dry_run_forks_from_named_base_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin = tmp_path / "origin"
        origin.mkdir()
        # Default branch = main; add a `release/candidate` branch
        # that diverges by one commit. The PR should fork from
        # release/candidate, not main.
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=origin, check=True, capture_output=True,
        )
        skills = origin / "runtime" / "skills" / "code-review"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text(
            "# Code Review\n\n## Review Checklist\n\n"
            "- Check docstrings.\n"
            "- Verify tests.\n"
        )
        subprocess.run(["git", "add", "."], cwd=origin, check=True, capture_output=True)
        subprocess.run(
            ["git",
             "-c", "user.email=init@test", "-c", "user.name=init",
             "commit", "-m", "init"],
            cwd=origin, check=True, capture_output=True,
        )
        # Branch off for the release/candidate line.
        subprocess.run(
            ["git", "checkout", "-b", "release/candidate"],
            cwd=origin, check=True, capture_output=True,
        )
        # Same file content on the branch — the dry-run's commit
        # must apply cleanly to this base.
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=origin, check=True, capture_output=True,
        )

        inputs = OpenPRInputs(
            lesson_id="LSN-basebr01",
            unified_diff=_valid_diff(),
            scope_key="xcsf30|salesforce|security|*.cls",
            detector_name="human_issue_cluster",
            rationale_md="r",
            evidence_trace_ids=[],
            harness_repo_url=str(origin),
            base_branch="release/candidate",
            dry_run=True,
        )
        result = open_pr_for_lesson(inputs)
        assert result.success is True, result.error
        assert result.dry_run is True
        # Commit sha is now reachable from release/candidate + one
        # revert/edit commit. The --branch clone flag made the
        # checkout origin/release/candidate resolvable.
        assert result.branch == "learning/lesson-LSN-basebr01"


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


class TestRecoveryUsesStateAll:
    """``_recover_pr_url_by_branch`` should find open OR merged OR
    closed PRs — a prior run may have left a PR in any state.
    """

    def test_list_command_includes_state_all(
        self, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_git = __import__(
            "learning_miner.pr_opener", fromlist=["_git"]
        )._git
        seen_args: list[list[str]] = []

        def fake_git(args, **kw):
            if args[:1] == ["push"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_git(args, **kw)

        def fake_gh(args, **kw):
            if args[:2] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="(no url here)\n", stderr="",
                )
            if args[:2] == ["pr", "list"]:
                seen_args.append(list(args))
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout='[{"url": "https://github.com/x/y/pull/7"}]\n',
                    stderr="",
                )
            raise AssertionError(f"unexpected gh args: {args}")

        monkeypatch.setattr("learning_miner.pr_opener._git", fake_git)
        monkeypatch.setattr("learning_miner.pr_opener._gh", fake_gh)
        monkeypatch.setenv("AGENT_GH_TOKEN", "fake-token")

        result = open_pr_for_lesson(_base_inputs(origin_repo, dry_run=False))
        assert result.success is True
        assert result.pr_url == "https://github.com/x/y/pull/7"
        # Verify --state all was passed so open+closed+merged all match.
        assert seen_args, "pr list was never called"
        assert "--state" in seen_args[0]
        assert "all" in seen_args[0]


class TestUrlRecoveryFallback:
    """When ``gh pr create`` exits 0 but doesn't echo a URL, the
    opener must (a) fall back to ``gh pr list --head --json url``,
    (b) if still nothing, best-effort delete the pushed branch so
    retries don't trip on ``branch exists``.
    """

    def test_recovers_url_via_pr_list(
        self, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Make git push a no-op so we don't need a real remote.
        real_git = __import__(
            "learning_miner.pr_opener", fromlist=["_git"]
        )._git

        def fake_git(args, **kw):
            if args[:1] == ["push"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_git(args, **kw)

        call_count = {"create": 0, "list": 0}

        def fake_gh(args, **kw):
            if args[:2] == ["pr", "create"]:
                call_count["create"] += 1
                # Exit 0 but NO URL in stdout — the failure mode.
                return subprocess.CompletedProcess(
                    args, 0, stdout="(no url here)\n", stderr="",
                )
            if args[:2] == ["pr", "list"]:
                call_count["list"] += 1
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout='[{"url": "https://github.com/x/y/pull/77"}]\n',
                    stderr="",
                )
            raise AssertionError(f"unexpected gh args: {args}")

        monkeypatch.setattr("learning_miner.pr_opener._git", fake_git)
        monkeypatch.setattr("learning_miner.pr_opener._gh", fake_gh)
        monkeypatch.setenv("AGENT_GH_TOKEN", "fake-token")

        inputs = _base_inputs(origin_repo, dry_run=False)
        result = open_pr_for_lesson(inputs)
        assert result.success is True
        assert result.pr_url == "https://github.com/x/y/pull/77"
        assert call_count["create"] == 1
        assert call_count["list"] == 1

    def test_deletes_remote_branch_when_recovery_fails(
        self, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_git = __import__(
            "learning_miner.pr_opener", fromlist=["_git"]
        )._git
        delete_calls: list[list[str]] = []

        def fake_git(args, **kw):
            if args[:1] == ["push"] and "--delete" in args:
                delete_calls.append(list(args))
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:1] == ["push"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_git(args, **kw)

        def fake_gh(args, **kw):
            if args[:2] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="(no url here)\n", stderr="",
                )
            if args[:2] == ["pr", "list"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="[]\n", stderr="",
                )
            raise AssertionError(f"unexpected gh args: {args}")

        monkeypatch.setattr("learning_miner.pr_opener._git", fake_git)
        monkeypatch.setattr("learning_miner.pr_opener._gh", fake_gh)
        monkeypatch.setenv("AGENT_GH_TOKEN", "fake-token")

        inputs = _base_inputs(origin_repo, dry_run=False)
        result = open_pr_for_lesson(inputs)
        assert result.success is False
        assert "no URL detected" in result.error
        # Delete-branch call fired exactly once.
        assert len(delete_calls) == 1
        assert "--delete" in delete_calls[0]
        assert result.branch in delete_calls[0]


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

    def test_disallowed_diff_path_rejected_before_clone(
        self, origin_repo: Path
    ) -> None:
        """Defense in depth: a tampered proposed_delta that slips a
        services/ or .github/ target past the drafter must still be
        rejected at the pr_opener boundary. Drafter validates at draft
        time; pr_opener revalidates so a hand-edited DB row can't
        write arbitrary paths during apply.
        """
        bad = OpenPRInputs(
            lesson_id="LSN-tampered",
            unified_diff=(
                "--- a/services/l1_preprocessing/main.py\n"
                "+++ b/services/l1_preprocessing/main.py\n"
                "@@ -1 +1 @@\n-old\n+new\n"
            ),
            scope_key="s",
            detector_name="d",
            rationale_md="",
            evidence_trace_ids=[],
            harness_repo_url=str(origin_repo),
            dry_run=True,
        )
        result = open_pr_for_lesson(bad)
        assert result.success is False
        # Must fail BEFORE anything touches disk — the error is the
        # drafter's path-allowlist message, not a git error.
        assert "disallowed" in result.error or "allowed" in result.error

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

    def test_gh_create_exit_nonzero_deletes_branch(
        self, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: gh pr create exit-nonzero used to return an error
        WITHOUT deleting the pushed remote branch. The retry then hit
        "branch already exists" at git push --set-upstream and the
        lesson could never recover. We now attempt URL recovery first,
        and fall back to deleting the branch on failure.
        """
        real_git = __import__(
            "learning_miner.pr_opener", fromlist=["_git"]
        )._git
        delete_calls: list[list[str]] = []

        def fake_git(args, **kw):
            if args[:1] == ["push"] and "--delete" in args:
                delete_calls.append(list(args))
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:1] == ["push"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_git(args, **kw)

        def fake_gh(args, **kw):
            if args[:2] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    args, 1,
                    stdout="", stderr="upstream error: 500 from api\n",
                )
            if args[:2] == ["pr", "list"]:
                # No PR found — forces the delete-branch fallback.
                return subprocess.CompletedProcess(
                    args, 0, stdout="[]\n", stderr="",
                )
            raise AssertionError(f"unexpected gh args: {args}")

        monkeypatch.setattr("learning_miner.pr_opener._git", fake_git)
        monkeypatch.setattr("learning_miner.pr_opener._gh", fake_gh)
        monkeypatch.setenv("AGENT_GH_TOKEN", "fake-token")

        inputs = _base_inputs(origin_repo, dry_run=False)
        result = open_pr_for_lesson(inputs)
        assert result.success is False
        assert "gh pr create" in result.error
        # Branch delete fired so retries don't trip on "branch exists".
        assert len(delete_calls) == 1
        assert "--delete" in delete_calls[0]
        assert result.branch in delete_calls[0]

    def test_gh_create_exit_nonzero_recovers_when_pr_exists(
        self, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If gh pr create fails because the PR already exists, recovery
        via pr list should return the existing URL as a success.

        Common case: a prior attempt pushed + created the PR but the
        process died before the response was captured; a retry runs the
        same push (no-op) + create (fails "already exists"), and
        recovery finds the existing PR.
        """
        real_git = __import__(
            "learning_miner.pr_opener", fromlist=["_git"]
        )._git

        def fake_git(args, **kw):
            if args[:1] == ["push"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_git(args, **kw)

        def fake_gh(args, **kw):
            if args[:2] == ["pr", "create"]:
                return subprocess.CompletedProcess(
                    args, 1,
                    stdout="", stderr="a pull request for branch already exists",
                )
            if args[:2] == ["pr", "list"]:
                return subprocess.CompletedProcess(
                    args, 0,
                    stdout='[{"url": "https://github.com/x/y/pull/55"}]\n',
                    stderr="",
                )
            raise AssertionError(f"unexpected gh args: {args}")

        monkeypatch.setattr("learning_miner.pr_opener._git", fake_git)
        monkeypatch.setattr("learning_miner.pr_opener._gh", fake_gh)
        monkeypatch.setenv("AGENT_GH_TOKEN", "fake-token")

        result = open_pr_for_lesson(_base_inputs(origin_repo, dry_run=False))
        assert result.success is True
        assert result.pr_url == "https://github.com/x/y/pull/55"


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


# ---- revert branch-name validation -----------------------------------


class TestBuildRevertBranchName:
    def test_valid_lesson_id(self) -> None:
        assert (
            _build_revert_branch_name("LSN-a1b2c3d4")
            == "learning/revert-LSN-a1b2c3d4"
        )

    def test_unsafe_id_raises(self) -> None:
        with pytest.raises(ValueError, match="unsafe branch name"):
            _build_revert_branch_name("LSN-; rm -rf /")

    def test_double_dot_rejected(self) -> None:
        with pytest.raises(ValueError):
            _build_revert_branch_name("LSN-a..b")


# ---- revert PR flow --------------------------------------------------


def _origin_with_committed_change(tmp_path: Path) -> tuple[Path, str]:
    """Build an origin repo where a previous commit introduces a change
    we can later revert. Returns (origin_path, sha_to_revert).
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=origin,
        check=True, capture_output=True,
    )
    f = origin / "runtime" / "skills" / "code-review" / "SKILL.md"
    f.parent.mkdir(parents=True)
    f.write_text("# initial\n")
    subprocess.run(
        ["git", "add", "."], cwd=origin, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=init@t", "-c", "user.name=init",
         "commit", "-m", "init"],
        cwd=origin, check=True, capture_output=True,
    )
    # Lesson-opener-like commit we'll revert.
    f.write_text("# initial\n\n- bad rule\n")
    subprocess.run(
        ["git", "add", "."], cwd=origin, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=xcagent@x.com", "-c", "user.name=agent",
         "commit", "-m", "chore(learning): LSN-1"],
        cwd=origin, check=True, capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return origin, sha


class TestOpenRevertPr:
    def test_malformed_sha_fails_fast(
        self, tmp_path: Path
    ) -> None:
        inputs = RevertPRInputs(
            lesson_id="LSN-a1b2c3d4",
            merged_commit_sha="not-a-sha",
            verdict="regressed",
            reason_md="x",
            harness_repo_url="https://example.invalid/x.git",
            dry_run=True,
        )
        out = open_revert_pr_for_lesson(inputs)
        assert out.success is False
        assert "malformed" in out.error

    def test_unsafe_lesson_id_fails_fast(self, tmp_path: Path) -> None:
        inputs = RevertPRInputs(
            lesson_id="LSN-; rm",
            merged_commit_sha="a" * 40,
            verdict="regressed",
            reason_md="x",
            harness_repo_url="https://example.invalid/x.git",
            dry_run=True,
        )
        out = open_revert_pr_for_lesson(inputs)
        assert out.success is False
        assert "unsafe" in out.error

    def test_dry_run_reverts_locally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin, sha = _origin_with_committed_change(tmp_path)
        # gh must not run in dry-run.
        monkeypatch.setattr(
            "learning_miner.pr_opener._gh",
            MagicMock(
                side_effect=AssertionError("gh must not run in dry-run")
            ),
        )
        inputs = RevertPRInputs(
            lesson_id="LSN-a1b2c3d4",
            merged_commit_sha=sha,
            verdict="regressed",
            reason_md="metrics dropped",
            harness_repo_url=str(origin),
            dry_run=True,
        )
        out = open_revert_pr_for_lesson(inputs)
        assert out.success is True
        assert out.dry_run is True
        assert out.branch == "learning/revert-LSN-a1b2c3d4"
        assert out.commit_sha  # revert produced a commit

    def test_real_run_calls_gh_pr_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin, sha = _origin_with_committed_change(tmp_path)
        # Capture gh; also stub git push since the test origin isn't
        # a push-able remote. We let git clone/checkout/revert run
        # for real — only gh + push are mocked.
        real_git = __import__("learning_miner.pr_opener", fromlist=["_git"])._git

        def fake_git(args, **kw):
            if args[:1] == ["push"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_git(args, **kw)

        fake_gh = MagicMock(
            return_value=subprocess.CompletedProcess(
                ["gh"], 0,
                stdout="https://github.com/x/y/pull/99\n",
                stderr="",
            )
        )
        monkeypatch.setattr("learning_miner.pr_opener._git", fake_git)
        monkeypatch.setattr("learning_miner.pr_opener._gh", fake_gh)
        monkeypatch.setenv("AGENT_GH_TOKEN", "fake-token")
        inputs = RevertPRInputs(
            lesson_id="LSN-a1b2c3d4",
            merged_commit_sha=sha,
            verdict="human_reedit",
            reason_md="Alice edited the file",
            harness_repo_url=str(origin),
            dry_run=False,
        )
        out = open_revert_pr_for_lesson(inputs)
        assert out.success is True
        assert out.pr_url == "https://github.com/x/y/pull/99"
        fake_gh.assert_called_once()
        # Title includes verdict + lesson id.
        called_args = fake_gh.call_args[0][0]
        title_idx = called_args.index("--title") + 1
        assert "LSN-a1b2c3d4" in called_args[title_idx]
        assert "human_reedit" in called_args[title_idx]

    def test_no_token_refuses_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin, sha = _origin_with_committed_change(tmp_path)
        monkeypatch.delenv("AGENT_GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        inputs = RevertPRInputs(
            lesson_id="LSN-a1b2c3d4",
            merged_commit_sha=sha,
            verdict="regressed",
            reason_md="x",
            harness_repo_url=str(origin),
            dry_run=False,
        )
        out = open_revert_pr_for_lesson(inputs)
        assert out.success is False
        assert "token" in out.error.lower() or "credentials" in out.error.lower()

    def test_reviewers_passed_to_gh_pr_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin, sha = _origin_with_committed_change(tmp_path)
        real_git = __import__(
            "learning_miner.pr_opener", fromlist=["_git"]
        )._git

        def fake_git(args, **kw):
            if args[:1] == ["push"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_git(args, **kw)

        fake_gh = MagicMock(
            return_value=subprocess.CompletedProcess(
                ["gh"], 0,
                stdout="https://github.com/x/y/pull/100\n",
                stderr="",
            )
        )
        monkeypatch.setattr("learning_miner.pr_opener._git", fake_git)
        monkeypatch.setattr("learning_miner.pr_opener._gh", fake_gh)
        monkeypatch.setenv("AGENT_GH_TOKEN", "fake-token")
        inputs = RevertPRInputs(
            lesson_id="LSN-a1b2c3d4",
            merged_commit_sha=sha,
            verdict="regressed",
            reason_md="x",
            harness_repo_url=str(origin),
            dry_run=False,
            reviewers=("xcthomaswagner", "xcentium/platform-reviewers"),
        )
        out = open_revert_pr_for_lesson(inputs)
        assert out.success is True
        called_args = fake_gh.call_args[0][0]
        assert "--reviewer" in called_args
        assert "xcthomaswagner" in called_args
        assert "xcentium/platform-reviewers" in called_args


class TestReviewerFlags:
    """Helper that expands tuple → gh CLI flag list."""

    def test_empty(self) -> None:
        from learning_miner.pr_opener import _reviewer_flags
        assert _reviewer_flags(()) == []

    def test_drops_blank_entries(self) -> None:
        from learning_miner.pr_opener import _reviewer_flags
        assert _reviewer_flags(("", "alice", "  ")) == [
            "--reviewer", "alice",
        ]

    def test_preserves_order(self) -> None:
        from learning_miner.pr_opener import _reviewer_flags
        assert _reviewer_flags(("a", "b", "c")) == [
            "--reviewer", "a",
            "--reviewer", "b",
            "--reviewer", "c",
        ]

    def test_dedupes_duplicates(self) -> None:
        """Regression: a misconfigured env (e.g. ``a,b,a``) passed
        duplicates straight to ``gh``, which some versions reject
        with "already requested review from @a" — failing the whole
        PR create. Dedup while preserving first-seen order.
        """
        from learning_miner.pr_opener import _reviewer_flags
        assert _reviewer_flags(("a", "b", "a", "c", "b")) == [
            "--reviewer", "a",
            "--reviewer", "b",
            "--reviewer", "c",
        ]


class TestSafeStderrTail:
    """Redact-before-truncate helper shared by outcomes + pr_opener."""

    def test_redacts_full_url_even_when_boundary_cuts_it(self) -> None:
        """Regression: each logger used
        ``redact_token_urls(stderr[-200:])`` — redact AFTER truncate.
        A ``https://user:tok@host`` URL straddling the boundary
        wouldn't match the regex on the clipped slice, and the
        partial token leaked.
        """
        from learning_miner._subprocess import safe_stderr_tail

        # Craft a stderr with a token URL positioned so its prefix
        # falls OUTSIDE the last 200 chars but trailing `@host` is
        # inside. Old behavior would leak ``user:tok`` because the
        # regex (``https://[^/@\s]*@``) can't anchor.
        prefix = "x" * 180
        token_url = "https://user:tok_SECRET@github.com/x/y.git"
        noise = "\nsome trailing error output here."
        raw = prefix + token_url + noise
        tail = safe_stderr_tail(raw, limit=200)
        assert "tok_SECRET" not in tail
        assert "github.com/x/y.git" in tail

    def test_none_stderr_returns_empty(self) -> None:
        from learning_miner._subprocess import safe_stderr_tail

        assert safe_stderr_tail(None) == ""

    def test_short_stderr_passes_through(self) -> None:
        from learning_miner._subprocess import safe_stderr_tail

        assert safe_stderr_tail("ok", limit=200) == "ok"
