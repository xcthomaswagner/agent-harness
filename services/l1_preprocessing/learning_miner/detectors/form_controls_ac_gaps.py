"""Detector 3 — form_controls_ac_gaps.

Surfaces cases where an analyst-generated acceptance criterion calls
out a form-control concern (cross-field validation, race safety, URL
state, or session timeout) but the AI code reviewer produced no
matching finding. Over several runs, a silent review gap in one of
those categories is a signal that the code-review supplement needs
an explicit check.

Gating:

1. A pr_run is eligible only when its archived ``ticket.json`` has
   at least one acceptance criterion (generated or authored) that
   matches a known taxonomy category.
2. For an eligible run, a "gap" exists when the AI review did NOT
   flag any issue whose category or summary mentions that same
   taxonomy category.
3. When at least ``MIN_CLUSTER_SIZE`` distinct pr_runs show the
   gap for the same (client_profile, platform_profile, category),
   emit one candidate.

The detector is purposely conservative — false positives here would
push the supplement drafter to add checks that have no teeth. We
only count a gap when the AC mentions the category in plain language.

Where to find the ticket.json:

The archive layout written by ``scripts/spawn_team.py`` places each
ticket's artifacts at::

    <archive_root>/trace-archive/<ticket_id>/ticket.json

``archive_root`` is derived from ``settings.default_client_repo``
(parent of the repo dir) when available, otherwise the module-level
``TICKET_ARCHIVE_ROOT`` override that tests monkeypatch.

See docs/self-learning-plan.md §4.4 for rationale and the list of
taxonomy categories this detector covers.
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

NAME = "form_controls_ac_gaps"
VERSION = 1

MIN_CLUSTER_SIZE = 3  # Distinct pr_runs per (profile, category) to emit.

# Optional override for tests / non-standard deployments. When None,
# _archive_root() falls back to the conventional settings-derived path.
TICKET_ARCHIVE_ROOT: Path | None = None

# Taxonomy: category → set of substring phrases that, when present in
# an AC string (case-insensitive), mark that AC as belonging to the
# category. Kept short and conservative — a false positive inflates
# the eligible pool and pushes the drafter toward vague checks.
_TAXONOMY: dict[str, tuple[str, ...]] = {
    "cross_field_validation": (
        "cross-field",
        "cross field",
        "field depends on",
        "dependent field",
        "field validation",
    ),
    "race_safety": (
        "race condition",
        "race safety",
        "concurrent update",
        "double submit",
        "double-submit",
        "optimistic locking",
    ),
    "url_state": (
        "url state",
        "url parameter",
        "query string",
        "querystring",
        "deep link",
        "browser back",
        "back button",
    ),
    "session_timeout": (
        "session timeout",
        "session expiry",
        "session expired",
        "idle timeout",
        "re-authenticate",
        "re-authentication",
    ),
}


@dataclass(frozen=True)
class _GapKey:
    client_profile: str
    platform_profile: str
    category: str


@dataclass(frozen=True)
class _GapObservation:
    pr_run_id: int
    ticket_id: str
    observed_at: str
    matched_phrases: tuple[str, ...]


def _archive_root() -> Path | None:
    """Return the configured archive root, or None if unresolvable."""
    if TICKET_ARCHIVE_ROOT is not None:
        return TICKET_ARCHIVE_ROOT
    repo = settings.default_client_repo
    if not repo:
        return None
    try:
        return Path(repo).parent / "trace-archive"
    except (OSError, ValueError):
        return None


def _locate_ticket_json(ticket_id: str) -> Path | None:
    """Find the archived ticket.json for ``ticket_id``, or None."""
    root = _archive_root()
    if root is None:
        return None
    candidate = root / ticket_id / "ticket.json"
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _load_ticket_json(path: Path) -> dict[str, Any] | None:
    """Read + parse the ticket.json file; log + skip on error."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug(
            "form_controls_ac_gaps_read_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.debug(
            "form_controls_ac_gaps_json_decode_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    return doc if isinstance(doc, dict) else None


def _extract_ac_list(ticket: dict[str, Any]) -> list[str]:
    """Return the combined authored + generated AC list (best-effort)."""
    out: list[str] = []
    for key in ("acceptance_criteria", "generated_acceptance_criteria"):
        raw = ticket.get(key) or []
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item)
    return out


def _categorize(ac_list: list[str]) -> dict[str, list[str]]:
    """Return category → list of AC strings that matched.

    Compile-once regex per category keeps the inner loop cheap even
    when the window contains hundreds of runs.
    """
    compiled: dict[str, re.Pattern[str]] = {
        cat: re.compile(
            "|".join(re.escape(p) for p in phrases), re.IGNORECASE
        )
        for cat, phrases in _TAXONOMY.items()
    }
    buckets: dict[str, list[str]] = {cat: [] for cat in _TAXONOMY}
    for ac in ac_list:
        for cat, pat in compiled.items():
            if pat.search(ac):
                buckets[cat].append(ac)
    return {cat: hits for cat, hits in buckets.items() if hits}


def _ai_reviews_touched_category(
    conn: sqlite3.Connection, pr_run_id: int, category: str
) -> bool:
    """True iff any AI review_issue on this run mentions the category.

    ``review_issues.category`` is free-form, so we match the taxonomy
    phrases against both ``category`` and ``summary`` — reviewers
    often file issues with a descriptive summary but a generic
    category. Case-insensitive, space-insensitive comparison.
    """
    phrases = _TAXONOMY[category]
    rows = conn.execute(
        """
        SELECT LOWER(COALESCE(category, '')) AS c,
               LOWER(COALESCE(summary, '')) AS s
        FROM review_issues
        WHERE pr_run_id = ?
          AND source IN ('ai_review', 'judge', 'qa')
        """,
        (pr_run_id,),
    ).fetchall()
    if not rows:
        return False
    for r in rows:
        haystack = f"{r['c']} {r['s']}"
        for p in phrases:
            if p.lower() in haystack:
                return True
    return False


def _build_scope_key(key: _GapKey) -> str:
    return "|".join([
        key.client_profile,
        key.platform_profile,
        key.category,
    ])


def _build_pattern_key(category: str) -> str:
    return f"form_controls_gap|{category}"


def _build_proposed_delta(
    platform_profile: str,
    category: str,
    observations: list[_GapObservation],
) -> str:
    target = (
        f"runtime/platform-profiles/{platform_profile}"
        "/CODE_REVIEW_SUPPLEMENT.md"
    )
    category_label = category.replace("_", " ")
    after_line = (
        f"- When reviewing code, check for {category_label} issues "
        "— the acceptance criteria call out this class of check."
    )
    rationale = (
        f"Across {len(observations)} distinct pr_runs the analyst AC "
        f"flagged {category_label} but the AI review produced no "
        "matching finding. Extend the supplement so the reviewer checks "
        "for this class of issue explicitly."
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


class FormControlsAcGapsDetector:
    """Detector 3 — see module docstring."""

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

        clusters: dict[_GapKey, list[_GapObservation]] = {}
        # Dedup per (ticket_id, category): a single ticket's retries
        # produce multiple pr_run rows sharing one archived ticket.json
        # and the same AI review coverage — counting each retry would
        # inflate the cluster size.
        seen: set[tuple[str, str]] = set()

        for pr in pr_rows:
            platform = _resolve_platform_profile(pr["client_profile"])
            if platform is None:
                continue
            ticket_path = _locate_ticket_json(pr["ticket_id"])
            if ticket_path is None:
                continue
            ticket = _load_ticket_json(ticket_path)
            if ticket is None:
                continue
            ac_list = _extract_ac_list(ticket)
            if not ac_list:
                continue
            categorized = _categorize(ac_list)
            if not categorized:
                continue
            for category, matched_phrases in categorized.items():
                dedup_key = (str(pr["ticket_id"]), category)
                if dedup_key in seen:
                    continue
                if _ai_reviews_touched_category(
                    conn, int(pr["id"]), category
                ):
                    # Review caught it — not a gap.
                    continue
                seen.add(dedup_key)
                key = _GapKey(
                    client_profile=pr["client_profile"],
                    platform_profile=platform,
                    category=category,
                )
                clusters.setdefault(key, []).append(
                    _GapObservation(
                        pr_run_id=int(pr["id"]),
                        ticket_id=str(pr["ticket_id"]),
                        observed_at=str(pr["opened_at"] or ""),
                        matched_phrases=tuple(matched_phrases[:3]),
                    )
                )

        proposals: list[CandidateProposal] = []
        for key, obs in clusters.items():
            if len({o.ticket_id for o in obs}) < MIN_CLUSTER_SIZE:
                continue
            proposals.append(self._build_proposal(key, obs))
        return proposals

    def _build_proposal(
        self, key: _GapKey, observations: list[_GapObservation]
    ) -> CandidateProposal:
        cluster_size = len({o.ticket_id for o in observations})
        severity = (
            "warn" if cluster_size >= MIN_CLUSTER_SIZE * 2 else "info"
        )
        return CandidateProposal(
            detector_name=NAME,
            detector_version=VERSION,
            pattern_key=_build_pattern_key(key.category),
            client_profile=key.client_profile,
            platform_profile=key.platform_profile,
            scope_key=_build_scope_key(key),
            severity=severity,
            proposed_delta_json=_build_proposed_delta(
                platform_profile=key.platform_profile,
                category=key.category,
                observations=observations,
            ),
            window_frequency=cluster_size,
            evidence=tuple(self._build_evidence(key, observations)),
        )

    def _build_evidence(
        self, key: _GapKey, observations: list[_GapObservation]
    ) -> list[EvidenceItem]:
        out: list[EvidenceItem] = []
        for o in observations:
            snippet_head = ", ".join(
                p for p in o.matched_phrases if p
            )
            snippet = (
                f"AC flagged {key.category.replace('_', ' ')} "
                f"({snippet_head[:120]}); AI review did not surface it"
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


def build() -> FormControlsAcGapsDetector:
    return FormControlsAcGapsDetector()
