"""PR opener — open a harness-repo PR for an approved lesson.

Clones the harness repo, applies the drafter's unified diff, stamps
edited Markdown with a ``lesson_id`` frontmatter field (so
``git log -S "lesson_id: LSN-..."`` finds the commit), commits,
pushes, and calls ``gh pr create --draft``.

Dry-run mode stops after the local commit — useful for exercising
the flow without creating real PRs.

On failure the scratch dir is retained for forensics and the error
lands on ``lesson_candidates.status_reason`` so operators see it in
the dashboard.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog

from redaction import redact_token_urls

logger = structlog.get_logger()


# Mirrors _is_safe_branch in services/l3_pr_review/spawner.py.
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9_./+-]+$")
_BRANCH_PREFIX = "learning/lesson-"

# Commit + PR defaults.
_DEFAULT_AUTHOR_NAME = "XCentium Agent"
_DEFAULT_AUTHOR_EMAIL = "xcagent.rockwell@xcentium.com"


@dataclass(frozen=True)
class PROpenerResult:
    """Outcome of a PR-opener run."""

    success: bool
    pr_url: str = ""
    branch: str = ""
    commit_sha: str = ""
    dry_run: bool = False
    error: str = ""


def _build_branch_name(lesson_id: str) -> str:
    """Return the branch name for a lesson, validating shape.

    ``..`` is rejected explicitly (not caught by the regex character
    class) because git ref resolution treats it specially — a branch
    named ``learning/lesson-a..b`` could reach sibling refs.
    """
    name = f"{_BRANCH_PREFIX}{lesson_id}"
    if ".." in name or not _SAFE_BRANCH_RE.fullmatch(name):
        raise ValueError(f"unsafe branch name derived from lesson_id: {name!r}")
    return name


def _resolve_auth_token() -> str:
    """Return the GitHub PAT for `xcagentrockwell`.

    Precedence: ``AGENT_GH_TOKEN`` > ``GITHUB_TOKEN``. Matches the
    fallback order in ``services/l3_pr_review/github_api.py``.
    Returns empty string when neither is set — the caller treats an
    empty token as a misconfigured deployment (the push will fail
    loudly rather than silently pushing with whoever's ambient
    credentials happen to be on the host).
    """
    return os.getenv("AGENT_GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""


def _run_bin(
    binary: str,
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


# Thin delegates so tests can still monkeypatch ``_gh`` specifically.
def _git(args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return _run_bin("git", args, **kw)  # type: ignore[arg-type]


def _gh(args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return _run_bin("gh", args, **kw)  # type: ignore[arg-type]


def _build_env(token: str) -> dict[str, str]:
    """Subprocess env with the agent PAT wired in as ``GH_TOKEN``.

    Allowlist not denylist — keeps ANTHROPIC_API_KEY and other L1
    secrets out of git/gh. ``GIT_TERMINAL_PROMPT=0`` +
    ``GIT_ASKPASS=/bin/true`` make auth failures fail instantly
    instead of hanging on an interactive prompt under
    ``capture_output=True``.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "HOME", "LANG", "LC_ALL", "USER"}
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/true"
    if token:
        env["GH_TOKEN"] = token
    return env


def _set_identity(worktree: Path) -> None:
    """Configure commit author for this clone (worktree-local, never global)."""
    name = os.environ.get("AGENT_GIT_NAME", _DEFAULT_AUTHOR_NAME)
    email = os.environ.get("AGENT_GIT_EMAIL", _DEFAULT_AUTHOR_EMAIL)
    _git(["config", "user.name", name], cwd=worktree)
    _git(["config", "user.email", email], cwd=worktree)


def _stamp_lesson_id(file_path: Path, lesson_id: str) -> bool:
    """Add ``lesson_id: <LSN-...>`` to the file's YAML frontmatter.

    Returns True when the stamp was added or updated. Non-markdown
    files are skipped (returns False). Files without frontmatter get
    a new block prepended. Files with an existing ``lesson_id`` line
    are idempotent — we replace the value in place so reverts don't
    accumulate stamps.
    """
    if file_path.suffix != ".md" or not file_path.exists():
        return False
    content = file_path.read_text()
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end == -1:
            return False
        head = content[4:end]
        body = content[end + len("\n---\n") :]
        lines = head.splitlines()
        new_head_lines: list[str] = []
        replaced = False
        for line in lines:
            if line.startswith("lesson_id:"):
                new_head_lines.append(f"lesson_id: {lesson_id}")
                replaced = True
            else:
                new_head_lines.append(line)
        if not replaced:
            new_head_lines.append(f"lesson_id: {lesson_id}")
        file_path.write_text(
            "---\n" + "\n".join(new_head_lines) + "\n---\n" + body
        )
    else:
        file_path.write_text(
            f"---\nlesson_id: {lesson_id}\n---\n\n{content}"
        )
    return True


def _edited_paths_from_diff(diff: str) -> list[str]:
    """Distinct ``+++ b/<path>`` entries from a unified diff.

    Used to know which files to stamp + include in the commit message
    scope. /dev/null (file-delete) entries are skipped.
    """
    paths: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+++ "):
            continue
        rest = line[4:].strip()
        if rest == "/dev/null":
            continue
        if rest.startswith("b/"):
            rest = rest[2:]
        if rest and rest not in paths:
            paths.append(rest)
    return paths


def _compose_pr_body(
    lesson_id: str,
    scope_key: str,
    detector_name: str,
    rationale_md: str,
    evidence_trace_ids: list[str],
) -> str:
    """Build the PR body — evidence + scope + lesson id + HTML marker."""
    evidence_list = "\n".join(
        f"- `{trace_id}`" for trace_id in evidence_trace_ids[:20]
    ) or "(no evidence captured)"
    rationale = rationale_md.strip() or "(no rationale supplied)"
    return (
        "## Summary\n\n"
        f"Self-learning lesson `{lesson_id}` — {detector_name} on "
        f"`{scope_key}`.\n\n"
        "## Rationale\n\n"
        f"{rationale}\n\n"
        "## Evidence\n\n"
        f"{evidence_list}\n\n"
        "## Revert\n\n"
        f"If this edit regresses behavior, `git log -S \"lesson_id: "
        f"{lesson_id}\"` will find the commit.\n\n"
        "---\n"
        f"Opened by the self-learning PR opener for `{lesson_id}`.\n"
        "<!-- xcagent -->\n"
    )


def _write_patch_file(scratch: Path, diff: str) -> Path:
    patch_path = scratch / "lesson.patch"
    if not diff.endswith("\n"):
        diff = diff + "\n"
    patch_path.write_text(diff)
    return patch_path


def _run(
    cmd: subprocess.CompletedProcess[str],
    *,
    label: str,
) -> str | None:
    """Return an error string if the subprocess failed, else None."""
    if cmd.returncode != 0:
        raw = (cmd.stderr or cmd.stdout or "")[-400:].strip()
        return f"{label} failed (exit {cmd.returncode}): {redact_token_urls(raw)}"
    return None


@dataclass
class OpenPRInputs:
    """Inputs for ``open_pr_for_lesson``.

    Kept as a dataclass so the handler can build one from the stored
    candidate row without a parameter explosion at the call site.
    """

    lesson_id: str
    unified_diff: str
    scope_key: str
    detector_name: str
    rationale_md: str
    evidence_trace_ids: list[str]
    harness_repo_url: str
    base_branch: str = "main"
    dry_run: bool = False


def open_pr_for_lesson(inputs: OpenPRInputs) -> PROpenerResult:
    """Clone + apply + commit + (push+PR) for an approved lesson.

    Returns a ``PROpenerResult``. Cleans up the scratch dir on
    success; leaves it in place on failure so an operator can
    investigate without re-running the LLM pipeline.
    """
    try:
        branch = _build_branch_name(inputs.lesson_id)
    except ValueError as exc:
        return PROpenerResult(success=False, error=str(exc))

    token = _resolve_auth_token()
    env = _build_env(token)

    scratch_parent = Path(tempfile.mkdtemp(prefix="learning-pr-"))
    clone_dir = scratch_parent / "harness"
    success = False
    try:
        clone_err = _run(
            _git(
                ["clone", "--depth", "10", inputs.harness_repo_url, str(clone_dir)],
                cwd=scratch_parent,
                env=env,
                timeout=120,
            ),
            label="git clone",
        )
        if clone_err is not None:
            return PROpenerResult(success=False, error=clone_err)

        _set_identity(clone_dir)

        checkout_err = _run(
            _git(["checkout", "-b", branch], cwd=clone_dir, env=env),
            label="git checkout -b",
        )
        if checkout_err is not None:
            return PROpenerResult(success=False, error=checkout_err)

        try:
            patch_path = _write_patch_file(scratch_parent, inputs.unified_diff)
        except OSError as exc:
            return PROpenerResult(
                success=False, error=f"could not write patch file: {exc}"
            )
        apply_err = _run(
            _git(
                ["apply", str(patch_path)],
                cwd=clone_dir,
                env=env,
            ),
            label="git apply",
        )
        if apply_err is not None:
            return PROpenerResult(success=False, error=apply_err)

        edited = _edited_paths_from_diff(inputs.unified_diff)
        for rel in edited:
            _stamp_lesson_id(clone_dir / rel, inputs.lesson_id)

        add_err = _run(
            _git(["add", "--", *edited], cwd=clone_dir, env=env),
            label="git add",
        )
        if add_err is not None:
            return PROpenerResult(success=False, error=add_err)

        scope_for_title = inputs.scope_key or inputs.detector_name
        commit_title = (
            f"chore(learning): {inputs.lesson_id} - {scope_for_title}"
        )
        commit_body = (
            f"{inputs.rationale_md.strip()}\n\n"
            f"lesson_id: {inputs.lesson_id}\n"
        )
        commit_err = _run(
            _git(
                [
                    "commit",
                    "-m",
                    commit_title,
                    "-m",
                    commit_body,
                ],
                cwd=clone_dir,
                env=env,
            ),
            label="git commit",
        )
        if commit_err is not None:
            return PROpenerResult(success=False, error=commit_err)

        sha_proc = _git(["rev-parse", "HEAD"], cwd=clone_dir, env=env)
        sha_err = _run(sha_proc, label="git rev-parse")
        if sha_err is not None:
            return PROpenerResult(success=False, error=sha_err)
        commit_sha = sha_proc.stdout.strip()

        if inputs.dry_run:
            logger.info(
                "learning_pr_opener_dry_run",
                lesson_id=inputs.lesson_id,
                branch=branch,
                commit_sha=commit_sha,
            )
            success = True
            return PROpenerResult(
                success=True,
                branch=branch,
                commit_sha=commit_sha,
                dry_run=True,
            )

        if not token:
            return PROpenerResult(
                success=False,
                error=(
                    "no AGENT_GH_TOKEN / GITHUB_TOKEN set — refusing "
                    "to push without explicit agent credentials"
                ),
            )

        push_err = _run(
            _git(
                ["push", "--set-upstream", "origin", branch],
                cwd=clone_dir,
                env=env,
                timeout=120,
            ),
            label="git push",
        )
        if push_err is not None:
            return PROpenerResult(success=False, error=push_err)

        pr_body = _compose_pr_body(
            lesson_id=inputs.lesson_id,
            scope_key=inputs.scope_key,
            detector_name=inputs.detector_name,
            rationale_md=inputs.rationale_md,
            evidence_trace_ids=inputs.evidence_trace_ids,
        )
        pr_proc = _gh(
            [
                "pr",
                "create",
                "--draft",
                "--base",
                inputs.base_branch,
                "--head",
                branch,
                "--title",
                commit_title,
                "--body",
                pr_body,
            ],
            cwd=clone_dir,
            env=env,
            timeout=120,
        )
        pr_err = _run(pr_proc, label="gh pr create")
        if pr_err is not None:
            return PROpenerResult(
                success=False,
                branch=branch,
                commit_sha=commit_sha,
                error=pr_err,
            )
        pr_url = _parse_pr_url(pr_proc.stdout)
        if not pr_url:
            return PROpenerResult(
                success=False,
                branch=branch,
                commit_sha=commit_sha,
                error=(
                    "gh pr create succeeded but no URL detected in "
                    f"output: {pr_proc.stdout[-200:]!r}"
                ),
            )
        logger.info(
            "learning_pr_opener_opened",
            lesson_id=inputs.lesson_id,
            branch=branch,
            pr_url=pr_url,
        )
        success = True
        return PROpenerResult(
            success=True,
            pr_url=pr_url,
            branch=branch,
            commit_sha=commit_sha,
        )
    except subprocess.TimeoutExpired as exc:
        return PROpenerResult(
            success=False, error=f"subprocess timeout: {exc}"
        )
    finally:
        if success:
            shutil.rmtree(scratch_parent, ignore_errors=True)
        else:
            logger.warning(
                "learning_pr_opener_scratch_retained",
                path=str(scratch_parent),
            )


def _parse_pr_url(gh_output: str) -> str:
    """Pick the ``https://github.com/.../pull/N`` URL out of ``gh`` output.

    ``gh pr create`` prints the URL as its last non-empty line on the
    happy path, but some versions also print a ``Creating pull
    request...`` preamble.
    """
    for line in reversed(gh_output.splitlines()):
        line = line.strip()
        if line.startswith("https://") and "/pull/" in line:
            return line
    return ""


