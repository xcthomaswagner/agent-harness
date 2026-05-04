#!/usr/bin/env python3
"""Inject harness runtime files into a client repo worktree.

Copies skills, agents, platform profiles, and pipeline instructions
into a client repo directory without launching a Claude Code session.

Usage:
    python scripts/inject_runtime.py --target-dir <path> [--platform-profile <name>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

HARNESS_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = HARNESS_ROOT / "runtime"

# Map supplement filenames to their target skill directories
SUPPLEMENT_MAP = {
    "IMPLEMENT_SUPPLEMENT.md": "implement",
    "CODE_REVIEW_SUPPLEMENT.md": "code-review",
    "QA_SUPPLEMENT.md": "qa-validation",
}

# Matches ${VAR} and ${VAR:-default} for env expansion in MCP configs
_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _inject_skill_dir(src_skill: Path, skills_dest: Path, label: str) -> None:
    """Copy a single skill directory into the client's .claude/skills/.

    Handles collision with existing client skills via a `.harness-injected`
    marker: harness-owned skills are cleaned and re-copied; client-owned
    skills trigger a fallback to a `harness-<name>` prefixed target.

    `label` is the log prefix used when announcing the copy (e.g. "Skill"
    or "Profile skill") so base and profile-level injections stay
    distinguishable in output.
    """
    skill_name = src_skill.name
    target_skill = skills_dest / skill_name

    if target_skill.exists():
        marker = target_skill / ".harness-injected"
        if marker.exists():
            shutil.rmtree(target_skill)
        else:
            print(f"[inject] WARNING: Client skill '{skill_name}' exists — prefixing with 'harness-'")
            target_skill = skills_dest / f"harness-{skill_name}"
            if target_skill.exists():
                shutil.rmtree(target_skill)

    shutil.copytree(src_skill, target_skill, dirs_exist_ok=True)
    (target_skill / ".harness-injected").touch()
    print(f"[inject] {label}: {skill_name}")


def expand_env_vars(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in strings within a JSON-like structure."""
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default = match.group(2) or ""
            return os.environ.get(var_name, default)
        return _ENV_VAR_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(v) for v in value]
    return value


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
        if skill_dir.is_dir():
            _inject_skill_dir(skill_dir, skills_dest, label="Skill")

    # --- Step 2: Inject agent definitions ---
    agents_dest = target_dir / ".claude" / "agents"
    agents_dest.mkdir(parents=True, exist_ok=True)

    for agent_file in (RUNTIME_DIR / "agents").glob("*.md"):
        shutil.copy2(agent_file, agents_dest / agent_file.name)

    print("[inject] Agent definitions copied")

    # --- Step 3: Platform profile supplements ---
    if platform_profile:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", platform_profile):
            print(f"Error: Invalid platform profile name: {platform_profile!r}")
            sys.exit(1)
        profile_dir = RUNTIME_DIR / "platform-profiles" / platform_profile

        if not profile_dir.is_dir():
            available = [p.name for p in (RUNTIME_DIR / "platform-profiles").iterdir() if p.is_dir()]
            print(f"Error: Platform profile not found: {platform_profile}")
            print(f"Available profiles: {', '.join(available)}")
            sys.exit(1)

        # Copy profile-local skills into .claude/skills/ (same semantics as Step 1)
        profile_skills_dir = profile_dir / "skills"
        if profile_skills_dir.is_dir():
            for profile_skill in sorted(profile_skills_dir.iterdir()):
                if profile_skill.is_dir():
                    _inject_skill_dir(profile_skill, skills_dest, label="Profile skill")

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
            else:
                print(f"[inject] WARNING: SKILL.md not found in {target_skill_name} — skipping {supplement.name}")

        # Copy CONVENTIONS.md if it exists
        conventions = profile_dir / "CONVENTIONS.md"
        if conventions.exists():
            shutil.copy2(conventions, skills_dest / "implement" / "CONVENTIONS.md")
            print("[inject] Platform conventions copied")

        # Append REFERENCE_URLS.md to all three skills (implement, code-review, qa-validation)
        ref_urls = profile_dir / "REFERENCE_URLS.md"
        if ref_urls.exists():
            ref_content = ref_urls.read_text()
            for skill_name in ("implement", "code-review", "qa-validation"):
                skill_file = skills_dest / skill_name / "SKILL.md"
                if skill_file.exists():
                    with skill_file.open("a") as f:
                        f.write(f"\n\n---\n\n{ref_content}")
            print("[inject] Reference URLs appended to all skills")

        print(f"[inject] Platform profile: {platform_profile}")

    # --- Step 4: Merge CLAUDE.md (idempotent — skip if already injected) ---
    harness_claude = RUNTIME_DIR / "harness-CLAUDE.md"
    target_claude = target_dir / "CLAUDE.md"
    harness_marker = "<!-- harness-injected -->"

    if target_claude.exists():
        existing = target_claude.read_text()
        if harness_marker in existing:
            print("[inject] CLAUDE.md already contains harness instructions — skipping")
        else:
            with target_claude.open("a") as f:
                f.write(f"\n\n---\n{harness_marker}\n\n")
                f.write(harness_claude.read_text())
            print("[inject] Harness instructions appended to existing CLAUDE.md")
    else:
        with target_claude.open("w") as f:
            f.write(f"{harness_marker}\n\n")
            f.write(harness_claude.read_text())
        print("[inject] CLAUDE.md created (harness only, no client conventions)")

    # --- Step 5: Generate .mcp.json (base + optional platform profile MCP merge) ---
    mcp_template = RUNTIME_DIR / "harness-mcp.json"
    if mcp_template.exists():
        base_mcp = json.loads(mcp_template.read_text())
        base_servers = base_mcp.setdefault("mcpServers", {})

        if platform_profile:
            profile_mcp_path = RUNTIME_DIR / "platform-profiles" / platform_profile / "harness-mcp.json"
            if profile_mcp_path.exists():
                profile_mcp = json.loads(profile_mcp_path.read_text())
                profile_servers = profile_mcp.get("mcpServers", {})
                # Profile keys override base on collision
                for server_name, server_cfg in profile_servers.items():
                    base_servers[server_name] = server_cfg
                    print(f"[inject] MCP server from profile: {server_name}")

        # Expand ${VAR} and ${VAR:-default} placeholders
        merged_mcp = expand_env_vars(base_mcp)

        mcp_target = target_dir / ".mcp.json"
        mcp_target.write_text(json.dumps(merged_mcp, indent=2) + "\n")
        mcp_target.chmod(0o600)
        print("[inject] MCP config written")

    # --- Step 6: Create harness directories ---
    for subdir in ("logs", "messages", "plans"):
        (target_dir / ".harness" / subdir).mkdir(parents=True, exist_ok=True)

    print("[inject] Harness directories created")

    # --- Step 7: Write runtime version stamp ---
    # Previously only the shell variant (inject-runtime.sh) wrote this
    # marker, so production agents launched through the Python injector
    # ended up with empty ``runtime_version`` fields in their telemetry.
    # Copying the source-of-truth VERSION file directly keeps both
    # injectors in lockstep.
    version_file = RUNTIME_DIR / "VERSION"
    if version_file.exists():
        stamp = target_dir / ".harness" / "runtime-version"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(version_file.read_text())
        print(f"[inject] Runtime version: {version_file.read_text().strip()}")
    else:
        print("[inject] WARNING: runtime/VERSION not found — skipping version marker")

    print("[inject] Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject harness runtime into a client repo")
    parser.add_argument("--target-dir", required=True, help="Path to the client repo or worktree")
    parser.add_argument("--platform-profile", default="", help="Platform profile (sitecore, salesforce)")
    args = parser.parse_args()

    inject(Path(args.target_dir), args.platform_profile)


if __name__ == "__main__":
    main()
