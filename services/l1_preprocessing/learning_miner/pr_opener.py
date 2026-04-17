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

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog

from redaction import redact_token_urls

from ._subprocess import build_env, resolve_auth_token, run_bin, safe_stderr_tail
from .drafter_markdown import validate_diff_internal_paths

logger = structlog.get_logger()


# Mirrors _is_safe_branch in services/l3_pr_review/spawner.py.
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9_./+-]+$")
_BRANCH_PREFIX = "learning/lesson-"
_REVERT_BRANCH_PREFIX = "learning/revert-"

# Merge commit sha shape. ``git revert`` rejects non-shas loudly
# anyway, but guarding input at the boundary prevents the shell
# call from ever seeing a crafted string.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

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


def _build_prefixed_branch_name(prefix: str, lesson_id: str) -> str:
    """Shared validator for ``_build_branch_name`` / ``_build_revert_branch_name``.

    Rejects ``..`` explicitly (git ref resolution treats it specially
    — a branch named ``learning/lesson-a..b`` could reach sibling
    refs) and disallowed chars via _SAFE_BRANCH_RE. Centralizes the
    validation so a future rule (e.g. rejecting leading ``-``, or
    consecutive ``//``) only needs to be added in one place instead
    of two near-identical copies.
    """
    name = f"{prefix}{lesson_id}"
    if ".." in name or not _SAFE_BRANCH_RE.fullmatch(name):
        raise ValueError(f"unsafe branch name derived from lesson_id: {name!r}")
    return name


def _build_branch_name(lesson_id: str) -> str:
    return _build_prefixed_branch_name(_BRANCH_PREFIX, lesson_id)


def _build_revert_branch_name(lesson_id: str) -> str:
    return _build_prefixed_branch_name(_REVERT_BRANCH_PREFIX, lesson_id)


def _reviewer_flags(reviewers: tuple[str, ...]) -> list[str]:
    """Expand the reviewers tuple into ``--reviewer h1 --reviewer h2 ...``.

    Falsy entries are dropped defensively — a comma-split with a
    trailing comma leaves an empty string that would make ``gh``
    complain. Duplicates are dropped too (order-preserving): some
    ``gh`` versions reject duplicate ``--reviewer`` flags with
    "already requested review from @X", failing the whole PR create.
    """
    out: list[str] = []
    seen: set[str] = set()
    for handle in reviewers:
        handle = (handle or "").strip()
        if handle and handle not in seen:
            seen.add(handle)
            out.extend(["--reviewer", handle])
    return out


# Thin delegates so tests can still monkeypatch ``_gh`` specifically.
def _git(args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return run_bin("git", args, **kw)  # type: ignore[arg-type]


def _gh(args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return run_bin("gh", args, **kw)  # type: ignore[arg-type]


def _set_identity(
    worktree: Path, env: dict[str, str] | None = None
) -> str | None:
    """Configure commit author for this clone (worktree-local, never global).

    ``env`` mirrors what the rest of the flow passes so ``git config``
    runs under the same filtered environment as clone/apply/push —
    without it, ``os.environ`` (including ANTHROPIC_API_KEY and other
    L1 secrets) leaks into the subprocess. Also keeps
    GIT_TERMINAL_PROMPT=0 / GIT_ASKPASS=/bin/true active if a future
    change makes ``git config`` touch auth machinery.

    Returns an error string if either ``git config`` call failed,
    else None. Previously the return values were ignored and a
    failed identity write would let ``git commit`` fall back to
    git's default identity (or fail cryptically if unconfigured) —
    either way the resulting commit author wouldn't be the agent's,
    and downstream human-reedit detection (which keys on
    ``AGENT_GIT_EMAIL``) would misclassify agent commits as human.
    """
    name = os.environ.get("AGENT_GIT_NAME", _DEFAULT_AUTHOR_NAME)
    email = os.environ.get("AGENT_GIT_EMAIL", _DEFAULT_AUTHOR_EMAIL)
    err = _run(
        _git(["config", "user.name", name], cwd=worktree, env=env),
        label="git config user.name",
    )
    if err is not None:
        return err
    return _run(
        _git(["config", "user.email", email], cwd=worktree, env=env),
        label="git config user.email",
    )


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
                # Collapse duplicates: only the FIRST lesson_id line is
                # replaced; subsequent duplicates are dropped. Without
                # this, a file whose frontmatter already had two
                # ``lesson_id:`` lines (from a prior stamping bug)
                # would end up with two ``lesson_id: LSN-NEW`` lines.
                if not replaced:
                    new_head_lines.append(f"lesson_id: {lesson_id}")
                    replaced = True
                # else: skip — the duplicate is absorbed.
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
    """Distinct paths referenced by a unified diff (adds + modifies +
    deletes + both sides of a rename).

    Used to know which files to stamp + include in the ``git add``
    list for the commit. For modifies and additions the ``+++ b/<path>``
    header names the post-image; for deletions the post-image is
    ``/dev/null`` and the pre-image ``--- a/<path>`` carries the real
    path. For renames, both the ``rename from`` (old) and ``rename
    to`` (new) paths are surfaced so ``git add`` stages the deletion
    of the old path alongside the new file — otherwise the rename
    commit only carries the addition and git-status shows the old
    path as unstaged-deleted.
    """
    paths: list[str] = []

    def _add(path: str) -> None:
        if path and path not in paths:
            paths.append(path)

    lines = diff.splitlines()
    # Pair +++/--- lines so we can tell a deletion from a rename.
    prev_minus: str | None = None
    for line in lines:
        if line.startswith("rename from "):
            _add(line[len("rename from ") :].strip())
            continue
        if line.startswith("rename to "):
            _add(line[len("rename to ") :].strip())
            continue
        if line.startswith("--- "):
            rest = line[4:].strip()
            prev_minus = rest[2:] if rest.startswith("a/") else rest
            continue
        if line.startswith("+++ "):
            rest = line[4:].strip()
            plus_path = rest[2:] if rest.startswith("b/") else rest
            if plus_path == "/dev/null":
                # Deletion: fall back to the pre-image path.
                if prev_minus and prev_minus != "/dev/null":
                    _add(prev_minus)
            else:
                _add(plus_path)
            prev_minus = None
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
    """Write the drafter's unified diff to ``lesson.patch``.

    Pins encoding + newline so the patch content hits disk byte-for-byte
    as the drafter emitted it. Default ``write_text`` does universal
    newline translation (on Windows, ``\\n`` becomes ``\\r\\n``) and
    uses the platform's preferred encoding — both can make ``git
    apply`` reject an otherwise-valid diff with "whitespace errors"
    or encoding confusion. Pinning keeps the fix behavior consistent
    regardless of where L1 is hosted.
    """
    patch_path = scratch / "lesson.patch"
    if not diff.endswith("\n"):
        diff = diff + "\n"
    patch_path.write_text(diff, encoding="utf-8", newline="")
    return patch_path


def _run(
    cmd: subprocess.CompletedProcess[str],
    *,
    label: str,
) -> str | None:
    """Return an error string if the subprocess failed, else None.

    Redacts BEFORE truncating — if the 400-char tail clips through a
    ``https://user:token@...`` URL, the regex wouldn't match on the
    truncated slice and the partial token would leak. Redacting on
    the full output first means a half-URL at the boundary has
    already been scrubbed when we clip.
    """
    if cmd.returncode != 0:
        raw = cmd.stderr or cmd.stdout or ""
        redacted = redact_token_urls(raw)
        tail = redacted[-400:].strip()
        return f"{label} failed (exit {cmd.returncode}): {tail}"
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
    reviewers: tuple[str, ...] = ()


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

    # Re-validate diff paths before any clone/apply. Defense in depth:
    # the drafter validates paths at draft-time, but an operator who
    # edits proposed_delta_json directly in the DB could slip a diff
    # with services/ or .github/ targets past the approve endpoint.
    # Re-checking here means the pr_opener can't be tricked into
    # applying a disallowed patch even if the DB was tampered with.
    path_err = validate_diff_internal_paths(inputs.unified_diff)
    if path_err is not None:
        return PROpenerResult(success=False, error=path_err)

    token = resolve_auth_token()
    env = build_env(token)

    scratch_parent = Path(tempfile.mkdtemp(prefix="learning-pr-"))
    clone_dir = scratch_parent / "harness"
    success = False
    try:
        # `--branch` pins the clone's default to base_branch so the
        # subsequent ``checkout -B <branch> origin/<base_branch>``
        # finds the ref even when base_branch isn't the remote default.
        # Without --branch, a non-default base_branch produces an
        # origin/<base_branch> ref that doesn't exist in the shallow
        # clone and checkout fails.
        clone_err = _run(
            _git(
                [
                    "clone", "--depth", "10",
                    "--branch", inputs.base_branch,
                    inputs.harness_repo_url, str(clone_dir),
                ],
                cwd=scratch_parent,
                env=env,
                timeout=120,
            ),
            label="git clone",
        )
        if clone_err is not None:
            return PROpenerResult(success=False, error=clone_err)

        identity_err = _set_identity(clone_dir, env=env)
        if identity_err is not None:
            return PROpenerResult(success=False, error=identity_err)

        # Branch off `origin/<base_branch>` explicitly. Without this,
        # ``git checkout -b <branch>`` forks from the remote default
        # HEAD — so a non-default ``base_branch`` (e.g. ``develop``)
        # produces a PR whose diff includes all commits from default
        # that aren't on base_branch. The `-B` form creates or resets.
        checkout_err = _run(
            _git(
                ["checkout", "-B", branch, f"origin/{inputs.base_branch}"],
                cwd=clone_dir, env=env,
            ),
            label="git checkout -B",
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
                *_reviewer_flags(inputs.reviewers),
            ],
            cwd=clone_dir,
            env=env,
            timeout=120,
        )
        pr_err = _run(pr_proc, label="gh pr create")
        if pr_err is not None:
            # gh pr create exit-nonzero could mean the PR already
            # exists (eg. branch was reused). Try to recover the URL
            # before deleting the branch; if recovery finds a PR, the
            # push+create succeeded semantically and we return it.
            recovered = _recover_pr_url_by_branch(clone_dir, branch, env)
            if recovered:
                logger.info(
                    "learning_pr_opener_recovered_after_create_err",
                    lesson_id=inputs.lesson_id,
                    branch=branch,
                    pr_url=recovered,
                )
                success = True
                return PROpenerResult(
                    success=True,
                    pr_url=recovered,
                    branch=branch,
                    commit_sha=commit_sha,
                )
            # No PR found — delete the remote branch so /approve retry
            # doesn't trip on "branch already exists" at git push.
            _best_effort_delete_remote_branch(clone_dir, branch, env)
            return PROpenerResult(
                success=False,
                branch=branch,
                commit_sha=commit_sha,
                error=pr_err,
            )
        pr_url = _parse_pr_url(pr_proc.stdout)
        if not pr_url:
            # ``gh`` version drift / interactive-prompt stderr-into-stdout
            # can swallow the URL even on exit=0. Ask ``gh`` for it by
            # branch head before giving up — recovers the URL AND
            # prevents a duplicate PR on retry.
            pr_url = _recover_pr_url_by_branch(clone_dir, branch, env)
        if not pr_url:
            # Still no URL — the PR may or may not exist. Delete the
            # remote branch so /approve retry doesn't trip on "branch
            # exists"; the operator inspects GitHub if a PR slipped
            # through.
            _best_effort_delete_remote_branch(clone_dir, branch, env)
            # Redact stdout before slicing — gh output is normally token-
            # free, but a misconfigured HTTPS remote URL could echo
            # ``https://user:tok@...`` in error context. Defensive.
            safe_tail = redact_token_urls(pr_proc.stdout)[-200:]
            return PROpenerResult(
                success=False,
                branch=branch,
                commit_sha=commit_sha,
                error=(
                    "gh pr create succeeded but no URL detected in "
                    f"output: {safe_tail!r}"
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


# URL characters. The `[^\s]+` form is too greedy — a comma-joined
# output like ``a/pull/1,b/pull/2`` matched as ONE "URL" spanning both.
# Restrict to RFC-3986-ish path chars (drop ``,``, ``;``, brackets,
# whitespace) so back-to-back URLs get matched separately.
_PR_URL_RE = re.compile(r"https://[A-Za-z0-9._~\-/%?=&#]+/pull/\d+")


def _parse_pr_url(gh_output: str) -> str:
    """Pick the ``https://github.com/.../pull/N`` URL out of ``gh`` output.

    ``gh pr create`` prints the URL as its last non-empty line on the
    happy path, but some versions also print a ``Creating pull
    request...`` preamble or embed the URL in a message like
    ``Opened: https://...``. Regex-match so an embedded URL is still
    picked up rather than only bare-URL lines.
    """
    matches = _PR_URL_RE.findall(gh_output)
    return matches[-1] if matches else ""


def _recover_pr_url_by_branch(
    clone_dir: Path, branch: str, env: dict[str, str]
) -> str:
    """Ask ``gh`` for the URL of the PR whose head matches ``branch``.

    Runs on the URL-parse fallback path — ``gh pr create`` exit=0 but
    no URL in stdout can happen with ``gh`` version drift or an
    interactive prompt leaking stderr into stdout. One targeted
    ``gh pr list --head --json url`` query recovers the URL without
    creating a duplicate PR on retry.

    ``--state all`` covers open + closed + merged. Without it, a prior
    attempt that landed a PR and then immediately got the PR merged
    (or closed) — or an "already exists" error against a pre-existing
    closed PR — would leave us unable to recover the URL, triggering
    the branch-delete fallback unnecessarily.
    """
    proc = _gh(
        ["pr", "list", "--head", branch, "--state", "all",
         "--json", "url", "--limit", "1"],
        cwd=clone_dir,
        env=env,
        timeout=30,
    )
    if proc.returncode != 0:
        return ""
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, list) or not payload:
        return ""
    entry = payload[0]
    if not isinstance(entry, dict):
        return ""
    url = str(entry.get("url") or "")
    return url if url.startswith("https://") else ""


def _best_effort_delete_remote_branch(
    clone_dir: Path, branch: str, env: dict[str, str]
) -> None:
    """``git push origin --delete <branch>`` — swallow errors.

    Called on the push-succeeded-but-PR-create-failed path to keep
    retries idempotent (otherwise /approve retry fails at
    ``git push --set-upstream`` because the branch already exists).
    Failures are logged and ignored — we only ran push if auth was
    valid a moment ago; anything worse than this function doing
    nothing is the operator's cleanup problem.
    """
    proc = _git(
        ["push", "origin", "--delete", branch],
        cwd=clone_dir,
        env=env,
        timeout=60,
    )
    if proc.returncode != 0:
        logger.warning(
            "learning_pr_opener_remote_branch_cleanup_failed",
            branch=branch,
            stderr=safe_stderr_tail(proc.stderr),
        )


# ---------------------------------------------------------------------------
# Revert flow — regressed / human_reedit lessons
# ---------------------------------------------------------------------------


@dataclass
class RevertPRInputs:
    """Inputs for ``open_revert_pr_for_lesson``.

    ``merged_commit_sha`` is the commit this lesson introduced; we
    ``git revert`` it and open a PR against ``base_branch``.
    """

    lesson_id: str
    merged_commit_sha: str
    verdict: str  # ``regressed`` or ``human_reedit`` — shown in PR body.
    reason_md: str
    harness_repo_url: str
    base_branch: str = "main"
    dry_run: bool = False
    reviewers: tuple[str, ...] = ()


def open_revert_pr_for_lesson(inputs: RevertPRInputs) -> PROpenerResult:
    """Clone + git revert <sha> + (push+PR) for a regressed lesson.

    Mirrors ``open_pr_for_lesson``: same scratch lifecycle, same
    auth env, same dry-run semantics. On success returns the revert
    PR URL in ``pr_url``.
    """
    try:
        branch = _build_revert_branch_name(inputs.lesson_id)
    except ValueError as exc:
        return PROpenerResult(success=False, error=str(exc))

    if not _SHA_RE.fullmatch(inputs.merged_commit_sha or ""):
        return PROpenerResult(
            success=False,
            error=(
                "merged_commit_sha missing or malformed — "
                f"got {inputs.merged_commit_sha!r}"
            ),
        )

    token = resolve_auth_token()
    env = build_env(token)

    scratch_parent = Path(tempfile.mkdtemp(prefix="learning-revert-"))
    clone_dir = scratch_parent / "harness"
    success = False
    try:
        clone_err = _run(
            _git(
                [
                    "clone", "--no-single-branch",
                    inputs.harness_repo_url, str(clone_dir),
                ],
                cwd=scratch_parent,
                env=env,
                timeout=120,
            ),
            label="git clone",
        )
        if clone_err is not None:
            return PROpenerResult(success=False, error=clone_err)

        identity_err = _set_identity(clone_dir, env=env)
        if identity_err is not None:
            return PROpenerResult(success=False, error=identity_err)

        # Same rationale as the approve flow: fork off
        # origin/<base_branch> explicitly so the revert PR's diff
        # stays scoped to the revert commit, not a mix with unrelated
        # default-branch commits when base_branch differs from the
        # remote default. ``--no-single-branch`` above gave us all
        # remote refs so origin/<base_branch> is available.
        checkout_err = _run(
            _git(
                ["checkout", "-B", branch, f"origin/{inputs.base_branch}"],
                cwd=clone_dir, env=env,
            ),
            label="git checkout -B",
        )
        if checkout_err is not None:
            return PROpenerResult(success=False, error=checkout_err)

        revert_err = _run(
            _git(
                ["revert", "--no-edit", inputs.merged_commit_sha],
                cwd=clone_dir,
                env=env,
            ),
            label="git revert",
        )
        if revert_err is not None:
            return PROpenerResult(success=False, error=revert_err)

        sha_proc = _git(["rev-parse", "HEAD"], cwd=clone_dir, env=env)
        sha_err = _run(sha_proc, label="git rev-parse")
        if sha_err is not None:
            return PROpenerResult(success=False, error=sha_err)
        commit_sha = sha_proc.stdout.strip()

        if inputs.dry_run:
            logger.info(
                "learning_pr_opener_revert_dry_run",
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
                    "to push revert without explicit agent credentials"
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

        pr_title = (
            f"revert(learning): {inputs.lesson_id} "
            f"({inputs.verdict})"
        )
        pr_body = _compose_revert_pr_body(
            lesson_id=inputs.lesson_id,
            verdict=inputs.verdict,
            reason_md=inputs.reason_md,
            merged_commit_sha=inputs.merged_commit_sha,
        )
        pr_proc = _gh(
            [
                "pr", "create", "--draft",
                "--base", inputs.base_branch,
                "--head", branch,
                "--title", pr_title,
                "--body", pr_body,
                *_reviewer_flags(inputs.reviewers),
            ],
            cwd=clone_dir,
            env=env,
            timeout=120,
        )
        pr_err = _run(pr_proc, label="gh pr create")
        if pr_err is not None:
            # Mirror the approve flow: recover URL first in case the
            # PR already exists; otherwise delete the pushed branch so
            # retries don't trip on "branch exists".
            recovered = _recover_pr_url_by_branch(clone_dir, branch, env)
            if recovered:
                logger.info(
                    "learning_pr_opener_revert_recovered_after_create_err",
                    lesson_id=inputs.lesson_id,
                    branch=branch,
                    pr_url=recovered,
                )
                success = True
                return PROpenerResult(
                    success=True,
                    pr_url=recovered,
                    branch=branch,
                    commit_sha=commit_sha,
                )
            _best_effort_delete_remote_branch(clone_dir, branch, env)
            return PROpenerResult(
                success=False,
                branch=branch,
                commit_sha=commit_sha,
                error=pr_err,
            )
        pr_url = _parse_pr_url(pr_proc.stdout)
        if not pr_url:
            pr_url = _recover_pr_url_by_branch(clone_dir, branch, env)
        if not pr_url:
            _best_effort_delete_remote_branch(clone_dir, branch, env)
            safe_tail = redact_token_urls(pr_proc.stdout)[-200:]
            return PROpenerResult(
                success=False,
                branch=branch,
                commit_sha=commit_sha,
                error=(
                    "gh pr create succeeded but no URL detected in "
                    f"output: {safe_tail!r}"
                ),
            )
        logger.info(
            "learning_pr_opener_revert_opened",
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
                "learning_pr_opener_revert_scratch_retained",
                path=str(scratch_parent),
            )


def _compose_revert_pr_body(
    *,
    lesson_id: str,
    verdict: str,
    reason_md: str,
    merged_commit_sha: str,
) -> str:
    reason = reason_md.strip() or "(no rationale supplied)"
    return (
        "## Summary\n\n"
        f"Reverting self-learning lesson `{lesson_id}` — outcome "
        f"verdict was `{verdict}`.\n\n"
        "## Rationale\n\n"
        f"{reason}\n\n"
        "## Provenance\n\n"
        f"- Original commit: `{merged_commit_sha}`\n"
        f"- Lesson: `{lesson_id}`\n"
        f"- Outcome verdict: `{verdict}`\n\n"
        "---\n"
        "Opened by the self-learning revert flow.\n"
        "<!-- xcagent-revert -->\n"
    )


