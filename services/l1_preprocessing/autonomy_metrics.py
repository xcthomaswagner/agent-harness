"""Shared per-profile autonomy metric computation.

Computes Phase 2/3 metrics (self-review catch rate, sidecar coverage, human
issue counts, defect escape rate, data-quality notes, recommended mode) for
a single client profile over a rolling window. Used by both the JSON read
API (autonomy_ingest.GET /api/autonomy) and the HTML dashboard
(autonomy_dashboard).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from autonomy_store import (
    count_merged_pr_runs_with_escape,
    get_latest_defect_sweep_heartbeat,
    list_pr_runs,
)

logger = structlog.get_logger()


# Tier-4 "suggested" matches don't count toward the self-review catch
# rate — they need manual promotion first. Tier-1..3 matches have
# confidence >= 0.8 by construction.
_CATCH_RATE_CONFIDENCE_THRESHOLD = 0.8


def _recommend_mode(
    sample_size: int,
    first_pass_acceptance_rate: float,
    defect_escape_rate: float | None,
    self_review_catch_rate: float | None,
    dq_status: str,
) -> str:
    """Tiered mode recommendation.

    - conservative by default or when data quality is degraded
    - semi_autonomous: 20+ samples, FPA >= 90%, escape <= 5%
    - full_autonomous: 50+ samples, FPA >= 95%, escape <= 3%,
      catch_rate >= 85% (must be known, not None)
    """
    if dq_status != "good":
        return "conservative"
    if defect_escape_rate is None:
        return "conservative"
    if (
        sample_size >= 50
        and first_pass_acceptance_rate >= 0.95
        and defect_escape_rate <= 0.03
        and self_review_catch_rate is not None
        and self_review_catch_rate >= 0.85
    ):
        return "full_autonomous"
    if (
        sample_size >= 20
        and first_pass_acceptance_rate >= 0.90
        and defect_escape_rate <= 0.05
    ):
        return "semi_autonomous"
    return "conservative"


def compute_profile_metrics(
    conn: sqlite3.Connection,
    profile: str,
    window_days: int,
    *,
    include_recent_rows: int = 20,
    defect_window_days: int = 30,
) -> dict[str, Any]:
    """Compute Phase 2/3 autonomy metrics for one client profile.

    Returns a dict with these keys (stable shape across callers):

        client_profile, sample_size, merged_count, first_pass_count,
        first_pass_acceptance_rate,
        self_review_catch_rate (float|None), human_issue_count,
        matched_human_issue_count, unmatched_human_issue_count,
        sidecar_coverage, ai_issue_count,
        defect_escape_rate (float|None),
        defect_link_coverage (float),
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

    # --- Phase 3: defect escape rate + link coverage ---
    merged_pr_run_ids = [
        int(r["id"]) for r in rows if int(r["merged"]) == 1 and r["merged_at"]
    ]
    merged_with_at = len(merged_pr_run_ids)

    defect_escape_rate: float | None
    if merged_with_at == 0:
        defect_escape_rate = None
        defect_link_coverage = 0.0
    else:
        escaped_count = count_merged_pr_runs_with_escape(
            conn, merged_pr_run_ids, window_days=defect_window_days
        )
        defect_escape_rate = round(escaped_count / merged_with_at, 3)
        heartbeat = get_latest_defect_sweep_heartbeat(conn, profile)
        if heartbeat:
            try:
                swept_dt = datetime.fromisoformat(heartbeat)
                if swept_dt.tzinfo is None:
                    swept_dt = swept_dt.replace(tzinfo=UTC)
                age_hours = (
                    datetime.now(UTC) - swept_dt
                ).total_seconds() / 3600
                defect_link_coverage = 1.0 if age_hours <= 24 else 0.5
            except (ValueError, TypeError):
                defect_link_coverage = 0.0
        else:
            defect_link_coverage = 0.0

    notes: list[str] = []
    if sample_size < 10:
        notes.append("low_sample_size")
    if sidecar_coverage < 0.8 and sample_size >= 5:
        notes.append("low_sidecar_coverage")
    if human_issue_count == 0 and sample_size >= 5:
        notes.append("no_human_baseline")
    if defect_link_coverage < 0.8 and merged_count >= 5:
        notes.append("low_defect_link_coverage")
    if defect_escape_rate is None and merged_count >= 5:
        notes.append("defect_escape_unknown")

    if not notes:
        status = "good"
    elif "low_sample_size" in notes:
        status = "insufficient_data"
    else:
        status = "degraded"

    recommended_mode = _recommend_mode(
        sample_size=sample_size,
        first_pass_acceptance_rate=first_pass_acceptance_rate,
        defect_escape_rate=defect_escape_rate,
        self_review_catch_rate=self_review_catch_rate,
        dq_status=status,
    )

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
        "defect_escape_rate": defect_escape_rate,
        "defect_link_coverage": defect_link_coverage,
        "recommended_mode": recommended_mode,
        "data_quality_status": status,
        "data_quality_notes": notes,
        "recent_rows": rows[:include_recent_rows],
    }


# ---------------------------------------------------------------------------
# Phase 3 Step 7: by-ticket-type breakdown + daily trends
# ---------------------------------------------------------------------------


def compute_ticket_type_breakdown(
    conn: sqlite3.Connection,
    profile: str,
    window_days: int,
) -> list[dict[str, Any]]:
    """Return one row per ticket_type for a profile in window.

    Each row: {ticket_type, sample_size, first_pass_acceptance_rate,
    self_review_catch_rate, defect_escape_rate, merged_count}.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    rows = list_pr_runs(conn, client_profile=profile, since_iso=cutoff)

    by_type: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        tt = (r["ticket_type"] or "").strip() or "(unspecified)"
        by_type.setdefault(tt, []).append(r)

    out: list[dict[str, Any]] = []
    for ticket_type, group in sorted(by_type.items()):
        sample = len(group)
        fp = sum(1 for g in group if int(g["first_pass_accepted"]) == 1)
        merged_with_at = [
            int(g["id"]) for g in group
            if int(g["merged"]) == 1 and g["merged_at"]
        ]
        fpa = round(fp / sample, 3) if sample else 0.0

        defect_escape: float | None
        if not merged_with_at:
            defect_escape = None
        else:
            escaped = count_merged_pr_runs_with_escape(
                conn, merged_with_at, window_days=30
            )
            defect_escape = round(escaped / len(merged_with_at), 3)

        pr_ids = [int(g["id"]) for g in group]
        if pr_ids:
            ph = ",".join("?" * len(pr_ids))
            humans = conn.execute(
                f"SELECT id FROM review_issues WHERE pr_run_id IN ({ph}) "
                f"AND source = 'human_review' AND is_valid = 1",
                pr_ids,
            ).fetchall()
            h_count = len(humans)
            if humans:
                h_ids = [int(h["id"]) for h in humans]
                hph = ",".join("?" * len(h_ids))
                matched = conn.execute(
                    f"SELECT DISTINCT human_issue_id FROM issue_matches "
                    f"WHERE human_issue_id IN ({hph}) AND confidence >= ? "
                    f"AND matched_by != 'suggested'",
                    [*h_ids, _CATCH_RATE_CONFIDENCE_THRESHOLD],
                ).fetchall()
                matched_count = len(matched)
            else:
                matched_count = 0
        else:
            h_count = 0
            matched_count = 0
        catch_rate = (
            round(matched_count / h_count, 3) if h_count > 0 else None
        )

        out.append(
            {
                "ticket_type": ticket_type,
                "sample_size": sample,
                "first_pass_acceptance_rate": fpa,
                "self_review_catch_rate": catch_rate,
                "defect_escape_rate": defect_escape,
                "merged_count": len(merged_with_at),
            }
        )
    return out


def compute_daily_trend(
    conn: sqlite3.Connection,
    profile: str,
    window_days: int,
    metric: str,
) -> list[tuple[str, float | None, int]]:
    """Return daily buckets for the given metric over window_days.

    metric in ('fpa', 'defect_escape', 'catch_rate').

    Each tuple: (iso_date, value_or_None, sample_count_that_day).

    - fpa: PRs opened that day → first_pass_accepted / sample_for_day
    - defect_escape: PRs merged that day (merged=1, merged_at set) →
      (escaped within window) / merged_for_day
    - catch_rate: human issues created that day → matched/total
    """
    today = datetime.now(UTC).date()
    start_date = today - timedelta(days=window_days - 1)

    # Build a list of (date, rows_that_day)
    buckets: list[tuple[str, float | None, int]] = []

    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    rows = list_pr_runs(conn, client_profile=profile, since_iso=cutoff)

    if metric == "fpa":
        by_day: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            opened = r["opened_at"] or ""
            if len(opened) < 10:
                continue
            day = opened[:10]
            by_day.setdefault(day, []).append(r)
        for i in range(window_days):
            d = (start_date + timedelta(days=i)).isoformat()
            day_rows = by_day.get(d, [])
            n = len(day_rows)
            if n == 0:
                buckets.append((d, None, 0))
            else:
                fp = sum(
                    1 for r in day_rows if int(r["first_pass_accepted"]) == 1
                )
                buckets.append((d, round(fp / n, 3), n))
        return buckets

    if metric == "defect_escape":
        by_day_merged: dict[str, list[int]] = {}
        for r in rows:
            if int(r["merged"]) != 1:
                continue
            merged_at = r["merged_at"] or ""
            if len(merged_at) < 10:
                continue
            day = merged_at[:10]
            by_day_merged.setdefault(day, []).append(int(r["id"]))
        for i in range(window_days):
            d = (start_date + timedelta(days=i)).isoformat()
            ids = by_day_merged.get(d, [])
            n = len(ids)
            if n == 0:
                buckets.append((d, None, 0))
            else:
                escaped = count_merged_pr_runs_with_escape(
                    conn, ids, window_days=30
                )
                buckets.append((d, round(escaped / n, 3), n))
        return buckets

    if metric == "catch_rate":
        pr_ids = [int(r["id"]) for r in rows]
        by_day_humans: dict[str, list[int]] = {}
        if pr_ids:
            ph = ",".join("?" * len(pr_ids))
            hrows = conn.execute(
                f"SELECT id, created_at FROM review_issues "
                f"WHERE pr_run_id IN ({ph}) AND source = 'human_review' "
                f"AND is_valid = 1",
                pr_ids,
            ).fetchall()
            for h in hrows:
                created = h["created_at"] or ""
                if len(created) < 10:
                    continue
                day = created[:10]
                by_day_humans.setdefault(day, []).append(int(h["id"]))
        for i in range(window_days):
            d = (start_date + timedelta(days=i)).isoformat()
            day_ids = by_day_humans.get(d, [])
            n = len(day_ids)
            if n == 0:
                buckets.append((d, None, 0))
            else:
                hph = ",".join("?" * len(day_ids))
                matched = conn.execute(
                    f"SELECT DISTINCT human_issue_id FROM issue_matches "
                    f"WHERE human_issue_id IN ({hph}) AND confidence >= ? "
                    f"AND matched_by != 'suggested'",
                    [*day_ids, _CATCH_RATE_CONFIDENCE_THRESHOLD],
                ).fetchall()
                buckets.append((d, round(len(matched) / n, 3), n))
        return buckets

    raise ValueError(f"unknown metric: {metric!r}")
