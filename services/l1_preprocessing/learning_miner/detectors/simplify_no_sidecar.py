"""Detector 5 — simplify_no_sidecar.

Surfaces runs where the Team Lead logged a ``simplify`` phase with
``changes_made=true`` but no ``.harness/logs/simplify.md`` sidecar
was written. This is a simplify-agent output-contract violation —
the miner and dashboards rely on the sidecar to attribute refactor
commits to the simplify pass, and the refactor commit message
alone isn't enough.

How we check:

* Read the trace entries for each pr_run in the window via
  ``read_trace(ticket_id)``.
* Scan for a ``simplify`` / ``Simplification complete`` entry with
  a truthy ``changes_made`` flag.
* In the same trace, look for an artifact entry with
  ``event=ARTIFACT_SIMPLIFY``. Presence proves the consolidator
  imported ``simplify.md`` from the worktree.
* When the "changes_made" claim exists but the artifact does not,
  emit one observation for that pr_run.

Emits a single candidate per (client_profile, platform_profile)
cluster once ``MIN_CLUSTER_SIZE`` distinct tickets show the
violation.

See runtime/skills/simplify/SKILL.md for the output contract this
detector polices.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from learning_miner.detectors.base import CandidateProposal, EvidenceItem
from learning_miner.detectors.human_issue_cluster import (
    _resolve_platform_profile,
)
from tracer import ARTIFACT_SIMPLIFY, safe_read_trace

logger = structlog.get_logger()

NAME = "simplify_no_sidecar"
VERSION = 1

MIN_CLUSTER_SIZE = 2


@dataclass(frozen=True)
class _NoSidecarObservation:
    pr_run_id: int
    ticket_id: str
    observed_at: str


def _changes_made_flag(entry: dict[str, Any]) -> bool:
    """Return True if the entry signals simplify changes were made."""
    # Canonical path: entry has ``changes_made: true`` on the simplify
    # phase-transition log.
    raw = entry.get("changes_made")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    if isinstance(raw, int):
        return raw != 0
    return False


def _is_simplify_phase_entry(entry: dict[str, Any]) -> bool:
    """True when the entry is the simplify phase-transition log line.

    Tolerates the three phrasings the Team Lead may emit:
    ``Simplification complete``, ``Simplification skipped``, and
    any future variant that keeps ``phase=simplify`` + ``event`` non-
    empty. We only care that phase == 'simplify' and the entry is a
    phase-transition (not an artifact import).
    """
    phase = entry.get("phase")
    event = entry.get("event") or ""
    if phase != "simplify":
        return False
    if not isinstance(event, str) or not event:
        return False
    # Artifact imports reuse phase=artifact, event=simplify_artifact —
    # filter to the completion event only.
    return event != ARTIFACT_SIMPLIFY


def _has_simplify_artifact(entries: list[dict[str, Any]]) -> bool:
    """True when any entry is the simplify_artifact consolidation row."""
    for e in entries:
        if (
            e.get("phase") == "artifact"
            and e.get("event") == ARTIFACT_SIMPLIFY
        ):
            return True
    return False


def _build_scope_key(client_profile: str, platform_profile: str) -> str:
    return f"{client_profile}|{platform_profile}|simplify_no_sidecar"


def _build_pattern_key() -> str:
    return "simplify_no_sidecar"


def _build_proposed_delta(
    platform_profile: str, observations: list[_NoSidecarObservation]
) -> str:
    # The fix is cross-platform (harden simplify output contract), so
    # the target is the skill itself rather than a platform supplement.
    target = "runtime/skills/simplify/SKILL.md"
    after_line = (
        "- The simplify agent MUST always write `.harness/logs/simplify.md`, "
        "whether it made changes or not. The sidecar is the evidence the "
        "learning miner uses to reconcile refactor commits with simplify "
        "passes — without it, simplify runs look like silent failures."
    )
    rationale = (
        f"{len(observations)} pr_runs logged simplify changes_made=true "
        "but no simplify.md sidecar landed in the trace archive. Reinforce "
        "the skill's post-condition so the output is written unconditionally."
    )
    delta = {
        "target_path": target,
        "edit_type": "append_section",
        "anchor": "## Output",
        "before": "",
        "after": after_line,
        "rationale_md": rationale,
        "token_budget_delta": len(after_line.split()) * 2,
    }
    return json.dumps(delta, sort_keys=True)


class SimplifyNoSidecarDetector:
    """Detector 5 — see module docstring."""

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

        clusters: dict[
            tuple[str, str], list[_NoSidecarObservation]
        ] = {}
        seen: set[str] = set()

        for pr in pr_rows:
            platform = _resolve_platform_profile(pr["client_profile"])
            if platform is None:
                continue
            ticket_id = str(pr["ticket_id"])
            if ticket_id in seen:
                continue
            entries = safe_read_trace(ticket_id)
            if not entries:
                continue
            claimed = any(
                _is_simplify_phase_entry(e) and _changes_made_flag(e)
                for e in entries
            )
            if not claimed:
                continue
            if _has_simplify_artifact(entries):
                # Sidecar landed — not a violation.
                continue
            seen.add(ticket_id)
            key = (str(pr["client_profile"]), platform)
            clusters.setdefault(key, []).append(
                _NoSidecarObservation(
                    pr_run_id=int(pr["id"]),
                    ticket_id=ticket_id,
                    observed_at=str(pr["opened_at"] or ""),
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
        observations: list[_NoSidecarObservation],
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
        self, observations: list[_NoSidecarObservation]
    ) -> list[EvidenceItem]:
        out: list[EvidenceItem] = []
        for o in observations:
            snippet = (
                "simplify phase logged changes_made=true but "
                "simplify.md sidecar was not consolidated into trace"
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


def build() -> SimplifyNoSidecarDetector:
    return SimplifyNoSidecarDetector()
