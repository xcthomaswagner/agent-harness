"""Tests for the Markdown drafter (learning_miner/drafter_markdown.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from learning_miner.drafter_markdown import (
    MAX_ADDED_LINES,
    MarkdownDrafter,
    _extract_added_lines,
    _extract_unified_diff,
    _git_apply_check,
    _validate_diff_internal_paths,
    check_target_path,
)
from tests.conftest import make_anthropic_response as _mock_anthropic_response

# ---- low-level helpers -----------------------------------------------


class TestExtractUnifiedDiff:
    def test_raw_diff_returned_unchanged(self) -> None:
        # The extractor strips trailing whitespace; accept either form.
        diff = "--- a/foo\n+++ b/foo\n@@\n+line\n"
        assert _extract_unified_diff(diff).strip() == diff.strip()

    def test_fenced_diff_unwrapped(self) -> None:
        text = "```diff\n--- a/foo\n+++ b/foo\n@@\n+line\n```"
        assert _extract_unified_diff(text) == "--- a/foo\n+++ b/foo\n@@\n+line"

    def test_prose_preamble_skipped(self) -> None:
        text = (
            "Here is the diff you asked for:\n\n"
            "--- a/foo\n+++ b/foo\n@@\n+line"
        )
        out = _extract_unified_diff(text)
        assert out.startswith("--- a/foo")

    def test_empty_on_no_diff(self) -> None:
        assert _extract_unified_diff("hi there") == ""


class TestExtractAddedLines:
    def test_ignores_header(self) -> None:
        diff = "+++ b/foo.md\n+real add\n-a drop\n"
        assert _extract_added_lines(diff) == ["real add"]

    def test_handles_multiple(self) -> None:
        diff = "+++ a\n+line one\n+line two\n @ context\n+line three\n"
        assert _extract_added_lines(diff) == ["line one", "line two", "line three"]

    def test_content_line_starting_with_triple_plus_counts(self) -> None:
        """A content line whose body starts with ``++`` is a real added line.

        Only ``+++`` followed by whitespace is a header; without this
        guard a drafter could smuggle content past MAX_ADDED_LINES by
        prefixing suspicious lines with ``++``.
        """
        diff = "+++ b/foo.md\n+++keep me\n+normal add\n"
        assert _extract_added_lines(diff) == ["++keep me", "normal add"]


class TestCheckTargetPath:
    """Shared helper used by both the drafter precheck and the /draft API."""

    def test_empty_rejected(self) -> None:
        assert "missing" in (check_target_path("") or "")

    def test_allowlisted_passes(self) -> None:
        assert check_target_path("runtime/skills/x/SKILL.md") is None
        assert check_target_path("runtime/agents/reviewer.md") is None
        assert check_target_path(
            "runtime/platform-profiles/salesforce/CODE_REVIEW_SUPPLEMENT.md"
        ) is None

    def test_absolute_path_rejected(self) -> None:
        # Regression: pathlib's / operator discards the repo_root when
        # the RHS is absolute, so the API must reject before reading.
        err = check_target_path("/etc/passwd") or ""
        assert "absolute" in err

    def test_traversal_rejected(self) -> None:
        err = check_target_path("runtime/skills/../../etc/passwd") or ""
        assert ".." in err

    def test_non_markdown_rejected(self) -> None:
        err = check_target_path("runtime/skills/foo.yaml") or ""
        assert "non-markdown" in err

    def test_outside_prefix_rejected(self) -> None:
        err = check_target_path("services/l1_preprocessing/main.py") or ""
        assert "outside allowed" in err


# ---- git apply check -------------------------------------------------


class TestValidateDiffInternalPaths:
    def test_allowed_paths_pass(self) -> None:
        diff = (
            "--- a/runtime/skills/code-review/SKILL.md\n"
            "+++ b/runtime/skills/code-review/SKILL.md\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        assert _validate_diff_internal_paths(diff) is None

    def test_disallowed_prefix_rejected(self) -> None:
        diff = (
            "--- a/services/l1/main.py\n"
            "+++ b/services/l1/main.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        err = _validate_diff_internal_paths(diff)
        assert err is not None
        assert "disallowed path" in err

    def test_path_traversal_rejected(self) -> None:
        diff = (
            "--- a/runtime/skills/../../services/x.py\n"
            "+++ b/runtime/skills/../../services/x.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        err = _validate_diff_internal_paths(diff)
        assert err is not None
        assert "traversal" in err

    def test_dev_null_ignored(self) -> None:
        # File-delete diffs from git have `+++ b/dev/null`. Detector
        # won't produce these but ignoring them keeps the check safe.
        diff = (
            "--- a/runtime/skills/code-review/SKILL.md\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n-old\n"
        )
        assert _validate_diff_internal_paths(diff) is None

    def test_mixed_disallowed_in_later_hunk_rejected(self) -> None:
        diff = (
            "--- a/runtime/skills/code-review/SKILL.md\n"
            "+++ b/runtime/skills/code-review/SKILL.md\n"
            "@@ -1 +1 @@\n-x\n+y\n"
            "--- a/.github/workflows/ci.yml\n"
            "+++ b/.github/workflows/ci.yml\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        err = _validate_diff_internal_paths(diff)
        assert err is not None
        assert ".github" in err

    def test_rename_to_disallowed_target_rejected(self) -> None:
        # Extended unified-diff format: rename headers are honored by
        # ``git apply`` even when the --- /+++ lines look innocent.
        diff = (
            "diff --git a/runtime/skills/foo.md b/services/l1/secrets.py\n"
            "rename from runtime/skills/foo.md\n"
            "rename to services/l1/secrets.py\n"
            "--- a/runtime/skills/foo.md\n"
            "+++ b/services/l1/secrets.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        err = _validate_diff_internal_paths(diff)
        assert err is not None
        assert "services/l1" in err

    def test_copy_from_disallowed_rejected(self) -> None:
        diff = (
            "diff --git a/runtime/skills/foo.md b/.github/CODEOWNERS\n"
            "copy from .github/workflows/ci.yml\n"
            "copy to runtime/skills/foo.md\n"
        )
        err = _validate_diff_internal_paths(diff)
        assert err is not None

    def test_diff_git_header_scanned(self) -> None:
        # Even if a malicious diff omits --- /+++ , the diff --git
        # preamble itself still names paths git apply honors.
        diff = (
            "diff --git a/services/l1/main.py b/services/l1/main.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        err = _validate_diff_internal_paths(diff)
        assert err is not None
        assert "services/l1" in err


class TestGitApplyCheck:
    def test_clean_apply_passes(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        target = tmp_path / "file.txt"
        target.write_text("a\nb\nc\n")
        subprocess.run(
            ["git", "add", "file.txt"], cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-m", "init"],
            cwd=tmp_path, check=True, capture_output=True,
        )
        diff = (
            "--- a/file.txt\n+++ b/file.txt\n"
            "@@ -1,3 +1,4 @@\n a\n b\n c\n+d\n"
        )
        assert _git_apply_check(tmp_path, diff) is True

    def test_malformed_diff_fails(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        assert _git_apply_check(tmp_path, "not a diff") is False

    def test_empty_diff_fails(self, tmp_path: Path) -> None:
        assert _git_apply_check(tmp_path, "") is False


# ---- drafter end-to-end (mocked client) ------------------------------


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Minimal git repo with one Markdown file the drafter can target."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    target = tmp_path / "runtime" / "skills" / "code-review"
    target.mkdir(parents=True)
    skill = target / "SKILL.md"
    skill.write_text(
        "# Code Review\n\n## Review Checklist\n\n"
        "- Check that new APIs have docstrings.\n"
        "- Verify tests cover the new path.\n"
    )
    subprocess.run(
        ["git", "add", "."], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    return tmp_path


@pytest.fixture
def mock_client(mock_anthropic_client):
    return mock_anthropic_client


@pytest.fixture
def drafter(fake_repo: Path, mock_client) -> MarkdownDrafter:
    return MarkdownDrafter(
        api_key="test",
        repo_root=fake_repo,
        client=mock_client,
    )


_VALID_DIFF = "\n".join(
    [
        "--- a/runtime/skills/code-review/SKILL.md",
        "+++ b/runtime/skills/code-review/SKILL.md",
        "@@ -3,4 +3,5 @@",
        " ## Review Checklist",
        " ",
        " - Check that new APIs have docstrings.",
        " - Verify tests cover the new path.",
        "+- Check SOQL injection in *.cls files.",
    ]
) + "\n"


class TestDrafterHappyPath:
    async def test_valid_diff_returns_success(
        self, drafter: MarkdownDrafter, mock_client: AsyncMock
    ) -> None:
        mock_client.messages.create.return_value = _mock_anthropic_response(
            _VALID_DIFF
        )
        result = await drafter.draft(
            proposed_delta={
                "target_path": "runtime/skills/code-review/SKILL.md",
                "anchor": "## Review Checklist",
                "rationale_md": "SOQL injection flagged in multiple PRs",
            },
            evidence_snippets=["force-app/foo.cls: SOQL injection"],
        )
        assert result.success is True
        assert "SOQL injection in *.cls" in result.unified_diff
        assert result.tokens_in == 100
        assert result.tokens_out == 50


class TestDrafterPrecheck:
    async def test_rejects_missing_target_path(
        self, drafter: MarkdownDrafter, mock_client: AsyncMock
    ) -> None:
        result = await drafter.draft(
            proposed_delta={"anchor": "## x"},
            evidence_snippets=[],
        )
        assert result.success is False
        assert "target_path" in result.error
        mock_client.messages.create.assert_not_called()

    async def test_rejects_out_of_scope_target(
        self, drafter: MarkdownDrafter, mock_client: AsyncMock
    ) -> None:
        result = await drafter.draft(
            proposed_delta={"target_path": "services/l1/main.py"},
            evidence_snippets=[],
        )
        assert result.success is False
        assert "outside allowed prefixes" in result.error
        mock_client.messages.create.assert_not_called()

    async def test_rejects_non_markdown_target(
        self, drafter: MarkdownDrafter, mock_client: AsyncMock
    ) -> None:
        # A .yaml target inside an allowed prefix hits the markdown gate.
        result = await drafter.draft(
            proposed_delta={"target_path": "runtime/skills/foo.yaml"},
            evidence_snippets=[],
        )
        assert result.success is False
        assert "non-markdown" in result.error
        mock_client.messages.create.assert_not_called()


class TestDrafterValidation:
    async def test_rejects_absolute_directive(
        self, drafter: MarkdownDrafter, mock_client: AsyncMock
    ) -> None:
        bad_diff = "\n".join(
            [
                "--- a/runtime/skills/code-review/SKILL.md",
                "+++ b/runtime/skills/code-review/SKILL.md",
                "@@ -3,4 +3,5 @@",
                " ## Review Checklist",
                " ",
                " - Check that new APIs have docstrings.",
                " - Verify tests cover the new path.",
                "+- Always check SOQL injection.",
            ]
        ) + "\n"
        mock_client.messages.create.return_value = _mock_anthropic_response(
            bad_diff
        )
        result = await drafter.draft(
            proposed_delta={
                "target_path": "runtime/skills/code-review/SKILL.md",
                "anchor": "## Review Checklist",
            },
            evidence_snippets=[],
        )
        assert result.success is False
        assert "absolute directive" in result.error

    async def test_rejects_oversized_diff(
        self,
        drafter: MarkdownDrafter,
        mock_client: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from learning_miner import drafter_markdown as dm

        monkeypatch.setattr(dm, "MAX_ADDED_LINES", 5)
        added_lines = [f"+- check thing {i}" for i in range(20)]
        big_diff = "\n".join(
            [
                "--- a/runtime/skills/code-review/SKILL.md",
                "+++ b/runtime/skills/code-review/SKILL.md",
                "@@ -3,4 +3,24 @@",
                " ## Review Checklist",
                " ",
                " - Check that new APIs have docstrings.",
                " - Verify tests cover the new path.",
                *added_lines,
            ]
        ) + "\n"
        mock_client.messages.create.return_value = _mock_anthropic_response(
            big_diff
        )
        result = await drafter.draft(
            proposed_delta={
                "target_path": "runtime/skills/code-review/SKILL.md",
                "anchor": "## Review Checklist",
            },
            evidence_snippets=[],
        )
        assert result.success is False
        assert "MAX_ADDED_LINES" in result.error

    async def test_rejects_when_git_apply_check_fails(
        self,
        drafter: MarkdownDrafter,
        mock_client: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Rather than relying on git's variable behavior across CI/local
        # environments (which was flaky in the suite), stub the check
        # helper directly so the test exercises the validator's error
        # propagation deterministically.
        from learning_miner import drafter_markdown as dm

        monkeypatch.setattr(dm, "_git_apply_check", lambda *_a, **_kw: False)
        mock_client.messages.create.return_value = _mock_anthropic_response(
            _VALID_DIFF
        )
        result = await drafter.draft(
            proposed_delta={
                "target_path": "runtime/skills/code-review/SKILL.md",
                "anchor": "## Review Checklist",
            },
            evidence_snippets=[],
        )
        assert result.success is False
        assert "git apply --check failed" in result.error


class TestDrafterAnthropicErrors:
    async def test_non_retryable_error_returns_failure(
        self, drafter: MarkdownDrafter, mock_client: AsyncMock
    ) -> None:
        mock_client.messages.create.side_effect = anthropic.BadRequestError(
            message="bad",
            response=MagicMock(status_code=400),
            body=None,
        )
        result = await drafter.draft(
            proposed_delta={
                "target_path": "runtime/skills/code-review/SKILL.md",
                "anchor": "## Review Checklist",
            },
            evidence_snippets=[],
        )
        assert result.success is False
        assert "BadRequestError" in result.error

    async def test_retries_on_server_error_then_fails(
        self, drafter: MarkdownDrafter, mock_client: AsyncMock, monkeypatch
    ) -> None:
        # Patch asyncio.sleep so retries don't actually wait.
        import asyncio

        async def _no_sleep(*_: object, **__: object) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)

        mock_client.messages.create.side_effect = anthropic.APIStatusError(
            message="upstream",
            response=MagicMock(status_code=503),
            body=None,
        )
        result = await drafter.draft(
            proposed_delta={
                "target_path": "runtime/skills/code-review/SKILL.md",
                "anchor": "## Review Checklist",
            },
            evidence_snippets=[],
        )
        assert result.success is False
        assert mock_client.messages.create.call_count == 3


class TestMaxAddedLinesDefault:
    def test_default_matches_constant(self) -> None:
        assert MAX_ADDED_LINES == 12
