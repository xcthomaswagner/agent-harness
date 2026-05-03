"""Tests for shared.platform_profile_env — pass-through discovery.

The helper scans a platform profile's harness-mcp.json for ${VAR}
placeholders so spawn_team can re-inject those specific vars into
the agent + inject_runtime subprocesses (which need them to start
the platform's MCP server with real credentials).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

SERVICES_DIR = Path(__file__).resolve().parents[2]
if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))

from shared.platform_profile_env import _scan_for_placeholders, pass_through_vars  # noqa: E402

# ---------------------------------------------------------------------------
# _scan_for_placeholders
# ---------------------------------------------------------------------------

def test_scan_returns_empty_for_plain_string() -> None:
    assert _scan_for_placeholders("no placeholders here") == set()


def test_scan_finds_simple_var() -> None:
    assert _scan_for_placeholders("${MY_VAR}") == {"MY_VAR"}


def test_scan_finds_var_with_default() -> None:
    """Default-syntax (${VAR:-default}) still surfaces the var name."""
    assert _scan_for_placeholders("${MY_VAR:-fallback}") == {"MY_VAR"}


def test_scan_handles_multiple_vars_same_string() -> None:
    found = _scan_for_placeholders("prefix-${A}-mid-${B:-x}-end-${C}")
    assert found == {"A", "B", "C"}


def test_scan_walks_nested_dict() -> None:
    config = {
        "command": "${BIN_PATH}",
        "env": {
            "TOKEN": "${MY_TOKEN}",
            "REGION": "${REGION:-NA}",
            "STATIC": "no-placeholder",
        },
    }
    assert _scan_for_placeholders(config) == {"BIN_PATH", "MY_TOKEN", "REGION"}


def test_scan_walks_lists() -> None:
    config = {
        "args": ["-y", "${PKG_NAME}", "--region=${REGION}"],
    }
    assert _scan_for_placeholders(config) == {"PKG_NAME", "REGION"}


def test_scan_ignores_dollar_without_braces() -> None:
    """``$VAR`` (no braces) is not the placeholder syntax inject_runtime expands."""
    assert _scan_for_placeholders("$VAR is shell-style not ${BRACED}") == {"BRACED"}


def test_scan_only_uppercase_var_names() -> None:
    """Mirrors inject_runtime regex — lowercase starts are ignored."""
    assert _scan_for_placeholders("${lowercase} ${UPPER}") == {"UPPER"}


# ---------------------------------------------------------------------------
# pass_through_vars
# ---------------------------------------------------------------------------

def test_pass_through_empty_profile_name() -> None:
    """Callers don't need to guard for the no-profile case."""
    assert pass_through_vars("") == set()
    assert pass_through_vars(None) == set()  # type: ignore[arg-type]


def test_pass_through_unknown_profile() -> None:
    """A profile that doesn't exist returns an empty set, not an exception."""
    assert pass_through_vars("does-not-exist") == set()


def test_pass_through_scans_real_contentstack_profile(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: write a fake profile to a tmp dir, point the helper at it,
    and confirm it surfaces the placeholders."""
    fake_profiles_dir = tmp_path / "platform-profiles" / "fake-cms"
    fake_profiles_dir.mkdir(parents=True)
    (fake_profiles_dir / "harness-mcp.json").write_text(json.dumps({
        "mcpServers": {
            "fake-cms": {
                "command": "npx",
                "args": ["-y", "@fake/mcp"],
                "env": {
                    "FAKE_API_KEY": "${FAKE_API_KEY}",
                    "FAKE_REGION": "${FAKE_REGION:-US}",
                    "FAKE_STATIC": "literal-no-placeholder",
                },
            },
        },
    }))

    with patch("shared.platform_profile_env._PROFILES_DIR", tmp_path / "platform-profiles"):
        assert pass_through_vars("fake-cms") == {"FAKE_API_KEY", "FAKE_REGION"}


def test_pass_through_handles_malformed_json(tmp_path: Path) -> None:
    """A broken harness-mcp.json returns empty rather than crashing the spawn."""
    fake_profiles_dir = tmp_path / "platform-profiles" / "broken"
    fake_profiles_dir.mkdir(parents=True)
    (fake_profiles_dir / "harness-mcp.json").write_text("not valid json {")

    with patch("shared.platform_profile_env._PROFILES_DIR", tmp_path / "platform-profiles"):
        assert pass_through_vars("broken") == set()


def test_pass_through_real_contentstack_profile() -> None:
    """The shipped contentstack profile lists exactly the 6 vars we declared
    on Settings — pin this so adding/removing one in the JSON without
    updating Settings (or vice versa) shows up as a test failure."""
    expected = {
        "CONTENTSTACK_API_KEY",
        "CONTENTSTACK_DELIVERY_TOKEN",
        "CONTENTSTACK_MANAGEMENT_TOKEN",
        "CONTENTSTACK_REGION",
        "CONTENTSTACK_ENVIRONMENT",
        "CONTENTSTACK_BRANCH",
    }
    assert pass_through_vars("contentstack") == expected
