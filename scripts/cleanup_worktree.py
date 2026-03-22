#!/usr/bin/env python3
"""Remove a worktree after an Agent Team session ends.

Usage:
    python scripts/cleanup_worktree.py --client-repo <path> --branch-name <name> [--preserve]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up a worktree")
    parser.add_argument("--client-repo", required=True, help="Path to the client git repository")
    parser.add_argument("--branch-name", required=True, help="Branch name of the worktree to remove")
    parser.add_argument("--preserve", action="store_true", help="Keep the worktree for debugging")
    args = parser.parse_args()

    client_repo = Path(args.client_repo).resolve()
    worktree_dir = client_repo.parent / "worktrees" / args.branch_name

    if not worktree_dir.exists():
        print(f"[cleanup] Worktree not found: {worktree_dir} (already cleaned up?)")
        sys.exit(0)

    if args.preserve:
        print(f"[cleanup] PRESERVED (--preserve flag): {worktree_dir}")
        sys.exit(0)

    print(f"[cleanup] Removing worktree: {worktree_dir}")
    result = subprocess.run(
        ["git", "-C", str(client_repo), "worktree", "remove", str(worktree_dir), "--force"],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        shutil.rmtree(worktree_dir, ignore_errors=True)

    print("[cleanup] Pruning stale worktree references...")
    subprocess.run(
        ["git", "-C", str(client_repo), "worktree", "prune"],
        capture_output=True, check=False,
    )
    print("[cleanup] Done.")


if __name__ == "__main__":
    main()
