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
    """Compute Phase 1 metrics for a single client profile."""
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    rows = list_pr_runs(conn, client_profile=profile, since_iso=cutoff)
    sample_size = len(rows)
    merged_count = sum(1 for r in rows if r["merged"])
    first_pass = sum(1 for r in rows if r["first_pass_accepted"])
    fpa_rate = first_pass / sample_size if sample_size else 0.0
    return {
        "client_profile": profile,
        "sample_size": sample_size,
        "merged_count": merged_count,
        "first_pass_count": first_pass,
        "first_pass_acceptance_rate": round(fpa_rate, 3),
        "defect_escape_rate": None,  # phase 2
        "self_review_catch_rate": None,  # phase 2
        "recommended_mode": "conservative",
        "data_quality_status": "phase1_partial",
        "data_quality_notes": [
            "defect_escape_not_yet_computed",
            "self_review_catch_not_yet_computed",
        ],
        "recent_rows": rows[:20],
    }


def _render_profile_card(metrics: dict[str, Any]) -> str:
    profile = metrics["client_profile"]
    fpa = metrics["first_pass_acceptance_rate"]
    first_pass = metrics["first_pass_count"]
    sample = metrics["sample_size"]
    merged = metrics["merged_count"]
    mode = metrics["recommended_mode"]
    mode_cls = _MODE_BADGE.get(mode, "badge-secondary")
    dq_status = metrics["data_quality_status"]
    dq_badge_cls = "badge-warning" if dq_status != "ok" else "badge-success"

    fpa_line = (
        f"{_fmt_pct(fpa)} ({first_pass} of {sample})" if sample else "— (no data)"
    )

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
        '<span class="metric-value">— (phase 2)</span></div>'
        '<div class="metric-row">'
        '<span class="metric-label">Self-review catch</span>'
        '<span class="metric-value">— (phase 2)</span></div>'
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
        f'<span class="badge {dq_badge_cls}">{_e(dq_status)}</span></span></div>'
        '</div>'
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
</div></body></html>"""

    return HTMLResponse(content=html_doc)
