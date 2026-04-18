"""Tests for pipeline._ensure_client_repo.

Covers:
- Missing path → clone from source_control.
- Exists but not a git repo → skip (don't clobber).
- Exists, git repo, wrong remote → skip.
- Exists, git repo, matching remote → fetch (no destructive reset).
- Concurrent calls for the same path → only one clone actually runs.
- PAT never appears in log output.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline import (
    _build_clone_url,
    _ensure_client_repo,
    _get_repo_lock,
    _normalize_remote,
)


class _FakeLog:
    """Minimal structlog-like logger that records everything."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _record(self, level: str, event: str, **kw: object) -> None:
        self.events.append((level, event, dict(kw)))

    def info(self, event: str, **kw: object) -> None:
        self._record("info", event, **kw)

    def warning(self, event: str, **kw: object) -> None:
        self._record("warning", event, **kw)

    def error(self, event: str, **kw: object) -> None:
        self._record("error", event, **kw)


def _event_names(log: _FakeLog) -> list[str]:
    return [e[1] for e in log.events]


# ---------------- helpers --------------------------------------------------


def test_normalize_remote_strips_creds_and_trailing_slash() -> None:
    a = "https://user:secret@dev.azure.com/org/project/_git/repo"
    b = "https://dev.azure.com/org/project/_git/repo/"
    assert _normalize_remote(a) == _normalize_remote(b)


def test_normalize_remote_strips_git_suffix() -> None:
    a = "https://github.com/foo/bar.git"
    b = "https://github.com/foo/bar"
    assert _normalize_remote(a) == _normalize_remote(b)


def test_build_clone_url_ado_requires_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADO_PAT", raising=False)
    url = _build_clone_url({
        "type": "azure-repos", "org": "https://x/", "ado_project": "P", "repo": "R",
    })
    assert url == ""


def test_build_clone_url_ado_embeds_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADO_PAT", "FAKE_PAT")
    url = _build_clone_url({
        "type": "azure-repos",
        "org": "https://x.visualstudio.com",
        "ado_project": "XC-SF-30in30",
        "repo": "XC-SF-30in30",
    })
    assert "FAKE_PAT" in url
    assert "x.visualstudio.com/XC-SF-30in30/_git/XC-SF-30in30" in url


# ---------------- _ensure_client_repo -------------------------------------


def test_missing_path_clones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADO_PAT", "FAKE_PAT")
    target = tmp_path / "new-repo"
    sc = {
        "type": "azure-repos",
        "org": "https://x.visualstudio.com",
        "ado_project": "P",
        "repo": "R",
    }
    log = _FakeLog()

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # Simulate clone creating the directory so subsequent checks pass.
        if cmd[0:2] == ["git", "clone"]:
            Path(cmd[3]).mkdir(parents=True)
            (Path(cmd[3]) / ".git").mkdir()
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("pipeline.subprocess.run", side_effect=fake_run):
        assert _ensure_client_repo(str(target), sc, log) is True

    assert target.exists()
    assert any(c[0:2] == ["git", "clone"] for c in calls)
    # PAT must never appear in structured logs.
    for _lvl, _evt, kw in log.events:
        for v in kw.values():
            assert "FAKE_PAT" not in str(v)


def test_partial_clone_is_cleaned_up_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: timed-out or non-zero-exit clone must not leave a
    partial .git dir behind. If it did, the next webhook would see
    "path exists and is a git repo" and silently return True with a
    broken repo underneath."""
    monkeypatch.setenv("ADO_PAT", "FAKE_PAT")
    target = tmp_path / "partial"
    sc = {
        "type": "azure-repos",
        "org": "https://x.visualstudio.com",
        "ado_project": "P",
        "repo": "R",
    }
    log = _FakeLog()

    def fake_run(cmd, **kw):
        if cmd[0:2] == ["git", "clone"]:
            # Simulate git having created the dir + a partial .git before
            # the exit.
            Path(cmd[3]).mkdir(parents=True, exist_ok=True)
            (Path(cmd[3]) / ".git").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(
                cmd, 128, stdout="", stderr="fatal: connection reset",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("pipeline.subprocess.run", side_effect=fake_run):
        assert _ensure_client_repo(str(target), sc, log) is False
    # The partial dir must be gone so a retry cleanly re-clones.
    assert not target.exists()


def test_missing_config_or_auth_skips_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADO_PAT", raising=False)
    target = tmp_path / "nope"
    log = _FakeLog()
    assert _ensure_client_repo(str(target), {"type": "azure-repos"}, log) is False
    assert "client_repo_clone_skipped" in _event_names(log)


def test_exists_but_not_git_does_not_clobber(tmp_path: Path) -> None:
    target = tmp_path / "plain"
    target.mkdir()
    (target / "keepme.txt").write_text("dont touch this")
    log = _FakeLog()
    result = _ensure_client_repo(str(target), {}, log)
    assert result is False
    assert (target / "keepme.txt").read_text() == "dont touch this"
    assert "client_repo_not_git" in _event_names(log)


def test_wrong_remote_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADO_PAT", "FAKE_PAT")
    target = tmp_path / "repo"
    target.mkdir()
    (target / ".git").mkdir()
    sc = {
        "type": "azure-repos",
        "org": "https://x.visualstudio.com",
        "ado_project": "P",
        "repo": "R",
    }
    log = _FakeLog()

    def fake_run(cmd, **kw):
        if cmd[:5] == ["git", "-C", str(target), "remote", "get-url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://other.example.com/foo/bar\n", stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("pipeline.subprocess.run", side_effect=fake_run):
        assert _ensure_client_repo(str(target), sc, log) is False
    assert "client_repo_remote_mismatch" in _event_names(log)


def test_existing_matching_repo_fetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADO_PAT", "FAKE_PAT")
    target = tmp_path / "repo"
    target.mkdir()
    (target / ".git").mkdir()
    sc = {
        "type": "azure-repos",
        "org": "https://x.visualstudio.com",
        "ado_project": "P",
        "repo": "R",
    }
    log = _FakeLog()
    captured_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        captured_calls.append(list(cmd))
        if cmd[:5] == ["git", "-C", str(target), "remote", "get-url"]:
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout="https://any:FAKE_PAT@x.visualstudio.com/P/_git/R\n",
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("pipeline.subprocess.run", side_effect=fake_run):
        assert _ensure_client_repo(str(target), sc, log) is True
    # Fetch must have run...
    assert any("fetch" in c for c in captured_calls), (
        "git fetch should have been called"
    )
    # ...but `reset --hard` must NOT, because worktrees share refs and a
    # reset during an in-flight agent push can corrupt refs.
    for c in captured_calls:
        assert "reset" not in c, f"Unexpected `git reset` in call: {c}"


def test_concurrent_calls_serialize_via_per_repo_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads hitting the same missing path must not both clone."""
    monkeypatch.setenv("ADO_PAT", "FAKE_PAT")
    target = tmp_path / "race"
    sc = {
        "type": "azure-repos",
        "org": "https://x.visualstudio.com",
        "ado_project": "P",
        "repo": "R",
    }
    log = _FakeLog()
    clone_counter = {"count": 0}
    clone_barrier = threading.Event()

    def fake_run(cmd, **kw):
        if cmd[0:2] == ["git", "clone"]:
            clone_counter["count"] += 1
            # Hold the first clone open so a second would see the in-progress
            # state if the lock weren't protecting it.
            clone_barrier.wait(timeout=2.0)
            Path(cmd[3]).mkdir(parents=True)
            (Path(cmd[3]) / ".git").mkdir()
        if cmd[:5] == ["git", "-C", str(target), "remote", "get-url"]:
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout="https://any:FAKE_PAT@x.visualstudio.com/P/_git/R\n",
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    results: list[bool] = []

    def worker() -> None:
        results.append(_ensure_client_repo(str(target), sc, log))

    # Patch at the test scope (not per-thread), so the patch lifetime
    # is bounded by ``with`` + the join below. A per-thread patch
    # could outlive a thread that's still mid-call at teardown, which
    # leaks the monkeypatch into sibling tests.
    with patch("pipeline.subprocess.run", side_effect=fake_run):
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        # Release the first clone after a beat so the second waiter can proceed.
        threading.Timer(0.3, clone_barrier.set).start()
        t1.join(timeout=5)
        t2.join(timeout=5)
    # Both callers should report success...
    assert results == [True, True]
    # ...but only ONE clone should have run.
    assert clone_counter["count"] == 1


def test_get_repo_lock_is_stable_per_path() -> None:
    a = _get_repo_lock("/x/y")
    b = _get_repo_lock("/x/y")
    c = _get_repo_lock("/x/z")
    assert a is b
    assert a is not c
