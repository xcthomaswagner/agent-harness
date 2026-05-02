#!/usr/bin/env bash
# Smoke-test the official @contentstack/mcp server against the cstk-demo
# profile's expected env vars. Reads from services/l1_preprocessing/.env
# if present, otherwise from the current shell.
#
# Per the setup guide:
#   ~/SecondBrain/50-Research/Harness Demo For ContentStack/docs/contentstack-harness-setup.docx
# Step 7 — verify the MCP starts cleanly before running a real ticket.
#
# Wrong region = silent 404s on every CMA call. Wrong env-var name = MCP
# refuses to start. This script catches both early.

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

# Recommended (warn but don't fail)
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
echo
echo "→ Starting @contentstack/mcp via npx (Ctrl-C to exit once you see startup log)..."
echo

GROUPS="${GROUPS:-all}" exec npx -y @contentstack/mcp
