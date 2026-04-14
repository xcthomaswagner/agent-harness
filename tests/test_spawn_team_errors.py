#!/usr/bin/env python3
"""Tests that spawn_team.py early-exit errors write to stderr (not stdout).

Regression coverage for the silent-failure mode where spawn_team.py printed
errors to stdout while L1's spawn wrapper captured only stderr. The symptom
was ``l2_spawn_failed exit_code=1 stderr=`` with no diagnostic context, and
operators had to manually diff the process tree to find the real cause
(almost always a missing client_repo path after a /tmp wipe on reboot).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SPAWN_TEAM = SCRIPTS_DIR / "spawn_team.py"


def _invoke(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SPAWN_TEAM), *args],
        capture_output=True, text=True, check=False,
    )


def test_missing_client_repo_writes_to_stderr(tmp_path: Path) -> None:
    """Missing client_repo path → error on stderr, nothing on stdout."""
    ticket = tmp_path / "ticket.json"
    ticket.write_text(json.dumps({"id": "TEST-1"}))
    missing = tmp_path / "does-not-exist"
    result = _invoke([
        "--client-repo", str(missing),
        "--ticket-json", str(ticket),
        "--branch-name", "ai/TEST-1",
    ])
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    # The specific path should surface in stderr so operators can grep it.
    assert str(missing) in result.stderr
    # And nothing should leak onto stdout — that's how we got silent
    # failures in the first place.
    assert "does not exist" not in result.stdout


def test_client_repo_exists_but_not_git_writes_to_stderr(tmp_path: Path) -> None:
    """Existing dir without .git → error on stderr."""
    not_git = tmp_path / "plain-dir"
    not_git.mkdir()
    ticket = tmp_path / "ticket.json"
    ticket.write_text(json.dumps({"id": "TEST-1"}))
    result = _invoke([
        "--client-repo", str(not_git),
        "--ticket-json", str(ticket),
        "--branch-name", "ai/TEST-1",
    ])
    assert result.returncode == 1
    assert "Not a git repository" in result.stderr
    assert str(not_git) in result.stderr
    assert "Not a git repository" not in result.stdout


def test_missing_ticket_json_writes_to_stderr(tmp_path: Path) -> None:
    """Missing ticket JSON → error on stderr."""
    # Needs a valid git repo first so we get past the first check.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    result = _invoke([
        "--client-repo", str(repo),
        "--ticket-json", str(tmp_path / "nope.json"),
        "--branch-name", "ai/TEST-1",
    ])
    assert result.returncode == 1
    assert "Ticket JSON file not found" in result.stderr


def test_invalid_ticket_json_writes_to_stderr(tmp_path: Path) -> None:
    """Invalid JSON → error on stderr."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not valid json")
    result = _invoke([
        "--client-repo", str(repo),
        "--ticket-json", str(bad_json),
        "--branch-name", "ai/TEST-1",
    ])
    assert result.returncode == 1
    assert "Invalid JSON" in result.stderr
