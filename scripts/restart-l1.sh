#!/usr/bin/env bash
# restart-l1.sh -- Restart the local L1 service on port 8000.
#
# Usage:
#   ./scripts/restart-l1.sh
#   ./scripts/restart-l1.sh --reload
#
# Environment:
#   PORT=8000
#   L1_LOG=/tmp/l1-service.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
L1_DIR="$HARNESS_ROOT/services/l1_preprocessing"
PORT="${PORT:-8000}"
LOG_PATH="${L1_LOG:-/tmp/l1-service.log}"
TMUX_SESSION="${L1_TMUX_SESSION:-agent-harness-l1}"
RELOAD=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --reload)
            RELOAD=true
            shift
            ;;
        --help)
            echo "Usage: $0 [--reload]"
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--reload]" >&2
            exit 2
            ;;
    esac
done

echo "[restart-l1] Stopping listeners on port $PORT..."
if command -v tmux >/dev/null 2>&1; then
    tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
fi
PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$PIDS" ]]; then
    # shellcheck disable=SC2086
    kill $PIDS 2>/dev/null || true
    for _ in {1..20}; do
        if ! lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
            break
        fi
        sleep 0.25
    done
fi

if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[restart-l1] ERROR: port $PORT is still busy" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
    exit 1
fi

UVICORN="$L1_DIR/.venv/bin/uvicorn"
if [[ ! -x "$UVICORN" ]]; then
    UVICORN="$(command -v uvicorn)"
fi

CMD=("$UVICORN" main:app --port "$PORT")
if [[ "$RELOAD" == true ]]; then
    CMD+=(--reload)
fi

echo "[restart-l1] Starting L1 on port $PORT..."
cd "$L1_DIR"

if command -v tmux >/dev/null 2>&1; then
    TMUX_CMD="cd '$L1_DIR' && exec"
    for part in "${CMD[@]}"; do
        TMUX_CMD+=" $(printf '%q' "$part")"
    done
    TMUX_CMD+=" > $(printf '%q' "$LOG_PATH") 2>&1"
    tmux new-session -d -s "$TMUX_SESSION" "$TMUX_CMD"
    PID="$(tmux list-panes -t "$TMUX_SESSION" -F '#{pane_pid}' | head -n 1)"
    echo "[restart-l1] tmux: $TMUX_SESSION"
else
    nohup "${CMD[@]}" > "$LOG_PATH" 2>&1 &
    PID=$!
fi
echo "[restart-l1] PID: $PID"

for _ in {1..30}; do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        echo "[restart-l1] Ready: http://127.0.0.1:$PORT"
        echo "[restart-l1] Log: $LOG_PATH"
        exit 0
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "[restart-l1] ERROR: L1 exited during startup" >&2
        tail -n 80 "$LOG_PATH" >&2 || true
        exit 1
    fi
    sleep 0.25
done

echo "[restart-l1] ERROR: L1 did not become healthy" >&2
tail -n 80 "$LOG_PATH" >&2 || true
exit 1
