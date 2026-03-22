#!/usr/bin/env bash
# load-test.sh — Spawn N concurrent tickets to measure throughput and reliability.
#
# Usage:
#   ./scripts/load-test.sh --count 5 [--project SCRUM] [--delay 10]
#
# Prerequisites:
#   - L1 service running on localhost:8000
#   - Jira credentials configured in services/l1_preprocessing/.env
#   - DEFAULT_CLIENT_REPO set

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

COUNT=3
PROJECT="SCRUM"
DELAY=5  # seconds between ticket submissions
BASE_URL="http://localhost:8000"

while [[ $# -gt 0 ]]; do
    case $1 in
        --count) COUNT="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        --delay) DELAY="$2"; shift 2 ;;
        --url) BASE_URL="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 --count N [--project SCRUM] [--delay 10] [--url http://...]"
            exit 0 ;;
        *) shift ;;
    esac
done

echo "=== Load Test ==="
echo "Tickets to create: $COUNT"
echo "Project: $PROJECT"
echo "Delay between tickets: ${DELAY}s"
echo "L1 URL: $BASE_URL"
echo ""

# Verify L1 is running
if ! curl -s "$BASE_URL/health" | grep -q "ok"; then
    echo "ERROR: L1 service not running at $BASE_URL"
    exit 1
fi

# Generate ticket data
TICKETS=()
TITLES=(
    "Add search functionality to the product list"
    "Fix pagination bug on mobile devices"
    "Create user settings page with notification preferences"
    "Add loading skeleton to dashboard cards"
    "Implement breadcrumb navigation component"
    "Fix memory leak in real-time notification listener"
    "Add export to CSV feature for reports"
    "Create onboarding wizard for new users"
    "Fix incorrect date formatting in activity feed"
    "Add keyboard shortcuts for common actions"
)

TYPES=("story" "bug" "task" "story" "task" "bug" "story" "story" "bug" "task")

START_TIME=$(date +%s)

echo "--- Creating and submitting tickets ---"

for i in $(seq 1 "$COUNT"); do
    IDX=$(( (i - 1) % ${#TITLES[@]} ))
    TITLE="${TITLES[$IDX]}"
    TYPE="${TYPES[$IDX]}"

    # Submit directly to the API (bypass Jira for speed)
    TICKET_JSON=$(cat <<ENDJSON
{
    "source": "jira",
    "id": "LOAD-$i",
    "ticket_type": "$TYPE",
    "title": "$TITLE",
    "description": "Load test ticket $i of $COUNT. $TITLE. This is an automated load test to measure pipeline throughput.",
    "acceptance_criteria": ["Feature works as described", "Tests pass", "No regressions"],
    "labels": ["ai-implement", "load-test"]
}
ENDJSON
)

    echo "[$i/$COUNT] Submitting LOAD-$i ($TYPE): $TITLE"
    RESPONSE=$(curl -s -X POST "$BASE_URL/api/process-ticket" \
        -H "Content-Type: application/json" \
        -d "$TICKET_JSON")
    echo "  Response: $RESPONSE"

    if [[ $i -lt $COUNT ]]; then
        sleep "$DELAY"
    fi
done

END_SUBMIT=$(date +%s)
SUBMIT_DURATION=$((END_SUBMIT - START_TIME))

echo ""
echo "--- Submission complete ---"
echo "Submitted $COUNT tickets in ${SUBMIT_DURATION}s"
echo ""

# Monitor progress
echo "--- Monitoring agent sessions ---"
echo "Checking every 30s for completion..."
echo ""

COMPLETED=0
TIMEOUT=1800  # 30 minute timeout
ELAPSED=0

while [[ $COMPLETED -lt $COUNT ]] && [[ $ELAPSED -lt $TIMEOUT ]]; do
    sleep 30
    ELAPSED=$((ELAPSED + 30))

    # Count active claude -p processes
    ACTIVE=$(ps aux | grep "claude -p" | grep -v grep | wc -l | tr -d ' ')

    # Count worktrees
    WORKTREES=$(ls -d "$HARNESS_ROOT/../worktrees/ai/"* 2>/dev/null | wc -l | tr -d ' ')

    # Count completed (have session.log with PR URL)
    COMPLETED=0
    for wt in "$HARNESS_ROOT"/../worktrees/ai/LOAD-*/; do
        if [[ -f "$wt/.harness/logs/session.log" ]]; then
            if grep -q "PR\|complete\|PASS" "$wt/.harness/logs/session.log" 2>/dev/null; then
                COMPLETED=$((COMPLETED + 1))
            fi
        fi
    done

    echo "[${ELAPSED}s] Active agents: $ACTIVE | Worktrees: $WORKTREES | Completed: $COMPLETED/$COUNT"
done

END_TIME=$(date +%s)
TOTAL_DURATION=$((END_TIME - START_TIME))

echo ""
echo "=== Load Test Results ==="
echo "Total tickets: $COUNT"
echo "Completed: $COMPLETED"
echo "Total time: ${TOTAL_DURATION}s ($(( TOTAL_DURATION / 60 ))m $(( TOTAL_DURATION % 60 ))s)"
echo "Throughput: $(echo "scale=2; $COMPLETED * 3600 / $TOTAL_DURATION" | bc 2>/dev/null || echo "N/A") tickets/hour"
echo ""

# Collect per-ticket results
echo "--- Per-Ticket Results ---"
for wt in "$HARNESS_ROOT"/../worktrees/ai/LOAD-*/; do
    TICKET=$(basename "$wt")
    if [[ -f "$wt/.harness/logs/pipeline.jsonl" ]]; then
        PR=$(grep -o '"pr_url": *"[^"]*"' "$wt/.harness/logs/pipeline.jsonl" 2>/dev/null | tail -1 | grep -o 'https://[^"]*' || echo "none")
        STATUS=$(grep -o '"event": *"[^"]*"' "$wt/.harness/logs/pipeline.jsonl" 2>/dev/null | tail -1 | grep -o '"[^"]*"$' | tr -d '"' || echo "unknown")
        echo "  $TICKET: $STATUS (PR: $PR)"
    else
        echo "  $TICKET: no pipeline log"
    fi
done

echo ""
echo "Detailed logs at: $HARNESS_ROOT/../worktrees/ai/LOAD-*/.harness/logs/"
