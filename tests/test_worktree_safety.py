#!/usr/bin/env python3
"""Tests for scripts/worktree_safety.py."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import json
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from worktree_safety import safe_remove_worktree  # noqa: E402


def _init_repo_and_worktree(
    tmp_path: Path,
    *,
    branch: str = "feat",
) -> tuple[Path, Path]:
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
    wt_dir = worktrees_parent / branch
    subprocess.run(
        [
            "git", "-C", str(client_repo), "worktree", "add",
            "-b", branch, str(wt_dir),
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


def test_archives_untracked_files_then_removes() -> None:
    """Untracked files are not in git diff HEAD, so archive them separately."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client_repo, wt_dir = _init_repo_and_worktree(tmp_path)

        untracked = wt_dir / "notes" / "scratch.txt"
        untracked.parent.mkdir()
        untracked.write_text("important scratch\n")
        archive_dir = tmp_path / "archive"

        safe_remove_worktree(
            wt_dir,
            archive_dir=archive_dir,
            client_repo=client_repo,
        )

        archive_target = archive_dir / wt_dir.name
        assert (archive_target / "untracked-files.txt").read_text() == (
            "notes/scratch.txt\n"
        )
        assert (
            archive_target / "untracked" / "notes" / "scratch.txt"
        ).read_text() == "important scratch\n"
        assert not wt_dir.exists()


def test_dirty_manifest_classifies_harness_and_generated_artifacts() -> None:
    """Archived dirty work includes operator-friendly artifact categories."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client_repo, wt_dir = _init_repo_and_worktree(tmp_path)

        (wt_dir / "CLAUDE.md").write_text("runtime\n<!-- harness-injected -->\n")
        (wt_dir / "next-env.d.ts").write_text("/// <reference types=\"next\" />\n")
        (wt_dir / "src").mkdir()
        (wt_dir / "src" / "feature.ts").write_text("export const x = 1\n")
        archive_dir = tmp_path / "archive"

        safe_remove_worktree(
            wt_dir,
            archive_dir=archive_dir,
            client_repo=client_repo,
        )

        manifest = json.loads(
            (archive_dir / wt_dir.name / "dirty-worktree-manifest.json").read_text()
        )
        categories = {item["path"]: item["category"] for item in manifest["items"]}
        assert categories["CLAUDE.md"] == "harness_injected"
        assert categories["next-env.d.ts"] == "generated_artifact"
        assert categories["src/feature.ts"] == "uncommitted_source"


def test_allows_nested_ai_branch_worktree_paths() -> None:
    """Branch names like ai/TICKET create nested worktree paths and must clean up."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client_repo, wt_dir = _init_repo_and_worktree(tmp_path, branch="ai/TEST-1")

        (wt_dir / "README.md").write_text("hi\nnested branch change\n")
        archive_dir = tmp_path / "archive"

        safe_remove_worktree(
            wt_dir,
            archive_dir=archive_dir,
            client_repo=client_repo,
        )

        assert not wt_dir.exists()
        assert (archive_dir / "ai" / wt_dir.name / "uncommitted.patch").exists()


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


def test_rejects_traversal_outside_client_worktrees_root() -> None:
    """A traversal-shaped path must not resolve outside the repo worktrees root."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client_repo = tmp_path / "client"
        client_repo.mkdir()
        bad = tmp_path / "worktrees" / ".." / "client"

        with pytest.raises(ValueError, match="outside expected worktrees root"):
            safe_remove_worktree(bad, archive_dir=None, client_repo=client_repo)

        assert client_repo.exists()


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
