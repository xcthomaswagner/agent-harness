#!/usr/bin/env bash
# Smoke-test the official @contentstack/mcp server against the cstk-demo
# profile's expected env vars. Reads from services/l1_preprocessing/.env
# if present, otherwise from the current shell.
#
# Per the setup guide:
#   ~/SecondBrain/50-Research/Harness Demo For ContentStack/docs/contentstack-harness-setup.docx
# Step 7 — verify the MCP starts cleanly before running a real ticket.
#
# Wrong region = silent 404s on every CMA call. Wrong env-var name, missing
# OAuth, or an unsupported GROUPS value = MCP refuses to start. This script
# catches those before a real ticket burns an agent run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/services/l1_preprocessing/.env"

if [[ -f "$ENV_FILE" ]]; then
  echo "→ Loading env from $ENV_FILE"
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
else
  echo "→ No .env at $ENV_FILE — relying on current shell env."
fi

# Required vars per the profile's harness-mcp.json
REQUIRED=(CONTENTSTACK_API_KEY CONTENTSTACK_DELIVERY_TOKEN CONTENTSTACK_REGION)
MISSING=()
for var in "${REQUIRED[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    MISSING+=("$var")
  fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "✗ Missing required env vars: ${MISSING[*]}"
  echo
  echo "  Set them in $ENV_FILE or export in your shell, then re-run."
  exit 1
fi

# Recommended (warn but don't fail). @contentstack/mcp@0.6.0 uses OAuth for
# CMA writes; the management token remains part of the harness profile for
# compatibility/future use but is not enough by itself to start CMA tools.
for var in CONTENTSTACK_MANAGEMENT_TOKEN CONTENTSTACK_ENVIRONMENT CONTENTSTACK_BRANCH; do
  if [[ -z "${!var:-}" ]]; then
    echo "⚠  $var not set — MCP will fall back to defaults."
  fi
done

# Validate region is a known value
case "${CONTENTSTACK_REGION:-}" in
  NA|EU|AZURE_NA|AZURE_EU) ;;
  *)
    echo "✗ CONTENTSTACK_REGION='${CONTENTSTACK_REGION}' is not one of: NA, EU, AZURE_NA, AZURE_EU"
    echo "  Wrong region = silent 404s on every CMA call. Fix before proceeding."
    exit 1
    ;;
esac

echo "→ Region: ${CONTENTSTACK_REGION}"
echo "→ Environment: ${CONTENTSTACK_ENVIRONMENT:-<default>}"
echo "→ Branch: ${CONTENTSTACK_BRANCH:-<default>}"
echo "→ Groups: ${CONTENTSTACK_MCP_GROUPS:-cma,cda}"
echo
echo "→ Starting @contentstack/mcp and listing tools..."
echo

env GROUPS="${CONTENTSTACK_MCP_GROUPS:-cma,cda}" python3 - <<'PY'
import json
import os
import select
import subprocess
import sys
import time

proc = subprocess.Popen(
    ["npx", "-y", "@contentstack/mcp"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env=os.environ.copy(),
)


def send(payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def read_id(message_id: int, timeout: int = 20) -> dict | None:
    assert proc.stdout is not None
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == message_id:
            return message
    return None


try:
    send({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "harness-contentstack-smoke", "version": "0.1"},
        },
    })
    init = read_id(1)
    if not init or "error" in init:
        print("✗ MCP initialize failed")
        raise SystemExit(1)

    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    listed = read_id(2)
    if not listed or "error" in listed:
        print("✗ MCP tools/list failed")
        raise SystemExit(1)

    tools = listed.get("result", {}).get("tools", [])
    names = [tool.get("name", "") for tool in tools]
    required = {"get_a_single_content_type", "update_content_type", "get_a_single_entry_cdn"}
    missing = sorted(required.difference(names))
    if missing:
        print(f"✗ MCP started but expected tools are missing: {', '.join(missing)}")
        raise SystemExit(1)

    print(f"✓ Contentstack MCP initialized; {len(tools)} tools available.")
except Exception as exc:
    print(f"✗ Contentstack MCP smoke test failed: {type(exc).__name__}: {exc}")
    raise SystemExit(1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    stderr = proc.stderr.read() if proc.stderr else ""
    if stderr.strip():
        print("stderr:")
        for line in stderr.splitlines()[:20]:
            print(line)
PY
