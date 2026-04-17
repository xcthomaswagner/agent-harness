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
        if not target_path:
            return DrafterResult(
                success=False, error="proposed_delta.target_path missing"
            )
        if not any(
            target_path.startswith(p) for p in _ALLOWED_TARGET_PREFIXES
        ):
            return DrafterResult(
                success=False,
                error=(
                    f"target_path {target_path!r} is outside allowed "
                    f"prefixes {_ALLOWED_TARGET_PREFIXES}"
                ),
            )
        if not target_path.endswith(".md"):
            # Markdown drafter is explicitly Markdown-only; the YAML
            # drafter (Phase H) handles client-profile YAML.
            return DrafterResult(
                success=False,
                error=(
                    "markdown drafter received non-markdown target "
                    f"{target_path!r}"
                ),
            )
        return None

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
        evidence_block = "\n".join(
            f"- {s}" for s in evidence_snippets[:20]
        ) or "(no evidence snippets captured)"
        return (
            "The harness emitted this mechanical starter proposal:\n\n"
            f"```json\n{json.dumps(proposed_delta, indent=2, sort_keys=True)}\n```\n\n"
            "Evidence snippets the detector collected "
            f"({len(evidence_snippets)} total, truncated):\n\n"
            f"{evidence_block}\n\n"
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
        if not _git_apply_check(self._repo_root, diff):
            return "git apply --check failed against the target file"
        return None


def _validate_diff_internal_paths(diff: str) -> str | None:
    """Ensure every path reference in the diff targets an allowlisted file.

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
    """
    text = text.strip()
    fenced = re.search(
        r"```(?:diff|patch)?\s*\n(.*?)\n```", text, re.DOTALL
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
    """Lines added by the diff (skipping the ``+++`` header)."""
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++"):
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
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False
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
