#!/usr/bin/env bash
# inject-runtime.sh — Inject harness runtime files into a client repo worktree.
#
# This script copies skills, agents, platform profiles, and pipeline instructions
# into a client repo directory without launching a Claude Code session.
# Used by spawn-team.sh and for testing injection in isolation.
#
# Usage:
#   ./scripts/inject-runtime.sh --target-dir <path> [--platform-profile <name>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="$HARNESS_ROOT/runtime"

# --- Argument parsing ---

TARGET_DIR=""
PLATFORM_PROFILE=""

usage() {
    echo "Usage: $0 --target-dir <path> [--platform-profile <name>]"
    echo ""
    echo "Options:"
    echo "  --target-dir        Path to the client repo (or worktree) to inject into"
    echo "  --platform-profile  Platform profile to activate (e.g., sitecore, salesforce)"
    echo "  --help              Show this help message"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --target-dir) TARGET_DIR="$2"; shift 2 ;;
        --platform-profile) PLATFORM_PROFILE="$2"; shift 2 ;;
        --help) usage ;;
        *) echo "Error: Unknown option $1"; usage ;;
    esac
done

if [[ -z "$TARGET_DIR" ]]; then
    echo "Error: --target-dir is required"
    usage
fi

if [[ ! -d "$TARGET_DIR" ]]; then
    echo "Error: Target directory does not exist: $TARGET_DIR"
    exit 1
fi

echo "[inject] Target: $TARGET_DIR"

# --- Step 1: Inject skills ---

mkdir -p "$TARGET_DIR/.claude/skills"

for skill_dir in "$RUNTIME_DIR"/skills/*/; do
    skill_name="$(basename "$skill_dir")"
    target_skill="$TARGET_DIR/.claude/skills/$skill_name"

    # Check for naming collision with existing client skills
    if [[ -d "$target_skill" ]]; then
        echo "[inject] WARNING: Client skill '$skill_name' exists — prefixing harness skill with 'harness-'"
        target_skill="$TARGET_DIR/.claude/skills/harness-$skill_name"
    fi

    cp -r "$skill_dir" "$target_skill"
    echo "[inject] Skill: $skill_name"
done

# --- Step 2: Inject agent definitions ---

mkdir -p "$TARGET_DIR/.claude/agents"
cp "$RUNTIME_DIR"/agents/*.md "$TARGET_DIR/.claude/agents/"
echo "[inject] Agent definitions copied"

# --- Step 3: Platform profile supplements ---

if [[ -n "$PLATFORM_PROFILE" ]]; then
    PROFILE_DIR="$RUNTIME_DIR/platform-profiles/$PLATFORM_PROFILE"

    if [[ ! -d "$PROFILE_DIR" ]]; then
        echo "Error: Platform profile not found: $PLATFORM_PROFILE"
        echo "Available profiles: $(ls "$RUNTIME_DIR/platform-profiles/")"
        exit 1
    fi

    # Append supplements to relevant skills
    for supplement in "$PROFILE_DIR"/*_SUPPLEMENT.md; do
        [[ -f "$supplement" ]] || continue
        supplement_name="$(basename "$supplement")"

        # Map supplement to skill: IMPLEMENT_SUPPLEMENT.md -> implement/SKILL.md
        case "$supplement_name" in
            IMPLEMENT_SUPPLEMENT.md) target_skill="implement" ;;
            CODE_REVIEW_SUPPLEMENT.md) target_skill="code-review" ;;
            QA_SUPPLEMENT.md) target_skill="qa-validation" ;;
            *) echo "[inject] WARNING: Unknown supplement $supplement_name — skipping"; continue ;;
        esac

        skill_file="$TARGET_DIR/.claude/skills/$target_skill/SKILL.md"
        if [[ -f "$skill_file" ]]; then
            echo "" >> "$skill_file"
            echo "---" >> "$skill_file"
            echo "# Platform Supplement: $PLATFORM_PROFILE" >> "$skill_file"
            echo "" >> "$skill_file"
            cat "$supplement" >> "$skill_file"
            echo "[inject] Platform supplement: $supplement_name -> $target_skill"
        fi
    done

    # Copy CONVENTIONS.md if it exists
    if [[ -f "$PROFILE_DIR/CONVENTIONS.md" ]]; then
        cp "$PROFILE_DIR/CONVENTIONS.md" "$TARGET_DIR/.claude/skills/implement/CONVENTIONS.md"
        echo "[inject] Platform conventions copied"
    fi

    echo "[inject] Platform profile: $PLATFORM_PROFILE"
fi

# --- Step 4: Merge CLAUDE.md ---

if [[ -f "$TARGET_DIR/CLAUDE.md" ]]; then
    # Append harness instructions after client conventions
    echo "" >> "$TARGET_DIR/CLAUDE.md"
    echo "---" >> "$TARGET_DIR/CLAUDE.md"
    echo "" >> "$TARGET_DIR/CLAUDE.md"
    cat "$RUNTIME_DIR/harness-CLAUDE.md" >> "$TARGET_DIR/CLAUDE.md"
    echo "[inject] Harness instructions appended to existing CLAUDE.md"
else
    # No client CLAUDE.md — create one with just harness instructions
    cp "$RUNTIME_DIR/harness-CLAUDE.md" "$TARGET_DIR/CLAUDE.md"
    echo "[inject] CLAUDE.md created (harness only, no client conventions)"
fi

# --- Step 5: Generate .mcp.json ---

if [[ -f "$RUNTIME_DIR/harness-mcp.json" ]]; then
    cp "$RUNTIME_DIR/harness-mcp.json" "$TARGET_DIR/.mcp.json"
    echo "[inject] MCP config copied"
fi

# --- Step 6: Create harness directories ---

mkdir -p "$TARGET_DIR/.harness/logs"
mkdir -p "$TARGET_DIR/.harness/messages"
mkdir -p "$TARGET_DIR/.harness/plans"
echo "[inject] Harness directories created"

echo "[inject] Done."
