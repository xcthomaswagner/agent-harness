"""Tests for shared.env_sanitize — the agent-subprocess env filter.

Phase 2 added a suffix denylist (``_TOKEN`` / ``_SECRET`` / ``_KEY`` /
``_PAT`` / ``_PASSWORD`` / ``_CREDENTIALS`` / ``_ACCESS_KEY``) on top
of the existing explicit ``SECRET_VARS`` set. These tests pin both
mechanisms so a future change can't silently regress either.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SERVICES_DIR = Path(__file__).resolve().parents[2]
if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))

from shared.env_sanitize import SECRET_VARS, _is_secret, sanitized_env  # noqa: E402

# ---------------------------------------------------------------------------
# Suffix-denylist coverage
# ---------------------------------------------------------------------------


def test_suffix_denylist_strips_custom_token() -> None:
    """A vendor-specific env var ending in ``_TOKEN`` is not in the
    explicit set but should still be stripped by the suffix rule."""
    with patch.dict(
        "os.environ",
        {"FOOBAR_TOKEN": "secret-value", "PATH": "/usr/bin:/bin"},
        clear=True,
    ):
        env = sanitized_env()
    assert "FOOBAR_TOKEN" not in env
    # PATH must survive — ``PATH`` doesn't end in ``_PATH`` (no
    # underscore prefix) and isn't in the explicit list.
    assert env.get("PATH") == "/usr/bin:/bin"


def test_suffix_denylist_strips_custom_pat() -> None:
    """A custom ``_PAT`` var gets stripped even if unknown."""
    with patch.dict(
        "os.environ",
        {"XYZ_PAT": "hunter2-pat", "HOME": "/Users/test"},
        clear=True,
    ):
        env = sanitized_env()
    assert "XYZ_PAT" not in env
    assert env.get("HOME") == "/Users/test"


def test_path_preserved() -> None:
    """``PATH`` must never be stripped — agents need it to exec binaries.
    Bug regression: an earlier suffix-only filter used ``KEY`` without
    an underscore anchor, matching ``MONKEY_SEE`` / ``HOMEBREW_KEY`` etc.
    The anchored ``_KEY`` suffix avoids that."""
    with patch.dict(
        "os.environ",
        {
            "PATH": "/usr/local/bin",
            "MONKEY": "see monkey do",
            "HOMEBREW_CELLAR": "/opt/homebrew/Cellar",
        },
        clear=True,
    ):
        env = sanitized_env()
    assert env.get("PATH") == "/usr/local/bin"
    assert env.get("MONKEY") == "see monkey do"
    assert env.get("HOMEBREW_CELLAR") == "/opt/homebrew/Cellar"


def test_home_preserved() -> None:
    """``HOME`` must never be stripped."""
    with patch.dict(
        "os.environ", {"HOME": "/Users/agent"}, clear=True,
    ):
        env = sanitized_env()
    assert env.get("HOME") == "/Users/agent"


# ---------------------------------------------------------------------------
# Explicit-list coverage
# ---------------------------------------------------------------------------


def test_claude_api_key_stripped() -> None:
    """``CLAUDE_API_KEY`` is in the explicit list — must be stripped."""
    with patch.dict(
        "os.environ",
        {"CLAUDE_API_KEY": "sk-ant-whatever"},
        clear=True,
    ):
        env = sanitized_env()
    assert "CLAUDE_API_KEY" not in env


def test_aws_access_key_id_stripped() -> None:
    """AWS creds are in the explicit list — stripping must happen even
    though ``AWS_ACCESS_KEY_ID`` would also match the ``_ACCESS_KEY``
    suffix rule (belt and braces)."""
    with patch.dict(
        "os.environ",
        {
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "abc",
            "AWS_SESSION_TOKEN": "xyz",
        },
        clear=True,
    ):
        env = sanitized_env()
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AWS_SESSION_TOKEN" not in env


# ---------------------------------------------------------------------------
# _is_secret predicate sanity
# ---------------------------------------------------------------------------


def test_is_secret_matches_explicit_list() -> None:
    assert _is_secret("ANTHROPIC_API_KEY")
    assert _is_secret("ADO_PAT")
    assert _is_secret("CLAUDE_API_KEY")


def test_is_secret_matches_suffix_rule() -> None:
    # None of these are in SECRET_VARS, but all end with a flagged suffix.
    assert _is_secret("VENDOR_X_TOKEN")
    assert _is_secret("CUSTOMER_PASSWORD")
    assert _is_secret("BOBS_CREDENTIALS")
    assert _is_secret("UNKNOWN_VENDOR_PAT")


def test_is_secret_false_positives_avoided() -> None:
    assert not _is_secret("PATH")
    assert not _is_secret("HOME")
    assert not _is_secret("USER")
    # ``MONKEY`` contains ``KEY`` but no leading underscore.
    assert not _is_secret("MONKEY")
    # ``PATH`` doesn't end in ``_PATH`` so no match.
    assert not _is_secret("XPATH")


# ---------------------------------------------------------------------------
# Contract: SECRET_VARS still recognised (existing behaviour preserved)
# ---------------------------------------------------------------------------


def test_legacy_secret_vars_still_stripped() -> None:
    """Existing behaviour: every name in SECRET_VARS must still be
    stripped. Regression guard for the suffix rewrite.

    Exclude ``AGENT_GH_TOKEN`` from the input entirely — when set,
    ``sanitized_env`` re-injects it as ``GH_TOKEN`` intentionally so
    the agent's ``gh`` CLI authenticates as the dedicated agent
    account. Including it in the input would trip the assertion on
    ``GH_TOKEN`` even though the re-injection is the documented
    behaviour.
    """
    preserved_env = {
        name: "value"
        for name in SECRET_VARS
        if name != "AGENT_GH_TOKEN"
    }
    preserved_env["HARMLESS_VAR"] = "keep-me"
    with patch.dict("os.environ", preserved_env, clear=True):
        env = sanitized_env()
    for name in SECRET_VARS:
        if name == "AGENT_GH_TOKEN":
            continue
        assert name not in env, f"{name} leaked through sanitized_env"
    assert env["HARMLESS_VAR"] == "keep-me"
