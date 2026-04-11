"""Unified landing page at /dashboard — summary of autonomy + traces."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import structlog
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from autonomy_metrics import compute_profile_metrics
from autonomy_store import (
    ensure_schema,
    get_auto_merge_toggle,
    list_client_profiles,
    list_recent_auto_merge_decisions,
    open_connection,
    resolve_db_path,
)
from client_profile import load_profile
from config import settings
from dashboard_common import (
    badge as _badge,
)
from dashboard_common import (
    escape_html as _e,
)
from dashboard_common import (
    fmt_pct as _fmt_pct,
)
from dashboard_common import (
    fmt_ts as _fmt_ts,
)
from dashboard_common import (
    safe_url as _safe_url,
)
from tracer import build_trace_list_row, list_traces, read_trace

logger = structlog.get_logger()

router = APIRouter()

# ---------------------------------------------------------------------------
# Styles — copied from trace_dashboard to avoid coupling
# ---------------------------------------------------------------------------

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
  display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px; margin: 16px 0;
}
.metric-row {
  display: flex; justify-content: space-between; padding: 2px 0;
  font-size: 12px;
}
.metric-label { color: #64748B; }
.metric-value { font-weight: 600; color: #0F172A; }
table { width: 100%; border-collapse: separate; border-spacing: 0;
  border: 1px solid #E2E8F0; border-radius: 8px; overflow: hidden;
  margin-top: 12px; font-size: 12px; }
thead th {
  background: #F7F9FB; color: #64748B; font-weight: 500; font-size: 11.2px;
  text-align: left; padding: 10px 12px; border-bottom: 1px solid #E2E8F0;
  white-space: nowrap;
}
tbody td { padding: 8px 12px; border-bottom: 1px solid #E2E8F0; vertical-align: middle; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: rgba(241,245,249,0.5); }
.nav-link { font-size: 12.5px; padding: 4px 10px; border-radius: 6px; }
.nav-link.active { background: #0F172A; color: #F7F9FB; font-weight: 600; }
"""

# Status badge mapping — imported from dashboard_common so the mapping
# here can't drift from trace_dashboard's. Previously this copy was
# missing Failed / Timed Out / Cleaned Up (silently rendered as the
# secondary-fallback class), which the shared module now fixes.
from dashboard_common import STATUS_BADGE as _STATUS_BADGE  # noqa: E402

_MODE_BADGE: dict[str, str] = {
    "conservative": "badge-secondary",
    "semi_autonomous": "badge-warning",
    "full_autonomous": "badge-success",
}

_AUTO_MERGE_DECISION_BADGE: dict[str, str] = {
    "merged": "badge-success",
    "dry_run": "badge-blue",
    "skipped": "badge-secondary",
    "failed": "badge-error",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# _e, _safe_url, _fmt_ts, _fmt_dur, _badge, _fmt_pct are imported from
# dashboard_common at the top of this file. Historically they were
# duplicated across four dashboard modules.


def _resolve_auto_merge_label(
    conn: sqlite3.Connection, profile: str,
) -> str:
    """Return compact auto-merge label: ENABLED or DRY-RUN."""
    runtime_toggle = get_auto_merge_toggle(conn, profile)
    if runtime_toggle is not None:
        return "ENABLED" if bool(runtime_toggle) else "DRY-RUN"
    yaml_profile = load_profile(profile)
    if yaml_profile is not None and yaml_profile.auto_merge_enabled:
        return "ENABLED"
    return "DRY-RUN"


def _truncate(text: str, max_len: int = 100) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "\u2026"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_nav(active: str = "dashboard") -> str:
    """Render the top navigation bar."""
    links = [
        ("Dashboard", "/dashboard"),
        ("Traces", "/traces"),
        ("Autonomy", "/autonomy"),
    ]
    parts: list[str] = []
    for label, href in links:
        cls = "nav-link active" if label.lower() == active else "nav-link"
        parts.append(f'<a href="{href}" class="{cls}">{_e(label)}</a>')
    return " ".join(parts)


def _render_autonomy_strip(conn: sqlite3.Connection) -> str:
    """Render compact profile summary cards in a horizontal strip."""
    profiles = list_client_profiles(conn)
    if not profiles:
        return '<p class="meta">No autonomy data yet.</p>'

    cards_html = ""
    for profile in profiles:
        metrics = compute_profile_metrics(conn, profile, window_days=30)
        fpa = _fmt_pct(metrics["first_pass_acceptance_rate"])
        escape = _fmt_pct(metrics.get("defect_escape_rate"))
        catch = _fmt_pct(metrics["self_review_catch_rate"])
        sample = metrics["sample_size"]
        mode = metrics["recommended_mode"]
        mode_cls = _MODE_BADGE.get(mode, "badge-secondary")
        auto_merge = _resolve_auto_merge_label(conn, profile)
        am_cls = "badge-success" if auto_merge == "ENABLED" else "badge-blue"

        cards_html += (
            '<div class="card" style="padding:12px">'
            f'<h2 style="font-size:13px;margin-bottom:6px">{_e(profile)}</h2>'
            '<div class="metric-row">'
            f'<span class="metric-label">FPA: {_e(fpa)}</span>'
            f'<span class="metric-label">Escape: {_e(escape)}</span>'
            f'<span class="metric-label">Catch: {_e(catch)}</span>'
            "</div>"
            '<div class="metric-row">'
            f'<span class="metric-value">{sample} PRs</span>'
            f'<span>{_badge(mode, mode_cls)}</span>'
            "</div>"
            '<div class="metric-row">'
            f'<span class="metric-label">Auto-merge:</span>'
            f'<span>{_badge(auto_merge, am_cls)}</span>'
            "</div>"
            "</div>"
        )

    return (
        f'<div class="card-grid">{cards_html}</div>'
        '<div class="meta" style="margin-top:4px">'
        '<a href="/autonomy">View full metrics &rarr;</a></div>'
    )


def _render_recent_traces() -> str:
    """Render a compact 20-row trace table."""
    traces = list_traces(offset=0, limit=20)
    if not traces:
        return '<p class="meta">No traces yet.</p>'

    enriched: list[dict[str, Any]] = []
    for t in traces:
        entries = t.pop("_raw_entries", None) or read_trace(t["ticket_id"])
        run_start_idx = t.pop("_run_start_idx", None)
        enriched.append(
            build_trace_list_row(t, entries, run_start_idx=run_start_idx)
        )

    rows_html = ""
    for t in enriched:
        tid = _e(t["ticket_id"])
        status = t.get("status", "")
        badge_cls = _STATUS_BADGE.get(status, "badge-secondary")
        mode = t.get("pipeline_mode", "")
        mode_html = _badge(mode, "badge-secondary") if mode else '<span class="meta">&mdash;</span>'
        pr = t.get("pr_url", "")
        pr_html = (
            f'<a href="{_e(_safe_url(pr))}" target="_blank" style="font-size:11.2px">'
            f'#{_e(pr.split("/")[-1])}</a>'
            if pr
            else '<span class="meta">&mdash;</span>'
        )
        duration = t.get("duration", "")
        dur_html = _e(duration) if duration else '<span class="meta">&mdash;</span>'
        started = _fmt_ts(t.get("started_at", ""))

        rows_html += (
            f'<tr onclick="location.href=\'/traces/{tid}\'" style="cursor:pointer">'
            f'<td><a href="/traces/{tid}" style="font-weight:500">{tid}</a></td>'
            f"<td>{_badge(status, badge_cls)}</td>"
            f"<td>{mode_html}</td>"
            f"<td>{pr_html}</td>"
            f"<td>{_e(dur_html)}</td>"
            f'<td class="meta">{_e(started)}</td>'
            "</tr>"
        )

    return (
        "<table>"
        "<thead><tr>"
        '<th style="width:110px">Ticket</th>'
        '<th style="width:130px">Status</th>'
        '<th style="width:80px">Mode</th>'
        '<th style="width:50px">PR</th>'
        '<th style="width:80px">Duration</th>'
        "<th>Started</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
        '<div class="meta" style="margin-top:8px">'
        '<a href="/traces">View all traces &rarr;</a></div>'
    )


def _render_auto_merge_decisions(conn: sqlite3.Connection) -> str:
    """Render last 5 auto-merge decisions. Returns empty string if none."""
    rows = list_recent_auto_merge_decisions(conn, limit=5)
    if not rows:
        return ""

    body_parts: list[str] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        created_at = _e((r["created_at"] or "")[:19])
        target_id = str(r["target_id"] or "")
        # Format target as PR link if possible
        if "#" in target_id:
            repo_part, _, pr_part = target_id.partition("#")
            if repo_part and pr_part and pr_part.isdigit():
                target_cell = (
                    f'<a href="https://github.com/{_e(repo_part)}/pull/{_e(pr_part)}"'
                    f' target="_blank">#{_e(pr_part)}</a>'
                )
            else:
                target_cell = _e(target_id)
        else:
            target_cell = _e(target_id or "\u2014")

        decision = str(payload.get("decision") or "")
        d_cls = _AUTO_MERGE_DECISION_BADGE.get(decision, "badge-secondary")
        reason = _e(_truncate(str(payload.get("reason") or ""), 80))

        body_parts.append(
            f"<tr><td>{created_at}</td><td>{target_cell}</td>"
            f'<td>{_badge(decision or "\u2014", d_cls)}</td>'
            f"<td>{reason}</td></tr>"
        )

    body = "".join(body_parts)
    return (
        '<h2 style="margin-top:24px">Recent Auto-merge Decisions</h2>'
        "<table><thead><tr>"
        "<th>Time</th><th>PR</th><th>Decision</th><th>Reason</th>"
        "</tr></thead>"
        f"<tbody>{body}</tbody></table>"
        '<div class="meta" style="margin-top:8px">'
        '<a href="/autonomy">View all decisions &rarr;</a></div>'
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=RedirectResponse)
async def root_redirect() -> RedirectResponse:
    """Redirect root to the unified dashboard."""
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/dashboard", response_class=HTMLResponse)
async def unified_dashboard() -> str:
    """Unified landing page combining autonomy summary and recent traces."""
    conn = open_connection(resolve_db_path(settings.autonomy_db_path))
    try:
        ensure_schema(conn)
        autonomy_strip = _render_autonomy_strip(conn)
        auto_merge_html = _render_auto_merge_decisions(conn)
    finally:
        conn.close()

    traces_html = _render_recent_traces()
    nav_html = _render_nav("dashboard")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Dashboard</title>
<style>{_LANGFUSE_STYLES}</style></head><body><div class="page">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
  <h1>Agentic Developer Harness</h1>
  <div>{nav_html}</div>
</div>
<h2>Autonomy Summary</h2>
{autonomy_strip}
<h2 style="margin-top:24px">Recent Traces</h2>
{traces_html}
{auto_merge_html}
</div></body></html>"""
