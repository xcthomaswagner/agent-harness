"""Shared subprocess helpers for the learning miner.

Previously duplicated between ``pr_opener`` (PR creation) and
``outcomes`` (merge-state polling + human-reedit git log). Pulled
out so both flows share the same env allowlist, token precedence,
and test monkeypatch surface.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from redaction import redact_token_urls


def safe_stderr_tail(stderr: str | None, limit: int = 200) -> str:
    """Redact token URLs, then tail to ``limit`` chars.

    Redact BEFORE truncate — a token URL clipped at the boundary
    wouldn't match the redaction regex and would leak the partial
    token. Centralized here so every logger that surfaces gh/git
    stderr gets the same treatment without each caller repeating
    the ordering (easy to forget, per iter-5's earlier fix in _run).
    """
    redacted = redact_token_urls(stderr or "")
    return redacted[-limit:] if len(redacted) > limit else redacted

# Proxy vars MUST be forwarded so git push / gh work behind corporate
# firewalls. Locking the allowlist to bare PATH/HOME/LANG/LC_ALL/USER
# silently breaks those deployments — the push hangs until the
# GIT_TERMINAL_PROMPT=0 guard forces an error with no useful message.
_ENV_ALLOWLIST = {
    "PATH", "HOME", "LANG", "LC_ALL", "USER",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "no_proxy",
}


def resolve_auth_token() -> str:
    """Return the GitHub PAT for the agent account.

    Precedence: ``AGENT_GH_TOKEN`` > ``GITHUB_TOKEN``. Empty string
    when neither is set — callers treat that as a misconfigured
    deployment (push fails loudly instead of silently using ambient
    credentials).

    Treat whitespace-only values as missing. A ``.env`` file with
    ``AGENT_GH_TOKEN=" "`` (or a trailing-space export) previously
    overrode GITHUB_TOKEN with whitespace — gh then rejected the
    token with a cryptic error, and the ``if not token:`` push
    guard let the whitespace through because the string is truthy.
    """
    raw = os.getenv("AGENT_GH_TOKEN", "").strip()
    if raw:
        return raw
    raw = os.getenv("GITHUB_TOKEN", "").strip()
    return raw


def build_env(token: str | None = None) -> dict[str, str]:
    """Subprocess env with the agent PAT wired in as ``GH_TOKEN``.

    Allowlist-based (not denylist) — keeps ANTHROPIC_API_KEY and other
    L1 secrets out of git/gh. ``GIT_TERMINAL_PROMPT=0`` +
    ``GIT_ASKPASS=/bin/true`` make auth failures fail instantly
    instead of hanging on an interactive prompt under
    ``capture_output=True``.

    Pass ``token=None`` to let the helper resolve via
    ``resolve_auth_token`` — that matches how both opener and polls
    invoke it today.
    """
    if token is None:
        token = resolve_auth_token()
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/true"
    if token:
        env["GH_TOKEN"] = token
    return env


def run_bin(
    binary: str,
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Thin subprocess.run wrapper shared by opener + outcomes.

    ``cwd`` optional so outcomes' merge-state poll (no working dir) and
    the opener's in-worktree calls can both share this helper.
    """
    return subprocess.run(
        [binary, *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
