"""Tests for repo-local WORKFLOW.md scanning helpers."""

from __future__ import annotations

from pathlib import Path

from repo_workflow import copy_repo_workflow_overlay


def test_copy_repo_workflow_overlay_is_silent_when_absent(tmp_path: Path) -> None:
    result = copy_repo_workflow_overlay(tmp_path)

    assert result["available"] is False
    assert not (tmp_path / ".harness" / "repo-workflow.md").exists()


def test_copy_repo_workflow_overlay_copies_existing_workflow(tmp_path: Path) -> None:
    (tmp_path / "WORKFLOW.md").write_text("# WORKFLOW.md\n\nRules\n", encoding="utf-8")

    result = copy_repo_workflow_overlay(tmp_path)

    assert result["available"] is True
    assert (tmp_path / ".harness" / "repo-workflow.md").read_text(
        encoding="utf-8"
    ) == "# WORKFLOW.md\n\nRules\n"
    assert (tmp_path / ".harness" / "repo-workflow.json").is_file()
