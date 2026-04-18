#!/usr/bin/env python3
"""Remove a worktree after an Agent Team session ends.

Usage:
    python scripts/cleanup_worktree.py --client-repo <path> --branch-name <name> [--preserve] [--dry-run]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from worktree_safety import safe_remove_worktree  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up a worktree")
    parser.add_argument("--client-repo", required=True, help="Path to the client git repository")
    parser.add_argument("--branch-name", required=True, help="Branch name of the worktree to remove")
    parser.add_argument("--preserve", action="store_true", help="Keep the worktree for debugging")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be removed without actually removing",
    )
    args = parser.parse_args()

    client_repo = Path(args.client_repo).resolve()
    worktree_dir = client_repo.parent / "worktrees" / args.branch_name
    dry_run = args.dry_run

    if dry_run:
        print("[cleanup] DRY RUN — no changes will be made")

    if not worktree_dir.exists():
        print(f"[cleanup] Worktree not found: {worktree_dir} (already cleaned up?)")
        sys.exit(0)

    if args.preserve:
        print(f"[cleanup] PRESERVED (--preserve flag): {worktree_dir}")
        sys.exit(0)

    if dry_run:
        print(f"[cleanup] WOULD REMOVE: {worktree_dir}")
        print("[cleanup] WOULD PRUNE: stale worktree references")
        return

    print(f"[cleanup] Removing worktree: {worktree_dir}")
    # Pass this module's ``subprocess.run`` so tests patching
    # ``cleanup_worktree.subprocess.run`` still intercept the call.
    safe_remove_worktree(
        worktree_dir,
        archive_dir=client_repo.parent / "trace-archive",
        client_repo=client_repo,
        run_fn=subprocess.run,
    )

    print("[cleanup] Pruning stale worktree references...")
    subprocess.run(
        ["git", "-C", str(client_repo), "worktree", "prune"],
        capture_output=True, check=False,
    )
    print("[cleanup] Done.")


if __name__ == "__main__":
    main()
