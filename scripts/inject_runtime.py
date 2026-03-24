#!/usr/bin/env python3
"""Inject harness runtime files into a client repo worktree.

Copies skills, agents, platform profiles, and pipeline instructions
into a client repo directory without launching a Claude Code session.

Usage:
    python scripts/inject_runtime.py --target-dir <path> [--platform-profile <name>]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = HARNESS_ROOT / "runtime"

# Map supplement filenames to their target skill directories
SUPPLEMENT_MAP = {
    "IMPLEMENT_SUPPLEMENT.md": "implement",
    "CODE_REVIEW_SUPPLEMENT.md": "code-review",
    "QA_SUPPLEMENT.md": "qa-validation",
}


def inject(target_dir: Path, platform_profile: str = "") -> None:
    """Inject harness runtime files into the target directory."""
    if not target_dir.is_dir():
        print(f"Error: Target directory does not exist: {target_dir}")
        sys.exit(1)

    print(f"[inject] Target: {target_dir}")

    # --- Step 1: Inject skills ---
    skills_dest = target_dir / ".claude" / "skills"
    skills_dest.mkdir(parents=True, exist_ok=True)

    for skill_dir in sorted((RUNTIME_DIR / "skills").iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name
        target_skill = skills_dest / skill_name

        # Check for naming collision with existing client skills
        if target_skill.exists():
            # If this is a harness skill (re-injection), clean it first
            marker = target_skill / ".harness-injected"
            if marker.exists():
                shutil.rmtree(target_skill)
            else:
                print(f"[inject] WARNING: Client skill '{skill_name}' exists — prefixing with 'harness-'")
                target_skill = skills_dest / f"harness-{skill_name}"

        shutil.copytree(skill_dir, target_skill, dirs_exist_ok=True)
        # Mark as harness-injected for clean re-injection
        (target_skill / ".harness-injected").touch()
        print(f"[inject] Skill: {skill_name}")

    # --- Step 2: Inject agent definitions ---
    agents_dest = target_dir / ".claude" / "agents"
    agents_dest.mkdir(parents=True, exist_ok=True)

    for agent_file in (RUNTIME_DIR / "agents").glob("*.md"):
        shutil.copy2(agent_file, agents_dest / agent_file.name)

    print("[inject] Agent definitions copied")

    # --- Step 3: Platform profile supplements ---
    if platform_profile:
        profile_dir = RUNTIME_DIR / "platform-profiles" / platform_profile

        if not profile_dir.is_dir():
            available = [p.name for p in (RUNTIME_DIR / "platform-profiles").iterdir() if p.is_dir()]
            print(f"Error: Platform profile not found: {platform_profile}")
            print(f"Available profiles: {', '.join(available)}")
            sys.exit(1)

        # Append supplements to relevant skills
        for supplement in profile_dir.glob("*_SUPPLEMENT.md"):
            target_skill_name = SUPPLEMENT_MAP.get(supplement.name)
            if not target_skill_name:
                print(f"[inject] WARNING: Unknown supplement {supplement.name} — skipping")
                continue

            skill_file = skills_dest / target_skill_name / "SKILL.md"
            if skill_file.exists():
                with skill_file.open("a") as f:
                    f.write(f"\n\n---\n# Platform Supplement: {platform_profile}\n\n")
                    f.write(supplement.read_text())
                print(f"[inject] Platform supplement: {supplement.name} -> {target_skill_name}")

        # Copy CONVENTIONS.md if it exists
        conventions = profile_dir / "CONVENTIONS.md"
        if conventions.exists():
            shutil.copy2(conventions, skills_dest / "implement" / "CONVENTIONS.md")
            print("[inject] Platform conventions copied")

        print(f"[inject] Platform profile: {platform_profile}")

    # --- Step 4: Merge CLAUDE.md ---
    harness_claude = RUNTIME_DIR / "harness-CLAUDE.md"
    target_claude = target_dir / "CLAUDE.md"

    if target_claude.exists():
        with target_claude.open("a") as f:
            f.write("\n\n---\n\n")
            f.write(harness_claude.read_text())
        print("[inject] Harness instructions appended to existing CLAUDE.md")
    else:
        shutil.copy2(harness_claude, target_claude)
        print("[inject] CLAUDE.md created (harness only, no client conventions)")

    # --- Step 5: Generate .mcp.json ---
    mcp_template = RUNTIME_DIR / "harness-mcp.json"
    if mcp_template.exists():
        shutil.copy2(mcp_template, target_dir / ".mcp.json")
        print("[inject] MCP config copied")

    # --- Step 6: Create harness directories ---
    for subdir in ("logs", "messages", "plans"):
        (target_dir / ".harness" / subdir).mkdir(parents=True, exist_ok=True)

    print("[inject] Harness directories created")
    print("[inject] Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject harness runtime into a client repo")
    parser.add_argument("--target-dir", required=True, help="Path to the client repo or worktree")
    parser.add_argument("--platform-profile", default="", help="Platform profile (sitecore, salesforce)")
    args = parser.parse_args()

    inject(Path(args.target_dir), args.platform_profile)


if __name__ == "__main__":
    main()
