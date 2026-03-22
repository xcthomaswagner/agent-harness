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
PIPELINE_MODE="multi"

usage() {
    echo "Usage: $0 --client-repo <path> --ticket-json <path> --branch-name <name> [--platform-profile <name>] [--mode multi|quick]"
    echo ""
    echo "Options:"
    echo "  --client-repo       Path to the client git repository"
    echo "  --ticket-json       Path to the enriched ticket JSON file"
    echo "  --branch-name       Branch name for the worktree (e.g., ai/PROJ-123)"
    echo "  --platform-profile  Platform profile to activate (e.g., sitecore, salesforce)"
    echo "  --mode              Pipeline mode: multi (default, full review/QA) or quick (single agent)"
    echo "  --help              Show this help message"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --client-repo) CLIENT_REPO="$2"; shift 2 ;;
        --ticket-json) TICKET_JSON="$2"; shift 2 ;;
        --branch-name) BRANCH_NAME="$2"; shift 2 ;;
        --platform-profile) PLATFORM_PROFILE="$2"; shift 2 ;;
        --mode) PIPELINE_MODE="$2"; shift 2 ;;
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

# --- Step 1: Create worktree (handle collisions) ---

WORKTREE_DIR="$CLIENT_REPO/../worktrees/$BRANCH_NAME"

if [[ -d "$WORKTREE_DIR" ]]; then
    echo "[spawn] Worktree already exists — cleaning up previous run"
    # Kill any running agent for this branch
    EXISTING_PID=$(ps aux | grep "claude -p" | grep "$BRANCH_NAME" | grep -v grep | awk '{print $2}')
    if [[ -n "$EXISTING_PID" ]]; then
        echo "[spawn] Killing existing agent (PID: $EXISTING_PID)"
        kill "$EXISTING_PID" 2>/dev/null
        sleep 2
    fi
    git -C "$CLIENT_REPO" worktree remove "$WORKTREE_DIR" --force 2>/dev/null || rm -rf "$WORKTREE_DIR"
    git -C "$CLIENT_REPO" worktree prune
    git -C "$CLIENT_REPO" branch -D "$BRANCH_NAME" 2>/dev/null
    echo "[spawn] Previous worktree cleaned up"
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

# --- Step 3: Write ticket and mode to harness directory ---

cp "$TICKET_JSON" "$WORKTREE_DIR/.harness/ticket.json"
echo "$PIPELINE_MODE" > "$WORKTREE_DIR/.harness/pipeline-mode"
echo "[spawn] Ticket written to .harness/ticket.json (mode: $PIPELINE_MODE)"

# --- Step 4: Launch Claude Code ---

echo "[spawn] Launching Claude Code session..."
echo "[spawn] Worktree: $WORKTREE_DIR"
echo "[spawn] Branch: $BRANCH_NAME"
echo "[spawn] Mode: $PIPELINE_MODE"

if [[ "$PIPELINE_MODE" == "quick" ]]; then
    PROMPT="You are the team lead in QUICK mode. Read the enriched ticket at /.harness/ticket.json. Implement the changes yourself (do NOT spawn sub-agents). Write tests, run them, commit, push, and open a draft PR. Follow the project conventions in CLAUDE.md. Use conventional commits: feat(<ticket-id>): <description>. Do not commit .env, secrets, or harness files."
else
    PROMPT="You are the team lead. Read the enriched ticket at /.harness/ticket.json and execute the pipeline per the Agentic Harness Pipeline Instructions in CLAUDE.md."
fi

cd "$WORKTREE_DIR"

# Launch and capture output
if claude -p "$PROMPT" --dangerously-skip-permissions 2>&1 | tee ".harness/logs/session.log"; then
    EXIT_CODE=0
else
    EXIT_CODE=$?
fi

echo "[spawn] Session ended with exit code: $EXIT_CODE"
echo "[spawn] Logs at: $WORKTREE_DIR/.harness/logs/session.log"

# --- Step 5: Notify L1 of completion ---

# Extract ticket ID from the ticket JSON
TICKET_ID=$(python3 -c "import json; print(json.load(open('$WORKTREE_DIR/.harness/ticket.json'))['id'])" 2>/dev/null || echo "unknown")

# Extract PR URL from pipeline log if available
PR_URL=$(grep -o '"pr_url": *"[^"]*"' "$WORKTREE_DIR/.harness/logs/pipeline.jsonl" 2>/dev/null | tail -1 | grep -o 'https://[^"]*' || echo "")

# Determine status
if [[ $EXIT_CODE -eq 0 ]] && [[ -n "$PR_URL" ]]; then
    STATUS="complete"
elif [[ $EXIT_CODE -eq 0 ]]; then
    STATUS="partial"
else
    STATUS="escalated"
fi

echo "[spawn] Notifying L1: ticket=$TICKET_ID status=$STATUS pr=$PR_URL"
curl -s -X POST "http://localhost:8000/api/agent-complete" \
    -H "Content-Type: application/json" \
    -d "{\"ticket_id\": \"$TICKET_ID\", \"status\": \"$STATUS\", \"pr_url\": \"$PR_URL\", \"branch\": \"$BRANCH_NAME\"}" \
    2>/dev/null || echo "[spawn] WARNING: Could not notify L1 (service may not be running)"

exit $EXIT_CODE
