#!/usr/bin/env bash
# start-services.sh — Start L1 and L3 services + ngrok tunnel.
#
# Usage: ./scripts/start-services.sh [--no-tunnel]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NO_TUNNEL=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-tunnel) NO_TUNNEL=true; shift ;;
        --help) echo "Usage: $0 [--no-tunnel]"; exit 0 ;;
        *) shift ;;
    esac
done

# Kill existing services
echo "[start] Stopping existing services..."
kill $(lsof -ti:8000) 2>/dev/null || true
kill $(lsof -ti:8001) 2>/dev/null || true
sleep 1

# Start L1
echo "[start] Starting L1 Pre-Processing Service on port 8000..."
cd "$HARNESS_ROOT/services/l1_preprocessing"
source .venv/bin/activate
nohup uvicorn main:app --port 8000 > /tmp/l1-service.log 2>&1 &
L1_PID=$!
echo "[start] L1 PID: $L1_PID"

# Start L3
echo "[start] Starting L3 PR Review Service on port 8001..."
cd "$HARNESS_ROOT/services/l3_pr_review"
if [[ -d .venv ]]; then
    source .venv/bin/activate
fi
nohup uvicorn main:app --port 8001 > /tmp/l3-service.log 2>&1 &
L3_PID=$!
echo "[start] L3 PID: $L3_PID"

# Wait for services to start
sleep 3

# Verify
L1_STATUS=$(curl -s http://localhost:8000/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "failed")
L3_STATUS=$(curl -s http://localhost:8001/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "failed")

echo "[start] L1 status: $L1_STATUS"
echo "[start] L3 status: $L3_STATUS"

if [[ "$L1_STATUS" != "ok" ]]; then
    echo "[start] ERROR: L1 failed to start. Check /tmp/l1-service.log"
    exit 1
fi

# Start tunnel if requested
if [[ "$NO_TUNNEL" == false ]]; then
    echo "[start] Starting ngrok tunnel..."
    # Tunnel to both services
    nohup ngrok http 8000 > /tmp/ngrok.log 2>&1 &
    sleep 4
    TUNNEL_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "import sys,json; data=json.load(sys.stdin); print(data['tunnels'][0]['public_url'])" 2>/dev/null || echo "failed")
    echo "[start] Tunnel URL: $TUNNEL_URL"
    echo "[start] Jira webhook URL: $TUNNEL_URL/webhooks/jira"
fi

echo ""
echo "[start] Services running:"
echo "  L1: http://localhost:8000 (Jira/ADO webhooks, ticket processing)"
echo "  L3: http://localhost:8001 (GitHub PR review webhooks)"
echo ""
echo "[start] Logs:"
echo "  L1: tail -f /tmp/l1-service.log"
echo "  L3: tail -f /tmp/l3-service.log"
echo ""
echo "[start] To stop: kill $L1_PID $L3_PID"
