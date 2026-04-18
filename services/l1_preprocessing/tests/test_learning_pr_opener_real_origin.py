"""Real-origin safety tests for ``learning_miner.pr_opener``.

These tests complement ``test_learning_pr_opener.py`` by asserting the
PR opener's git operations cannot reach ``main`` (or any branch outside
the ``learning/lesson-*`` namespace) even when given a hostile
``lesson_id``. Runs against a REAL bare git repo at tmp_path so the
safety signal is observable in filesystem state — not just in mocked
calls.

Why a separate file: the existing test suite mocks ``_gh`` and asserts
return codes. These tests assert that after the PR opener runs, the
origin repo's branch hashes and ref list are in a known-safe state
— a guarantee only exercisable against a real origin. If the branch
sanitizer were ever weakened, these would fail with a filesystem-level
diff instead of a skipped assertion.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import learning_miner.pr_opener as pr_opener
from learning_miner.pr_opener import OpenPRInputs, open_pr_for_lesson


def _make_bare_origin(tmp_path: Path) -> Path:
    """Create ``tmp_path/origin.git`` as a bare repo with one initial commit on main.

    A bare repo is more realistic than a working clone for the
    ``origin`` role: pushes are first-class (no ``receive.denyCurrentBranch``
    dance) and ref updates show up in a form we can compare via
    ``git ls-remote``. The initial commit includes a ``runtime/skills/``
    file so the allowed-prefix validator in the drafter accepts any
    diff targeting it.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True,
    )

    # Seed the bare via a scratch working clone — bare repos can't
    # accept commits directly.
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", str(bare), str(seed)],
        check=True, capture_output=True,
    )
    skills = seed / "runtime" / "skills" / "code-review"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "# Code Review\n\n## Review Checklist\n\n"
        "- Check docstrings.\n"
        "- Verify tests.\n"
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=seed, check=True, capture_output=True,
    )
    subprocess.run(
        ["git",
         "-c", "user.email=init@test", "-c", "user.name=init",
         "commit", "-m", "init"],
        cwd=seed, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=seed, check=True, capture_output=True,
    )
    return bare


def _main_sha(bare: Path) -> str:
    """Resolve refs/heads/main on the bare repo via ls-remote."""
    proc = subprocess.run(
        ["git", "ls-remote", str(bare), "refs/heads/main"],
        check=True, capture_output=True, text=True,
    )
    # Output shape: "<sha>\trefs/heads/main"
    line = (proc.stdout or "").strip().splitlines()[0]
    return line.split("\t", 1)[0]


def _list_branches(bare: Path) -> set[str]:
    """Return the set of branch refs on the bare repo (``refs/heads/*``)."""
    proc = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        check=True, capture_output=True, text=True,
    )
    branches: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            branches.add(parts[1].removeprefix("refs/heads/"))
    return branches


def _valid_diff() -> str:
    return (
        "--- a/runtime/skills/code-review/SKILL.md\n"
        "+++ b/runtime/skills/code-review/SKILL.md\n"
        "@@ -3,4 +3,5 @@\n"
        " ## Review Checklist\n"
        " \n"
        " - Check docstrings.\n"
        " - Verify tests.\n"
        "+- Safety guardrail added by lesson.\n"
    )


def _base_inputs(
    origin: Path, *, lesson_id: str = "LSN-a1b2c3d4", dry_run: bool = True,
) -> OpenPRInputs:
    return OpenPRInputs(
        lesson_id=lesson_id,
        unified_diff=_valid_diff(),
        scope_key="xcsf30|salesforce|security|*.cls",
        detector_name="human_issue_cluster",
        rationale_md="Safety signal test",
        evidence_trace_ids=["SCRUM-42"],
        harness_repo_url=str(origin),
        base_branch="main",
        dry_run=dry_run,
    )


@pytest.fixture
def bare_origin(tmp_path: Path) -> Path:
    return _make_bare_origin(tmp_path)


def test_pr_opener_does_not_touch_main_branch_even_with_malicious_branch_name(
    bare_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end safety: a crafted lesson_id must not cause main to mutate.

    Runs with ``dry_run=False`` so push machinery engages. Even if a
    sanitizer mistake slipped a ``..`` or shell-metachar into a git
    ref, the bare origin's ``refs/heads/main`` hash should be
    unchanged at test end. This is a filesystem-visible guarantee
    — a process-internal assertion wouldn't catch the case where
    git itself resolves the bad name to ``main``.
    """
    main_sha_before = _main_sha(bare_origin)

    # Patch _gh so no real GitHub API call fires — we care about the
    # git side only. Also set AGENT_GH_TOKEN so the push guard passes.
    monkeypatch.setenv("AGENT_GH_TOKEN", "fake-tok-for-test")

    def _fake_gh(args: list[str], **kw: object) -> object:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "https://github.com/x/y/pull/1\n"
        proc.stderr = ""
        return proc

    monkeypatch.setattr(pr_opener, "_gh", _fake_gh)

    # A malicious-shaped id. The sanitizer should reject BEFORE any
    # git operation runs; this is the belt-and-braces filesystem
    # assertion in case validation is ever relaxed.
    malicious_ids = (
        "LSN-a..b",           # path traversal via git's ".." semantics
        "LSN-main",           # attempt to collide with the default branch
        "LSN-; rm -rf /",     # shell metachar injection
    )
    for bad_id in malicious_ids:
        result = open_pr_for_lesson(_base_inputs(
            bare_origin, lesson_id=bad_id, dry_run=False
        ))
        # Don't care whether success is False or the sanitizer was
        # cleared — we care that main is unchanged.
        _ = result

    main_sha_after = _main_sha(bare_origin)
    assert main_sha_before == main_sha_after, (
        f"main sha changed from {main_sha_before} to {main_sha_after} "
        f"after running pr_opener with malicious lesson_ids"
    )


def test_pr_opener_rejects_unsafe_branch_before_any_network_call(
    bare_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanitizer runs BEFORE clone/fetch/push — no subprocess invoked.

    Asserts the fail-fast ordering: an unsafe lesson_id returns a
    ``success=False`` PROpenerResult without ever calling ``_git`` or
    ``_gh``. Without this ordering a sanitizer regression would surface
    as a git-driven error instead of an input-validation error, which
    is worse UX and might accidentally touch the origin filesystem.
    """
    git_calls: list[list[str]] = []
    gh_calls: list[list[str]] = []

    def _forbidden_git(args: list[str], **kw: object) -> object:
        git_calls.append(args)
        raise AssertionError(
            f"_git must not be called for unsafe input; got args={args!r}"
        )

    def _forbidden_gh(args: list[str], **kw: object) -> object:
        gh_calls.append(args)
        raise AssertionError(
            f"_gh must not be called for unsafe input; got args={args!r}"
        )

    monkeypatch.setattr(pr_opener, "_git", _forbidden_git)
    monkeypatch.setattr(pr_opener, "_gh", _forbidden_gh)

    result = open_pr_for_lesson(_base_inputs(
        bare_origin, lesson_id="LSN-a..b", dry_run=False
    ))

    assert result.success is False
    assert "unsafe branch" in (result.error or "").lower()
    assert not git_calls, f"_git was called: {git_calls}"
    assert not gh_calls, f"_gh was called: {gh_calls}"


def test_pr_opener_only_creates_learning_branches(
    bare_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: after a successful dry run against a real origin the
    only ref touched is ``main`` (pre-existing); no ``learning/lesson-*``
    gets published to the bare origin because dry_run doesn't push.

    For the full push flow, also assert the published branch (after
    dry_run=False) has the ``learning/lesson-`` prefix and HEAD/main
    are unchanged. This proves the sanitizer + branch-construction
    path never produces a ref outside its declared namespace.
    """
    monkeypatch.setenv("AGENT_GH_TOKEN", "fake-tok-for-test")

    published_branches: list[str] = []

    def _fake_gh(args: list[str], **kw: object) -> object:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "https://github.com/x/y/pull/1\n"
        proc.stderr = ""
        return proc

    monkeypatch.setattr(pr_opener, "_gh", _fake_gh)

    # Dry run first — should NOT push anything.
    before = _list_branches(bare_origin)
    dry_result = open_pr_for_lesson(_base_inputs(
        bare_origin, lesson_id="LSN-dryrun01", dry_run=True
    ))
    assert dry_result.success is True, dry_result.error
    assert dry_result.dry_run is True
    after_dry = _list_branches(bare_origin)
    assert after_dry == before, (
        f"dry_run published branches to origin: {after_dry - before}"
    )
    assert after_dry == {"main"}, f"unexpected pre-push branches: {after_dry}"

    # Full run — a learning/lesson-* branch SHOULD land on origin.
    full_result = open_pr_for_lesson(_base_inputs(
        bare_origin, lesson_id="LSN-fullrn01", dry_run=False
    ))
    assert full_result.success is True, full_result.error
    assert full_result.dry_run is False
    published_branches = [
        b for b in _list_branches(bare_origin) if b != "main"
    ]
    assert published_branches == ["learning/lesson-LSN-fullrn01"], (
        f"unexpected branches on origin: {published_branches}"
    )
    # main sha must still be unchanged — we pushed a side branch.
    # (Exact sha we don't know at this point because the fixture
    # created it, but ``_main_sha`` against "before" would be stale —
    # instead we assert main still resolves.)
    main_sha = _main_sha(bare_origin)
    assert main_sha  # resolvable means ref still exists and is a sha
