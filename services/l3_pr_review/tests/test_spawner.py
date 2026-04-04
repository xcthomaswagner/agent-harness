"""Tests for SessionSpawner — verifies CLI invocation and error handling."""

from __future__ import annotations

from unittest.mock import patch

from spawner import SessionSpawner


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

    def test_truncates_diff_to_2000_chars(self) -> None:
        spawner = SessionSpawner(repo_path="/tmp/repo")
        long_diff = "x" * 5000

        with patch("spawner.subprocess.Popen") as mock_popen:
            spawner.spawn_pr_review(pr_number=1, pr_diff=long_diff, ticket_context="")

        cmd = mock_popen.call_args[0][0]
        prompt = cmd[cmd.index("-p") + 1]
        # The diff portion should be truncated
        assert len(prompt) < 5000


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
