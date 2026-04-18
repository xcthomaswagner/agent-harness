"""Shared per-profile autonomy metric computation.

Computes Phase 2/3 metrics (self-review catch rate, sidecar coverage, human
issue counts, defect escape rate, data-quality notes, recommended mode) for
a single client profile over a rolling window. Used by both the JSON read
API (autonomy_ingest.GET /api/autonomy) and the HTML dashboard
(autonomy_dashboard).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from autonomy_store import (
    count_merged_pr_runs_with_escape,
    get_latest_defect_sweep_heartbeat,
    list_pr_runs,
)

logger = structlog.get_logger()


# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999 (or higher on newer
# builds). Stay well under it to be safe with IN (?, ?, ...) queries.
_SQLITE_IN_CHUNK_SIZE = 900


def _chunks(items: list[Any], size: int | None = None) -> Iterator[list[Any]]:
    """Yield successive sub-lists of `items` with at most `size` elements each.

    When `size` is None, reads the module-level _SQLITE_IN_CHUNK_SIZE at call
    time (so tests can monkeypatch the constant).
    """
    effective = _SQLITE_IN_CHUNK_SIZE if size is None else size
    if effective <= 0:
        raise ValueError("size must be positive")
    for i in range(0, len(items), effective):
        yield items[i : i + effective]


def _chunked_in_query(
    conn: sqlite3.Connection,
    sql_template: str,
    ids: list[int],
    extra_params: list[Any] | tuple[Any, ...] = (),
) -> list[sqlite3.Row]:
    """Execute ``sql_template`` once per chunk of ``ids`` and collect rows.

    SQLite's default ``SQLITE_MAX_VARIABLE_NUMBER`` is 999, so any
    ``IN (...)`` lookup over more than ~900 IDs has to be chunked.
    Callers used to hand-roll this pattern — build
    ``",".join("?" * len(chunk))``, splice into the SQL, extend a
    collector — and every new metric that needed it copy-pasted the
    same 4-5 lines. Six instances of this shape existed in
    ``compute_profile_metrics`` / ``compute_ticket_type_breakdown``
    / ``compute_daily_trend`` before extraction.

    ``sql_template`` must contain a single ``{placeholders}`` slot
    where the ``?`` list should go. ``extra_params`` are appended to
    each chunk's parameter list so queries with trailing bound
    parameters (e.g. a confidence threshold) still work.
    """
    out: list[sqlite3.Row] = []
    for chunk in _chunks(ids):
        placeholders = ",".join("?" * len(chunk))
        out.extend(
            conn.execute(
                sql_template.format(placeholders=placeholders),
                [*chunk, *extra_params],
            ).fetchall()
        )
    return out


# Tier-4 "suggested" matches don't count toward the self-review catch
# rate — they need manual promotion first. Tier-1..3 matches have
# confidence >= 0.8 by construction.
_CATCH_RATE_CONFIDENCE_THRESHOLD = 0.8


def _count_human_issues_and_matches(
    conn: sqlite3.Connection,
    pr_run_ids: list[int],
) -> tuple[int, int]:
    """Return ``(human_issue_count, matched_human_issue_count)`` for ``pr_run_ids``.

    Centralizes the two-query catch-rate pattern that previously
    lived inline in three metric functions
    (``compute_profile_metrics``, ``compute_ticket_type_breakdown``,
    ``compute_daily_trend``). Each site ran the exact same pair of
    chunked ``IN (...)`` queries:

    1. Select all ``review_issues`` rows with
       ``source = 'human_review' AND is_valid = 1`` whose pr_run_id
       is in the caller's list.
    2. Count distinct ``human_issue_id`` values in ``issue_matches``
       whose confidence passes ``_CATCH_RATE_CONFIDENCE_THRESHOLD``
       and whose ``matched_by`` is not the unpromoted ``suggested``
       tier.

    Returns zero counts when ``pr_run_ids`` is empty. A single
    source of truth here means a future tweak to the catch-rate
    definition (e.g., promoting tier-4 matches, tightening the
    confidence threshold) lands in one place instead of three —
    dashboard views cannot disagree on the metric.
    """
    if not pr_run_ids:
        return 0, 0

    human_rows = _chunked_in_query(
        conn,
        "SELECT id FROM review_issues WHERE pr_run_id IN ({placeholders}) "
        "AND source = 'human_review' AND is_valid = 1",
        pr_run_ids,
    )
    human_count = len(human_rows)
    if not human_rows:
        return 0, 0

    human_ids = [int(h["id"]) for h in human_rows]
    matched_rows = _chunked_in_query(
        conn,
        "SELECT DISTINCT human_issue_id FROM issue_matches "
        "WHERE human_issue_id IN ({placeholders}) AND confidence >= ? "
        "AND matched_by != 'suggested'",
        human_ids,
        extra_params=[_CATCH_RATE_CONFIDENCE_THRESHOLD],
    )
    matched_count = len({int(m["human_issue_id"]) for m in matched_rows})
    return human_count, matched_count


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
    # Exclude backfilled rows from FPA — their first_pass_accepted is unknown,
    # not a confirmed failure. Including them would systematically bias the
    # dashboard toward conservative mode.
    live_rows = [r for r in rows if not int(r["backfilled"])]
    live_count = len(live_rows)
    first_pass_count = sum(1 for r in live_rows if int(r["first_pass_accepted"]) == 1)
    first_pass_acceptance_rate = (
        round(first_pass_count / live_count, 3) if live_count else 0.0
    )

    pr_run_ids = [int(r["id"]) for r in rows]
    if not pr_run_ids:
        human_issue_count = 0
        matched_human_issue_count = 0
        ai_issue_count = 0
        prs_with_ai = 0
    else:
        # Catch rate — single shared helper.
        human_issue_count, matched_human_issue_count = (
            _count_human_issues_and_matches(conn, pr_run_ids)
        )

        # Sidecar coverage (distinct pr_runs with any valid AI/QA issue).
        ai_pr_rows = _chunked_in_query(
            conn,
            "SELECT DISTINCT pr_run_id FROM review_issues "
            "WHERE pr_run_id IN ({placeholders}) "
            "AND source IN ('ai_review', 'qa') AND is_valid = 1",
            pr_run_ids,
        )
        ai_pr_set = {int(r_["pr_run_id"]) for r_ in ai_pr_rows}
        ai_count_rows = _chunked_in_query(
            conn,
            "SELECT COUNT(*) AS n FROM review_issues "
            "WHERE pr_run_id IN ({placeholders}) "
            "AND source IN ('ai_review', 'qa') AND is_valid = 1",
            pr_run_ids,
        )
        ai_issue_count = sum(int(r_["n"]) for r_ in ai_count_rows)
        prs_with_ai = len(ai_pr_set)

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
        # Exclude backfilled rows from FPA — same rationale as
        # compute_profile_metrics: backfilled ``first_pass_accepted``
        # is unknown (defaults to 0), not a confirmed failure, so
        # counting them would systematically bias the by-type
        # dashboard toward conservative mode. Before this filter the
        # overall FPA panel and the by-type panel could show
        # contradictory numbers for any profile with backfilled
        # history.
        live_group = [g for g in group if not int(g["backfilled"])]
        sample = len(live_group)
        fp = sum(1 for g in live_group if int(g["first_pass_accepted"]) == 1)
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
        h_count, matched_count = _count_human_issues_and_matches(
            conn, pr_ids
        )
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
            hrows = _chunked_in_query(
                conn,
                "SELECT id, created_at FROM review_issues "
                "WHERE pr_run_id IN ({placeholders}) AND source = 'human_review' "
                "AND is_valid = 1",
                pr_ids,
            )
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
                matched_rows = _chunked_in_query(
                    conn,
                    "SELECT DISTINCT human_issue_id FROM issue_matches "
                    "WHERE human_issue_id IN ({placeholders}) AND confidence >= ? "
                    "AND matched_by != 'suggested'",
                    day_ids,
                    extra_params=[_CATCH_RATE_CONFIDENCE_THRESHOLD],
                )
                matched_count = len(
                    {int(m["human_issue_id"]) for m in matched_rows}
                )
                buckets.append((d, round(matched_count / n, 3), n))
        return buckets

    raise ValueError(f"unknown metric: {metric!r}")


def compute_rolling_trend(
    conn: sqlite3.Connection,
    profile: str,
    window_days: int,
    metric: str,
    *,
    smoothing_window: int = 7,
) -> list[tuple[str, float | None, int]]:
    """Return a smoothed daily trend (rolling average over trailing N days).

    For each day D in the last `window_days`, aggregates samples from
    (D - smoothing_window + 1 .. D) inclusive and computes a single rate:
      - sum of numerators (rate * sample_count) over the window
      - divided by sum of sample_counts
    If the window has zero samples, the smoothed value is None.

    Returns list of (iso_date, smoothed_value_or_None, samples_in_window).
    """
    if smoothing_window < 1:
        raise ValueError("smoothing_window must be >= 1")
    # Fetch enough history to have a full trailing window for each day in
    # the visible range.
    lookback_days = window_days + smoothing_window - 1
    raw = compute_daily_trend(conn, profile, lookback_days, metric)
    # Keep only days with a full trailing window.
    smoothed: list[tuple[str, float | None, int]] = []
    for i in range(smoothing_window - 1, len(raw)):
        window_slice = raw[i - smoothing_window + 1 : i + 1]
        num = 0.0
        denom = 0
        for (_d, v, n) in window_slice:
            if v is not None:
                num += v * n
            denom += n
        date_str = raw[i][0]
        if denom == 0:
            smoothed.append((date_str, None, 0))
        else:
            smoothed.append((date_str, round(num / denom, 3), denom))
    return smoothed
