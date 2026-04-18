"""Tests for scripts/cleanup_worktree.py."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from cleanup_worktree import main


class TestDryRun:
    def test_dry_run_prints_would_remove_without_deleting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--dry-run lists what would be deleted and exits 0 without touching the filesystem."""
        client_repo = tmp_path / "repo"
        client_repo.mkdir()
        worktree_dir = tmp_path / "worktrees" / "ai-PROJ-1"
        worktree_dir.mkdir(parents=True)

        with patch(
            "sys.argv",
            ["prog", "--client-repo", str(client_repo), "--branch-name", "ai-PROJ-1", "--dry-run"],
        ):
            main()

        output = capsys.readouterr().out
        assert "DRY RUN" in output
        assert "WOULD REMOVE" in output
        assert "WOULD PRUNE" in output
        # Nothing actually deleted
        assert worktree_dir.exists()

    def test_dry_run_missing_worktree_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--dry-run with a missing worktree still exits 0 (already cleaned up)."""
        client_repo = tmp_path / "repo"
        client_repo.mkdir()

        with patch(
            "sys.argv",
            ["prog", "--client-repo", str(client_repo), "--branch-name", "ai-PROJ-99", "--dry-run"],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0

        output = capsys.readouterr().out
        assert "not found" in output
        assert "WOULD REMOVE" not in output

    def test_dry_run_no_git_commands_executed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--dry-run must not call git worktree remove or git worktree prune."""
        client_repo = tmp_path / "repo"
        client_repo.mkdir()
        worktree_dir = tmp_path / "worktrees" / "ai-PROJ-2"
        worktree_dir.mkdir(parents=True)

        with patch("cleanup_worktree.subprocess.run") as mock_run:
            with patch(
                "sys.argv",
                [
                    "prog",
                    "--client-repo",
                    str(client_repo),
                    "--branch-name",
                    "ai-PROJ-2",
                    "--dry-run",
                ],
            ):
                main()

        mock_run.assert_not_called()


class TestNormalOperation:
    def test_removes_worktree_and_prunes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Normal (non-dry-run) run calls git worktree remove and git worktree prune."""
        client_repo = tmp_path / "repo"
        client_repo.mkdir()
        worktree_dir = tmp_path / "worktrees" / "ai-PROJ-3"
        worktree_dir.mkdir(parents=True)

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            # Simulate successful removal
            if "remove" in cmd:
                import shutil
                shutil.rmtree(worktree_dir, ignore_errors=True)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with patch("cleanup_worktree.subprocess.run", side_effect=fake_run):
            with patch(
                "sys.argv",
                ["prog", "--client-repo", str(client_repo), "--branch-name", "ai-PROJ-3"],
            ):
                main()

        output = capsys.readouterr().out
        assert "Removing worktree" in output
        assert "Pruning" in output
        assert "Done" in output
        assert any("remove" in c for c in calls)
        assert any("prune" in c for c in calls)

    def test_preserve_skips_removal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--preserve exits early and leaves the worktree untouched."""
        client_repo = tmp_path / "repo"
        client_repo.mkdir()
        worktree_dir = tmp_path / "worktrees" / "ai-PROJ-4"
        worktree_dir.mkdir(parents=True)

        with patch("cleanup_worktree.subprocess.run") as mock_run:
            with patch(
                "sys.argv",
                ["prog", "--client-repo", str(client_repo), "--branch-name", "ai-PROJ-4", "--preserve"],
            ):
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code == 0
        mock_run.assert_not_called()
        assert worktree_dir.exists()
        output = capsys.readouterr().out
        assert "PRESERVED" in output

    def test_missing_worktree_exits_zero_without_git(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When worktree doesn't exist, exits 0 and runs no git commands."""
        client_repo = tmp_path / "repo"
        client_repo.mkdir()

        with patch("cleanup_worktree.subprocess.run") as mock_run:
            with patch(
                "sys.argv",
                ["prog", "--client-repo", str(client_repo), "--branch-name", "ai-PROJ-no-exist"],
            ):
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code == 0
        mock_run.assert_not_called()
        output = capsys.readouterr().out
        assert "not found" in output
