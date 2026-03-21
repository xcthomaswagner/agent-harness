#!/usr/bin/env bash
# spawn-team.sh — Create a worktree, inject runtime, and launch a Claude Code session.
#
# This is the bridge between L1 (pre-processing service) and L2 (Agent Team execution).
# It creates an isolated worktree for the ticket, injects harness runtime files,
# writes the enriched ticket, and launches Claude Code in headless mode.
#
# Usage:
#   ./scripts/spawn-team.sh \
#     --client-repo <path> \
#     --ticket-json <path> \
#     --branch-name <name> \
#     [--platform-profile <name>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Argument parsing ---

CLIENT_REPO=""
TICKET_JSON=""
BRANCH_NAME=""
PLATFORM_PROFILE=""

usage() {
    echo "Usage: $0 --client-repo <path> --ticket-json <path> --branch-name <name> [--platform-profile <name>]"
    echo ""
    echo "Options:"
    echo "  --client-repo       Path to the client git repository"
    echo "  --ticket-json       Path to the enriched ticket JSON file"
    echo "  --branch-name       Branch name for the worktree (e.g., ai/PROJ-123)"
    echo "  --platform-profile  Platform profile to activate (e.g., sitecore, salesforce)"
    echo "  --help              Show this help message"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --client-repo) CLIENT_REPO="$2"; shift 2 ;;
        --ticket-json) TICKET_JSON="$2"; shift 2 ;;
        --branch-name) BRANCH_NAME="$2"; shift 2 ;;
        --platform-profile) PLATFORM_PROFILE="$2"; shift 2 ;;
        --help) usage ;;
        *) echo "Error: Unknown option $1"; usage ;;
    esac
done

# --- Validate inputs ---

if [[ -z "$CLIENT_REPO" ]] || [[ -z "$TICKET_JSON" ]] || [[ -z "$BRANCH_NAME" ]]; then
    echo "Error: --client-repo, --ticket-json, and --branch-name are all required"
    usage
fi

if [[ ! -d "$CLIENT_REPO/.git" ]] && [[ ! -f "$CLIENT_REPO/.git" ]]; then
    echo "Error: Not a git repository: $CLIENT_REPO"
    exit 1
fi

if [[ ! -f "$TICKET_JSON" ]]; then
    echo "Error: Ticket JSON file not found: $TICKET_JSON"
    exit 1
fi

# Validate JSON
if ! python3 -c "import json; json.load(open('$TICKET_JSON'))" 2>/dev/null; then
    echo "Error: Invalid JSON in ticket file: $TICKET_JSON"
    exit 1
fi

# --- Step 1: Create worktree ---

WORKTREE_DIR="$CLIENT_REPO/../worktrees/$BRANCH_NAME"

if [[ -d "$WORKTREE_DIR" ]]; then
    echo "Error: Worktree already exists for this ticket: $WORKTREE_DIR"
    echo "Run cleanup-worktree.sh first, or use a different branch name."
    exit 1
fi

echo "[spawn] Creating worktree at: $WORKTREE_DIR"
git -C "$CLIENT_REPO" worktree add "$WORKTREE_DIR" -b "$BRANCH_NAME" 2>/dev/null || \
    git -C "$CLIENT_REPO" worktree add "$WORKTREE_DIR" "$BRANCH_NAME"

# --- Step 2: Inject runtime ---

INJECT_ARGS=(--target-dir "$WORKTREE_DIR")
if [[ -n "$PLATFORM_PROFILE" ]]; then
    INJECT_ARGS+=(--platform-profile "$PLATFORM_PROFILE")
fi

"$SCRIPT_DIR/inject-runtime.sh" "${INJECT_ARGS[@]}"

# --- Step 3: Write ticket to harness directory ---

cp "$TICKET_JSON" "$WORKTREE_DIR/.harness/ticket.json"
echo "[spawn] Ticket written to .harness/ticket.json"

# --- Step 4: Launch Claude Code ---

echo "[spawn] Launching Claude Code session..."
echo "[spawn] Worktree: $WORKTREE_DIR"
echo "[spawn] Branch: $BRANCH_NAME"

PROMPT="You are the team lead. Read the enriched ticket at /.harness/ticket.json and execute the pipeline per the Agentic Harness Pipeline Instructions in CLAUDE.md."

cd "$WORKTREE_DIR"

# Launch and capture output
if claude -p "$PROMPT" 2>&1 | tee ".harness/logs/session.log"; then
    EXIT_CODE=0
else
    EXIT_CODE=$?
fi

echo "[spawn] Session ended with exit code: $EXIT_CODE"
echo "[spawn] Logs at: $WORKTREE_DIR/.harness/logs/session.log"

exit $EXIT_CODE
