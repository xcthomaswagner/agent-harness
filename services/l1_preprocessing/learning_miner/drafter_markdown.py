"""Markdown drafter — Claude-drafted unified-diff for a lesson's target file.

Called by ``POST /api/learning/candidates/{id}/draft`` only; the
nightly miner never hits Claude. Validates the drafted diff against
``git apply --check``, a bounded added-line count, an absolute-directive
filter, and a target-path allowlist before returning success. Any
failure leaves the candidate at ``status='proposed'`` so the operator
can retry.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import anthropic
import structlog

from learning_miner._anthropic_retry import (
    RetryFailure,
    call_with_retry,
)

logger = structlog.get_logger()

MODEL = "claude-opus-4-20250514"

# Lessons needing more than MAX_ADDED_LINES have outgrown a mechanical
# rule and should be a human edit, not a drafter output.
MAX_ADDED_LINES = 12

_BANNED_DIRECTIVE_RE = re.compile(
    r"^\s*[-*]?\s*(always|never|must)\b", re.IGNORECASE
)

_ALLOWED_TARGET_PREFIXES: tuple[str, ...] = (
    "runtime/skills/",
    "runtime/platform-profiles/",
    "runtime/agents/",
)


def check_target_path(target_path: str) -> str | None:
    """Return an error string if target_path is not a legal edit target.

    Reused by the /draft API handler BEFORE it reads the target file
    off disk — without this, an absolute path like ``/etc/passwd`` in
    proposed_delta slips the repo_root prefix via pathlib's `/` operator
    (which discards the LHS when RHS is absolute). Mirrors the precheck
    the drafter runs internally; the API needs the same check earlier.

    Also rejects control characters (null byte, CR, LF, tab) embedded
    in the path. A null byte in particular — ``runtime/skills/x\\0.md``
    — passes allowlist + extension checks, but ``pathlib.Path`` later
    raises ValueError when such a path reaches C-level file APIs; the
    API handler only catches OSError, so the unhandled ValueError
    surfaces as a 500 instead of a graceful 200 with drafter_success
    false.
    """
    if not target_path:
        return "proposed_delta.target_path missing"
    if any(c in target_path for c in ("\x00", "\n", "\r", "\t")):
        return f"target_path {target_path!r} contains a control character"
    if target_path.startswith("/") or ".." in target_path.split("/"):
        return (
            f"target_path {target_path!r} is absolute or contains .."
        )
    if not any(target_path.startswith(p) for p in _ALLOWED_TARGET_PREFIXES):
        return (
            f"target_path {target_path!r} is outside allowed "
            f"prefixes {_ALLOWED_TARGET_PREFIXES}"
        )
    if not target_path.endswith(".md"):
        return (
            "markdown drafter received non-markdown target "
            f"{target_path!r}"
        )
    return None


@dataclass(frozen=True)
class DrafterResult:
    success: bool
    unified_diff: str = ""
    error: str = ""
    tokens_in: int = 0
    tokens_out: int = 0


class MarkdownDrafter:
    """Drafts Markdown edits for self-learning lesson candidates."""

    def __init__(
        self,
        *,
        api_key: str,
        repo_root: Path,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._repo_root = repo_root
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key)

    async def draft(
        self,
        *,
        proposed_delta: dict[str, object],
        evidence_snippets: list[str],
        current_content: str | None = None,
    ) -> DrafterResult:
        """Draft a unified diff for the target file in ``proposed_delta``.

        ``current_content`` can be supplied by the caller to skip the
        drafter-internal file read — useful when the caller will reuse
        the same content for a subsequent consistency check.
        """
        target_path = str(proposed_delta.get("target_path") or "")
        check = self._precheck(target_path)
        if check is not None:
            return check

        target_abs = self._repo_root / target_path
        if current_content is None:
            try:
                current_content = target_abs.read_text()
            except OSError as exc:
                return DrafterResult(
                    success=False,
                    error=f"target file not readable: {exc}",
                )

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(
            proposed_delta=proposed_delta,
            current_content=current_content,
            evidence_snippets=evidence_snippets,
        )

        response = await self._call_with_retry(system_prompt, user_prompt)
        if isinstance(response, DrafterResult):
            return response
        raw_text, tokens_in, tokens_out = response

        diff = _extract_unified_diff(raw_text)
        if not diff:
            return DrafterResult(
                success=False,
                error="drafter returned no unified diff",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        validation_error = self._validate_diff(diff, target_path, target_abs)
        if validation_error is not None:
            return DrafterResult(
                success=False,
                error=validation_error,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        return DrafterResult(
            success=True,
            unified_diff=diff,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    # ---- precheck ---------------------------------------------------

    def _precheck(self, target_path: str) -> DrafterResult | None:
        """Fail fast before any LLM call if the target is out of scope."""
        err = check_target_path(target_path)
        return DrafterResult(success=False, error=err) if err else None

    # ---- prompts ----------------------------------------------------

    def _build_system_prompt(self) -> str:
        return (
            "You are a senior engineer editing Markdown skill documentation "
            "for an AI-coding harness. The harness observed a pattern across "
            "multiple traces and wants you to propose the smallest possible "
            "edit that prevents recurrence.\n\n"
            "Rules you must follow:\n"
            "1. Output ONLY a valid unified diff (no prose, no backticks).\n"
            f"2. Add at most {MAX_ADDED_LINES} lines total.\n"
            "3. Do NOT start any added line with 'always', 'never', or "
            "'must' (case-insensitive). Absolute directives cause agents "
            "to get stuck.\n"
            "4. Prefer concrete, file- or tool-scoped guidance over "
            "general principles.\n"
            "5. The diff must apply cleanly against the provided current "
            "file content.\n"
            "6. Preserve the file's existing heading structure; insert "
            "the new content under the anchor heading specified in the "
            "proposed delta.\n"
        )

    def _build_user_prompt(
        self,
        *,
        proposed_delta: dict[str, object],
        current_content: str,
        evidence_snippets: list[str],
    ) -> str:
        snippet_cap = 20
        total = len(evidence_snippets)
        # Evidence snippets come from trace output that's only a short
        # hop from untrusted input (commit messages, ticket descriptions,
        # LLM-generated content). Strip the sentinel closing tag from
        # each snippet so a snippet containing ``</evidence>`` cannot
        # terminate the wrapping tag early and inject instructions into
        # the drafter's prompt. The consistency checker (see
        # drafter_consistency_check.py) uses the same sentinel shape.
        sanitized = [s.replace("</evidence>", "") for s in evidence_snippets[:snippet_cap]]
        evidence_block = "\n".join(
            f"- {s}" for s in sanitized
        ) or "(no evidence snippets captured)"
        # Only say "truncated" when we actually dropped snippets —
        # previously the prompt always said "truncated" even when
        # all snippets fit, which was a minor prompt-accuracy smell
        # that LLMs occasionally commented on in their reasoning.
        count_suffix = (
            f"{total} total, showing first {snippet_cap}"
            if total > snippet_cap
            else f"{total} total"
        )
        return (
            "The harness emitted this mechanical starter proposal:\n\n"
            f"```json\n{json.dumps(proposed_delta, indent=2, sort_keys=True)}\n```\n\n"
            "Evidence snippets the detector collected "
            f"({count_suffix}):\n\n"
            "<evidence>\n"
            f"{evidence_block}\n"
            "</evidence>\n\n"
            "Current content of the target file "
            f"(`{proposed_delta.get('target_path')}`):\n\n"
            "<target_file>\n"
            f"{current_content}\n"
            "</target_file>\n\n"
            "Return a unified diff (starting with `--- ` / `+++ ` or `diff `) "
            "that makes the smallest possible edit preventing the pattern "
            "from recurring. Output ONLY the diff."
        )

    # ---- retry ------------------------------------------------------

    async def _call_with_retry(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[str, int, int] | DrafterResult:
        outcome = await call_with_retry(
            self._client,
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            user=user_prompt,
            log_event="learning_drafter_retrying",
        )
        if isinstance(outcome, RetryFailure):
            return DrafterResult(success=False, error=outcome.error)
        return outcome

    # ---- validation -------------------------------------------------

    def _validate_diff(
        self, diff: str, target_path: str, target_abs: Path
    ) -> str | None:
        """Return an error string if the diff should be rejected, else None."""
        added_lines = _extract_added_lines(diff)
        if len(added_lines) > MAX_ADDED_LINES:
            return (
                f"drafter exceeded MAX_ADDED_LINES "
                f"({len(added_lines)} > {MAX_ADDED_LINES})"
            )
        for line in added_lines:
            if _BANNED_DIRECTIVE_RE.match(line):
                return (
                    "drafter emitted an absolute directive "
                    f"(always/never/must): {line!r}"
                )
        path_error = _validate_diff_internal_paths(diff)
        if path_error is not None:
            return path_error
        # Require ``target_path`` to appear in the diff. Without this,
        # a hallucinated sibling like ``runtime/skills/b/`` (when the
        # lesson names ``runtime/skills/a/``) passes the allowlist —
        # and if the sibling exists in the repo, the diff applies
        # cleanly and the PR edits the wrong file.
        #
        # Previously we rejected ANY path other than target_path, but
        # that broke legitimate rename diffs (``old.md → new.md``)
        # where the pre-image path necessarily differs from the
        # post-image. Now we only require target_path to be present;
        # the allowlist check above already bounds the other paths
        # to safe prefixes.
        del target_abs  # retained for future file-local checks
        diff_paths = _extract_all_diff_paths(diff)
        if diff_paths and target_path not in diff_paths:
            return (
                f"drafter diff does not touch target_path "
                f"{target_path!r}; edits {diff_paths!r}"
            )
        if not _git_apply_check(self._repo_root, diff):
            return "git apply --check failed against the target file"
        return None


def _extract_all_diff_paths(diff: str) -> list[str]:
    """All distinct non-``/dev/null`` paths referenced in the diff.

    Used by the drafter's target-path-match check: collects every
    path the diff touches so the caller can assert they all equal
    the expected ``target_path``. Ignores ``/dev/null`` entries.
    Unlike ``_edited_paths_from_diff`` in pr_opener, this helper
    returns paths from BOTH ``--- a/`` and ``+++ b/`` headers plus
    rename/copy variants, because the drafter must not straddle
    targets via any header flavor.
    """
    seen: list[str] = []
    for raw_line in diff.splitlines():
        for path in _extract_header_paths(raw_line):
            if path and path != "/dev/null" and path not in seen:
                seen.append(path)
    return seen


def validate_diff_internal_paths(diff: str) -> str | None:
    """Ensure every path reference in the diff targets an allowlisted file.

    Public entry point — previously only ``_validate_diff_internal_paths``
    (underscore-prefixed) existed, but pr_opener imported it across
    modules anyway. Renaming the underscore form to a public alias
    keeps pr_opener's iter-3 defense-in-depth check on a stable API
    surface (a private helper rename would silently break that guard).
    The underscore alias is retained for backwards compatibility with
    existing callers.

    Inspects four families of header lines that ``git apply`` honors:
    ``--- a/<path>``, ``+++ b/<path>``, ``rename from/to <path>``,
    ``copy from/to <path>``, and the ``diff --git a/<p1> b/<p2>``
    preamble. A rename or copy header could otherwise slip
    ``services/`` or ``.github/`` past the simple ``--- / +++`` check.
    """
    for raw_line in diff.splitlines():
        for path in _extract_header_paths(raw_line):
            if path == "/dev/null" or not path:
                continue
            if ".." in path.split("/"):
                return f"drafter diff contains path traversal: {path!r}"
            if not any(
                path.startswith(prefix) for prefix in _ALLOWED_TARGET_PREFIXES
            ):
                return (
                    f"drafter diff targets disallowed path {path!r} "
                    f"(allowed prefixes: {_ALLOWED_TARGET_PREFIXES})"
                )
    return None


# Backwards-compat alias — kept so existing callers (including
# pr_opener, which imported the underscore form prior to this rename)
# continue to work without churn.
_validate_diff_internal_paths = validate_diff_internal_paths


def _extract_header_paths(line: str) -> list[str]:
    """Pull git-diff path references out of a single diff line.

    Handles ``--- a/``, ``+++ b/``, ``rename from/to``, ``copy from/to``,
    and ``diff --git a/P1 b/P2``. Returns an empty list for non-header
    lines. Each returned path has any ``a/``/``b/`` prefix stripped.
    """
    if line.startswith(("--- ", "+++ ")):
        rest = line[4:].strip()
        return [rest[2:] if rest.startswith(("a/", "b/")) else rest]
    for header in ("rename from ", "rename to ", "copy from ", "copy to "):
        if line.startswith(header):
            return [line[len(header) :].strip()]
    if line.startswith("diff --git "):
        # shlex respects double-quote-wrapped tokens so a path with
        # spaces ("a/file with space.md") stays whole. Plain .split()
        # would shred it into ["\"a/file", "with", "space.md\""] —
        # each fragment would fail the allowlist for unrelated reasons
        # and the real path wouldn't be validated.
        try:
            tokens = shlex.split(line[len("diff --git ") :].strip())
        except ValueError:
            tokens = line[len("diff --git ") :].strip().split()
        paths: list[str] = []
        for tok in tokens:
            paths.append(tok[2:] if tok.startswith(("a/", "b/")) else tok)
        return paths
    return []


def _extract_unified_diff(text: str) -> str:
    """Pull a unified diff out of Claude's response, stripping any prose.

    Accepts either a raw diff or one fenced in ```diff / ``` blocks.
    Returns an empty string when no diff marker is found.

    The fenced regex tolerates a missing trailing newline — some LLM
    outputs emit ``\\n```\\n`` while others emit ``\\n```\\n`` with
    whitespace between content and closing fence. Previously the
    strict ``\\n```\\n`` pattern missed the second shape and the
    last-resort path returned the diff WITH the trailing backticks
    still attached, which later tripped ``git apply --check``.
    """
    text = text.strip()
    fenced = re.search(
        r"```(?:diff|patch)?\s*\n(.*?)\s*```", text, re.DOTALL
    )
    if fenced:
        return fenced.group(1).strip()
    if text.startswith(("--- ", "diff ")):
        return text
    # Last resort: find the first `--- ` line and return from there.
    idx = text.find("\n--- ")
    if idx >= 0:
        return text[idx + 1:].strip()
    return ""


def _extract_added_lines(diff: str) -> list[str]:
    """Lines added by the diff (skipping the ``+++`` header).

    Only ``+++ `` (with whitespace) is a header — a content line like
    ``+++suspicious`` must NOT be elided from the added-lines count,
    or MAX_ADDED_LINES validation under-counts and the drafter can
    sneak past the cap.
    """
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++") and (len(line) == 3 or line[3] in " \t"):
            continue
        if line.startswith("+"):
            out.append(line[1:])
    return out


def _git_apply_check(repo_root: Path, diff: str) -> bool:
    """Run ``git apply --check`` inside ``repo_root`` against the diff.

    Returns True when the diff applies cleanly. ``git apply --check``
    does not modify any files — it returns non-zero iff the patch
    cannot be applied. We pre-screen for a ``--- `` marker because
    ``git apply --check`` exits 0 on blank-ish inputs (an empty file
    is a valid no-op patch), which would let the drafter pass a
    completely non-diff response through.
    """
    stripped = diff.strip()
    if not stripped:
        return False
    if "--- " not in stripped and "diff " not in stripped:
        return False
    # Pin encoding=utf-8 + newline="" so the patch lands byte-for-byte
    # on disk. Default ``NamedTemporaryFile(mode="w")`` uses the
    # platform encoding and does universal-newline translation — on
    # Windows ``\n`` becomes ``\r\n`` and git apply --check rejects
    # the file as "whitespace errors". Same rationale as the fix in
    # pr_opener._write_patch_file (iter 9).
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False,
        encoding="utf-8", newline="",
    ) as tmp:
        tmp.write(diff)
        if not diff.endswith("\n"):
            tmp.write("\n")
        patch_path = tmp.name
    try:
        result = subprocess.run(
            ["git", "apply", "--check", patch_path],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.info(
                "learning_drafter_git_apply_failed",
                stderr=result.stderr[:500],
            )
            return False
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "learning_drafter_git_apply_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    finally:
        Path(patch_path).unlink(missing_ok=True)
