"""Detector 6 — reviewer_judge_rejection_rate.

Surfaces runs where the Judge consistently overrides the Code
Reviewer — specifically, when the rolling mean rejection rate
over the last N runs exceeds a threshold. High rejection rate
is a signal that the reviewer rubric is producing false positives
faster than the judge can filter them.

Two phases:

1. **Metrics capture** — per pr_run, read the archived
   ``judge-verdict.json`` and compute

       rejection_rate =
           len(rejected_issues) / (len(validated_issues) + len(rejected_issues))

   Upsert that value into ``pipeline_metrics`` under the
   ``reviewer_judge_rejection_rate`` metric name. Idempotent: a
   repeat scan replaces the prior row.

2. **Rolling window** — query the last ``WINDOW_RUNS`` metric rows
   by ``observed_at`` DESC and compute the mean. When the window
   has at least ``WINDOW_RUNS`` rows AND the mean exceeds
   ``RATE_THRESHOLD``, emit a single lesson keyed on
   (client_profile, platform_profile).

Why a rolling window and not a cluster-size threshold: a single
high-rejection run can be noise. The learning signal is "this is
persistent," which requires multiple runs aligned in the same
direction. Keeping the window small (5) keeps the detector
responsive to changes in reviewer behavior without requiring a
large historical corpus.

Note: when a run has zero code-review issues, the judge never
runs and the metric is undefined — those runs contribute nothing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from autonomy_store import (
    list_recent_metrics,
    upsert_pipeline_metric,
)
from learning_miner.detectors._archive import (
    judge_verdict_path,
    load_json_object,
)
from learning_miner.detectors.base import CandidateProposal, EvidenceItem
from learning_miner.detectors.human_issue_cluster import (
    _resolve_platform_profile,
)

logger = structlog.get_logger()

NAME = "reviewer_judge_rejection_rate"
VERSION = 1

METRIC_NAME = "reviewer_judge_rejection_rate"
WINDOW_RUNS = 5
RATE_THRESHOLD = 0.7

# ``list_recent_metrics`` returns rows globally sorted by observed_at
# DESC, but each lesson is emitted per-platform. With only WINDOW_RUNS
# rows fetched, a multi-platform deployment cannot satisfy the per-
# platform "at least WINDOW_RUNS observations" check — the 5 rows
# could be split 3/2 across two platforms and neither platform's
# rolling window would pass. Fetching WINDOW_RUNS * MAX_PLATFORMS
# guarantees that if any one platform has WINDOW_RUNS fresh rows
# they are all visible, regardless of how many other platforms are
# reporting simultaneously. Platform count is small and bounded so
# this stays cheap.
MAX_PLATFORMS = 4

# Test override, same pattern as the other archive-reading detectors.
JUDGE_ARCHIVE_ROOT: Path | None = None


@dataclass(frozen=True)
class _MetricObservation:
    ticket_id: str
    trace_id: str
    value: float


def _locate_judge_verdict(ticket_id: str) -> Path | None:
    """Find the archived judge-verdict.json; delegates to shared helper.

    The archive's logs directory matches the worktree convention:
    ``<archive_root>/<ticket_id>/logs/judge-verdict.json``.
    """
    return judge_verdict_path(ticket_id, JUDGE_ARCHIVE_ROOT)


def _load_judge_verdict(path: Path) -> dict[str, Any] | None:
    return load_json_object(
        path, event_prefix="reviewer_judge_rejection_rate"
    )


def _compute_rejection_rate(verdict: dict[str, Any]) -> float | None:
    """Return rejected / (validated + rejected), or None if undefined."""
    validated = verdict.get("validated_issues") or []
    rejected = verdict.get("rejected_issues") or []
    if not isinstance(validated, list) or not isinstance(rejected, list):
        return None
    total = len(validated) + len(rejected)
    if total == 0:
        return None
    return len(rejected) / total


def _build_scope_key(client_profile: str, platform_profile: str) -> str:
    return f"{client_profile}|{platform_profile}|reviewer_judge_rejection_rate"


def _build_pattern_key() -> str:
    return "reviewer_judge_rejection_rate"


def _build_proposed_delta(
    platform_profile: str,
    mean_rate: float,
    window_size: int,
    sample_tickets: list[str],
) -> str:
    target = (
        f"runtime/platform-profiles/{platform_profile}"
        "/CODE_REVIEW_SUPPLEMENT.md"
    )
    after_line = (
        "- Tighten the code-review rubric before filing a finding: require "
        "that the reviewer cite the exact code path making the issue real. "
        "The judge has been rejecting a majority of findings as false "
        "positives over the recent window, which means the reviewer is "
        "flagging issues the judge can't validate."
    )
    sample = ", ".join(sample_tickets[:3])
    rationale = (
        f"Rolling mean rejection_rate={mean_rate:.2f} across the last "
        f"{window_size} runs (threshold {RATE_THRESHOLD:.2f}). Recent "
        f"examples: {sample}. Extend the code-review supplement so the "
        "reviewer self-checks false-positive risk before filing."
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


class ReviewerJudgeRejectionRateDetector:
    """Detector 6 — see module docstring."""

    name = NAME
    version = VERSION

    def scan(
        self, conn: sqlite3.Connection, window_days: int
    ) -> list[CandidateProposal]:
        # Phase 1: capture metrics for every pr_run in the window that
        # has an archived judge-verdict.json.
        self._capture_metrics(conn, window_days)

        # Phase 2: rolling-mean check.
        return self._emit_if_rolling_threshold_met(conn)

    def _capture_metrics(
        self, conn: sqlite3.Connection, window_days: int
    ) -> None:
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
            return

        seen: set[str] = set()
        for pr in pr_rows:
            platform = _resolve_platform_profile(pr["client_profile"])
            if platform is None:
                continue
            ticket_id = str(pr["ticket_id"])
            if ticket_id in seen:
                continue
            path = _locate_judge_verdict(ticket_id)
            if path is None:
                continue
            verdict = _load_judge_verdict(path)
            if verdict is None:
                continue
            rate = _compute_rejection_rate(verdict)
            if rate is None:
                continue
            # trace_id uses the ticket_id — we persist per ticket so
            # repeat pr_runs (retries) don't bloat the rolling window.
            try:
                upsert_pipeline_metric(
                    conn,
                    ticket_id=ticket_id,
                    trace_id=ticket_id,
                    metric_name=METRIC_NAME,
                    metric_value=float(rate),
                    observed_at=str(pr["opened_at"] or ""),
                )
            except (sqlite3.DatabaseError, ValueError) as exc:
                logger.warning(
                    "reviewer_judge_rejection_rate_upsert_failed",
                    ticket_id=ticket_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            seen.add(ticket_id)

    def _emit_if_rolling_threshold_met(
        self, conn: sqlite3.Connection
    ) -> list[CandidateProposal]:
        # Pull WINDOW_RUNS * MAX_PLATFORMS rows so a multi-platform
        # deployment can independently satisfy the per-platform
        # rolling-window check below. If we only fetched WINDOW_RUNS
        # rows, a 3/2 split across two platforms would silently skip
        # emission on both.
        metrics = list_recent_metrics(
            conn,
            metric_name=METRIC_NAME,
            limit=WINDOW_RUNS * MAX_PLATFORMS,
        )
        if len(metrics) < WINDOW_RUNS:
            # Insufficient history — do not emit yet.
            return []

        # Use the most recent ticket's client/platform as the scope.
        # Cross-profile mining isn't meaningful here — the judge/reviewer
        # dynamic is per-platform. Group metrics by resolved platform.
        by_platform: dict[tuple[str, str], list[_MetricObservation]] = {}
        for m in metrics:
            pr = conn.execute(
                "SELECT client_profile FROM pr_runs "
                "WHERE ticket_id = ? "
                "ORDER BY opened_at DESC LIMIT 1",
                (m.ticket_id,),
            ).fetchone()
            client_profile = str(pr["client_profile"]) if pr else ""
            if not client_profile:
                continue
            platform = _resolve_platform_profile(client_profile)
            if platform is None:
                continue
            obs = _MetricObservation(
                ticket_id=m.ticket_id,
                trace_id=m.trace_id,
                value=m.metric_value,
            )
            by_platform.setdefault(
                (client_profile, platform), []
            ).append(obs)

        proposals: list[CandidateProposal] = []
        for (client_profile, platform_profile), group_obs in by_platform.items():
            if len(group_obs) < WINDOW_RUNS:
                # Every grouped window must independently satisfy the
                # threshold count — otherwise we'd emit on partial
                # cross-platform noise.
                continue
            group_mean = sum(o.value for o in group_obs) / len(group_obs)
            if group_mean <= RATE_THRESHOLD:
                continue
            proposals.append(
                self._build_proposal(
                    client_profile, platform_profile, group_mean, group_obs
                )
            )
        return proposals

    def _build_proposal(
        self,
        client_profile: str,
        platform_profile: str,
        mean_rate: float,
        observations: list[_MetricObservation],
    ) -> CandidateProposal:
        # Severity bumps when the mean is very high.
        if mean_rate >= 0.9:
            severity = "critical"
        elif mean_rate >= 0.8:
            severity = "warn"
        else:
            severity = "info"
        sample_tickets = [o.ticket_id for o in observations]
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
                mean_rate=mean_rate,
                window_size=len(observations),
                sample_tickets=sample_tickets,
            ),
            window_frequency=len(observations),
            evidence=tuple(
                self._build_evidence(observations, mean_rate)
            ),
        )

    def _build_evidence(
        self,
        observations: list[_MetricObservation],
        mean_rate: float,
    ) -> list[EvidenceItem]:
        out: list[EvidenceItem] = []
        for o in observations:
            snippet = (
                f"rejection_rate={o.value:.2f} (rolling mean {mean_rate:.2f} "
                f"across last {len(observations)} runs)"
            )
            out.append(
                EvidenceItem(
                    trace_id=o.trace_id,
                    observed_at="",
                    source_ref=f"pipeline_metrics:{o.ticket_id}",
                    snippet=snippet,
                )
            )
        return out


def build() -> ReviewerJudgeRejectionRateDetector:
    return ReviewerJudgeRejectionRateDetector()
