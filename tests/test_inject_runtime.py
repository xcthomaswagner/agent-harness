#!/usr/bin/env python3
"""Test inject_runtime.py — verifies that runtime injection works correctly."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inject_runtime.py"


def run_inject(
    target_dir: str,
    platform_profile: str = "",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(SCRIPT), "--target-dir", target_dir]
    if platform_profile:
        cmd.extend(["--platform-profile", platform_profile])
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


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


def test_salesforce_mcp_merged() -> None:
    """Injecting with --platform-profile salesforce must merge the SF MCP server
    into .mcp.json alongside the base servers, expand ${SALESFORCE_MCP_PATH}
    from the environment, and set SF_HARNESS_MODE=true."""
    with tempfile.TemporaryDirectory() as tmp:
        client = Path(tmp) / "sf-client"
        client.mkdir()

        env = os.environ.copy()
        env["SALESFORCE_MCP_PATH"] = "/opt/test-sf-mcp"

        cmd = [
            sys.executable,
            str(SCRIPT),
            "--target-dir", str(client),
            "--platform-profile", "salesforce",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stderr

        mcp = json.loads((client / ".mcp.json").read_text())
        servers = mcp["mcpServers"]

        # Base server preserved
        assert "playwright" in servers
        # Profile server merged in
        assert "salesforce" in servers

        sf = servers["salesforce"]
        assert sf["command"] == "node"
        # ${SALESFORCE_MCP_PATH} expanded from env
        assert sf["args"] == ["/opt/test-sf-mcp/dist/index.js"]
        # Production guard enabled
        assert sf["env"]["SF_HARNESS_MODE"] == "true"


def test_mcp_env_var_default_fallback() -> None:
    """When SALESFORCE_MCP_PATH is not set, the profile's default path is used."""
    with tempfile.TemporaryDirectory() as tmp:
        client = Path(tmp) / "sf-default"
        client.mkdir()

        env = os.environ.copy()
        env.pop("SALESFORCE_MCP_PATH", None)

        cmd = [
            sys.executable,
            str(SCRIPT),
            "--target-dir", str(client),
            "--platform-profile", "salesforce",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stderr

        mcp = json.loads((client / ".mcp.json").read_text())
        sf_args = mcp["mcpServers"]["salesforce"]["args"]
        # Default resolves to the literal path from the profile
        assert sf_args == [
            "/Users/thomaswagner/Desktop/Projects.nosync/salesforce-mcp-server/dist/index.js"
        ]


def test_contentstack_mcp_uses_supported_groups_without_persisting_secrets() -> None:
    """Contentstack MCP must avoid GROUPS=all and keep secrets out of .mcp.json."""
    with tempfile.TemporaryDirectory() as tmp:
        client = Path(tmp) / "contentstack-client"
        client.mkdir()

        env = os.environ.copy()
        env.update({
            "CONTENTSTACK_API_KEY": "stack-key",
            "CONTENTSTACK_DELIVERY_TOKEN": "delivery-token",
            "CONTENTSTACK_MANAGEMENT_TOKEN": "management-token",
            "CONTENTSTACK_REGION": "NA",
            "CONTENTSTACK_ENVIRONMENT": "development",
            "CONTENTSTACK_BRANCH": "ai",
        })

        result = run_inject(
            str(client), platform_profile="contentstack", env=env
        )
        assert result.returncode == 0, result.stderr

        mcp = json.loads((client / ".mcp.json").read_text())
        contentstack = mcp["mcpServers"]["contentstack"]
        assert contentstack["args"] == ["-y", "@contentstack/mcp"]
        assert contentstack["env"]["GROUPS"] == "cma,cda"
        assert contentstack["env"]["CONTENTSTACK_REGION"] == "NA"

        serialized = json.dumps(mcp)
        assert "stack-key" not in serialized
        assert "delivery-token" not in serialized
        assert "management-token" not in serialized
        assert "CONTENTSTACK_API_KEY" not in contentstack["env"]
        assert "CONTENTSTACK_DELIVERY_TOKEN" not in contentstack["env"]
        assert "CONTENTSTACK_MANAGEMENT_TOKEN" not in contentstack["env"]


def test_salesforce_profile_skills_copied() -> None:
    """Injecting with --platform-profile salesforce must copy profile-local skills
    (e.g. salesforce-dev-loop) into .claude/skills/ alongside the base skills."""
    with tempfile.TemporaryDirectory() as tmp:
        client = Path(tmp) / "sf-skills"
        client.mkdir()

        result = run_inject(str(client), platform_profile="salesforce")
        assert result.returncode == 0, result.stderr

        # Profile-local skill injected
        dev_loop = client / ".claude" / "skills" / "salesforce-dev-loop"
        assert dev_loop.is_dir()
        assert (dev_loop / "SKILL.md").exists()
        assert (dev_loop / "SCRATCH_ORG_LIFECYCLE.md").exists()
        assert (dev_loop / "DEPLOY_VALIDATE.md").exists()
        assert (dev_loop / "APEX_TEST_STRATEGY.md").exists()
        assert (dev_loop / "METADATA_DEPLOYMENT_ORDER.md").exists()

        # Base skills still present
        assert (client / ".claude" / "skills" / "implement").is_dir()
        assert (client / ".claude" / "skills" / "ticket-analyst").is_dir()

        # Marker present for clean re-injection
        assert (dev_loop / ".harness-injected").exists()


def test_non_salesforce_profile_skill_not_leaked() -> None:
    """Without --platform-profile, profile-local skills must NOT be copied."""
    with tempfile.TemporaryDirectory() as tmp:
        client = Path(tmp) / "no-profile-skill"
        client.mkdir()

        result = run_inject(str(client))
        assert result.returncode == 0

        assert not (client / ".claude" / "skills" / "salesforce-dev-loop").exists()


def test_non_salesforce_profile_unaffected() -> None:
    """Injecting without a platform profile must NOT add the SF MCP server."""
    with tempfile.TemporaryDirectory() as tmp:
        client = Path(tmp) / "no-profile"
        client.mkdir()

        result = run_inject(str(client))
        assert result.returncode == 0

        mcp = json.loads((client / ".mcp.json").read_text())
        assert "playwright" in mcp["mcpServers"]
        assert "salesforce" not in mcp["mcpServers"]


def test_runtime_version_stamp_written() -> None:
    """inject_runtime.py must write .harness/runtime-version so agents can
    report which harness version they're running under. Previously only the
    shell variant did this, so production agents logged empty values.
    """
    runtime_version = (Path(__file__).resolve().parents[1] / "runtime" / "VERSION").read_text()

    with tempfile.TemporaryDirectory() as tmp:
        client = Path(tmp) / "version-stamp"
        client.mkdir()

        result = run_inject(str(client))
        assert result.returncode == 0, result.stderr

        stamp = client / ".harness" / "runtime-version"
        assert stamp.exists(), "runtime-version stamp not written"
        assert stamp.read_text() == runtime_version


if __name__ == "__main__":
    test_basic_injection()
    test_no_client_claude_md()
    test_skill_collision()
    test_invalid_target()
    test_salesforce_mcp_merged()
    test_mcp_env_var_default_fallback()
    test_contentstack_mcp_uses_supported_groups_without_persisting_secrets()
    test_salesforce_profile_skills_copied()
    test_non_salesforce_profile_skill_not_leaked()
    test_non_salesforce_profile_unaffected()
    test_runtime_version_stamp_written()
    print("All tests passed")
