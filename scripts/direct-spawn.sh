#!/usr/bin/env bash
# direct-spawn.sh — Bypass L1 and spawn an Agent Team session directly.
#
# This is useful for testing L2 in isolation without running the webhook service.
#
# Usage:
#   ./scripts/direct-spawn.sh \
#     --client-repo <path> \
#     --ticket-json <path> \
#     [--platform-profile <name>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CLIENT_REPO=""
TICKET_JSON=""
PLATFORM_PROFILE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --client-repo) CLIENT_REPO="$2"; shift 2 ;;
        --ticket-json) TICKET_JSON="$2"; shift 2 ;;
        --platform-profile) PLATFORM_PROFILE="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 --client-repo <path> --ticket-json <path> [--platform-profile <name>]"
            exit 0
            ;;
        *) echo "Error: Unknown option $1"; exit 1 ;;
    esac
done

if [[ -z "$CLIENT_REPO" ]] || [[ -z "$TICKET_JSON" ]]; then
    echo "Error: --client-repo and --ticket-json are required"
    exit 1
fi

# Extract ticket ID from JSON for branch name
TICKET_ID=$(python3 -c "import json; print(json.load(open('$TICKET_JSON'))['id'])")
BRANCH_NAME="ai/$TICKET_ID"

echo "Direct spawn: ticket=$TICKET_ID, branch=$BRANCH_NAME"

SPAWN_ARGS=(
    --client-repo "$CLIENT_REPO"
    --ticket-json "$TICKET_JSON"
    --branch-name "$BRANCH_NAME"
)

if [[ -n "$PLATFORM_PROFILE" ]]; then
    SPAWN_ARGS+=(--platform-profile "$PLATFORM_PROFILE")
fi

"$SCRIPT_DIR/spawn-team.sh" "${SPAWN_ARGS[@]}"
