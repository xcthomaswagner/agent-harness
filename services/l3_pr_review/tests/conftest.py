"""Shared pytest fixtures for L3 tests."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_backlog_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Redirect the L3 backlog file to a per-test temp path by default.

    Tests that need explicit control can re-override backlog.BACKLOG_PATH.
    """
    import backlog as backlog_mod

    monkeypatch.setattr(backlog_mod, "BACKLOG_PATH", tmp_path / "backlog.jsonl")
