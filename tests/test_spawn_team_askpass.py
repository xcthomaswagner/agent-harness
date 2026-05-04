#!/usr/bin/env python3
"""Regression tests for the ADO PAT askpass migration in scripts/spawn_team.py.

Before this fix, Azure Repos auth was installed by writing
``https://ado-agent:{PAT}@host/...`` verbatim into the worktree's
``.git/config`` via ``git remote set-url``. The file persisted for the
full agent session and stayed on disk whenever the worktree was kept
for debugging (failed / escalated runs, completion-pending backlog) —
a grep of ``.git/config`` across preserved worktrees would leak the
PAT.

The fix:
  * Remote URL is plain (no credentials inline).
  * A ``GIT_ASKPASS`` helper script reads ``$ADO_PAT`` from the child
    process env at run time — the file body has no secrets.
  * Helper is ``chmod 0700`` (owner-only).
  * Helper is deleted in a finally block after the session exits,
    success or failure.

These tests verify each of those properties end-to-end by running
``spawn_team.main()`` under controlled mocks — the ``claude`` binary
invocation is replaced with a no-op so the tests run in seconds and
don't require Anthropic's CLI to be installed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_spawn_team_module():
    """Import scripts/spawn_team.py as a module.

    Reused from test_spawn_team_watcher.py — the script is a CLI tool,
    not a package, so we load it by path.
    """
    module_name = "_spawn_team_askpass_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPTS_DIR / "spawn_team.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ado_client_repo(tmp_path: Path) -> Path:
    """Build a minimal git repo the spawn script can worktree from."""
    repo = tmp_path / "client-repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    # Need an identity for the initial commit.
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
    )
    (repo / "README.md").write_text("test\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def ado_profile_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create an Azure Repos client profile in a tmp profile dir and
    patch the client_profile module to look there."""
    profile_name = "test-ado-profile"
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / f"{profile_name}.yaml").write_text(
        """
client: "Test Client"
ticket_source:
  type: "ado"
  instance: "https://dev.azure.com/test-org"
  project_key: "TEST"
  ado_project_name: "Test"
  ai_label: "ai-implement"
  quick_label: "ai-quick"
source_control:
  type: "azure-repos"
  org: "https://dev.azure.com/test-org"
  repo: "test-repo"
  default_branch: "main"
  branch_prefix: "ai/"
  ado_project: "Test"
  ado_repository_id: "00000000-0000-0000-0000-000000000000"
"""
    )
    # spawn_team imports load_profile from l1_preprocessing.client_profile
    # AFTER adding services/ to sys.path. Redirect PROFILES_DIR at import
    # time — it's the module-level constant the function reads when
    # called with no profiles_dir argument.
    sys.path.insert(0, str(REPO_ROOT / "services"))
    from l1_preprocessing import client_profile  # type: ignore
    monkeypatch.setattr(client_profile, "PROFILES_DIR", profiles_dir)
    return profile_name


def _make_mock_subprocess_run(
    captured_calls: list[dict],
    worktree_dir_holder: list[Path],
):
    """Return a subprocess.run replacement that:
      * records the call,
      * for ``claude`` invocations — snapshots the askpass file state
        (as if the agent were running) and returns a success result,
      * for ``git`` invocations — forwards to the real subprocess.run.
    """
    real_run = subprocess.run

    def fake_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args", [])
        captured_calls.append({
            "argv": list(argv),
            "env": dict(kwargs.get("env") or {}),
            "cwd": kwargs.get("cwd"),
        })
        # Only intercept the claude binary — everything else is real git.
        if argv and argv[0] == "claude":
            # Record worktree state at the moment claude would run.
            cwd = Path(kwargs.get("cwd", "."))
            worktree_dir_holder.append(cwd)
            # Return a minimal CompletedProcess.
            proc = MagicMock()
            proc.returncode = 0
            return proc
        return real_run(*args, **kwargs)

    return fake_run


def test_spawn_does_not_leak_pat_into_git_config(
    ado_client_repo: Path,
    ado_profile_fixture: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug regression: the Phase-2 fix removed embedded credentials from
    .git/config. Grep of the file must find no occurrence of the PAT.
    """
    spawn_team = _load_spawn_team_module()

    pat_value = "super-secret-ado-pat-value-0123456789"
    monkeypatch.setenv("ADO_PAT", pat_value)

    ticket_json = tmp_path / "ticket.json"
    ticket_json.write_text(json.dumps({"id": "TEST-1"}))

    captured: list[dict] = []
    worktree_holder: list[Path] = []

    # Patch argv + subprocess.run in the spawn_team module namespace.
    argv = [
        "spawn_team.py",
        "--client-repo", str(ado_client_repo),
        "--ticket-json", str(ticket_json),
        "--branch-name", "ai/TEST-1",
        "--client-profile", ado_profile_fixture,
    ]

    with patch.object(sys, "argv", argv), patch.object(
        spawn_team.subprocess, "run",
        side_effect=_make_mock_subprocess_run(captured, worktree_holder),
    ), patch.object(
        spawn_team.urllib.request, "urlopen",
        side_effect=lambda *a, **kw: _FakeUrlopenResponse(),
    ):
        try:
            spawn_team.main()
        except SystemExit as exc:
            # main() calls sys.exit(exit_code). Success (0) is fine.
            assert exc.code == 0 or exc.code is None

    # Worktree dir is <client_repo>.parent / worktrees / ai/TEST-1
    worktree_dir = ado_client_repo.parent / "worktrees" / "ai/TEST-1"
    git_config = worktree_dir / ".git" / "config"
    # Worktree .git might be a file (worktree config) — fall back
    # to reading the main repo's config-worktree when that's the case.
    if git_config.exists():
        content = git_config.read_text()
    else:
        # .git could be a file pointing to the shared dir.
        dot_git = worktree_dir / ".git"
        if dot_git.is_file():
            gitdir_line = dot_git.read_text().strip()
            # "gitdir: /path/to/..."
            _, _, gitdir = gitdir_line.partition("gitdir: ")
            gitdir = gitdir.strip()
            candidate = Path(gitdir) / "config"
            if candidate.exists():
                content = candidate.read_text()
            else:
                # Walk up for a worktree config file.
                content = ""
        else:
            content = ""

    # Also check the main repo's config, just in case.
    main_config = ado_client_repo / ".git" / "config"
    combined = content + "\n" + (main_config.read_text() if main_config.exists() else "")
    assert pat_value not in combined, (
        "ADO PAT must not appear anywhere in .git/config after spawn"
    )


def test_spawn_writes_askpass_with_0700_and_deletes_after(
    ado_client_repo: Path,
    ado_profile_fixture: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The askpass helper must exist with mode 0700 during the session
    (verified via the 'claude' invocation snapshot) AND be deleted
    after spawn exits."""
    spawn_team = _load_spawn_team_module()

    pat_value = "secret-pat-askpass-test"
    monkeypatch.setenv("ADO_PAT", pat_value)

    ticket_json = tmp_path / "ticket.json"
    ticket_json.write_text(json.dumps({"id": "TEST-2"}))

    askpass_observed: list[dict] = []
    captured_during_claude: list[dict] = []

    real_run = subprocess.run

    def recording_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args", [])
        if argv and argv[0] == "claude":
            cwd = Path(kwargs.get("cwd", "."))
            askpass_path = cwd / ".harness" / ".harness-askpass"
            entry = {
                "exists_during_session": askpass_path.exists(),
                "mode_during_session": (
                    askpass_path.stat().st_mode & 0o777
                    if askpass_path.exists()
                    else None
                ),
                "content_contains_secret": (
                    pat_value in askpass_path.read_text()
                    if askpass_path.exists()
                    else False
                ),
                "env_has_git_askpass": "GIT_ASKPASS" in (kwargs.get("env") or {}),
                "env_has_ado_pat": "ADO_PAT" in (kwargs.get("env") or {}),
                "askpass_path": str(askpass_path),
            }
            askpass_observed.append(entry)
            captured_during_claude.append(entry)
            proc = MagicMock()
            proc.returncode = 0
            return proc
        return real_run(*args, **kwargs)

    argv = [
        "spawn_team.py",
        "--client-repo", str(ado_client_repo),
        "--ticket-json", str(ticket_json),
        "--branch-name", "ai/TEST-2",
        "--client-profile", ado_profile_fixture,
    ]

    with patch.object(sys, "argv", argv), patch.object(
        spawn_team.subprocess, "run", side_effect=recording_run,
    ), patch.object(
        spawn_team.urllib.request, "urlopen",
        side_effect=lambda *a, **kw: _FakeUrlopenResponse(),
    ):
        try:
            spawn_team.main()
        except SystemExit as exc:
            assert exc.code == 0 or exc.code is None

    # Claude was invoked exactly once.
    assert len(askpass_observed) == 1, (
        f"expected one claude invocation, got {len(askpass_observed)}"
    )
    snap = askpass_observed[0]
    # During the session the helper existed with mode 0700.
    assert snap["exists_during_session"], "askpass helper missing during session"
    assert snap["mode_during_session"] == 0o700, (
        f"askpass must be 0o700, got {oct(snap['mode_during_session'] or 0)}"
    )
    # File body MUST NOT contain the PAT — it reads from env at run time.
    assert not snap["content_contains_secret"], (
        "askpass file body leaks the PAT — should read $ADO_PAT at run time"
    )
    # Env wiring: both GIT_ASKPASS and ADO_PAT must reach the child.
    assert snap["env_has_git_askpass"], (
        "GIT_ASKPASS must be in the subprocess env so git can pick it up"
    )
    assert snap["env_has_ado_pat"], (
        "ADO_PAT must be re-injected after env sanitization for the helper"
    )
    # Post-exit: helper is gone.
    askpass_path = Path(snap["askpass_path"])
    assert not askpass_path.exists(), (
        f"askpass file must be deleted after spawn exits: {askpass_path}"
    )


def test_spawn_deletes_askpass_even_when_session_times_out(
    ado_client_repo: Path,
    ado_profile_fixture: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cleanup path must fire on timeouts too. Simulate a timeout
    by having the mock subprocess.run raise TimeoutExpired.
    """
    spawn_team = _load_spawn_team_module()

    pat_value = "timeout-pat"
    monkeypatch.setenv("ADO_PAT", pat_value)

    ticket_json = tmp_path / "ticket.json"
    ticket_json.write_text(json.dumps({"id": "TEST-3"}))

    askpass_paths_during: list[Path] = []
    real_run = subprocess.run

    def timeout_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args", [])
        if argv and argv[0] == "claude":
            cwd = Path(kwargs.get("cwd", "."))
            askpass_paths_during.append(cwd / ".harness" / ".harness-askpass")
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)
        return real_run(*args, **kwargs)

    argv = [
        "spawn_team.py",
        "--client-repo", str(ado_client_repo),
        "--ticket-json", str(ticket_json),
        "--branch-name", "ai/TEST-3",
        "--client-profile", ado_profile_fixture,
    ]

    with patch.object(sys, "argv", argv), patch.object(
        spawn_team.subprocess, "run", side_effect=timeout_run,
    ), patch.object(
        spawn_team.urllib.request, "urlopen",
        side_effect=lambda *a, **kw: _FakeUrlopenResponse(),
    ):
        try:
            spawn_team.main()
        except SystemExit:
            pass  # timeout path sys.exits with 124

    # Even though the subprocess raised, the finally block deleted the helper.
    assert askpass_paths_during, "claude was never invoked"
    assert not askpass_paths_during[0].exists(), (
        "askpass must be deleted on timeout too"
    )


def test_completion_callback_sends_api_key_header(
    ado_client_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When L1 protects /api/agent-complete with API_KEY, spawn_team must
    send it as X-API-Key or completed tickets remain stuck in-flight."""
    spawn_team = _load_spawn_team_module()

    monkeypatch.setenv("API_KEY", "l1-control-plane-key")
    ticket_json = tmp_path / "ticket.json"
    ticket_json.write_text(json.dumps({"id": "TEST-4"}))

    requests: list[object] = []
    real_run = subprocess.run

    def recording_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args", [])
        if argv and argv[0] == "claude":
            proc = MagicMock()
            proc.returncode = 0
            return proc
        return real_run(*args, **kwargs)

    def recording_urlopen(req, *args, **kwargs):
        requests.append(req)
        return _FakeUrlopenResponse()

    argv = [
        "spawn_team.py",
        "--client-repo", str(ado_client_repo),
        "--ticket-json", str(ticket_json),
        "--branch-name", "ai/TEST-4",
    ]

    with patch.object(sys, "argv", argv), patch.object(
        spawn_team.subprocess, "run", side_effect=recording_run,
    ), patch.object(
        spawn_team.urllib.request, "urlopen", side_effect=recording_urlopen,
    ):
        try:
            spawn_team.main()
        except SystemExit as exc:
            assert exc.code == 0 or exc.code is None

    completion_requests = [
        req for req in requests if req.full_url.endswith("/api/agent-complete")
    ]
    assert completion_requests, "spawn_team never called /api/agent-complete"
    assert completion_requests[-1].headers["X-api-key"] == "l1-control-plane-key"


def test_contentstack_preflight_requires_minimum_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawn_team = _load_spawn_team_module()
    for name in (
        "CONTENTSTACK_API_KEY",
        "CONTENTSTACK_DELIVERY_TOKEN",
        "CONTENTSTACK_REGION",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as exc:
        spawn_team._preflight_platform_profile("contentstack")

    assert exc.value.code == 1
    assert "CONTENTSTACK_API_KEY" in capsys.readouterr().err


def test_contentstack_preflight_rejects_unsupported_groups(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawn_team = _load_spawn_team_module()
    monkeypatch.setenv("CONTENTSTACK_API_KEY", "key")
    monkeypatch.setenv("CONTENTSTACK_DELIVERY_TOKEN", "token")
    monkeypatch.setenv("CONTENTSTACK_REGION", "NA")
    monkeypatch.setenv("CONTENTSTACK_MCP_GROUPS", "all")

    with pytest.raises(SystemExit) as exc:
        spawn_team._preflight_platform_profile("contentstack")

    assert exc.value.code == 1
    assert "CONTENTSTACK_MCP_GROUPS" in capsys.readouterr().err


def test_replays_pending_completion_with_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prior failed completion callback should be retried before its
    worktree is cleaned up for a same-ticket rerun."""
    spawn_team = _load_spawn_team_module()
    monkeypatch.setenv("API_KEY", "l1-control-plane-key")

    worktree_dir = tmp_path / "worktree"
    pending = worktree_dir / ".harness" / "completion-pending.json"
    pending.parent.mkdir(parents=True)
    pending.write_text(json.dumps({
        "ticket_id": "TEST-5",
        "trace_id": "trace-5",
        "status": "complete",
        "pr_url": "https://example.test/pr/5",
        "branch": "ai/TEST-5",
        "failed_units": [],
        "source": "ado",
    }))

    requests: list[object] = []

    def recording_urlopen(req, *args, **kwargs):
        requests.append(req)
        return _FakeUrlopenResponse()

    with patch.object(
        spawn_team.urllib.request, "urlopen", side_effect=recording_urlopen,
    ):
        assert spawn_team._replay_completion_pending(worktree_dir)

    assert not pending.exists()
    assert requests
    assert requests[-1].headers["X-api-key"] == "l1-control-plane-key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeUrlopenResponse:
    """Minimal urlopen stand-in for the completion-callback POST."""

    def __enter__(self) -> "_FakeUrlopenResponse":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def read(self) -> bytes:
        return b""

    def getcode(self) -> int:
        return 200
