#!/usr/bin/env python3
"""Tests for the shell-script → Python wrapper scripts.

Phase 3 of the security remediation replaced the following shell scripts
with thin wrappers that exec their Python canonical equivalents:

* ``scripts/spawn-team.sh``        → ``scripts/spawn_team.py``
* ``scripts/cleanup-worktree.sh``  → ``scripts/cleanup_worktree.py``
* ``scripts/direct-spawn.sh``      → ``scripts/direct_spawn.py``

These tests enforce the wrapper invariants so the shell variants stay
trivial and never re-accumulate their own Bash implementation (which
had drifted from the Python path — lock-file semantics, uncommitted-work
guards, and stale PID detection were missing or wrong).
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"

_WRAPPERS = [
    ("spawn-team.sh", "spawn_team.py"),
    ("cleanup-worktree.sh", "cleanup_worktree.py"),
    ("direct-spawn.sh", "direct_spawn.py"),
]


@pytest.mark.parametrize(("shell_name", "py_name"), _WRAPPERS)
def test_wrapper_exists(shell_name: str, py_name: str) -> None:
    shell_path = SCRIPTS_DIR / shell_name
    py_path = SCRIPTS_DIR / py_name
    assert shell_path.exists(), f"wrapper missing: {shell_path}"
    assert py_path.exists(), f"canonical impl missing: {py_path}"


@pytest.mark.parametrize(("shell_name", "_py"), _WRAPPERS)
def test_wrapper_is_executable(shell_name: str, _py: str) -> None:
    shell_path = SCRIPTS_DIR / shell_name
    mode = shell_path.stat().st_mode
    assert mode & 0o111, f"{shell_path} not executable (mode={oct(mode)})"


@pytest.mark.parametrize(("shell_name", "py_name"), _WRAPPERS)
def test_wrapper_exec_python_substring(shell_name: str, py_name: str) -> None:
    """Wrapper must exec the Python canonical script with forwarded args."""
    shell_path = SCRIPTS_DIR / shell_name
    body = shell_path.read_text()
    # Accepts the variants `"$(dirname "$0")/name.py"` and
    # `$(dirname $0)/name.py` to stay tolerant of quoting tweaks.
    assert 'exec python3' in body, (
        f"{shell_path} must use 'exec python3' so signals/exit codes pass through"
    )
    assert py_name in body, (
        f"{shell_path} must reference {py_name!r} — the canonical Python script"
    )
    assert '"$@"' in body, (
        f'{shell_path} must forward all args with "$@"'
    )


@pytest.mark.parametrize(("shell_name", "_py"), _WRAPPERS)
def test_wrapper_is_thin(shell_name: str, _py: str) -> None:
    """Wrapper body must be <10 lines of actual code (excl. comments/blank).

    Guards against the wrapper drifting back into its own Bash
    reimplementation of logic that belongs in the Python canonical
    path.
    """
    shell_path = SCRIPTS_DIR / shell_name
    lines = shell_path.read_text().splitlines()
    code_lines = [
        ln for ln in lines
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert len(code_lines) < 10, (
        f"{shell_path} has {len(code_lines)} lines of code; "
        "wrappers must stay thin. Move logic to the Python canonical path."
    )
