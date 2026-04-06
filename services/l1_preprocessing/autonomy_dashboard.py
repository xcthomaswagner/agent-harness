"""Autonomy metrics dashboard — per-profile cards at /autonomy.

Renders a server-side HTML dashboard showing autonomy metrics scoped per
client_profile. The "All" view (no client_profile filter) renders per-profile
cards side-by-side and MUST NOT display a single averaged headline metric
(§14a.3 + §15 isolation rule).
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from autonomy_metrics import _chunks, compute_profile_metrics
from autonomy_store import (
    ensure_schema,
    get_auto_merge_toggle,
    list_client_profiles,
    list_defect_links_for_profile,
    list_pr_runs,
    list_recent_auto_merge_decisions,
    list_review_issues_by_pr_run,
    open_connection,
    resolve_db_path,
)
from client_profile import load_profile
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


def _defect_escape_badge_class(value: float | None) -> str:
    if value is None:
        return "badge-secondary"
    if value <= 0.03:
        return "badge-success"
    if value <= 0.05:
        return "badge-warning"
    return "badge-error"


def _defect_escape_display(value: float | None) -> str:
    if value is None:
        return "— (unknown)"
    return f"{value * 100:.1f}%"


def _sidecar_badge_class(value: float) -> str:
    if value >= 0.8:
        return "badge-success"
    if value >= 0.5:
        return "badge-warning"
    return "badge-error"


def _resolve_auto_merge_state(
    conn: sqlite3.Connection, profile: str
) -> tuple[bool, str]:
    """Return (enabled, source) for a profile's effective auto-merge state.

    Precedence: runtime toggle (if ever set) > YAML auto_merge_enabled > False.
    """
    runtime_toggle = get_auto_merge_toggle(conn, profile)
    if runtime_toggle is not None:
        return bool(runtime_toggle), "runtime_toggle"
    yaml_profile = load_profile(profile)
    if yaml_profile is not None:
        return bool(yaml_profile.auto_merge_enabled), "yaml"
    return False, "yaml"


def _render_auto_merge_state_block(profile: str, enabled: bool, source: str) -> str:
    label = "ENABLED" if enabled else "DRY-RUN"
    badge_cls = "badge-success" if enabled else "badge-blue"
    curl_snippet = (
        "curl -X POST localhost:8000/api/autonomy/auto-merge-toggle \\\n"
        "  -H 'X-Autonomy-Admin-Token: $TOKEN' \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        f"  -d '{{\"client_profile\":\"{profile}\",\"enabled\":true}}'"
    )
    curl_html = (
        '<pre style="font-size:10.5px;background:#F8FAFC;padding:4px 8px;'
        'border-radius:4px;margin:4px 0 0 0;white-space:pre-wrap;'
        f'word-break:break-all">{_e(curl_snippet)}</pre>'
    )
    return (
        '<div class="metric-row">'
        '<span class="metric-label">Auto-merge</span>'
        f'<span class="metric-value">'
        f'<span class="badge {badge_cls}">{_e(label)}</span>'
        f' <span class="meta">(source: {_e(source)})</span>'
        f'</span></div>'
        f'{curl_html}'
    )


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

    defect_escape = metrics.get("defect_escape_rate")
    defect_escape_cls = _defect_escape_badge_class(defect_escape)
    defect_escape_text = _defect_escape_display(defect_escape)
    sparkline_html = metrics.get("_sparkline_html", "")
    auto_merge_html = metrics.get("_auto_merge_html", "")

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
        f'<span class="metric-value">'
        f'<span class="badge {defect_escape_cls}">{_e(defect_escape_text)}</span>'
        '</span></div>'
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
        f'{auto_merge_html}'
        f'{sparkline_html}'
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
    collected: list[sqlite3.Row] = []
    for chunk in _chunks(pr_run_ids):
        placeholders = ",".join("?" * len(chunk))
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
        collected.extend(conn.execute(sql, [*chunk, limit]).fetchall())
    collected.sort(
        key=lambda r: ((r["created_at"] or ""), int(r["id"])), reverse=True
    )
    return collected[:limit]


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
    collected: list[sqlite3.Row] = []
    for chunk in _chunks(pr_run_ids):
        placeholders = ",".join("?" * len(chunk))
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
        collected.extend(conn.execute(sql, [*chunk, limit]).fetchall())
    collected.sort(
        key=lambda r: ((r["matched_at"] or ""), int(r["match_id"])), reverse=True
    )
    return collected[:limit]


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
        match_id = int(r["match_id"])
        curl_snippet = (
            "curl -X POST localhost:8000/api/autonomy/manual-match \\\n"
            "  -H 'X-Autonomy-Admin-Token: $TOKEN' \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"mode\":\"promote\",\"match_id\":{match_id}}}'"
        )
        promote_cell = (
            '<pre style="font-size:10.5px;background:#F8FAFC;padding:4px 8px;'
            'border-radius:4px;margin:0;white-space:pre-wrap;'
            f'word-break:break-all">{_e(curl_snippet)}</pre>'
        )
        body_parts.append(
            f"<tr><td>{human_summary}</td><td>{ai_summary}</td>"
            f"<td>{ac_ref}</td><td>{_e(conf_cell)}</td>"
            f"<td>{promote_cell}</td></tr>"
        )
    body = "".join(body_parts)
    return (
        '<h2 style="margin-top:24px">Suggested Matches (Tier 4)</h2>'
        '<p class="meta">promote via POST /api/autonomy/manual-match</p>'
        '<table><thead><tr>'
        '<th>Human issue</th><th>AI issue</th><th>AC ref</th>'
        '<th>Confidence</th><th>Promote</th>'
        '</tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


def _render_escaped_defects_section(
    conn: sqlite3.Connection,
    profiles: list[str],
    window_days: int,
    limit: int = 25,
) -> str:
    """Render escaped defects table for the given profiles.

    Queries list_defect_links_for_profile per profile, filters to
    confirmed=1 AND category='escaped'. Empty profiles → short message.
    """
    since_iso = (
        datetime.now(UTC) - timedelta(days=window_days)
    ).isoformat()
    all_rows: list[sqlite3.Row] = []
    for p in profiles:
        rows = list_defect_links_for_profile(
            conn, p, since_iso=since_iso, limit=limit
        )
        all_rows.extend(rows)

    escaped = [
        r for r in all_rows
        if int(r["confirmed"]) == 1 and r["category"] == "escaped"
    ]
    escaped.sort(key=lambda r: r["reported_at"] or "", reverse=True)
    escaped = escaped[:limit]

    header = '<h2 style="margin-top:24px">Escaped Defects</h2>'
    if not escaped:
        return header + '<p class="meta">No escaped defects in window.</p>'

    body_parts: list[str] = []
    for r in escaped:
        ticket = _e(r["ticket_id"] or "—")
        pr_url = r["pr_url"] or ""
        pr_num = r["pr_number"]
        pr_cell = (
            f'<a href="{_e(pr_url)}">#{pr_num}</a>' if pr_url else f"#{pr_num}"
        )
        profile = _e(r["client_profile"] or "—")
        defect_key = _e(r["defect_key"] or "—")
        src = _e(r["source"] or "—")
        sev = _e(r["severity"] or "—")
        category = _e(r["category"] or "—")
        reported = _e((r["reported_at"] or "")[:19])
        body_parts.append(
            f"<tr><td>{ticket}</td><td>{pr_cell}</td><td>{profile}</td>"
            f"<td>{defect_key}</td><td>{src}</td><td>{sev}</td>"
            f"<td>{category}</td><td>{reported}</td></tr>"
        )
    body = "".join(body_parts)
    return (
        header
        + '<table><thead><tr>'
        '<th>Ticket</th><th>PR</th><th>Profile</th><th>Defect Key</th>'
        '<th>Source</th><th>Severity</th><th>Category</th><th>Reported At</th>'
        '</tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


_AUTO_MERGE_DECISION_BADGE: dict[str, str] = {
    "merged": "badge-success",
    "dry_run": "badge-blue",
    "skipped": "badge-secondary",
    "failed": "badge-error",
}


def _query_auto_merge_decisions(
    conn: sqlite3.Connection,
    profile_filter: str | None,
    limit: int = 25,
    *,
    since_days: int = 7,
) -> list[sqlite3.Row]:
    """Fetch recent auto-merge decisions, optionally scoped to one profile.

    since_days: how far back to look (default 7 days).
    """
    since_iso = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
    return list_recent_auto_merge_decisions(
        conn,
        limit=limit,
        since_iso=since_iso,
        client_profile=profile_filter,
    )


def _parse_decision_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _format_target_cell(target_id: str) -> str:
    """Turn 'owner/repo#123' into a clickable GitHub PR link. Fallback to text."""
    if not target_id or "#" not in target_id:
        return _e(target_id or "—")
    repo_part, _, pr_part = target_id.partition("#")
    if not repo_part or not pr_part or not pr_part.isdigit():
        return _e(target_id)
    url = f"https://github.com/{repo_part}/pull/{pr_part}"
    return f'<a href="{_e(url)}">{_e(target_id)}</a>'


def _format_gates_summary(gates: Any) -> str:
    """Turn a gates dict/list into a short human-readable summary."""
    if not gates:
        return "—"
    if isinstance(gates, dict):
        parts: list[str] = []
        for key, val in gates.items():
            if isinstance(val, bool):
                mark = "✓" if val else "✗"
                parts.append(f"{key}={mark}")
            else:
                parts.append(f"{key}={val}")
        return ", ".join(parts)
    if isinstance(gates, list):
        return ", ".join(str(g) for g in gates)
    return str(gates)


def _render_auto_merge_decisions_section(
    rows: list[sqlite3.Row], profile_filter: str | None
) -> str:
    header = '<h2 style="margin-top:24px">Auto-merge Decisions</h2>'
    if not rows:
        return (
            header
            + '<p class="meta">No auto-merge decisions in the last 7 days</p>'
        )
    body_parts: list[str] = []
    for r in rows:
        payload = _parse_decision_payload(r)
        created_at = _e((r["created_at"] or "")[:19])
        target_cell = _format_target_cell(str(r["target_id"] or ""))
        decision = str(payload.get("decision") or "")
        badge_cls = _AUTO_MERGE_DECISION_BADGE.get(decision, "badge-secondary")
        decision_cell = (
            f'<span class="badge {badge_cls}">{_e(decision or "—")}</span>'
        )
        reason = _e(_truncate(str(payload.get("reason") or ""), 100))
        mode = _e(str(payload.get("recommended_mode") or "—"))
        dry_run_val = payload.get("dry_run")
        if isinstance(dry_run_val, bool):
            dry_run_cell = "yes" if dry_run_val else "no"
        else:
            dry_run_cell = "—"
        gates_cell = _e(_truncate(_format_gates_summary(payload.get("gates")), 120))
        body_parts.append(
            f"<tr><td>{created_at}</td><td>{target_cell}</td>"
            f"<td>{decision_cell}</td><td>{reason}</td>"
            f"<td>{mode}</td><td>{_e(dry_run_cell)}</td>"
            f"<td>{gates_cell}</td></tr>"
        )
    body = "".join(body_parts)
    return (
        header
        + '<table><thead><tr>'
        '<th>Time</th><th>Target</th><th>Decision</th><th>Reason</th>'
        '<th>Mode</th><th>Dry-run?</th><th>Gates</th>'
        '</tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


def _render_sparkline_svg(
    points: list[tuple[str, float | None]], *, label: str
) -> str:
    """Render a 120x32 sparkline SVG with circles. Missing values = gaps."""
    width = 120
    height = 32
    pad = 2
    if not points:
        return (
            f'<svg width="{width}" height="{height}" '
            f'aria-label="{_e(label)}"></svg>'
        )
    n = len(points)
    # x-spacing
    inner_w = width - 2 * pad
    step = inner_w / max(1, n - 1) if n > 1 else 0
    inner_h = height - 2 * pad
    circles: list[str] = []
    for i, (_, v) in enumerate(points):
        if v is None:
            continue
        # Clamp v into [0, 1] (metrics are rates). Escape rate can be >1 in
        # theory but for rendering we clip at 1.
        vc = max(0.0, min(1.0, v))
        x = pad + i * step
        y = pad + inner_h - (vc * inner_h)
        circles.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.8" fill="#4D45E5"/>'
        )
    body = "".join(circles)
    return (
        f'<svg width="{width}" height="{height}" '
        f'aria-label="{_e(label)}" '
        f'style="display:inline-block;vertical-align:middle">'
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        f'fill="#F8FAFC" stroke="#E2E8F0" stroke-width="0.5"/>'
        f'{body}'
        f'</svg>'
    )


def _render_ticket_type_breakdown(rows: list[dict[str, Any]]) -> str:
    """Small table: Ticket Type | Sample | FPA | Catch Rate | Defect Escape."""
    if not rows:
        return ""
    body_parts: list[str] = []
    for r in rows:
        body_parts.append(
            f"<tr><td>{_e(r['ticket_type'])}</td>"
            f"<td>{r['sample_size']}</td>"
            f"<td>{_e(_fmt_pct(r['first_pass_acceptance_rate']))}</td>"
            f"<td>{_e(_fmt_pct(r['self_review_catch_rate']))}</td>"
            f"<td>{_e(_defect_escape_display(r['defect_escape_rate']))}</td>"
            f"</tr>"
        )
    body = "".join(body_parts)
    return (
        '<h2 style="margin-top:24px">By Ticket Type</h2>'
        '<table><thead><tr>'
        '<th>Ticket Type</th><th>Sample</th><th>FPA</th>'
        '<th>Catch Rate</th><th>Defect Escape</th>'
        '</tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


def _build_sparklines_html(
    conn: sqlite3.Connection, profile: str, window_days: int
) -> str:
    """Build stacked sparkline HTML for a profile card (7-day rolling avg)."""
    from autonomy_metrics import compute_rolling_trend

    fpa_points = [
        (d, v) for (d, v, _n) in compute_rolling_trend(
            conn, profile, window_days, "fpa", smoothing_window=7
        )
    ]
    esc_points = [
        (d, v) for (d, v, _n) in compute_rolling_trend(
            conn, profile, window_days, "defect_escape", smoothing_window=7
        )
    ]
    catch_points = [
        (d, v) for (d, v, _n) in compute_rolling_trend(
            conn, profile, window_days, "catch_rate", smoothing_window=7
        )
    ]
    return (
        '<div style="margin-top:8px;padding-top:8px;'
        'border-top:1px solid #E2E8F0">'
        '<div class="metric-row">'
        '<span class="metric-label">FPA trend <span class="meta">(7-day avg)</span></span>'
        f'<span>{_render_sparkline_svg(fpa_points, label="FPA trend (7-day avg)")}</span>'
        '</div>'
        '<div class="metric-row">'
        '<span class="metric-label">Defect escape trend <span class="meta">(7-day avg)</span></span>'
        f'<span>{_render_sparkline_svg(esc_points, label="Defect escape trend (7-day avg)")}</span>'
        '</div>'
        '<div class="metric-row">'
        '<span class="metric-label">Catch rate trend <span class="meta">(7-day avg)</span></span>'
        f'<span>{_render_sparkline_svg(catch_points, label="Catch rate trend (7-day avg)")}</span>'
        '</div>'
        '</div>'
    )


def _render_pr_row(row: sqlite3.Row) -> str:
    ticket_id = _e(row["ticket_id"])
    pr_run_id = int(row["id"])
    ticket = f'<a href="/autonomy/pr/{pr_run_id}">{ticket_id}</a>'
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
        # Attach sparkline HTML + auto-merge state per card
        for m in profile_metrics:
            m["_sparkline_html"] = _build_sparklines_html(
                conn, m["client_profile"], window_days
            )
            enabled, source = _resolve_auto_merge_state(conn, m["client_profile"])
            m["_auto_merge_html"] = _render_auto_merge_state_block(
                m["client_profile"], enabled, source
            )

        # Build recent PR table scope
        cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
        if client_profile is not None:
            table_rows = list_pr_runs(
                conn, client_profile=client_profile, since_iso=cutoff
            )
        else:
            table_rows = list_pr_runs(conn, since_iso=cutoff)

        # Scope for unmatched/suggested sections = full window of pr_runs
        # (table_rows), not the 20-row recent_rows cap on each card.
        scoped_pr_run_ids = [int(r["id"]) for r in table_rows]

        unmatched_rows = _query_unmatched_human_issues(conn, scoped_pr_run_ids)
        suggested_rows = _query_suggested_matches(conn, scoped_pr_run_ids)

        escaped_html = _render_escaped_defects_section(
            conn, profiles_to_show, window_days
        )

        auto_merge_decision_rows = _query_auto_merge_decisions(
            conn, client_profile, limit=25
        )

        # Ticket-type breakdown: only for single profile view
        from autonomy_metrics import compute_ticket_type_breakdown
        if client_profile is not None:
            breakdown_rows = compute_ticket_type_breakdown(
                conn, client_profile, window_days
            )
            breakdown_html = _render_ticket_type_breakdown(breakdown_rows)
        else:
            breakdown_html = ""
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
    auto_merge_decisions_html = _render_auto_merge_decisions_section(
        auto_merge_decision_rows, client_profile
    )

    title_suffix = (
        f" — {_e(client_profile)}" if client_profile is not None else " — All"
    )

    html_doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Autonomy Metrics</title>
<style>{_LANGFUSE_STYLES}</style></head><body><div class="page">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
  <h1>Autonomy Metrics{title_suffix}</h1>
  <div class="meta">
    <a href="/dashboard">Dashboard</a> <span class="sep">|</span>
    <a href="/traces">Traces</a> <span class="sep">|</span>
    <a href="/autonomy">Autonomy</a>
  </div>
</div>
{selector_html}
{summary_line}
<div class="card-grid">{cards_html}</div>
<h2 style="margin-top:24px">Recent PR Outcomes</h2>
{table_html}
{breakdown_html}
{escaped_html}
{auto_merge_decisions_html}
{unmatched_html}
{suggested_html}
</div></body></html>"""

    return HTMLResponse(content=html_doc)


# ---------------------------------------------------------------------------
# Per-PR drilldown route
# ---------------------------------------------------------------------------


def _render_issues_table(rows: list[sqlite3.Row], *, title: str) -> str:
    if not rows:
        return f'<h3 style="margin-top:16px">{_e(title)}</h3><p class="meta">None.</p>'
    body_parts: list[str] = []
    for r in rows:
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
        severity = _e(r["severity"] or "—")
        category = _e(r["category"] or "—")
        is_valid = "yes" if int(r["is_valid"]) == 1 else "no"
        body_parts.append(
            f"<tr><td>{int(r['id'])}</td><td>{file_path}</td>"
            f"<td>{_e(lines)}</td><td>{summary}</td>"
            f"<td>{category}</td><td>{severity}</td><td>{is_valid}</td></tr>"
        )
    body = "".join(body_parts)
    return (
        f'<h3 style="margin-top:16px">{_e(title)}</h3>'
        '<table><thead><tr>'
        '<th>ID</th><th>File</th><th>Lines</th><th>Summary</th>'
        '<th>Category</th><th>Severity</th><th>Valid</th>'
        '</tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


def _render_matches_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return (
            '<h3 style="margin-top:16px">Matches</h3>'
            '<p class="meta">None.</p>'
        )
    body_parts: list[str] = []
    for r in rows:
        body_parts.append(
            f"<tr><td>{int(r['id'])}</td>"
            f"<td>{int(r['human_issue_id'])}</td>"
            f"<td>{int(r['ai_issue_id'])}</td>"
            f"<td>{_e(r['match_type'] or '—')}</td>"
            f"<td>{float(r['confidence'] or 0.0):.2f}</td>"
            f"<td>{_e(r['matched_by'] or '—')}</td>"
            f"<td>{_e(_truncate(r['human_summary'] or '', 60))}</td>"
            f"<td>{_e(_truncate(r['ai_summary'] or '', 60))}</td></tr>"
        )
    body = "".join(body_parts)
    return (
        '<h3 style="margin-top:16px">Matches</h3>'
        '<table><thead><tr>'
        '<th>ID</th><th>Human</th><th>AI</th><th>Type</th>'
        '<th>Confidence</th><th>By</th><th>Human summary</th>'
        '<th>AI summary</th>'
        '</tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


def _render_defects_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return (
            '<h3 style="margin-top:16px">Defect Links</h3>'
            '<p class="meta">None.</p>'
        )
    body_parts: list[str] = []
    for r in rows:
        body_parts.append(
            f"<tr><td>{_e(r['defect_key'] or '—')}</td>"
            f"<td>{_e(r['source'] or '—')}</td>"
            f"<td>{_e(r['severity'] or '—')}</td>"
            f"<td>{_e(r['category'] or '—')}</td>"
            f"<td>{'yes' if int(r['confirmed']) == 1 else 'no'}</td>"
            f"<td>{_e((r['reported_at'] or '')[:19])}</td></tr>"
        )
    body = "".join(body_parts)
    return (
        '<h3 style="margin-top:16px">Defect Links</h3>'
        '<table><thead><tr>'
        '<th>Defect Key</th><th>Source</th><th>Severity</th>'
        '<th>Category</th><th>Confirmed</th><th>Reported At</th>'
        '</tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )


@router.get("/autonomy/pr/{pr_run_id}", response_class=HTMLResponse)
def autonomy_pr_drilldown(pr_run_id: int) -> HTMLResponse:
    """Per-PR drilldown view."""
    db_path = resolve_db_path(settings.autonomy_db_path)
    conn = open_connection(db_path)
    try:
        ensure_schema(conn)
        pr_row = conn.execute(
            "SELECT * FROM pr_runs WHERE id = ?", (pr_run_id,)
        ).fetchone()
        if pr_row is None:
            return HTMLResponse(
                content=(
                    f"<!DOCTYPE html><html><body>"
                    f"<h1>pr_run {pr_run_id} not found</h1>"
                    f'<a href="/autonomy">Back to Autonomy</a>'
                    f"</body></html>"
                ),
                status_code=404,
            )

        human_issues = list_review_issues_by_pr_run(
            conn, pr_run_id, source="human_review"
        )
        ai_issues = list_review_issues_by_pr_run(
            conn, pr_run_id, source="ai_review"
        )
        judge_issues = list_review_issues_by_pr_run(
            conn, pr_run_id, source="judge"
        )
        qa_issues = list_review_issues_by_pr_run(
            conn, pr_run_id, source="qa"
        )

        matches_rows = conn.execute(
            """
            SELECT m.id, m.human_issue_id, m.ai_issue_id, m.match_type,
                   m.confidence, m.matched_by,
                   h.summary AS human_summary,
                   a.summary AS ai_summary
            FROM issue_matches m
            JOIN review_issues h ON h.id = m.human_issue_id
            JOIN review_issues a ON a.id = m.ai_issue_id
            WHERE h.pr_run_id = ?
            ORDER BY m.id
            """,
            (pr_run_id,),
        ).fetchall()

        defect_rows = conn.execute(
            "SELECT * FROM defect_links WHERE pr_run_id = ? "
            "ORDER BY reported_at DESC, id DESC",
            (pr_run_id,),
        ).fetchall()
    finally:
        conn.close()

    ticket = _e(pr_row["ticket_id"] or "—")
    pr_url = pr_row["pr_url"] or ""
    pr_number = pr_row["pr_number"]
    pr_link = (
        f'<a href="{_e(pr_url)}">#{pr_number}</a>'
        if pr_url else f"#{pr_number}"
    )
    profile = _e(pr_row["client_profile"] or "—")
    opened = _e((pr_row["opened_at"] or "")[:19])
    merged_at = _e((pr_row["merged_at"] or "")[:19]) or "—"
    flags: list[str] = []
    if int(pr_row["first_pass_accepted"]) == 1:
        flags.append('<span class="badge badge-success">first-pass</span>')
    if int(pr_row["merged"]) == 1:
        flags.append('<span class="badge badge-success">merged</span>')
    if int(pr_row["escalated"]) == 1:
        flags.append('<span class="badge badge-error">escalated</span>')
    if int(pr_row["backfilled"]) == 1:
        flags.append('<span class="badge badge-secondary">backfilled</span>')
    flags_html = " ".join(flags) if flags else "—"

    header_html = (
        '<div class="card">'
        f'<h2>{ticket} — PR {pr_link}</h2>'
        f'<div class="metric-row"><span class="metric-label">Profile</span>'
        f'<span class="metric-value">{profile}</span></div>'
        f'<div class="metric-row"><span class="metric-label">Opened</span>'
        f'<span class="metric-value">{opened}</span></div>'
        f'<div class="metric-row"><span class="metric-label">Merged at</span>'
        f'<span class="metric-value">{merged_at}</span></div>'
        f'<div class="metric-row"><span class="metric-label">Flags</span>'
        f'<span class="metric-value">{flags_html}</span></div>'
        '</div>'
    )

    html_doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>PR Drilldown {pr_run_id}</title>
<style>{_LANGFUSE_STYLES}</style></head><body><div class="page">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
  <h1>PR Drilldown</h1>
  <div class="meta"><a href="/autonomy">← Back to Autonomy</a></div>
</div>
{header_html}
{_render_issues_table(human_issues, title="Human Issues")}
{_render_issues_table(ai_issues, title="AI Review Issues")}
{_render_issues_table(judge_issues, title="Judge Issues")}
{_render_issues_table(qa_issues, title="QA Issues")}
{_render_matches_table(matches_rows)}
{_render_defects_table(defect_rows)}
</div></body></html>"""
    return HTMLResponse(content=html_doc)
