"""Safe worktree removal helper.

Centralises the ``git worktree remove --force`` + ``shutil.rmtree``
sequence that every cleanup path previously copy-pasted. Adds two
safety improvements the inline version skipped:

1. **Archive uncommitted work** when the worktree has a dirty
   status — stash-like ``git diff HEAD`` snapshot written to
   ``<archive_dir>/<wt-name>/uncommitted.patch`` so a crash-window
   restart that triggers cleanup doesn't silently discard pending
   changes.

2. **Path-prefix guard** against ``rm -rf /..``. The worktrees root
   (``<wt_path>.parent``) must live beneath a well-known project
   directory (we require ``worktrees`` as the last path segment of
   the parent) before any destructive call runs. A typo'd caller
   that hands us ``/`` or ``~`` fails loudly instead of erasing
   the home directory.

Callers: ``scripts/spawn_team.py`` (pre-flight stale cleanup + post-run
removal), ``scripts/cleanup_worktree.py``, ``scripts/cleanup_stale_worktrees.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Name the worktrees parent dir must have to satisfy the safety prefix
# guard. Every caller constructs worktrees as ``<project>/worktrees/<branch>``
# so the parent's last segment is ``worktrees`` by convention.
_EXPECTED_PARENT_NAME = "worktrees"


# Type alias for an injectable ``subprocess.run``. Callers pass their own
# module-local ``subprocess.run`` reference so ``patch("<module>.subprocess.run")``
# in tests still flows through — previously cleanup_worktree.py called
# ``subprocess.run`` directly in its own namespace, and the existing test
# mocks rely on that. Defaults to the stdlib call when not provided.
_RunFn = Callable[..., Any]


def _run_git_diff(wt_path: Path, run_fn: _RunFn) -> str:
    """Return ``git diff HEAD`` output captured from ``wt_path``."""
    result = run_fn(
        ["git", "-C", str(wt_path), "diff", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout if isinstance(result.stdout, str) else result.stdout.decode()


def _status_porcelain(wt_path: Path, run_fn: _RunFn) -> list[str]:
    """Return non-empty lines from ``git status --porcelain`` in ``wt_path``."""
    result = run_fn(
        ["git", "-C", str(wt_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    raw = result.stdout if isinstance(result.stdout, str) else result.stdout.decode()
    return [line for line in raw.splitlines() if line.strip()]


def _untracked_files(wt_path: Path, run_fn: _RunFn) -> list[Path]:
    """Return untracked file paths relative to ``wt_path``."""
    result = run_fn(
        ["git", "-C", str(wt_path), "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    raw = result.stdout if isinstance(result.stdout, str) else result.stdout.decode()
    return [Path(line) for line in raw.splitlines() if line.strip()]


def _archive_untracked_files(
    wt_path: Path,
    archive_target: Path,
    untracked_files: list[Path],
) -> None:
    """Copy untracked files under archive_target/untracked."""
    untracked_root = archive_target / "untracked"
    for rel_path in untracked_files:
        source = (wt_path / rel_path).resolve()
        try:
            source.relative_to(wt_path.resolve())
        except ValueError:
            continue
        if not source.is_file():
            continue
        dest = untracked_root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)


def safe_remove_worktree(
    wt_path: Path,
    *,
    archive_dir: Path | None = None,
    client_repo: Path | None = None,
    run_fn: _RunFn | None = None,
) -> None:
    """Remove a git worktree, archiving uncommitted work first.

    Behavior:
      1. If ``wt_path`` does not exist OR has no ``.git`` child, the
         call is a no-op — we treat the path as "already gone / never
         was a worktree".
      2. ``git status --porcelain`` in ``wt_path``. When non-empty and
         ``archive_dir`` is set, write ``git diff HEAD`` to
         ``<archive_dir>/<wt-name>/uncommitted.patch`` so any pending
         changes can be recovered.
      3. Warn with the dirty file count so cleanup output flags the
         archival.
      4. ``git worktree remove --force`` via ``client_repo`` (falls
         back to ``wt_path.parent.parent`` when the caller did not
         supply a repo — matches the existing conventions where
         worktrees live at ``<client_repo>/../worktrees/<branch>``).
      5. ``shutil.rmtree(wt_path, ignore_errors=True)`` as a belt-and-
         braces fallback if the git command reported success but left
         the directory behind, or if the command failed.

    Raises ``ValueError`` when the path's parent name is not
    ``worktrees`` — a sanity check against typo'd callers that would
    otherwise rmtree unrelated directory trees.
    """
    # Callers pass their own ``subprocess.run`` so test mocks patched on
    # the caller's module keep working. Fall back to the stdlib call when
    # no runner is supplied.
    run = run_fn if run_fn is not None else subprocess.run

    # Step 7: prefix guard. Must fire BEFORE any destructive work so a
    # bad path never reaches ``shutil.rmtree``.
    if wt_path.parent.name != _EXPECTED_PARENT_NAME:
        raise ValueError(
            f"safe_remove_worktree: refusing to operate on {wt_path} — "
            f"parent directory is {wt_path.parent.name!r}, expected "
            f"{_EXPECTED_PARENT_NAME!r}. Callers must construct worktree "
            f"paths as <project>/worktrees/<branch>."
        )

    # Step 1: no-op if the directory doesn't exist.
    if not wt_path.exists():
        return

    # Step 2-3: archive + warn. Only probe status when the directory
    # looks like a git worktree; a bare directory would noisily fail
    # ``git status`` for no gain.
    is_worktree = (wt_path / ".git").exists()
    dirty_lines = _status_porcelain(wt_path, run) if is_worktree else []
    if dirty_lines:
        file_count = len(dirty_lines)
        print(
            f"[safe_remove] WARNING: {wt_path} has {file_count} uncommitted "
            f"file(s); archival {'enabled' if archive_dir else 'skipped (no archive_dir)'}"
        )
        if archive_dir is not None:
            patch_text = _run_git_diff(wt_path, run)
            untracked_files = _untracked_files(wt_path, run)
            if patch_text or untracked_files:
                archive_target = archive_dir / wt_path.name
                archive_target.mkdir(parents=True, exist_ok=True)
                if patch_text:
                    (archive_target / "uncommitted.patch").write_text(patch_text)
                if untracked_files:
                    _archive_untracked_files(wt_path, archive_target, untracked_files)
                    (archive_target / "untracked-files.txt").write_text(
                        "\n".join(str(path) for path in untracked_files) + "\n"
                    )
                print(
                    f"[safe_remove] Archived uncommitted work to "
                    f"{archive_target}"
                )

    # Step 4: git worktree remove --force.
    repo_for_git = client_repo if client_repo is not None else wt_path.parent.parent
    run(
        ["git", "-C", str(repo_for_git), "worktree", "remove", "--force", str(wt_path)],
        capture_output=True,
        check=False,
    )

    # Step 5-6: fallback rmtree. Idempotent — fine to call even if the
    # git command already removed the directory.
    shutil.rmtree(wt_path, ignore_errors=True)
