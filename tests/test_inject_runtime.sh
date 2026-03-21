#!/usr/bin/env bash
# Test inject-runtime.sh — verifies that runtime injection works correctly.
#
# Usage: bash tests/test_inject_runtime.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INJECT_SCRIPT="$PROJECT_ROOT/scripts/inject-runtime.sh"

# Create a temporary directory to simulate a client repo
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

echo "=== Test: inject-runtime.sh ==="
echo "Test directory: $TEST_DIR"

# --- Test 1: Basic injection ---

echo ""
echo "--- Test 1: Basic injection (no platform profile) ---"

# Create a minimal client repo structure
mkdir -p "$TEST_DIR/client"
echo "# Client Project" > "$TEST_DIR/client/CLAUDE.md"
echo "Use tabs for indentation." >> "$TEST_DIR/client/CLAUDE.md"

# Run injection
"$INJECT_SCRIPT" --target-dir "$TEST_DIR/client"

# Verify skills were injected
if [[ ! -d "$TEST_DIR/client/.claude/skills/ticket-analyst" ]]; then
    echo "FAIL: ticket-analyst skill not injected"
    exit 1
fi
echo "PASS: Skills injected"

# Verify agent definitions
if [[ ! -f "$TEST_DIR/client/.claude/agents/team-lead.md" ]]; then
    echo "FAIL: team-lead agent not injected"
    exit 1
fi
echo "PASS: Agent definitions injected"

# Verify CLAUDE.md was merged (client content + harness instructions)
if ! grep -q "Client Project" "$TEST_DIR/client/CLAUDE.md"; then
    echo "FAIL: Client CLAUDE.md content lost"
    exit 1
fi
if ! grep -q "Agentic Harness Pipeline Instructions" "$TEST_DIR/client/CLAUDE.md"; then
    echo "FAIL: Harness instructions not appended to CLAUDE.md"
    exit 1
fi
# Client content should appear BEFORE harness content (priority)
CLIENT_POS=$(grep -n "Client Project" "$TEST_DIR/client/CLAUDE.md" | head -1 | cut -d: -f1)
HARNESS_POS=$(grep -n "Agentic Harness" "$TEST_DIR/client/CLAUDE.md" | head -1 | cut -d: -f1)
if [[ "$CLIENT_POS" -ge "$HARNESS_POS" ]]; then
    echo "FAIL: Client content should appear before harness content"
    exit 1
fi
echo "PASS: CLAUDE.md merged correctly (client first, harness second)"

# Verify MCP config
if [[ ! -f "$TEST_DIR/client/.mcp.json" ]]; then
    echo "FAIL: MCP config not created"
    exit 1
fi
echo "PASS: MCP config created"

# Verify harness directories
if [[ ! -d "$TEST_DIR/client/.harness/logs" ]]; then
    echo "FAIL: .harness/logs not created"
    exit 1
fi
if [[ ! -d "$TEST_DIR/client/.harness/messages" ]]; then
    echo "FAIL: .harness/messages not created"
    exit 1
fi
echo "PASS: Harness directories created"

# --- Test 2: No client CLAUDE.md ---

echo ""
echo "--- Test 2: No client CLAUDE.md ---"

mkdir -p "$TEST_DIR/no-claude"
"$INJECT_SCRIPT" --target-dir "$TEST_DIR/no-claude"

if [[ ! -f "$TEST_DIR/no-claude/CLAUDE.md" ]]; then
    echo "FAIL: CLAUDE.md not created when client has none"
    exit 1
fi
if ! grep -q "Agentic Harness" "$TEST_DIR/no-claude/CLAUDE.md"; then
    echo "FAIL: CLAUDE.md should contain harness instructions"
    exit 1
fi
echo "PASS: CLAUDE.md created from harness-only when client has none"

# --- Test 3: Skill naming collision ---

echo ""
echo "--- Test 3: Skill naming collision ---"

mkdir -p "$TEST_DIR/collision"
mkdir -p "$TEST_DIR/collision/.claude/skills/implement"
echo "# Client's custom implement skill" > "$TEST_DIR/collision/.claude/skills/implement/SKILL.md"

"$INJECT_SCRIPT" --target-dir "$TEST_DIR/collision"

# Client's original should be preserved
if ! grep -q "Client's custom" "$TEST_DIR/collision/.claude/skills/implement/SKILL.md"; then
    echo "FAIL: Client's implement skill was overwritten"
    exit 1
fi
# Harness skill should be prefixed
if [[ ! -d "$TEST_DIR/collision/.claude/skills/harness-implement" ]]; then
    echo "FAIL: Harness implement skill not prefixed on collision"
    exit 1
fi
echo "PASS: Skill collision handled correctly (harness- prefix)"

# --- Test 4: Invalid target directory ---

echo ""
echo "--- Test 4: Invalid target directory ---"

if "$INJECT_SCRIPT" --target-dir "/nonexistent/path" 2>/dev/null; then
    echo "FAIL: Should have failed with nonexistent directory"
    exit 1
fi
echo "PASS: Rejects nonexistent target directory"

echo ""
echo "=== All tests passed ==="
