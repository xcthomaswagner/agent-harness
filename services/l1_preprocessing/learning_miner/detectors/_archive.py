"""Shared archive-lookup helpers for trace-reading detectors.

Three detectors (``form_controls_ac_gaps``, ``cross_unit_object_pivot``,
``reviewer_judge_rejection_rate``) each previously carried a nearly
identical ``_archive_root()`` + ``_locate_*()`` pair. This module
centralizes that logic so the layout convention — the archive root
is ``<settings.default_client_repo_parent>/trace-archive`` with
per-ticket subdirectories — lives in exactly one place.

Tests monkeypatch ``<detector>.TICKET_ARCHIVE_ROOT`` /
``PLAN_ARCHIVE_ROOT`` / ``JUDGE_ARCHIVE_ROOT`` on the detector module
to override. Callers pass the overriding Path in as ``override`` and
this helper falls back to the settings-derived path when ``override``
is ``None``. Keeping the override argument explicit (rather than
reaching into the detector module) avoids circular-import tightrope
walks and keeps the helper pure.
"""

from __future__ import annotations

from pathlib import Path

from config import settings


def archive_root(override: Path | None) -> Path | None:
    """Return the conventional archive root, or ``None`` if unresolvable.

    When ``override`` is a Path, it wins (test fixtures use this to
    point at a scratch ``tmp_path``). Otherwise we compute
    ``parent(settings.default_client_repo) / "trace-archive"`` and
    return ``None`` when the setting is unset or malformed.
    """
    if override is not None:
        return override
    repo = settings.default_client_repo
    if not repo:
        return None
    try:
        return Path(repo).parent / "trace-archive"
    except (OSError, ValueError):
        return None


def ticket_json_path(ticket_id: str, override: Path | None) -> Path | None:
    """Return ``<archive_root>/<ticket_id>/ticket.json`` if it's a file."""
    root = archive_root(override)
    if root is None or not ticket_id:
        return None
    candidate = root / ticket_id / "ticket.json"
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def plans_dir(ticket_id: str, override: Path | None) -> Path | None:
    """Return ``<archive_root>/<ticket_id>/plans`` if it's a directory."""
    root = archive_root(override)
    if root is None or not ticket_id:
        return None
    candidate = root / ticket_id / "plans"
    try:
        return candidate if candidate.is_dir() else None
    except OSError:
        return None


def judge_verdict_path(ticket_id: str, override: Path | None) -> Path | None:
    """Return ``<archive_root>/<ticket_id>/logs/judge-verdict.json``."""
    root = archive_root(override)
    if root is None or not ticket_id:
        return None
    candidate = root / ticket_id / "logs" / "judge-verdict.json"
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None
