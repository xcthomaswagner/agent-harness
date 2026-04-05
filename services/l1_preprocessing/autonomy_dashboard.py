"""Autonomy metrics dashboard — per-profile cards at /autonomy.

Renders a server-side HTML dashboard showing autonomy metrics scoped per
client_profile. The "All" view (no client_profile filter) renders per-profile
cards side-by-side and MUST NOT display a single averaged headline metric
(§14a.3 + §15 isolation rule).
"""

from __future__ import annotations

import html
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from autonomy_metrics import compute_profile_metrics
from autonomy_store import (
    ensure_schema,
    list_client_profiles,
    list_pr_runs,
    open_connection,
    resolve_db_path,
)
from config import settings

logger = structlog.get_logger()

router = APIRouter()


# --- Langfuse Design System (copied from trace_dashboard) ---

_LANGFUSE_STYLES = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-feature-settings: "rlig" 1, "calt" 1;
  background: #FFFFFF; color: #0F172A; font-size: 13.2px; line-height: 1.5;
}
a { color: #4D45E5; text-decoration: none; }
a:hover { text-decoration: underline; }
.page { max-width: 1400px; margin: 0 auto; padding: 24px; }
h1 { font-size: 20.8px; font-weight: 600; color: #0F172A; }
h2 { font-size: 15px; font-weight: 600; color: #0F172A; margin-bottom: 8px; }
.meta { font-size: 11.2px; color: #64748B; }
.badge {
  display: inline-flex; align-items: center; border-radius: 6px;
  font-weight: 600; font-size: 11.2px; padding: 1px 8px; white-space: nowrap;
}
.badge-success { background: #DBFBE7; color: #124D49; }
.badge-error { background: #FBE6F1; color: #DB2626; }
.badge-warning { background: #FEFCE8; color: #C79004; }
.badge-blue { background: #DAEAFD; color: #3B82F5; }
.badge-secondary { background: #F1F5F9; color: #0F172A; }
.card {
  border: 1px solid #E2E8F0; border-radius: 8px; padding: 16px;
  background: #FFFFFF; margin-bottom: 12px;
}
.card-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px; margin: 16px 0;
}
.metric-row {
  display: flex; justify-content: space-between; padding: 4px 0;
  font-size: 12.5px;
}
.metric-label { color: #64748B; }
.metric-value { font-weight: 600; color: #0F172A; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }
thead th {
  text-align: left; padding: 8px 12px; border-bottom: 1px solid #E2E8F0;
  font-weight: 600; color: #64748B; background: #F8FAFC;
}
tbody td { padding: 8px 12px; border-bottom: 1px solid #E2E8F0; vertical-align: middle; }
tbody tr:last-child td { border-bottom: none; }
.selector { margin: 16px 0; font-size: 12.5px; }
.selector a { margin: 0 4px; }
.selector .sep { color: #CBD5E1; }
"""


_MODE_BADGE: dict[str, str] = {
    "conservative": "badge-secondary",
    "semi_autonomous": "badge-warning",
    "full_autonomous": "badge-success",
}


def _e(text: Any) -> str:
    """HTML-escape a value."""
    return html.escape(str(text), quote=True)


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.0f}%"


def _compute_profile_metrics(
    conn: sqlite3.Connection, profile: str, window_days: int
) -> dict[str, Any]:
    """Compute Phase 2 metrics for a single client profile (dashboard view)."""
    return compute_profile_metrics(conn, profile, window_days)


_DQ_STATUS_BADGE: dict[str, str] = {
    "good": "badge-success",
    "degraded": "badge-warning",
    "insufficient_data": "badge-secondary",
}


def _catch_rate_badge_class(value: float | None) -> str:
    if value is None:
        return "badge-secondary"
    if value >= 0.85:
        return "badge-success"
    if value >= 0.70:
        return "badge-warning"
    return "badge-error"


def _sidecar_badge_class(value: float) -> str:
    if value >= 0.8:
        return "badge-success"
    if value >= 0.5:
        return "badge-warning"
    return "badge-error"


def _render_profile_card(metrics: dict[str, Any]) -> str:
    profile = metrics["client_profile"]
    fpa = metrics["first_pass_acceptance_rate"]
    first_pass = metrics["first_pass_count"]
    sample = metrics["sample_size"]
    merged = metrics["merged_count"]
    mode = metrics["recommended_mode"]
    mode_cls = _MODE_BADGE.get(mode, "badge-secondary")

    catch = metrics["self_review_catch_rate"]
    human_count = metrics["human_issue_count"]
    matched_count = metrics["matched_human_issue_count"]
    unmatched_count = metrics["unmatched_human_issue_count"]
    sidecar_cov = metrics["sidecar_coverage"]
    dq_status = metrics["data_quality_status"]
    dq_notes: list[str] = list(metrics["data_quality_notes"])

    dq_badge_cls = _DQ_STATUS_BADGE.get(dq_status, "badge-warning")

    fpa_line = (
        f"{_fmt_pct(fpa)} ({first_pass} of {sample})" if sample else "— (no data)"
    )

    catch_cls = _catch_rate_badge_class(catch)
    catch_display = _fmt_pct(catch)
    sidecar_cls = _sidecar_badge_class(sidecar_cov)
    sidecar_display = _fmt_pct(sidecar_cov)

    humans_line = (
        f"{matched_count}/{human_count} matched"
        + (f" ({unmatched_count} unmatched)" if unmatched_count > 0 else "")
        if human_count > 0
        else "— (no human issues)"
    )

    if dq_notes:
        notes_html = " ".join(
            f'<span class="badge badge-warning" '
            f'style="margin-left:4px">{_e(n)}</span>'
            for n in dq_notes
        )
    else:
        notes_html = ""

    return (
        '<div class="card">'
        f'<h2>{_e(profile)}</h2>'
        '<div class="metric-row">'
        '<span class="metric-label">First-pass acceptance</span>'
        f'<span class="metric-value">{_e(fpa_line)}</span></div>'
        '<div class="metric-row">'
        '<span class="metric-label">Merged</span>'
        f'<span class="metric-value">{merged}</span></div>'
        '<div class="metric-row">'
        '<span class="metric-label">Defect escape</span>'
        '<span class="metric-value">— (phase 3)</span></div>'
        '<div class="metric-row">'
        '<span class="metric-label">Self-review catch</span>'
        f'<span class="metric-value">'
        f'<span class="badge {catch_cls}">{_e(catch_display)}</span>'
        '</span></div>'
        '<div class="metric-row">'
        '<span class="metric-label">Sidecar coverage</span>'
        f'<span class="metric-value">'
        f'<span class="badge {sidecar_cls}">{_e(sidecar_display)}</span>'
        '</span></div>'
        '<div class="metric-row">'
        '<span class="metric-label">Human issues</span>'
        f'<span class="metric-value">{_e(humans_line)}</span></div>'
        '<div class="metric-row">'
        '<span class="metric-label">Sample size</span>'
        f'<span class="metric-value">{sample} PRs</span></div>'
        '<div class="metric-row">'
        '<span class="metric-label">Recommended mode</span>'
        f'<span class="metric-value">'
        f'<span class="badge {mode_cls}">{_e(mode)}</span></span></div>'
        '<div class="metric-row">'
        '<span class="metric-label">Data quality</span>'
        f'<span class="metric-value">'
        f'<span class="badge {dq_badge_cls}">{_e(dq_status)}</span>'
        f'{notes_html}'
        '</span></div>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Unmatched human issues + suggested matches sections
# ---------------------------------------------------------------------------

def _query_unmatched_human_issues(
    conn: sqlite3.Connection,
    pr_run_ids: list[int],
    limit: int = 25,
) -> list[sqlite3.Row]:
    """Return up to `limit` human-review issues (pr_run_ids scope) that have
    no qualifying issue_matches row (confidence >= 0.8 and not 'suggested').
    """
    if not pr_run_ids:
        return []
    placeholders = ",".join("?" * len(pr_run_ids))
    sql = f"""
        SELECT ri.id, ri.pr_run_id, ri.file_path, ri.line_start, ri.line_end,
               ri.summary, ri.created_at,
               p.pr_number, p.pr_url, p.client_profile, p.ticket_id
        FROM review_issues ri
        JOIN pr_runs p ON p.id = ri.pr_run_id
        WHERE ri.pr_run_id IN ({placeholders})
          AND ri.source = 'human_review'
          AND ri.is_valid = 1
          AND NOT EXISTS (
              SELECT 1 FROM issue_matches m
              WHERE m.human_issue_id = ri.id
                AND m.confidence >= 0.8
                AND m.matched_by != 'suggested'
          )
        ORDER BY ri.created_at DESC, ri.id DESC
        LIMIT ?
    """
    return list(conn.execute(sql, [*pr_run_ids, limit]).fetchall())


def _query_suggested_matches(
    conn: sqlite3.Connection,
    pr_run_ids: list[int],
    limit: int = 25,
) -> list[sqlite3.Row]:
    """Return Tier-4 suggested matches (matched_by='suggested' and
    confidence < 0.8) joined with both sides' summaries, scoped to
    pr_runs in `pr_run_ids`.
    """
    if not pr_run_ids:
        return []
    placeholders = ",".join("?" * len(pr_run_ids))
    sql = f"""
        SELECT m.id AS match_id, m.confidence, m.matched_at,
               h.id AS human_id, h.summary AS human_summary,
               h.acceptance_criterion_ref AS ac_ref,
               a.id AS ai_id, a.summary AS ai_summary,
               p.pr_number, p.pr_url, p.client_profile, p.ticket_id
        FROM issue_matches m
        JOIN review_issues h ON h.id = m.human_issue_id
        JOIN review_issues a ON a.id = m.ai_issue_id
        JOIN pr_runs p ON p.id = h.pr_run_id
        WHERE m.matched_by = 'suggested'
          AND m.confidence < 0.8
          AND h.pr_run_id IN ({placeholders})
        ORDER BY m.matched_at DESC, m.id DESC
        LIMIT ?
    """
    return list(conn.execute(sql, [*pr_run_ids, limit]).fetchall())


def _truncate(text: str, max_len: int = 120) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _render_unmatched_section(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return ""
    body_parts: list[str] = []
    for r in rows:
        pr_url = r["pr_url"] or ""
        pr_num = r["pr_number"]
        pr_cell = (
            f'<a href="{_e(pr_url)}">#{pr_num}</a>' if pr_url else f"#{pr_num}"
        )
        profile = _e(r["client_profile"] or "—")
        file_path = _e(r["file_path"] or "—")
        line_start = int(r["line_start"] or 0)
        line_end = int(r["line_end"] or 0)
        if line_start == 0 and line_end == 0:
            lines = "—"
        elif line_start == line_end or line_end == 0:
            lines = str(line_start)
        else:
            lines = f"{line_start}-{line_end}"
        summary = _e(_truncate(r["summary"] or ""))
        opened = _e((r["created_at"] or "")[:19])
        body_parts.append(
            f"<tr><td>{pr_cell}</td><td>{profile}</td><td>{file_path}</td>"
            f"<td>{_e(lines)}</td><td>{summary}</td><td>{opened}</td></tr>"
        )
    body = "".join(body_parts)
    return (
        '<h2 style="margin-top:24px">Unmatched Human Issues</h2>'
        '<table><thead><tr>'
        '<th>PR</th><th>Profile</th><th>File</th><th>Lines</th>'
        '<th>Summary</th><th>Opened</th>'
        '</tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


def _render_suggested_section(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return ""
    body_parts: list[str] = []
    for r in rows:
        human_summary = _e(_truncate(r["human_summary"] or ""))
        ai_summary = _e(_truncate(r["ai_summary"] or ""))
        ac_ref = _e(r["ac_ref"] or "—")
        confidence = float(r["confidence"] or 0.0)
        conf_cell = f"{confidence:.2f}"
        body_parts.append(
            f"<tr><td>{human_summary}</td><td>{ai_summary}</td>"
            f"<td>{ac_ref}</td><td>{_e(conf_cell)}</td></tr>"
        )
    body = "".join(body_parts)
    return (
        '<h2 style="margin-top:24px">Suggested Matches (Tier 4)</h2>'
        '<p class="meta">promote via POST /api/autonomy/manual-match '
        '(Phase 3)</p>'
        '<table><thead><tr>'
        '<th>Human issue</th><th>AI issue</th><th>AC ref</th>'
        '<th>Confidence</th>'
        '</tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


def _render_pr_row(row: sqlite3.Row) -> str:
    ticket = _e(row["ticket_id"])
    pr_url = row["pr_url"] or ""
    pr_number = row["pr_number"]
    pr_cell = (
        f'<a href="{_e(pr_url)}">#{pr_number}</a>' if pr_url else f"#{pr_number}"
    )
    profile = _e(row["client_profile"] or "—")
    opened = _e((row["opened_at"] or "")[:19])
    approved = "✓" if row["approved_at"] else "—"
    merged = "✓" if row["merged"] else "—"
    first_pass = (
        '<span class="badge badge-success">yes</span>'
        if row["first_pass_accepted"]
        else '<span class="badge badge-secondary">no</span>'
    )
    if row["merged"]:
        status = '<span class="badge badge-success">merged</span>'
    elif row["escalated"]:
        status = '<span class="badge badge-error">escalated</span>'
    elif row["closed_at"]:
        status = '<span class="badge badge-secondary">closed</span>'
    else:
        status = '<span class="badge badge-blue">open</span>'
    return (
        f"<tr><td>{ticket}</td><td>{pr_cell}</td><td>{profile}</td>"
        f"<td>{opened}</td><td>{approved}</td><td>{merged}</td>"
        f"<td>{first_pass}</td><td>{status}</td></tr>"
    )


def _render_pr_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return '<p class="meta">No PR runs in window.</p>'
    body = "".join(_render_pr_row(r) for r in rows[:50])
    return (
        '<table><thead><tr>'
        '<th>Ticket</th><th>PR</th><th>Profile</th><th>Opened</th>'
        '<th>Approved</th><th>Merged</th><th>First-Pass</th><th>Status</th>'
        '</tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


def _render_selector(profiles: list[str], selected: str | None) -> str:
    parts: list[str] = ['<strong>Project:</strong> ']
    all_style = ' style="font-weight:600"' if selected is None else ""
    parts.append(f'<a href="/autonomy"{all_style}>All</a>')
    for p in profiles:
        parts.append('<span class="sep">|</span>')
        sel_style = ' style="font-weight:600"' if p == selected else ""
        parts.append(
            f'<a href="/autonomy?client_profile={_e(p)}"{sel_style}>{_e(p)}</a>'
        )
    return '<div class="selector">' + " ".join(parts) + "</div>"


@router.get("/autonomy", response_class=HTMLResponse)
def autonomy_dashboard(
    client_profile: str | None = None,
    window_days: int = 30,
) -> HTMLResponse:
    """Render the autonomy metrics dashboard."""
    db_path = resolve_db_path(settings.autonomy_db_path)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        all_profiles = list_client_profiles(conn)

        profiles_to_show = (
            [client_profile] if client_profile is not None else all_profiles
        )

        profile_metrics = [
            _compute_profile_metrics(conn, p, window_days) for p in profiles_to_show
        ]

        # Build recent PR table scope
        cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
        if client_profile is not None:
            table_rows = list_pr_runs(
                conn, client_profile=client_profile, since_iso=cutoff
            )
        else:
            table_rows = list_pr_runs(conn, since_iso=cutoff)

        # Scope for unmatched/suggested sections = all pr_runs shown in cards.
        scoped_pr_run_ids: list[int] = []
        for m in profile_metrics:
            for r in m.get("recent_rows", []):
                scoped_pr_run_ids.append(int(r["id"]))
        # recent_rows on each card is capped at 20; for the issue sections
        # we want the full window scope, so recompute via table_rows too.
        scoped_pr_run_ids = [int(r["id"]) for r in table_rows]

        unmatched_rows = _query_unmatched_human_issues(conn, scoped_pr_run_ids)
        suggested_rows = _query_suggested_matches(conn, scoped_pr_run_ids)
    finally:
        conn.close()

    cards_html = "".join(_render_profile_card(m) for m in profile_metrics)
    if not profile_metrics:
        cards_html = (
            '<p class="meta">No client profiles with PR runs in the last '
            f'{window_days} days.</p>'
        )

    # Aggregate count only (NOT an averaged metric) — safe per §14a.3
    if client_profile is None and all_profiles:
        total_prs = sum(m["sample_size"] for m in profile_metrics)
        summary_line = (
            f'<p class="meta">Total: {len(all_profiles)} profiles, '
            f'{total_prs} PRs (last {window_days} days)</p>'
        )
    else:
        summary_line = ""

    selector_html = _render_selector(all_profiles, client_profile)
    table_html = _render_pr_table(table_rows)
    unmatched_html = _render_unmatched_section(unmatched_rows)
    suggested_html = _render_suggested_section(suggested_rows)

    title_suffix = (
        f" — {_e(client_profile)}" if client_profile is not None else " — All"
    )

    html_doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Autonomy Metrics</title>
<style>{_LANGFUSE_STYLES}</style></head><body><div class="page">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
  <h1>Autonomy Metrics{title_suffix}</h1>
  <div class="meta">
    <a href="/traces">Traces</a> <span class="sep">|</span>
    <a href="/autonomy">Autonomy</a>
  </div>
</div>
{selector_html}
{summary_line}
<div class="card-grid">{cards_html}</div>
<h2 style="margin-top:24px">Recent PR Outcomes</h2>
{table_html}
{unmatched_html}
{suggested_html}
</div></body></html>"""

    return HTMLResponse(content=html_doc)
