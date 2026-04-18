"""Detector 4 — cross_unit_object_pivot.

Surfaces cases where a plan retargets its SObject layer across units
(pivot from one object to another, or adds objects mid-plan) but
doesn't include a corresponding permission-set update. In Salesforce,
a pivot without a permset realignment typically means the code
compiles but the CRUD/FLS grants are still pointed at the old object
— a real production failure class we've seen land on shipped PRs.

Gating rules:

1. Only runs on archived plans from ``pr_runs`` in the window.
2. Requires at least two plan versions (plan-v1.json plus plan-v2+)
   — a single-version plan is not a pivot.
3. The latest plan must declare at least one unit whose
   ``affected_files`` touches ``force-app/main/default/objects/<X>``
   paths.
4. The latest plan must NOT declare any unit whose affected_files
   touch ``force-app/main/default/permissionsets/``.
5. When a pr_run meets (1-4), that's one pivot observation.

Emits a candidate keyed on the client/platform once
``MIN_CLUSTER_SIZE`` distinct tickets show the pattern. Evidence cites
each offending pr_run.

Archive layout expected (matches ``scripts/spawn_team.py``)::

    <archive_root>/trace-archive/<ticket_id>/plans/plan-v{1,2,...}.json
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from config import settings
from learning_miner.detectors.base import CandidateProposal, EvidenceItem
from learning_miner.detectors.human_issue_cluster import (
    _resolve_platform_profile,
)

logger = structlog.get_logger()

NAME = "cross_unit_object_pivot"
VERSION = 1

MIN_CLUSTER_SIZE = 2  # Cross-ticket; the signal is strong per-ticket
#                       so a lower threshold is appropriate.

# Test override knob, same pattern as form_controls_ac_gaps.
PLAN_ARCHIVE_ROOT: Path | None = None

_OBJECT_PATH_RE = re.compile(
    r"force-app/main/default/objects/([A-Za-z0-9_]+)/",
    re.IGNORECASE,
)
_PERMSET_PATH_RE = re.compile(
    r"force-app/main/default/permissionsets/",
    re.IGNORECASE,
)
_PLAN_FILENAME_RE = re.compile(r"^plan-v(\d+)\.json$")


@dataclass(frozen=True)
class _PivotObservation:
    pr_run_id: int
    ticket_id: str
    observed_at: str
    object_names: tuple[str, ...]
    plan_versions: int


def _archive_root() -> Path | None:
    if PLAN_ARCHIVE_ROOT is not None:
        return PLAN_ARCHIVE_ROOT
    repo = settings.default_client_repo
    if not repo:
        return None
    try:
        return Path(repo).parent / "trace-archive"
    except (OSError, ValueError):
        return None


def _locate_plans_dir(ticket_id: str) -> Path | None:
    root = _archive_root()
    if root is None:
        return None
    candidate = root / ticket_id / "plans"
    try:
        return candidate if candidate.is_dir() else None
    except OSError:
        return None


def _load_plan(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug(
            "cross_unit_object_pivot_read_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.debug(
            "cross_unit_object_pivot_json_decode_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    return doc if isinstance(doc, dict) else None


def _list_plan_versions(plans_dir: Path) -> list[tuple[int, Path]]:
    """Return (version_number, path) pairs sorted ascending."""
    out: list[tuple[int, Path]] = []
    try:
        for p in plans_dir.iterdir():
            if not p.is_file():
                continue
            m = _PLAN_FILENAME_RE.match(p.name)
            if m:
                out.append((int(m.group(1)), p))
    except OSError as exc:
        logger.debug(
            "cross_unit_object_pivot_iter_failed",
            path=str(plans_dir),
            error=f"{type(exc).__name__}: {exc}",
        )
        return []
    out.sort(key=lambda t: t[0])
    return out


def _extract_affected_files(plan: dict[str, Any]) -> list[list[str]]:
    """Return list-of-lists: each inner list is one unit's affected_files."""
    units = plan.get("units") or []
    if not isinstance(units, list):
        return []
    out: list[list[str]] = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        files = unit.get("affected_files") or []
        if not isinstance(files, list):
            continue
        out.append([str(f) for f in files if isinstance(f, str)])
    return out


def _object_names_in_plan(files_per_unit: list[list[str]]) -> set[str]:
    names: set[str] = set()
    for files in files_per_unit:
        for f in files:
            m = _OBJECT_PATH_RE.search(f)
            if m:
                names.add(m.group(1))
    return names


def _plan_touches_permsets(files_per_unit: list[list[str]]) -> bool:
    for files in files_per_unit:
        for f in files:
            if _PERMSET_PATH_RE.search(f):
                return True
    return False


def _build_scope_key(client_profile: str, platform_profile: str) -> str:
    return f"{client_profile}|{platform_profile}|object_pivot_no_permset"


def _build_pattern_key() -> str:
    return "object_pivot_no_permset"


def _build_proposed_delta(
    platform_profile: str, observations: list[_PivotObservation]
) -> str:
    target = (
        f"runtime/platform-profiles/{platform_profile}"
        "/PLAN_REVIEW_SUPPLEMENT.md"
    )
    after_line = (
        "- When a plan revises SObject targets across units, verify the "
        "plan also includes a unit that updates the corresponding "
        "permission set(s). Missing permset realignment ships as a "
        "runtime CRUD/FLS failure class."
    )
    rationale = (
        f"{len(observations)} pr_runs shipped plans that pivoted "
        "SObject targets across units without a companion permset "
        "update. Add a plan-review supplement so the plan reviewer "
        "flags this class of gap explicitly."
    )
    delta = {
        "target_path": target,
        "edit_type": "append_section",
        "anchor": "## Review Checklist",
        "before": "",
        "after": after_line,
        "rationale_md": rationale,
        "token_budget_delta": len(after_line.split()) * 2,
    }
    return json.dumps(delta, sort_keys=True)


class CrossUnitObjectPivotDetector:
    """Detector 4 — see module docstring."""

    name = NAME
    version = VERSION

    def scan(
        self, conn: sqlite3.Connection, window_days: int
    ) -> list[CandidateProposal]:
        cutoff_iso = (
            datetime.now(UTC) - timedelta(days=window_days)
        ).isoformat()

        pr_rows = conn.execute(
            """
            SELECT id, ticket_id, client_profile, opened_at
            FROM pr_runs
            WHERE opened_at >= ?
              AND COALESCE(ticket_id, '') != ''
              AND COALESCE(client_profile, '') != ''
            ORDER BY id
            """,
            (cutoff_iso,),
        ).fetchall()

        if not pr_rows:
            return []

        # Per (client, platform) clusters of observations.
        clusters: dict[
            tuple[str, str], list[_PivotObservation]
        ] = {}
        # Dedupe multiple pr_runs per ticket sharing one plan archive.
        seen: set[str] = set()

        for pr in pr_rows:
            platform = _resolve_platform_profile(pr["client_profile"])
            if platform is None:
                continue
            # Detector is Salesforce-specific (object / permset paths).
            # Skip other platforms cleanly rather than erroring.
            if platform != "salesforce":
                continue
            ticket_id = str(pr["ticket_id"])
            if ticket_id in seen:
                continue
            plans_dir = _locate_plans_dir(ticket_id)
            if plans_dir is None:
                continue
            versions = _list_plan_versions(plans_dir)
            if len(versions) < 2:
                # A single plan version is not a pivot — the plan
                # reviewer didn't revise anything.
                continue
            _latest_version, latest_path = versions[-1]
            plan = _load_plan(latest_path)
            if plan is None:
                continue
            files_per_unit = _extract_affected_files(plan)
            object_names = _object_names_in_plan(files_per_unit)
            if not object_names:
                continue
            if _plan_touches_permsets(files_per_unit):
                # Permset updated alongside — not a gap.
                continue
            seen.add(ticket_id)
            key = (str(pr["client_profile"]), platform)
            clusters.setdefault(key, []).append(
                _PivotObservation(
                    pr_run_id=int(pr["id"]),
                    ticket_id=ticket_id,
                    observed_at=str(pr["opened_at"] or ""),
                    object_names=tuple(sorted(object_names)),
                    plan_versions=len(versions),
                )
            )

        proposals: list[CandidateProposal] = []
        for (client_profile, platform_profile), obs in clusters.items():
            if len({o.ticket_id for o in obs}) < MIN_CLUSTER_SIZE:
                continue
            proposals.append(
                self._build_proposal(
                    client_profile, platform_profile, obs
                )
            )
        return proposals

    def _build_proposal(
        self,
        client_profile: str,
        platform_profile: str,
        observations: list[_PivotObservation],
    ) -> CandidateProposal:
        cluster_size = len({o.ticket_id for o in observations})
        severity = (
            "warn" if cluster_size >= MIN_CLUSTER_SIZE * 2 else "info"
        )
        return CandidateProposal(
            detector_name=NAME,
            detector_version=VERSION,
            pattern_key=_build_pattern_key(),
            client_profile=client_profile,
            platform_profile=platform_profile,
            scope_key=_build_scope_key(client_profile, platform_profile),
            severity=severity,
            proposed_delta_json=_build_proposed_delta(
                platform_profile=platform_profile,
                observations=observations,
            ),
            window_frequency=cluster_size,
            evidence=tuple(self._build_evidence(observations)),
        )

    def _build_evidence(
        self, observations: list[_PivotObservation]
    ) -> list[EvidenceItem]:
        out: list[EvidenceItem] = []
        for o in observations:
            objects_head = ", ".join(o.object_names[:3])
            snippet = (
                f"Plan revised {o.plan_versions}x, final targets "
                f"objects=[{objects_head}] — no permset update unit"
            )
            out.append(
                EvidenceItem(
                    trace_id=o.ticket_id,
                    observed_at=o.observed_at,
                    source_ref=f"pr_runs#{o.pr_run_id}",
                    snippet=snippet,
                    pr_run_id=o.pr_run_id,
                )
            )
        return out


def build() -> CrossUnitObjectPivotDetector:
    return CrossUnitObjectPivotDetector()
