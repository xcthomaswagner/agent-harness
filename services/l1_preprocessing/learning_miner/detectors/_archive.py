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

import json
from pathlib import Path
from typing import Any

import structlog

from config import settings

logger = structlog.get_logger()


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


def load_json_object(
    path: Path, *, event_prefix: str
) -> dict[str, Any] | None:
    """Read and parse ``path`` as a JSON object; return None on any error.

    Logs one debug-level warning with ``<event_prefix>_read_failed`` or
    ``<event_prefix>_json_decode_failed`` so operators can grep a
    specific detector's diagnostics without the helpers mixing names.
    Non-object JSON (lists, scalars) returns ``None`` — every current
    caller reads an object-shaped sidecar file.

    Previously each of the three sidecar-reading detectors (ticket.json,
    plan-vN.json, judge-verdict.json) carried an identical read+parse
    helper with its own logger event name. Centralizing the boilerplate
    here leaves the event-prefix per detector intact via the keyword
    argument.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug(
            f"{event_prefix}_read_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.debug(
            f"{event_prefix}_json_decode_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    return doc if isinstance(doc, dict) else None


def build_append_section_delta(
    *,
    target_path: str,
    anchor: str,
    after_line: str,
    rationale_md: str,
) -> str:
    """Return the JSON-encoded ``append_section`` delta used by detectors.

    Five detectors (human_issue_cluster, form_controls_ac_gaps,
    cross_unit_object_pivot, reviewer_judge_rejection_rate,
    simplify_no_sidecar) each constructed the same 6-key dict inline:

        {
            "target_path": ...,
            "edit_type": "append_section",
            "anchor": ...,
            "before": "",
            "after": after_line,
            "rationale_md": ...,
            "token_budget_delta": <estimate>,
        }

    Centralizing avoids drift when the proposed_delta schema grows a
    field (a new key added in five places is five opportunities to
    miss one). ``token_budget_delta`` is estimated as
    ``len(after_line.split()) * 2`` — a simple word-count proxy for
    "append this many tokens to the target." Callers supplied their
    own estimate before; keeping the shared proxy here gives a
    uniform estimate across the detector suite.
    """
    delta = {
        "target_path": target_path,
        "edit_type": "append_section",
        "anchor": anchor,
        "before": "",
        "after": after_line,
        "rationale_md": rationale_md,
        "token_budget_delta": len(after_line.split()) * 2,
    }
    return json.dumps(delta, sort_keys=True)
