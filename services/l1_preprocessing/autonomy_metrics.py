"""Shared per-profile autonomy metric computation.

Computes Phase 2 metrics (self-review catch rate, sidecar coverage, human
issue counts, data-quality notes, recommended mode) for a single client
profile over a rolling window. Used by both the JSON read API
(autonomy_ingest.GET /api/autonomy) and the HTML dashboard
(autonomy_dashboard).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from autonomy_store import list_pr_runs

logger = structlog.get_logger()


# Tier-4 "suggested" matches don't count toward the self-review catch
# rate — they need manual promotion first. Tier-1..3 matches have
# confidence >= 0.8 by construction.
_CATCH_RATE_CONFIDENCE_THRESHOLD = 0.8


def compute_profile_metrics(
    conn: sqlite3.Connection,
    profile: str,
    window_days: int,
    *,
    include_recent_rows: int = 20,
) -> dict[str, Any]:
    """Compute Phase 2 autonomy metrics for one client profile.

    Returns a dict with these keys (stable shape across callers):

        client_profile, sample_size, merged_count, first_pass_count,
        first_pass_acceptance_rate,
        self_review_catch_rate (float|None), human_issue_count,
        matched_human_issue_count, unmatched_human_issue_count,
        sidecar_coverage, ai_issue_count,
        defect_escape_rate (None, Phase 3),
        recommended_mode, data_quality_status, data_quality_notes,
        recent_rows (list of sqlite3.Row).
    """
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    rows = list_pr_runs(conn, client_profile=profile, since_iso=cutoff)
    sample_size = len(rows)
    merged_count = sum(1 for r in rows if int(r["merged"]) == 1)
    first_pass_count = sum(1 for r in rows if int(r["first_pass_accepted"]) == 1)
    first_pass_acceptance_rate = (
        round(first_pass_count / sample_size, 3) if sample_size else 0.0
    )

    pr_run_ids = [int(r["id"]) for r in rows]
    if not pr_run_ids:
        human_issue_count = 0
        matched_human_issue_count = 0
        ai_issue_count = 0
        prs_with_ai = 0
    else:
        placeholders = ",".join("?" * len(pr_run_ids))

        # Valid human-review issues in window
        human_rows = conn.execute(
            f"SELECT id FROM review_issues WHERE pr_run_id IN ({placeholders}) "
            f"AND source = 'human_review' AND is_valid = 1",
            pr_run_ids,
        ).fetchall()
        human_issue_count = len(human_rows)

        if human_rows:
            human_ids = [int(h["id"]) for h in human_rows]
            hph = ",".join("?" * len(human_ids))
            matched = conn.execute(
                f"SELECT DISTINCT human_issue_id FROM issue_matches "
                f"WHERE human_issue_id IN ({hph}) AND confidence >= ? "
                f"AND matched_by != 'suggested'",
                [*human_ids, _CATCH_RATE_CONFIDENCE_THRESHOLD],
            ).fetchall()
            matched_human_issue_count = len(matched)
        else:
            matched_human_issue_count = 0

        # Sidecar coverage (distinct pr_runs with any valid AI/QA issue)
        ai_pr_rows = conn.execute(
            f"SELECT DISTINCT pr_run_id FROM review_issues "
            f"WHERE pr_run_id IN ({placeholders}) "
            f"AND source IN ('ai_review', 'qa') AND is_valid = 1",
            pr_run_ids,
        ).fetchall()
        prs_with_ai = len(ai_pr_rows)

        ai_issue_count_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM review_issues "
            f"WHERE pr_run_id IN ({placeholders}) "
            f"AND source IN ('ai_review', 'qa') AND is_valid = 1",
            pr_run_ids,
        ).fetchone()
        ai_issue_count = int(ai_issue_count_row["n"]) if ai_issue_count_row else 0

    unmatched_human_issue_count = human_issue_count - matched_human_issue_count

    if human_issue_count == 0:
        self_review_catch_rate: float | None = None
    else:
        self_review_catch_rate = round(
            matched_human_issue_count / human_issue_count, 3
        )

    sidecar_coverage = (
        round(prs_with_ai / sample_size, 3) if sample_size > 0 else 0.0
    )

    notes: list[str] = []
    if sample_size < 10:
        notes.append("low_sample_size")
    if sidecar_coverage < 0.8 and sample_size >= 5:
        notes.append("low_sidecar_coverage")
    if human_issue_count == 0 and sample_size >= 5:
        notes.append("no_human_baseline")

    if not notes:
        status = "good"
    elif "low_sample_size" in notes:
        status = "insufficient_data"
    else:
        status = "degraded"

    # Phase 2: still conservative. Low sidecar coverage (or any degraded
    # signal) is explicitly captured here so Phase 3 can layer thresholds
    # on top without unseating this fallback.
    recommended_mode = "conservative"

    return {
        "client_profile": profile,
        "sample_size": sample_size,
        "merged_count": merged_count,
        "first_pass_count": first_pass_count,
        "first_pass_acceptance_rate": first_pass_acceptance_rate,
        "self_review_catch_rate": self_review_catch_rate,
        "human_issue_count": human_issue_count,
        "matched_human_issue_count": matched_human_issue_count,
        "unmatched_human_issue_count": unmatched_human_issue_count,
        "sidecar_coverage": sidecar_coverage,
        "ai_issue_count": ai_issue_count,
        "defect_escape_rate": None,
        "recommended_mode": recommended_mode,
        "data_quality_status": status,
        "data_quality_notes": notes,
        "recent_rows": rows[:include_recent_rows],
    }
