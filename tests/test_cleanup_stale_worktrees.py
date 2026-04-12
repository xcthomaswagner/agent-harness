"""Tests for scripts/cleanup_stale_worktrees.py."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the module under test
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from cleanup_stale_worktrees import main, _parse_worktree_list


def _make_porcelain_output(entries: list[dict[str, str]]) -> str:
    """Build git worktree list --porcelain output from a list of dicts."""
    blocks = []
    for entry in entries:
        lines = []
        if "worktree" in entry:
            lines.append(f"worktree {entry['worktree']}")
        if "HEAD" in entry:
            lines.append(f"HEAD {entry['HEAD']}")
        if "branch" in entry:
            lines.append(f"branch {entry['branch']}")
        if entry.get("bare"):
            lines.append("bare")
        if entry.get("detached"):
            lines.append("detached")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _create_worktree_dir(path: Path, age_hours: float = 0) -> None:
    """Create a fake worktree directory and set its mtime to simulate age."""
    path.mkdir(parents=True, exist_ok=True)
    if age_hours > 0:
        old_time = time.time() - (age_hours * 3600)
        os.utime(path, (old_time, old_time))


class TestParsePorcelainOutput:
    def test_parses_multiple_entries(self) -> None:
        output = _make_porcelain_output([
            {"worktree": "/repo", "HEAD": "abc123", "branch": "refs/heads/main"},
            {"worktree": "/repo-wt", "HEAD": "def456", "branch": "refs/heads/feature"},
        ])
        entries = _parse_worktree_list(output)
        assert len(entries) == 2
        assert entries[0]["worktree"] == "/repo"
        assert entries[1]["branch"] == "refs/heads/feature"

    def test_handles_empty_output(self) -> None:
        assert _parse_worktree_list("") == []


class TestIdentifiesStaleWorktrees:
    def test_identifies_stale_worktrees(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Stale worktrees (age > max) are identified in dry-run output."""
        client_repo = tmp_path / "repo"
        client_repo.mkdir()
        (client_repo / ".git").mkdir()

        # Create two worktrees: one stale (72h), one fresh (1h)
        wt_stale = tmp_path / "worktrees" / "ai-PROJ-100"
        wt_fresh = tmp_path / "worktrees" / "ai-PROJ-200"
        _create_worktree_dir(wt_stale, age_hours=72)
        _create_worktree_dir(wt_fresh, age_hours=1)

        porcelain = _make_porcelain_output([
            {"worktree": str(client_repo), "HEAD": "aaa", "branch": "refs/heads/main"},
            {"worktree": str(wt_stale), "HEAD": "bbb", "branch": "refs/heads/ai-PROJ-100"},
            {"worktree": str(wt_fresh), "HEAD": "ccc", "branch": "refs/heads/ai-PROJ-200"},
        ])

        def fake_run(cmd, **kwargs):
            if "worktree" in cmd and "list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=porcelain, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("cleanup_stale_worktrees.subprocess.run", side_effect=fake_run):
            with patch("sys.argv", ["prog", "--client-repo", str(client_repo), "--dry-run"]):
                main()

        output = capsys.readouterr().out
        assert "WOULD REMOVE" in output
        assert "ai-PROJ-100" in output
        assert "SKIP" in output
        assert "ai-PROJ-200" in output

    def test_skips_main_worktree(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The main repo worktree is never removed regardless of age."""
        client_repo = tmp_path / "repo"
        client_repo.mkdir()
        (client_repo / ".git").mkdir()

        # Make the main repo "old" — it should still never appear as a removal candidate
        old_time = time.time() - (100 * 3600)
        os.utime(client_repo, (old_time, old_time))

        porcelain = _make_porcelain_output([
            {"worktree": str(client_repo), "HEAD": "aaa", "branch": "refs/heads/main"},
        ])

        def fake_run(cmd, **kwargs):
            if "worktree" in cmd and "list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=porcelain, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("cleanup_stale_worktrees.subprocess.run", side_effect=fake_run):
            with patch("sys.argv", ["prog", "--client-repo", str(client_repo), "--dry-run"]):
                main()

        output = capsys.readouterr().out
        assert "WOULD REMOVE" not in output
        assert "Main repo" in output


class TestSkipsAliveAgents:
    def test_skips_alive_agents(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Worktrees with a live agent process are skipped."""
        client_repo = tmp_path / "repo"
        client_repo.mkdir()
        (client_repo / ".git").mkdir()

        wt = tmp_path / "worktrees" / "ai-PROJ-300"
        wt.mkdir(parents=True, exist_ok=True)

        # Write current PID to lock file
        lock_dir = wt / ".harness"
        lock_dir.mkdir(parents=True)
        (lock_dir / ".agent.lock").write_text(str(os.getpid()))

        # Set mtime AFTER creating subdirs (creating files updates parent mtime)
        old_time = time.time() - (72 * 3600)
        os.utime(wt, (old_time, old_time))

        porcelain = _make_porcelain_output([
            {"worktree": str(client_repo), "HEAD": "aaa", "branch": "refs/heads/main"},
            {"worktree": str(wt), "HEAD": "bbb", "branch": "refs/heads/ai-PROJ-300"},
        ])

        def fake_run(cmd, **kwargs):
            if "worktree" in cmd and "list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=porcelain, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        # os.kill with signal 0 on current PID will succeed (process alive)
        with patch("cleanup_stale_worktrees.subprocess.run", side_effect=fake_run):
            with patch("sys.argv", ["prog", "--client-repo", str(client_repo), "--dry-run"]):
                main()

        output = capsys.readouterr().out
        assert "SKIP (agent alive)" in output
        assert "ai-PROJ-300" in output
        assert "WOULD REMOVE" not in output


class TestArchivesLogsBeforeRemoval:
    def test_archives_logs_before_removal(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Logs are copied to trace-archive/ before worktree removal."""
        client_repo = tmp_path / "repo"
        client_repo.mkdir()
        (client_repo / ".git").mkdir()

        wt = tmp_path / "worktrees" / "ai-PROJ-400"
        wt.mkdir(parents=True, exist_ok=True)

        # Create logs
        logs_dir = wt / ".harness" / "logs"
        logs_dir.mkdir(parents=True)
        test_content = '{"phase":"plan","event":"start"}\n'
        (logs_dir / "pipeline.jsonl").write_text(test_content)

        # Set mtime AFTER creating all contents
        old_time = time.time() - (72 * 3600)
        os.utime(wt, (old_time, old_time))

        porcelain = _make_porcelain_output([
            {"worktree": str(client_repo), "HEAD": "aaa", "branch": "refs/heads/main"},
            {"worktree": str(wt), "HEAD": "bbb", "branch": "refs/heads/ai-PROJ-400"},
        ])

        def fake_run(cmd, **kwargs):
            if "worktree" in cmd and "list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=porcelain, stderr="")
            if "worktree" in cmd and "remove" in cmd:
                # Simulate successful removal by deleting the dir
                import shutil
                shutil.rmtree(wt, ignore_errors=True)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "worktree" in cmd and "prune" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("cleanup_stale_worktrees.subprocess.run", side_effect=fake_run):
            with patch("sys.argv", ["prog", "--client-repo", str(client_repo), "--max-age-hours", "48"]):
                main()

        output = capsys.readouterr().out
        assert "Archived logs" in output
        assert "Removing" in output

        # Verify the archive
        archive_dir = tmp_path / "trace-archive" / "ai-PROJ-400"
        assert archive_dir.exists()
        archived_file = archive_dir / "pipeline.jsonl"
        assert archived_file.exists()
        assert archived_file.read_text() == test_content
