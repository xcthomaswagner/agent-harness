#!/usr/bin/env python3
"""Remove stale worktrees based on age (TTL-based cleanup).

Iterates all worktrees for a client repo, skips the main repo and any
worktrees with a live agent process, archives logs, and removes stale ones.

Usage:
    python scripts/cleanup_stale_worktrees.py \
        --client-repo <path> \
        [--max-age-hours 48] \
        [--dry-run]
"""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from worktree_safety import safe_remove_worktree  # noqa: E402


def _parse_worktree_list(output: str) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` output into a list of dicts.

    Each dict has at least a ``worktree`` key. Optional keys: ``HEAD``,
    ``branch``, ``bare``, ``detached``.
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["worktree"] = line[len("worktree "):]
        elif line.startswith("HEAD "):
            current["HEAD"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):]
        elif line == "bare":
            current["bare"] = "true"
        elif line == "detached":
            current["detached"] = "true"
    if current:
        entries.append(current)
    return entries


def _is_agent_alive(worktree_path: Path) -> bool:
    """Check if an agent process is still running in this worktree."""
    lock_file = worktree_path / ".harness" / ".agent.lock"
    if not lock_file.exists():
        return False
    try:
        content = lock_file.read_text().strip()
        pid = int(content) if content.isdigit() else 0
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        # PermissionError (EPERM) means the process exists but we can't signal it
        if exc.errno == errno.EPERM:
            return True
        return False
    except (ValueError, TypeError):
        return False


def _worktree_age_hours(worktree_path: Path) -> float:
    """Return the age of a worktree directory in hours based on mtime."""
    try:
        mtime = worktree_path.stat().st_mtime
        return (time.time() - mtime) / 3600.0
    except OSError:
        return 0.0


def _archive_logs(worktree_path: Path, archive_base: Path) -> bool:
    """Copy .harness/logs/ to the trace archive. Returns True if anything was archived."""
    logs_dir = worktree_path / ".harness" / "logs"
    if not logs_dir.exists():
        return False

    archive_dir = archive_base / worktree_path.name
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for log_file in logs_dir.iterdir():
            if log_file.is_file():
                shutil.copy2(log_file, archive_dir / log_file.name)
        return True
    except OSError as exc:
        print(f"[cleanup] WARNING: Log archival failed for {worktree_path.name}: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove stale worktrees based on age (TTL-based cleanup)"
    )
    parser.add_argument(
        "--client-repo", required=True, help="Path to the client git repository"
    )
    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=48,
        help="Positive maximum age in hours before a worktree is considered stale (default: 48)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be removed without actually removing",
    )
    args = parser.parse_args()

    client_repo = Path(args.client_repo).resolve()
    max_age_hours = args.max_age_hours
    dry_run = args.dry_run

    if max_age_hours < 1:
        print("[cleanup] Error: --max-age-hours must be a positive integer")
        sys.exit(1)

    if not (client_repo / ".git").exists() and not (client_repo / ".git").is_file():
        print(f"[cleanup] Error: Not a git repository: {client_repo}")
        sys.exit(1)

    # Get worktree list from git
    result = subprocess.run(
        ["git", "-C", str(client_repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"[cleanup] Error: git worktree list failed: {result.stderr.strip()}")
        sys.exit(1)

    entries = _parse_worktree_list(result.stdout)
    if not entries:
        print("[cleanup] No worktrees found.")
        return

    # First entry is always the main repo — skip it
    main_entry = entries[0]
    worktrees = entries[1:]

    print(f"[cleanup] Main repo: {main_entry.get('worktree', '?')}")
    print(f"[cleanup] Found {len(worktrees)} worktree(s) to evaluate (max age: {max_age_hours}h)")
    if dry_run:
        print("[cleanup] DRY RUN — no changes will be made")

    removed = 0
    skipped_alive = 0
    skipped_young = 0
    archived = 0
    archive_base = client_repo.parent / "trace-archive"

    for entry in worktrees:
        wt_path_str = entry.get("worktree", "")
        if not wt_path_str:
            continue
        wt_path = Path(wt_path_str)
        branch = entry.get("branch", "").replace("refs/heads/", "")
        age_hours = _worktree_age_hours(wt_path)

        label = branch or wt_path.name

        if age_hours < max_age_hours:
            skipped_young += 1
            print(f"[cleanup] SKIP (age {age_hours:.1f}h < {max_age_hours}h): {label}")
            continue

        if _is_agent_alive(wt_path):
            skipped_alive += 1
            print(f"[cleanup] SKIP (agent alive): {label} (age {age_hours:.1f}h)")
            continue

        if dry_run:
            print(f"[cleanup] WOULD REMOVE: {label} (age {age_hours:.1f}h) at {wt_path}")
            removed += 1
            continue

        # Archive logs before removal
        if _archive_logs(wt_path, archive_base):
            archived += 1
            print(f"[cleanup] Archived logs: {label} -> {archive_base / wt_path.name}")

        # Remove worktree (archives any still-uncommitted work + guards
        # against typo'd paths that could point outside the worktrees tree).
        # Pass this module's ``subprocess.run`` so tests patching
        # ``cleanup_stale_worktrees.subprocess.run`` still intercept the
        # call.
        print(f"[cleanup] Removing: {label} (age {age_hours:.1f}h)")
        safe_remove_worktree(
            wt_path,
            archive_dir=archive_base,
            client_repo=client_repo,
            run_fn=subprocess.run,
        )
        removed += 1

    # Prune once at the end (not per worktree)
    if removed > 0 and not dry_run:
        print("[cleanup] Pruning stale worktree references...")
        subprocess.run(
            ["git", "-C", str(client_repo), "worktree", "prune"],
            capture_output=True,
            check=False,
        )

    # Summary
    action = "Would remove" if dry_run else "Removed"
    print(f"\n[cleanup] Summary: {action} {removed} worktree(s), "
          f"skipped {skipped_alive} (alive), skipped {skipped_young} (young), "
          f"archived logs for {archived}")


if __name__ == "__main__":
    main()
