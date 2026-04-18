#!/usr/bin/env python3
"""Tests for scripts/worktree_safety.py."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from worktree_safety import safe_remove_worktree  # noqa: E402


def _init_repo_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Initialise a git repo with one worktree under <tmp>/worktrees/feat.

    Returns ``(client_repo, worktree_dir)``.
    """
    client_repo = tmp_path / "client"
    client_repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(client_repo)], check=True
    )
    subprocess.run(
        ["git", "-C", str(client_repo), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(client_repo), "config", "user.name", "t"],
        check=True,
    )
    (client_repo / "README.md").write_text("hi\n")
    subprocess.run(
        ["git", "-C", str(client_repo), "add", "."], check=True
    )
    subprocess.run(
        ["git", "-C", str(client_repo), "commit", "-q", "-m", "init"],
        check=True,
    )

    worktrees_parent = tmp_path / "worktrees"
    worktrees_parent.mkdir()
    wt_dir = worktrees_parent / "feat"
    subprocess.run(
        [
            "git", "-C", str(client_repo), "worktree", "add",
            "-b", "feat", str(wt_dir),
        ],
        check=True,
    )
    return client_repo, wt_dir


def test_archives_uncommitted_diff_then_removes() -> None:
    """Dirty worktree with archive_dir set: patch is captured, worktree removed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client_repo, wt_dir = _init_repo_and_worktree(tmp_path)

        # Introduce an uncommitted change
        (wt_dir / "README.md").write_text("hi\nnew line\n")
        archive_dir = tmp_path / "archive"

        safe_remove_worktree(
            wt_dir,
            archive_dir=archive_dir,
            client_repo=client_repo,
        )

        # Worktree is gone
        assert not wt_dir.exists(), "worktree still on disk after remove"

        # Patch captured
        patch_path = archive_dir / wt_dir.name / "uncommitted.patch"
        assert patch_path.exists(), "uncommitted.patch not written"
        patch_text = patch_path.read_text()
        assert "new line" in patch_text
        assert "README.md" in patch_text


def test_noop_when_worktree_missing() -> None:
    """Nonexistent worktree path should be a silent no-op."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        worktrees_parent = tmp_path / "worktrees"
        worktrees_parent.mkdir()
        ghost = worktrees_parent / "ghost"
        # not created
        safe_remove_worktree(ghost, archive_dir=None)
        assert not ghost.exists()


def test_rejects_path_outside_worktrees_parent() -> None:
    """Path whose parent is not ``worktrees`` must raise ValueError."""
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "not-a-worktrees-dir" / "feat"
        bad.parent.mkdir()
        bad.mkdir()
        with pytest.raises(ValueError, match="worktrees"):
            safe_remove_worktree(bad, archive_dir=None)
        # Defensive rmtree did not fire
        assert bad.exists()


def test_no_archive_when_archive_dir_none_but_dirty() -> None:
    """Dirty worktree without archive_dir still removes, just skips archival."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client_repo, wt_dir = _init_repo_and_worktree(tmp_path)
        (wt_dir / "README.md").write_text("hi\nchanged\n")

        safe_remove_worktree(wt_dir, archive_dir=None, client_repo=client_repo)

        assert not wt_dir.exists()


if __name__ == "__main__":
    test_archives_uncommitted_diff_then_removes()
    test_noop_when_worktree_missing()
    test_rejects_path_outside_worktrees_parent()
    test_no_archive_when_archive_dir_none_but_dirty()
    print("All tests passed")
