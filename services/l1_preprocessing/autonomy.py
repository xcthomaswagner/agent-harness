"""Graduated autonomy engine — tracks PR outcomes and determines merge policy.

Metrics are stored in a JSON file and evaluated over rolling 30-day windows.
When confidence thresholds are met, the system recommends expanding auto-merge scope.

Merge Modes:
- conservative: Human reviews every PR (default)
- semi_autonomous: Auto-merge low-risk PRs (bug fixes, config, deps)
- full_autonomous: Auto-merge all PRs, human reviews a statistical sample
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

DEFAULT_METRICS_PATH = Path(__file__).resolve().parents[2] / "data" / "autonomy-metrics.json"

# Thresholds from architecture doc Section 16
SEMI_AUTONOMOUS_THRESHOLD = {
    "first_pass_acceptance_rate": 0.90,  # 90% PRs approved without revision
    "defect_escape_rate": 0.05,  # <5% merged PRs have bugs found later
    "min_sample_size": 20,  # Minimum PRs in the window
}

FULL_AUTONOMOUS_THRESHOLD = {
    "self_review_catch_rate": 0.85,  # AI review catches 85%+ of what humans find
    "first_pass_acceptance_rate": 0.95,
    "defect_escape_rate": 0.03,
    "min_sample_size": 50,
}

ROLLING_WINDOW_DAYS = 30
LOW_RISK_TYPES = {"bug", "chore", "config", "dependency", "docs"}


class PROutcome:
    """A single PR outcome record."""

    def __init__(
        self,
        ticket_id: str,
        pr_url: str,
        ticket_type: str,
        created_at: str,
        first_pass_accepted: bool = False,
        human_issues_found: int = 0,
        ai_issues_found: int = 0,
        defect_escaped: bool = False,
        merged: bool = False,
        time_to_pr_seconds: int = 0,
        escalated: bool = False,
    ) -> None:
        self.ticket_id = ticket_id
        self.pr_url = pr_url
        self.ticket_type = ticket_type
        self.created_at = created_at
        self.first_pass_accepted = first_pass_accepted
        self.human_issues_found = human_issues_found
        self.ai_issues_found = ai_issues_found
        self.defect_escaped = defect_escaped
        self.merged = merged
        self.time_to_pr_seconds = time_to_pr_seconds
        self.escalated = escalated

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "pr_url": self.pr_url,
            "ticket_type": self.ticket_type,
            "created_at": self.created_at,
            "first_pass_accepted": self.first_pass_accepted,
            "human_issues_found": self.human_issues_found,
            "ai_issues_found": self.ai_issues_found,
            "defect_escaped": self.defect_escaped,
            "merged": self.merged,
            "time_to_pr_seconds": self.time_to_pr_seconds,
            "escalated": self.escalated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PROutcome:
        return cls(
            ticket_id=str(data.get("ticket_id", "")),
            pr_url=str(data.get("pr_url", "")),
            ticket_type=str(data.get("ticket_type", "")),
            created_at=str(data.get("created_at", "")),
            first_pass_accepted=bool(data.get("first_pass_accepted", False)),
            human_issues_found=int(data.get("human_issues_found", 0)),
            ai_issues_found=int(data.get("ai_issues_found", 0)),
            defect_escaped=bool(data.get("defect_escaped", False)),
            merged=bool(data.get("merged", False)),
            time_to_pr_seconds=int(data.get("time_to_pr_seconds", 0)),
            escalated=bool(data.get("escalated", False)),
        )


class AutonomyEngine:
    """Tracks PR outcomes and recommends merge policy."""

    def __init__(self, metrics_path: Path | None = None) -> None:
        self._path = metrics_path or DEFAULT_METRICS_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[PROutcome]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
            return [PROutcome.from_dict(d) for d in data]
        except (json.JSONDecodeError, KeyError):
            logger.warning("autonomy_metrics_corrupt", path=str(self._path))
            return []

    def _save(self, outcomes: list[PROutcome]) -> None:
        data = [o.to_dict() for o in outcomes]
        self._path.write_text(json.dumps(data, indent=2))

    def record_outcome(self, outcome: PROutcome) -> None:
        """Record a PR outcome."""
        outcomes = self._load()
        # Replace existing record for same ticket
        outcomes = [o for o in outcomes if o.ticket_id != outcome.ticket_id]
        outcomes.append(outcome)
        self._save(outcomes)
        logger.info("autonomy_outcome_recorded", ticket_id=outcome.ticket_id)

    def get_metrics(self, window_days: int = ROLLING_WINDOW_DAYS) -> dict[str, Any]:
        """Calculate metrics over a rolling window."""
        outcomes = self._load()
        cutoff = datetime.now(UTC) - timedelta(days=window_days)

        # Filter to window
        windowed = [
            o for o in outcomes
            if o.created_at >= cutoff.isoformat()
        ]

        if not windowed:
            return {
                "window_days": window_days,
                "sample_size": 0,
                "first_pass_acceptance_rate": 0.0,
                "defect_escape_rate": 0.0,
                "self_review_catch_rate": 0.0,
                "recommended_mode": "conservative",
            }

        total = len(windowed)
        merged = [o for o in windowed if o.merged]
        first_pass = sum(1 for o in windowed if o.first_pass_accepted)
        defects = sum(1 for o in merged if o.defect_escaped) if merged else 0

        # Self-review catch rate: of issues humans found, how many did AI also find?
        total_human_issues = sum(o.human_issues_found for o in windowed)
        total_ai_issues = sum(o.ai_issues_found for o in windowed)
        catch_rate = (
            total_ai_issues / total_human_issues
            if total_human_issues > 0
            else 1.0  # If humans found nothing, AI caught "everything"
        )

        first_pass_rate = first_pass / total if total > 0 else 0.0
        defect_rate = defects / len(merged) if merged else 0.0

        # Determine recommended mode
        mode = self._recommend_mode(first_pass_rate, defect_rate, catch_rate, total)

        # Time-to-PR metrics
        pr_times = [o.time_to_pr_seconds for o in windowed if o.time_to_pr_seconds > 0]
        avg_time_to_pr = round(sum(pr_times) / len(pr_times)) if pr_times else 0

        # By ticket type
        type_times: dict[str, list[int]] = {}
        for o in windowed:
            if o.time_to_pr_seconds > 0:
                type_times.setdefault(o.ticket_type, []).append(o.time_to_pr_seconds)
        avg_by_type = {
            t: round(sum(times) / len(times))
            for t, times in type_times.items()
        }

        # Escalation rate
        escalated = sum(1 for o in windowed if o.escalated)
        escalation_rate = round(escalated / total, 3) if total > 0 else 0.0

        return {
            "window_days": window_days,
            "sample_size": total,
            "merged_count": len(merged),
            "first_pass_acceptance_rate": round(first_pass_rate, 3),
            "defect_escape_rate": round(defect_rate, 3),
            "self_review_catch_rate": round(min(catch_rate, 1.0), 3),
            "recommended_mode": mode,
            "avg_time_to_pr_seconds": avg_time_to_pr,
            "avg_time_to_pr_by_type": avg_by_type,
            "escalation_rate": escalation_rate,
        }

    @staticmethod
    def _recommend_mode(
        first_pass_rate: float,
        defect_rate: float,
        catch_rate: float,
        sample_size: int,
    ) -> str:
        """Determine the recommended merge mode based on metrics."""
        # Full autonomous
        if (
            sample_size >= FULL_AUTONOMOUS_THRESHOLD["min_sample_size"]
            and first_pass_rate >= FULL_AUTONOMOUS_THRESHOLD["first_pass_acceptance_rate"]
            and defect_rate <= FULL_AUTONOMOUS_THRESHOLD["defect_escape_rate"]
            and catch_rate >= FULL_AUTONOMOUS_THRESHOLD["self_review_catch_rate"]
        ):
            return "full_autonomous"

        # Semi-autonomous
        if (
            sample_size >= SEMI_AUTONOMOUS_THRESHOLD["min_sample_size"]
            and first_pass_rate >= SEMI_AUTONOMOUS_THRESHOLD["first_pass_acceptance_rate"]
            and defect_rate <= SEMI_AUTONOMOUS_THRESHOLD["defect_escape_rate"]
        ):
            return "semi_autonomous"

        return "conservative"

    def should_auto_merge(self, ticket_type: str) -> bool:
        """Determine if a PR for this ticket type should be auto-merged."""
        metrics = self.get_metrics()
        mode = metrics["recommended_mode"]

        if mode == "full_autonomous":
            return True
        if mode == "semi_autonomous":
            return ticket_type.lower() in LOW_RISK_TYPES
        return False
