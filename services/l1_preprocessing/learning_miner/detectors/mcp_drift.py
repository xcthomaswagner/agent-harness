"""Detector 1 — mcp_drift.

Surfaces cases where an agent reaches for a CLI (``sf``, ``gh``, ``az``)
via Bash when the equivalent MCP server IS listed as available in the
session init but wasn't used — a signal that the profile's prompts
aren't steering toward the MCP tools. Ratio-based so "tried MCP once,
fell back to CLI 8x" still triggers.

Reads the ``tool_index`` artifact from each run's trace file (produced
by ``tracer.consolidate_worktree_logs`` via ``tool_index.build_tool_index``)
— no new persistence; we re-scan traces on each mining run.

See docs/self-learning-plan.md §4.1 for the prereq ``bash_verb_counts``
extension on ``tool_index.py`` and §4.3 for why this detector ships
after Detector 2.
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
from tool_index import _canonical_server
from tracer import ARTIFACT_TOOL_INDEX, latest_artifacts, safe_read_trace

logger = structlog.get_logger()

NAME = "mcp_drift"
VERSION = 1

# Ratio gate: Bash count must be >= MIN_BASH_COUNT AND
# >= RATIO_THRESHOLD * (mcp_call_count + 1). The +1 absorbs the
# "tried once then gave up" case.
MIN_BASH_COUNT = 3
RATIO_THRESHOLD = 3
MIN_CLUSTER_SIZE = 3  # Traces per (profile, verb) to emit a proposal.

# Bash verb → canonical MCP server name (matches tool_index canonicalization
# where hyphens become underscores). Keep conservative; adding an entry
# should be driven by observed drift, not speculation.
_BASH_VERB_TO_MCP_SERVER: dict[str, str] = {
    "sf": "salesforce_capability_mcp",
    "gh": "github",
    "az": "ado",
}

# Equivalence aliases folded into canonical verbs before lookup.
_BASH_VERB_ALIASES: dict[str, str] = {
    "sfdx": "sf",
}


@dataclass(frozen=True)
class _DriftKey:
    client_profile: str
    platform_profile: str
    bash_verb: str
    mcp_server: str


@dataclass(frozen=True)
class _DriftObservation:
    """One trace's contribution to a drift cluster."""

    pr_run_id: int
    ticket_id: str
    observed_at: str
    bash_count: int
    mcp_count: int


def _canonical_verb(verb: str) -> str:
    """Apply alias table; empty/non-verb passes through unchanged."""
    v = (verb or "").strip().lower()
    return _BASH_VERB_ALIASES.get(v, v)


def _count_mcp_calls_for_server(
    tool_counts: dict[str, Any], server: str
) -> int:
    """Sum calls to any tool whose prefix resolves to ``server``.

    Tool names are ``mcp__<server>__<tool>``. ``server`` here is
    already canonicalized; we recanonicalize the prefix so hyphenated
    original names match. Shared with tool_index._canonical_server so
    the two canonicalization paths can't drift.
    """
    total = 0
    for name, count in (tool_counts or {}).items():
        if not isinstance(name, str) or not name.startswith("mcp__"):
            continue
        rest = name[len("mcp__") :]
        sep = rest.find("__")
        if sep <= 0:
            continue
        if _canonical_server(rest[:sep]) == server:
            total += int(count) if isinstance(count, int | float) else 0
    return total


def _find_tool_index(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the latest tool_index artifact payload, or None."""
    artifacts = latest_artifacts(entries)
    entry = artifacts.get(ARTIFACT_TOOL_INDEX)
    if entry is None:
        return None
    idx = entry.get("index")
    return idx if isinstance(idx, dict) else None


def _run_started_at(entries: list[dict[str, Any]]) -> str:
    """Pick the earliest timestamp off the trace's first non-empty entry."""
    for e in entries:
        ts = e.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return ""


def _build_scope_key(key: _DriftKey) -> str:
    return "|".join([
        key.client_profile,
        key.platform_profile,
        key.bash_verb,
        key.mcp_server,
    ])


def _build_pattern_key(bash_verb: str, mcp_server: str) -> str:
    return f"{bash_verb}|{mcp_server}"


def _build_proposed_delta(
    platform_profile: str,
    bash_verb: str,
    mcp_server: str,
    observations: list[_DriftObservation],
) -> str:
    target = (
        f"runtime/platform-profiles/{platform_profile}/IMPLEMENT_SUPPLEMENT.md"
    )
    total_bash = sum(o.bash_count for o in observations)
    total_mcp = sum(o.mcp_count for o in observations)
    after_line = (
        f"- Prefer the `{mcp_server}` MCP tools over `{bash_verb}` CLI "
        "calls when both are available — the MCP wrapper enforces "
        "post-condition verification and safer env handling."
    )
    rationale = (
        f"Across {len(observations)} runs the agent invoked `{bash_verb}` "
        f"{total_bash}x while the `{mcp_server}` MCP server was connected "
        f"and used only {total_mcp}x in total. Add an explicit tool-preference "
        "note to the platform profile so the planner steers toward MCP "
        "wrappers by default."
    )
    delta = {
        "target_path": target,
        "edit_type": "append_section",
        "anchor": "## Tool Preferences",
        "before": "",
        "after": after_line,
        "rationale_md": rationale,
        "token_budget_delta": len(after_line.split()) * 2,
    }
    return json.dumps(delta, sort_keys=True)


class McpDriftDetector:
    """Detector 1 — see module docstring."""

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

        # Cluster: _DriftKey -> list[_DriftObservation]
        clusters: dict[_DriftKey, list[_DriftObservation]] = {}
        # Dedupe per ticket_id + verb + server: retries create multiple
        # pr_runs sharing one trace. Without this, the same bash_count /
        # mcp_count gets summed N times, inflating `total_bash` in the
        # rationale and possibly flipping severity when MCP usage is
        # actually zero.
        seen_per_key: set[tuple[str, str, str]] = set()

        for pr in pr_rows:
            platform = _resolve_platform_profile(pr["client_profile"])
            if platform is None:
                continue
            entries = safe_read_trace(pr["ticket_id"])
            if not entries:
                continue
            idx = _find_tool_index(entries)
            if idx is None:
                continue

            available = set(idx.get("mcp_servers_available") or [])
            if not available:
                continue
            tool_counts = idx.get("tool_counts") or {}
            bash_verb_counts = idx.get("bash_verb_counts") or {}
            observed_at = _run_started_at(entries) or pr["opened_at"] or ""
            ticket_id = str(pr["ticket_id"])

            for raw_verb, raw_count in bash_verb_counts.items():
                verb = _canonical_verb(raw_verb)
                if verb not in _BASH_VERB_TO_MCP_SERVER:
                    continue
                server = _BASH_VERB_TO_MCP_SERVER[verb]
                if server not in available:
                    continue
                bash_count = int(raw_count) if isinstance(raw_count, int | float) else 0
                mcp_count = _count_mcp_calls_for_server(tool_counts, server)
                # Ratio gate + absolute floor.
                if bash_count < MIN_BASH_COUNT:
                    continue
                if bash_count < RATIO_THRESHOLD * (mcp_count + 1):
                    continue

                # Include ``server`` alongside ``verb`` for robustness —
                # if _BASH_VERB_TO_MCP_SERVER ever becomes many-to-one
                # (aliases for the same server) or one-to-many (a verb
                # mapping to multiple servers), this dedup would still
                # collapse correctly rather than silently merging.
                dedup_key = (ticket_id, verb, server)
                if dedup_key in seen_per_key:
                    continue
                seen_per_key.add(dedup_key)

                key = _DriftKey(
                    client_profile=pr["client_profile"],
                    platform_profile=platform,
                    bash_verb=verb,
                    mcp_server=server,
                )
                clusters.setdefault(key, []).append(
                    _DriftObservation(
                        pr_run_id=int(pr["id"]),
                        ticket_id=ticket_id,
                        observed_at=observed_at,
                        bash_count=bash_count,
                        mcp_count=mcp_count,
                    )
                )

        proposals: list[CandidateProposal] = []
        for key, obs in clusters.items():
            unique_tickets = {o.ticket_id for o in obs}
            if len(unique_tickets) < MIN_CLUSTER_SIZE:
                continue
            proposals.append(self._build_proposal(key, obs))
        return proposals

    def _build_proposal(
        self, key: _DriftKey, observations: list[_DriftObservation]
    ) -> CandidateProposal:
        pattern_key = _build_pattern_key(key.bash_verb, key.mcp_server)
        scope_key = _build_scope_key(key)
        cluster_size = len({o.ticket_id for o in observations})
        proposed_delta = _build_proposed_delta(
            platform_profile=key.platform_profile,
            bash_verb=key.bash_verb,
            mcp_server=key.mcp_server,
            observations=observations,
        )
        # Severity bump when MCP usage is literally zero — a "never tried"
        # signal is stronger than a ratio violation.
        total_mcp = sum(o.mcp_count for o in observations)
        if total_mcp == 0 and cluster_size >= MIN_CLUSTER_SIZE * 2:
            severity = "critical"
        elif total_mcp == 0 or cluster_size >= MIN_CLUSTER_SIZE * 2:
            severity = "warn"
        else:
            severity = "info"
        return CandidateProposal(
            detector_name=NAME,
            detector_version=VERSION,
            pattern_key=pattern_key,
            client_profile=key.client_profile,
            platform_profile=key.platform_profile,
            scope_key=scope_key,
            severity=severity,
            proposed_delta_json=proposed_delta,
            window_frequency=cluster_size,
            evidence=tuple(self._build_evidence(key, observations)),
        )

    def _build_evidence(
        self, key: _DriftKey, observations: list[_DriftObservation]
    ) -> list[EvidenceItem]:
        out: list[EvidenceItem] = []
        for o in observations:
            snippet = (
                f"bash `{key.bash_verb}` used {o.bash_count}x while "
                f"MCP `{key.mcp_server}` was available and used "
                f"{o.mcp_count}x"
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


def build() -> McpDriftDetector:
    """Factory for the runner / tests."""
    return McpDriftDetector()
