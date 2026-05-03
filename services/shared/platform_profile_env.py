"""Platform-profile env pass-through helpers.

A platform profile (`runtime/platform-profiles/<name>/`) may ship a
``harness-mcp.json`` whose env values reference shell-style placeholders
(``${VAR}`` / ``${VAR:-default}``) — for example, the ContentStack
profile's MCP needs ``CONTENTSTACK_API_KEY``, ``CONTENTSTACK_REGION``,
etc.

These vars live in ``services/l1_preprocessing/.env`` so they're
inherited by spawned L2 agent subprocesses. The problem this module
solves: ``shared.env_sanitize.sanitized_env()`` strips anything ending
in ``_API_KEY`` / ``_TOKEN`` / ``_KEY`` (correct for security), and the
inject-runtime / spawn-team subprocesses don't have a generic way to
re-inject the *specific* vars a given platform profile needs.

This module exposes one function: :func:`pass_through_vars(profile_name)`
returns the set of env var names the profile's harness-mcp.json
references. Callers (spawn_team for the agent subprocess, spawn_team
for the inject_runtime subprocess) can use that set to surgically
re-inject only what each platform actually needs.

The pattern matches how ``ADO_PAT`` is conditionally re-injected today
(spawn_team.py, around the ``profile.is_azure_repos`` block) — but
generalized so future platform profiles don't need parallel hardcoded
blocks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Matches ${VAR} and ${VAR:-default}. Mirrors the regex used by
# scripts/inject_runtime.py so a placeholder accepted there is also
# discoverable here.
_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROFILES_DIR = _REPO_ROOT / "runtime" / "platform-profiles"


def _scan_for_placeholders(value: object) -> set[str]:
    """Walk a JSON-decoded value, returning every ${VAR} name found."""
    found: set[str] = set()
    if isinstance(value, str):
        for match in _ENV_VAR_RE.finditer(value):
            found.add(match.group(1))
    elif isinstance(value, dict):
        for v in value.values():
            found |= _scan_for_placeholders(v)
    elif isinstance(value, list):
        for v in value:
            found |= _scan_for_placeholders(v)
    return found


def pass_through_vars(profile_name: str) -> set[str]:
    """Return env var names the named platform profile's MCP needs.

    Reads ``runtime/platform-profiles/<profile_name>/harness-mcp.json``
    and returns every ``${VAR}`` name found in any string value (env
    blocks, args, etc.). Returns an empty set if the profile or the
    file doesn't exist — callers fall back to the no-pass-through
    behavior cleanly.

    Empty/None ``profile_name`` returns an empty set, so callers don't
    need to guard for "no profile selected" themselves.
    """
    if not profile_name:
        return set()

    mcp_path = _PROFILES_DIR / profile_name / "harness-mcp.json"
    if not mcp_path.is_file():
        return set()

    try:
        config = json.loads(mcp_path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()

    return _scan_for_placeholders(config)
