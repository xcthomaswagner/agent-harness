#!/usr/bin/env bash
# cleanup-worktree.sh — Remove a worktree after an Agent Team session ends.
#
# Usage:
#   ./scripts/cleanup-worktree.sh --client-repo <path> --branch-name <name> [--preserve]

set -euo pipefail

CLIENT_REPO=""
BRANCH_NAME=""
PRESERVE=false

usage() {
    echo "Usage: $0 --client-repo <path> --branch-name <name> [--preserve]"
    echo ""
    echo "Options:"
    echo "  --client-repo   Path to the client git repository"
    echo "  --branch-name   Branch name of the worktree to remove"
    echo "  --preserve      Keep the worktree for debugging (just log, don't delete)"
    echo "  --help          Show this help message"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --client-repo) CLIENT_REPO="$2"; shift 2 ;;
        --branch-name) BRANCH_NAME="$2"; shift 2 ;;
        --preserve) PRESERVE=true; shift ;;
        --help) usage ;;
        *) echo "Error: Unknown option $1"; usage ;;
    esac
done

if [[ -z "$CLIENT_REPO" ]] || [[ -z "$BRANCH_NAME" ]]; then
    echo "Error: --client-repo and --branch-name are required"
    usage
fi

WORKTREE_DIR="$CLIENT_REPO/../worktrees/$BRANCH_NAME"

if [[ ! -d "$WORKTREE_DIR" ]]; then
    echo "[cleanup] Worktree not found: $WORKTREE_DIR (already cleaned up?)"
    exit 0
fi

if [[ "$PRESERVE" == true ]]; then
    echo "[cleanup] PRESERVED (--preserve flag): $WORKTREE_DIR"
    echo "[cleanup] To clean up later: $0 --client-repo $CLIENT_REPO --branch-name $BRANCH_NAME"
    exit 0
fi

echo "[cleanup] Removing worktree: $WORKTREE_DIR"
git -C "$CLIENT_REPO" worktree remove "$WORKTREE_DIR" --force 2>/dev/null || \
    rm -rf "$WORKTREE_DIR"

echo "[cleanup] Pruning stale worktree references..."
git -C "$CLIENT_REPO" worktree prune

echo "[cleanup] Done."
