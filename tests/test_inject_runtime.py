#!/usr/bin/env python3
"""Test inject_runtime.py — verifies that runtime injection works correctly."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inject_runtime.py"


def run_inject(target_dir: str, platform_profile: str = "") -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(SCRIPT), "--target-dir", target_dir]
    if platform_profile:
        cmd.extend(["--platform-profile", platform_profile])
    return subprocess.run(cmd, capture_output=True, text=True)


def test_basic_injection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = Path(tmp) / "client"
        client.mkdir()
        (client / "CLAUDE.md").write_text("# Client Project\nUse tabs.")

        result = run_inject(str(client))
        assert result.returncode == 0

        # Skills injected
        assert (client / ".claude" / "skills" / "ticket-analyst").is_dir()
        assert (client / ".claude" / "skills" / "implement").is_dir()

        # Agents injected
        assert (client / ".claude" / "agents" / "team-lead.md").exists()

        # CLAUDE.md merged (client first, harness second)
        content = (client / "CLAUDE.md").read_text()
        client_pos = content.index("Client Project")
        harness_pos = content.index("Agentic Harness")
        assert client_pos < harness_pos

        # MCP config
        assert (client / ".mcp.json").exists()

        # Harness directories
        assert (client / ".harness" / "logs").is_dir()
        assert (client / ".harness" / "messages").is_dir()


def test_no_client_claude_md() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = Path(tmp) / "no-claude"
        client.mkdir()

        result = run_inject(str(client))
        assert result.returncode == 0

        content = (client / "CLAUDE.md").read_text()
        assert "Agentic Harness" in content


def test_skill_collision() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = Path(tmp) / "collision"
        client.mkdir()
        (client / ".claude" / "skills" / "implement").mkdir(parents=True)
        (client / ".claude" / "skills" / "implement" / "SKILL.md").write_text("# Client's custom")

        result = run_inject(str(client))
        assert result.returncode == 0

        # Client's original preserved
        assert "Client's custom" in (client / ".claude" / "skills" / "implement" / "SKILL.md").read_text()
        # Harness skill prefixed
        assert (client / ".claude" / "skills" / "harness-implement").is_dir()


def test_invalid_target() -> None:
    result = run_inject("/nonexistent/path")
    assert result.returncode != 0


if __name__ == "__main__":
    test_basic_injection()
    test_no_client_claude_md()
    test_skill_collision()
    test_invalid_target()
    print("All tests passed")
