"""Environment sanitization for agent subprocess sessions.

Strips secret environment variables so agent sessions use the Max
subscription and don't leak credentials into logs or subprocess
environments.

Two gating mechanisms:

1. **Explicit denylist** — ``SECRET_VARS`` is an exact-match frozenset
   covering well-known cloud/tool credentials. Variables whose names
   don't match a suffix pattern (e.g. ``API_KEY`` — no underscore
   suffix, so the suffix matcher would miss it) belong here.
2. **Suffix denylist** — ``_SECRET_SUFFIXES`` is a tuple of
   name-ending suffixes. Any env var whose name ends in ``_TOKEN`` /
   ``_SECRET`` / ``_API_KEY`` / ``_PAT`` / ``_PASSWORD`` /
   ``_CREDENTIALS`` / ``_KEY`` / ``_ACCESS_KEY`` gets stripped.
   Catches the long tail of custom vendor keys (``STRIPE_SECRET``,
   ``MYSERVICE_API_TOKEN``, ``FOO_CREDENTIALS``) without listing
   every name by hand. The underscore anchor is deliberate —
   ``PATH`` doesn't end in ``_PATH`` (no leading underscore) so it's
   never matched.

Agent sessions authenticate to GitHub as the dedicated agent account
(xcagentrockwell) via ``AGENT_GH_TOKEN``, not the operator's
``GITHUB_TOKEN``.
"""

from __future__ import annotations

import os

# Canonical list of secret env vars to strip from agent sessions.
# Variables that don't have a ``_TOKEN`` / ``_SECRET`` / ``_KEY`` /
# ``_PAT`` / ``_PASSWORD`` / ``_CREDENTIALS`` suffix go here so the
# suffix denylist below doesn't miss them. Add new secrets here.
SECRET_VARS: frozenset[str] = frozenset({
    "ANTHROPIC_API_KEY",
    "JIRA_API_TOKEN",
    "ADO_PAT",
    "GITHUB_WEBHOOK_SECRET",
    "FIGMA_API_TOKEN",
    "WEBHOOK_SECRET",
    "REDIS_URL",
    "GITHUB_TOKEN",       # Operator's token — agents use AGENT_GH_TOKEN instead
    "GH_TOKEN",           # Operator's gh CLI token — overwritten by AGENT_GH_TOKEN below
    "AGENT_GH_TOKEN",     # Raw agent token — injected as GH_TOKEN below
    "API_KEY",            # L1 control-plane auth — must not leak to agents
    "L1_INTERNAL_API_TOKEN",  # L1 autonomy internal API — must not leak to agents
    "AUTONOMY_ADMIN_TOKEN",   # L1 autonomy admin writes — must not leak to agents
    "JIRA_BUG_WEBHOOK_TOKEN", # Jira bug webhook bearer — must not leak to agents
    "ADO_WEBHOOK_TOKEN",      # ADO Service Hook shared secret — must not leak to agents
    # Cloud provider / tooling secrets. These are "always strip" —
    # operator machines very likely have them set for local dev,
    # and none of them should ever reach an agent subprocess.
    "CLAUDE_API_KEY",
    "OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_CLIENT_SECRET",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "SLACK_TOKEN",
    "NPM_TOKEN",
    "HF_TOKEN",
})

# Suffixes that mark an env var as secret. The suffix must include
# the leading underscore — ``_KEY`` not ``KEY`` — so benign vars
# like ``PATH`` / ``MONKEY`` / ``HOMEBREW`` don't trip the filter.
# Ordered from most to least specific so the strip log line is
# accurate if we ever add per-suffix logging.
_SECRET_SUFFIXES: tuple[str, ...] = (
    "_ACCESS_KEY",
    "_API_KEY",
    "_CREDENTIALS",
    "_PASSWORD",
    "_SECRET",
    "_TOKEN",
    "_PAT",
    "_KEY",
)


def _is_secret(name: str) -> bool:
    """Return True if ``name`` should be stripped from an agent's env.

    Matches against the explicit ``SECRET_VARS`` allowlist first,
    then against the suffix denylist. Case-sensitive — env vars are
    conventionally UPPER_SNAKE_CASE and mixed-case matches would be
    false positives for rare user-set vars like ``Monkey`` or ``path``.
    """
    if name in SECRET_VARS:
        return True
    return any(name.endswith(suffix) for suffix in _SECRET_SUFFIXES)


def sanitized_env() -> dict[str, str]:
    """Return a copy of os.environ with secret variables removed.

    If AGENT_GH_TOKEN is set, it is injected as GH_TOKEN so the agent's
    gh CLI commands authenticate as the dedicated agent GitHub account.
    """
    env = {k: v for k, v in os.environ.items() if not _is_secret(k)}

    agent_token = os.environ.get("AGENT_GH_TOKEN", "")
    if agent_token:
        env["GH_TOKEN"] = agent_token

    return env
