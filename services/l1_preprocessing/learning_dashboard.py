"""Self-learning dashboard — /autonomy/learning.

Server-rendered HTML triage view for lesson candidates produced by
the miner. Reuses the Langfuse-style base CSS + badge palette from
``dashboard_common`` so the view blends with ``/autonomy`` and
``/dashboard``.

Phase B scope: list candidates, expand evidence, drive approve /
reject / snooze via the POST endpoints in ``learning_api``. Phase
D will add a "PR opened" column once the PR opener lands.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from urllib.parse import quote as _url_quote
from urllib.parse import urlencode as _urlencode

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from autonomy_store import (
    autonomy_conn,
    list_lesson_candidates,
    list_lesson_evidence,
)
from dashboard_common import LANGFUSE_BASE_CSS
from dashboard_common import badge as _badge
from dashboard_common import escape_html as _e
from dashboard_common import fmt_ts as _fmt_ts

router = APIRouter()


# Status → badge class. proposed/snoozed/draft_ready are intermediate;
# approved/applied are terminal-positive; rejected/reverted/stale terminal-negative.
_STATUS_BADGE: dict[str, str] = {
    "proposed": "badge-secondary",
    "draft_ready": "badge-blue",
    "snoozed": "badge-warning",
    "approved": "badge-success",
    "applied": "badge-success",
    "rejected": "badge-error",
    "reverted": "badge-error",
    "stale": "badge-secondary",
}

_SEVERITY_BADGE: dict[str, str] = {
    "info": "badge-secondary",
    "warn": "badge-warning",
    "critical": "badge-error",
}


_LOCAL_CSS = """
h2 { font-size: 15px; font-weight: 600; color: #0F172A; margin: 8px 0; }
h3 { font-size: 13px; font-weight: 600; color: #334155; margin: 8px 0; }
.card {
  border: 1px solid #E2E8F0; border-radius: 8px; padding: 16px;
  background: #FFFFFF; margin-bottom: 12px;
}
.meta { font-size: 11.2px; color: #64748B; }
.selector { margin: 16px 0; font-size: 12.5px; }
.selector a, .selector span.current {
  margin: 0 4px; padding: 3px 10px; border-radius: 6px;
}
.selector a { color: #4D45E5; }
.selector span.current { background: #0F172A; color: #F7F9FB; font-weight: 600; }
table { width: 100%; border-collapse: separate; border-spacing: 0;
  border: 1px solid #E2E8F0; border-radius: 8px; overflow: hidden;
  margin-top: 12px; font-size: 12px; }
thead th {
  background: #F7F9FB; color: #64748B; font-weight: 500; font-size: 11.2px;
  text-align: left; padding: 10px 12px; border-bottom: 1px solid #E2E8F0;
  white-space: nowrap;
}
tbody td {
  padding: 10px 12px; border-bottom: 1px solid #E2E8F0; vertical-align: top;
}
tbody tr:last-child td { border-bottom: none; }
details.evidence summary {
  cursor: pointer; color: #4D45E5; font-size: 11.2px; padding: 4px 0;
}
details.evidence ul { margin: 6px 0 6px 18px; font-size: 11.5px; color: #334155; }
details.evidence li { margin: 3px 0; }
.actions { display: inline-flex; gap: 6px; }
.btn {
  border: 1px solid #CBD5E1; background: #FFFFFF; color: #0F172A;
  padding: 3px 10px; border-radius: 6px; font-size: 11.5px; font-weight: 600;
  cursor: pointer;
}
.btn-draft   { border-color: #4D45E5; color: #4D45E5; }
.btn-approve { border-color: #12A87B; color: #12A87B; }
.btn-reject  { border-color: #DB2626; color: #DB2626; }
.btn-snooze  { border-color: #C79004; color: #C79004; }
.btn[disabled] { opacity: 0.45; cursor: default; }
pre.delta {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px; background: #F8FAFC; padding: 6px 8px;
  border: 1px solid #E2E8F0; border-radius: 4px;
  white-space: pre-wrap; word-break: break-word;
  max-width: 540px; margin-top: 4px;
}
"""

_STYLES = LANGFUSE_BASE_CSS + _LOCAL_CSS


_PROFILE_ANY = "(all)"


def _mapped_badge(text: str, mapping: dict[str, str]) -> str:
    return _badge(text, mapping.get(text, "badge-secondary"))


def _learning_href(*, client_profile: str | None, status: str | None) -> str:
    """Build a /autonomy/learning URL with filter params, skipping empties."""
    params = {
        k: v
        for k, v in (("client_profile", client_profile), ("status", status))
        if v
    }
    query = _urlencode(params) if params else ""
    return "/autonomy/learning" + (f"?{query}" if query else "")


def _render_selector(
    profiles: list[str],
    current_profile: str | None,
    current_status: str | None,
) -> str:
    """Render profile + status filter selectors above the table.

    Keeps the ``/autonomy/learning`` URL the one source of filter
    state so the view is bookmarkable and bots can triage by URL.
    """
    parts: list[str] = []
    parts.append('<div class="selector"><strong>Profile:</strong> ')
    if not current_profile:
        parts.append(f'<span class="current">{_e(_PROFILE_ANY)}</span>')
    else:
        href = _learning_href(client_profile=None, status=current_status)
        parts.append(f'<a href="{_e(href)}">{_e(_PROFILE_ANY)}</a>')
    for profile in profiles:
        if profile == current_profile:
            parts.append(f'<span class="current">{_e(profile)}</span>')
        else:
            href = _learning_href(
                client_profile=profile, status=current_status
            )
            parts.append(f'<a href="{_e(href)}">{_e(profile)}</a>')
    parts.append("</div>")
    parts.append('<div class="selector"><strong>Status:</strong> ')
    status_filters: list[str | None] = [
        None, "proposed", "draft_ready", "approved", "applied",
        "rejected", "snoozed",
    ]
    for s in status_filters:
        label = s or "all"
        if s == current_status:
            parts.append(f'<span class="current">{_e(label)}</span>')
        else:
            href = _learning_href(client_profile=current_profile, status=s)
            parts.append(f'<a href="{_e(href)}">{_e(label)}</a>')
    parts.append("</div>")
    return "".join(parts)


def _render_evidence_list(evidence_rows: list[sqlite3.Row]) -> str:
    if not evidence_rows:
        return '<span class="meta">No evidence rows.</span>'
    items: list[str] = []
    for r in evidence_rows:
        trace_id = r["trace_id"] or ""
        # quote() to handle ticket IDs with slashes or other URL-unsafe
        # chars; _e() on the displayed text so HTML-unsafe chars don't
        # escape the anchor.
        trace_link = (
            f'<a href="/traces/{_url_quote(trace_id, safe="")}">'
            f'{_e(trace_id)}</a>'
            if trace_id
            else '<span class="meta">(no trace)</span>'
        )
        source_ref = _e(r["source_ref"] or "")
        snippet = _e(r["snippet"] or "")
        observed = _e(_fmt_ts(r["observed_at"]))
        items.append(
            f"<li>{trace_link} · "
            f'<span class="meta">{observed} · {source_ref}</span><br>'
            f"{snippet}</li>"
        )
    return (
        "<details class='evidence'>"
        f"<summary>Evidence ({len(evidence_rows)})</summary>"
        f"<ul>{''.join(items)}</ul>"
        "</details>"
    )


def _render_action_buttons(lesson_id: str, status: str) -> str:
    """Render action buttons as disabled HTML ``<button>`` elements.

    The dashboard is server-rendered without JS, so a form-submit
    flow would require a round-trip that reads the admin token from
    somewhere — deferred. For now the operator reads the POST
    endpoint off the ``title``/``data-endpoint`` attributes and hits
    it via curl (or a tool that injects the admin header). Plain
    ``<a>`` tags would invite clicks that issue a GET (405).
    """
    terminal = status in {
        "applied", "rejected", "reverted", "stale", "approved"
    }
    # Draft is only applicable at `proposed` — the drafter transitions
    # proposed -> draft_ready and a lesson already past proposed can't
    # re-draft in place.
    buttons: list[tuple[str, str, str, bool]] = [
        ("Draft diff", "draft", "btn btn-draft", status != "proposed"),
        ("Approve", "approve", "btn btn-approve", status != "draft_ready"),
        ("Reject", "reject", "btn btn-reject", terminal),
        ("Snooze", "snooze", "btn btn-snooze", terminal),
    ]
    out: list[str] = ['<div class="actions">']
    for label, action, cls, disabled in buttons:
        endpoint = f"/api/learning/candidates/{_e(lesson_id)}/{_e(action)}"
        if disabled:
            tooltip = f"Disabled — current status is {status}"
        else:
            tooltip = f"POST {endpoint} (requires X-Autonomy-Admin-Token)"
        out.append(
            f'<button class="{_e(cls)}" disabled '
            f'data-endpoint="{endpoint}" '
            f'title="{_e(tooltip)}">{_e(label)}</button>'
        )
    out.append("</div>")
    return "".join(out)


def _format_proposed_delta(raw_json: str) -> str:
    """Pretty-print a proposed_delta_json blob into a <pre> block.

    Only the keys humans care about when triaging — target_path,
    edit_type, anchor, rationale_md, after — are shown. Unknown
    shapes render as-is so future detector variants stay visible.
    """
    if not raw_json:
        return ""
    try:
        obj = json.loads(raw_json)
    except (ValueError, TypeError):
        return f'<pre class="delta">{_e(raw_json)}</pre>'
    if not isinstance(obj, dict):
        return f'<pre class="delta">{_e(raw_json)}</pre>'
    ordered_keys = (
        "target_path",
        "edit_type",
        "anchor",
        "rationale_md",
        "after",
        "before",
    )
    lines: list[str] = []
    for k in ordered_keys:
        if k in obj:
            val = obj[k]
            lines.append(f"{k}: {val}")
    for k, v in obj.items():
        if k not in ordered_keys:
            lines.append(f"{k}: {v}")
    body = "\n".join(lines)
    return f'<pre class="delta">{_e(body)}</pre>'


def _render_candidate_row(
    candidate: sqlite3.Row,
    evidence: list[sqlite3.Row],
) -> str:
    status = candidate["status"] or ""
    severity = candidate["severity"] or "info"
    scope_cell = (
        f"<code>{_e(candidate['scope_key'])}</code><br>"
        f'<span class="meta">detector: {_e(candidate["detector_name"])} '
        f'· pattern: {_e(candidate["pattern_key"])}</span>'
    )
    first_seen = _fmt_ts(candidate["detected_at"])
    last_seen = _fmt_ts(candidate["last_seen_at"])
    time_cell = f"{_e(last_seen)}<br><span class='meta'>first {_e(first_seen)}</span>"
    profile_cell = (
        f"{_e(candidate['client_profile'] or '—')}"
        f"<br><span class='meta'>{_e(candidate['platform_profile'] or '')}</span>"
    )
    delta_html = _format_proposed_delta(candidate["proposed_delta_json"] or "")
    evidence_html = _render_evidence_list(evidence)
    actions_html = _render_action_buttons(candidate["lesson_id"], status)
    return (
        "<tr>"
        f"<td>{_mapped_badge(status, _STATUS_BADGE)}<br>"
        f"<span class='meta'>{_e(candidate['lesson_id'])}</span></td>"
        f"<td>{profile_cell}</td>"
        f"<td>{scope_cell}<br>{delta_html}</td>"
        f"<td>{int(candidate['frequency'] or 0)}</td>"
        f"<td>{_mapped_badge(severity, _SEVERITY_BADGE)}</td>"
        f"<td>{time_cell}</td>"
        f"<td>{evidence_html}</td>"
        f"<td>{actions_html}</td>"
        "</tr>"
    )


def _render_candidate_table(
    rows: list[tuple[sqlite3.Row, list[sqlite3.Row]]],
) -> str:
    if not rows:
        return (
            '<p class="meta" style="margin-top:16px">'
            "No lesson candidates match these filters. "
            "Check that <code>LEARNING_MINER_ENABLED</code> is on "
            "and a backfill has run.</p>"
        )
    body = "".join(_render_candidate_row(c, ev) for c, ev in rows)
    return (
        "<table>"
        "<thead><tr>"
        "<th>Status / Lesson</th>"
        "<th>Profile</th>"
        "<th>Scope · proposed delta</th>"
        "<th>Frequency</th>"
        "<th>Severity</th>"
        "<th>Last seen</th>"
        "<th>Evidence</th>"
        "<th>Actions</th>"
        "</tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _list_distinct_profiles(conn: sqlite3.Connection) -> list[str]:
    """Profiles that actually have candidates — avoids cluttering the
    selector with empty profiles just because they exist in YAML.
    """
    rows = conn.execute(
        "SELECT DISTINCT client_profile FROM lesson_candidates "
        "WHERE client_profile != '' ORDER BY client_profile"
    ).fetchall()
    return [str(r["client_profile"]) for r in rows]


@router.get("/autonomy/learning", response_class=HTMLResponse)
async def get_learning_dashboard(
    client_profile: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> HTMLResponse:
    """Triage view for the self-learning miner's lesson candidates."""
    with autonomy_conn() as conn:
        profiles = _list_distinct_profiles(conn)
        candidates = list_lesson_candidates(
            conn,
            status=status,
            client_profile=client_profile,
            limit=limit,
        )
        enriched: list[tuple[Any, list[Any]]] = [
            (c, list_lesson_evidence(conn, c["lesson_id"]))
            for c in candidates
        ]
    selector_html = _render_selector(profiles, client_profile, status)
    table_html = _render_candidate_table(enriched)
    summary = (
        f'<p class="meta">{len(candidates)} candidates shown '
        f"(limit {limit}).</p>"
    )
    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Self-learning</title>
<style>{_STYLES}</style></head>
<body><div class="page">
<h1>Self-learning — lesson candidates</h1>
<div class="meta" style="margin-bottom:8px">
  <a href="/dashboard">Home</a> ·
  <a href="/autonomy">Autonomy</a> ·
  <a href="/traces">Traces</a>
</div>
{selector_html}
{summary}
{table_html}
</div></body></html>"""
    return HTMLResponse(content=html_doc)
