"""Environment sanitization for agent subprocess sessions.

Strips secret environment variables so agent sessions use the Max subscription
and don't leak credentials into logs or subprocess environments.

Agent sessions authenticate to GitHub as the dedicated agent account
(xcagentrockwell) via AGENT_GH_TOKEN, not the operator's GITHUB_TOKEN.
"""

from __future__ import annotations

import os

# Canonical list of secret env vars to strip from agent sessions.
# Add new secrets here — this is the single source of truth.
SECRET_VARS: frozenset[str] = frozenset({
    "ANTHROPIC_API_KEY",
    "JIRA_API_TOKEN",
    "ADO_PAT",
    "GITHUB_WEBHOOK_SECRET",
    "FIGMA_API_TOKEN",
    "WEBHOOK_SECRET",
    "REDIS_URL",
    "GITHUB_TOKEN",       # Operator's token — agents use AGENT_GH_TOKEN instead
    "AGENT_GH_TOKEN",     # Raw agent token — injected as GH_TOKEN below
    "API_KEY",            # L1 control-plane auth — must not leak to agents
    "L1_INTERNAL_API_TOKEN",  # L1 autonomy internal API — must not leak to agents
    "AUTONOMY_ADMIN_TOKEN",   # L1 autonomy admin writes — must not leak to agents
})


def sanitized_env() -> dict[str, str]:
    """Return a copy of os.environ with secret variables removed.

    If AGENT_GH_TOKEN is set, it is injected as GH_TOKEN so the agent's
    gh CLI commands authenticate as the dedicated agent GitHub account.
    """
    env = {k: v for k, v in os.environ.items() if k not in SECRET_VARS}

    agent_token = os.environ.get("AGENT_GH_TOKEN", "")
    if agent_token:
        env["GH_TOKEN"] = agent_token

    return env
