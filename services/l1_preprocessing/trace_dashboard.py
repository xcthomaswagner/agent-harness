"""Trace dashboard — Langfuse-style observability views at /traces.

Provides three views:
- Table view (default): filterable trace list with phase dots and duration bars
- Board view (?view=board): Kanban columns (In-Flight / Completed / Stuck)
- Detail view (/traces/<id>): L1/L2/L3 span tree with artifact expansion
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from tracer import (
    build_span_tree,
    build_trace_list_row,
    compute_phase_durations,
    count_traces,
    extract_diagnostic_info,
    list_traces,
    read_trace,
)

router = APIRouter()

# --- Langfuse Design System ---

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
"""

# Status → badge class mapping
_STATUS_BADGE: dict[str, str] = {
    "Complete": "badge-success",
    "PR Created": "badge-success",
    "Escalated": "badge-error",
    "Agent Done (no PR)": "badge-error",
    "Dispatched": "badge-error",
    "QA Done": "badge-warning",
    "Review Done": "badge-blue",
    "Implementing": "badge-blue",
    "Planned": "badge-blue",
    "Merged": "badge-blue",
    "CI Fix": "badge-warning",
    "Processing": "badge-blue",
    "Enriched": "badge-secondary",
    "Received": "badge-secondary",
}

# Phase colors for duration bar and span icons
_PHASE_COLORS: dict[str, str] = {
    "ticket_read": "#64748B", "planning": "#9333EA", "plan_review": "#9333EA",
    "implementation": "#EA580C", "merge": "#82CB15", "code_review": "#6466F1",
    "judge": "#6466F1", "qa_validation": "#124D49", "simplify": "#64748B",
    "pr_created": "#64748B", "complete": "#64748B",
}

# Icon type letters for span tree
_ICON_LETTERS: dict[str, str] = {
    "trace": "T", "agent": "A", "span": "S", "tool": "T", "event": "E",
    "generation": "G",
}

# Stuck detection thresholds (hours)
_STUCK_THRESHOLDS: dict[str, float] = {
    "Received": 1, "Processing": 1, "Enriched": 1, "Dispatched": 1,
    "Planned": 2, "Implementing": 2, "Merged": 2,
    "Review Done": 2, "QA Done": 2, "PR Created": 2, "CI Fix": 2,
}
_FAILED_STATUSES = {"Escalated", "Agent Done (no PR)"}
_COMPLETED_STATUSES = {"Complete"}


def _e(text: str) -> str:
    """HTML-escape a string."""
    return html.escape(str(text), quote=True)


def _safe_url(url: str) -> str:
    """Return URL only if safe scheme."""
    s = url.strip().lower()
    return url if s.startswith("https://") or s.startswith("http://") else "#"


def _fmt_dur(seconds: float) -> str:
    """Format seconds as Nm Ss."""
    if seconds <= 0:
        return "0s"
    m, s = int(seconds // 60), int(seconds % 60)
    return f"{m}m {s}s" if m else f"{s}s"


def _badge(text: str, cls: str) -> str:
    """Render a badge span."""
    return f'<span class="badge {cls}">{_e(text)}</span>'


def _fmt_ts(ts: str) -> str:
    """Format ISO timestamp for display."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        return ts[:16]


# --- Trace List (Table View) ---


def _render_trace_table(traces: list[dict], total: int, page: int, per_page: int) -> str:
    """Render the Langfuse-style trace list table."""
    # Enrich each trace with phase dots (use cached entries from list_traces)
    enriched: list[dict] = []
    for t in traces:
        entries = t.pop("_raw_entries", None) or read_trace(t["ticket_id"])
        enriched.append(build_trace_list_row(t, entries))

    # Compute stats
    n_complete = sum(1 for t in traces if t.get("status") in _COMPLETED_STATUSES)
    n_stuck = sum(1 for t in traces if t.get("status") in _FAILED_STATUSES)
    n_flight = total - n_complete - n_stuck

    # Stats bar
    stats = (
        f'<div data-stats style="display:flex;gap:16px;margin-bottom:16px;padding:10px 16px;'
        f'background:#F7F9FB;border:1px solid #E2E8F0;border-radius:8px">'
        f'<div style="display:flex;align-items:baseline;gap:6px">'
        f'<span style="font-size:17.6px;font-weight:600">{total}</span>'
        f'<span class="meta">total</span></div>'
        f'<div style="display:flex;align-items:baseline;gap:6px">'
        f'<span style="font-size:17.6px;font-weight:600;color:#124D49">{n_complete}</span>'
        f'<span class="meta">completed</span></div>'
        f'<div style="display:flex;align-items:baseline;gap:6px">'
        f'<span style="font-size:17.6px;font-weight:600;color:#EA580C">{n_flight}</span>'
        f'<span class="meta">in-flight</span></div>'
        f'<div style="display:flex;align-items:baseline;gap:6px">'
        f'<span style="font-size:17.6px;font-weight:600;color:#DB2626">{n_stuck}</span>'
        f'<span class="meta">stuck</span></div>'
        f'</div>'
    )

    # Filter bar
    filters = (
        '<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">'
        '<input id="search" style="font-size:11.2px;padding:5px 10px;border:1px solid #E2E8F0;'
        'border-radius:6px;width:200px;font-family:inherit" placeholder="Search tickets..." '
        'oninput="filterTable()">'
        '<span class="meta" style="margin-right:-4px">Status</span>'
        '<select id="f-status" style="font-size:11.2px;padding:5px 8px;border:1px solid #E2E8F0;'
        'border-radius:6px;font-family:inherit" onchange="filterTable()">'
        '<option value="">All</option><option>Complete</option><option>Dispatched</option>'
        '<option>Escalated</option><option>Processing</option><option>Enriched</option>'
        '</select>'
        '<span class="meta" style="margin-right:-4px">Mode</span>'
        '<select id="f-mode" style="font-size:11.2px;padding:5px 8px;border:1px solid #E2E8F0;'
        'border-radius:6px;font-family:inherit" onchange="filterTable()">'
        '<option value="">All</option><option>quick</option><option>simple</option>'
        '<option>full</option><option>multi</option></select>'
        '</div>'
    )

    # Table rows
    rows = ""
    for t in enriched:
        status = t.get("status", "")
        badge_cls = _STATUS_BADGE.get(status, "badge-secondary")
        mode = t.get("pipeline_mode", "")
        review = t.get("review_verdict", "")
        qa = t.get("qa_result", "")
        pr = t.get("pr_url", "")
        duration = t.get("duration", "")
        dur_pct = t.get("duration_pct", 0)
        dur_color = t.get("duration_color", "#124D49")
        dots = t.get("phase_dots", [])
        started = _fmt_ts(t.get("started_at", ""))
        tid = _e(t["ticket_id"])

        review_html = _badge(review, "badge-success") if review == "APPROVED" else (
            _badge(review, "badge-warning") if review else '<span class="meta">&mdash;</span>')
        qa_html = _badge(qa, "badge-success") if qa == "PASS" else (
            _badge(qa, "badge-error") if qa else '<span class="meta">&mdash;</span>')
        pr_html = (
            f'<a href="{_e(_safe_url(pr))}" target="_blank" style="font-size:11.2px">'
            f'#{pr.split("/")[-1]}</a>' if pr else '<span class="meta">&mdash;</span>')
        mode_html = _badge(mode, "badge-secondary") if mode else '<span class="meta">&mdash;</span>'

        dur_html = f'<span style="color:{dur_color}">{_e(duration)}</span>' if duration else '<span class="meta">&mdash;</span>'
        bar_html = (
            f'<div style="display:inline-flex;width:60px;height:6px;border-radius:3px;'
            f'background:#F1F5F9;vertical-align:middle;margin-left:6px">'
            f'<div style="width:{dur_pct}%;background:{dur_color};border-radius:3px"></div>'
            f'</div>' if dur_pct else ""
        )

        dots_html = '<div style="display:flex;gap:2px">'
        for d in dots:
            dots_html += f'<div style="width:8px;height:8px;border-radius:2px;background:{d["color"]}" title="{_e(d["phase"])}"></div>'
        dots_html += '</div>'

        rows += (
            f'<tr data-status="{_e(status)}" data-mode="{_e(mode)}" data-ticket="{tid}" '
            f'onclick="location.href=\'/traces/{tid}\'" style="cursor:pointer">'
            f'<td><a href="/traces/{tid}" style="font-weight:500">{tid}</a></td>'
            f'<td>{_badge(status, badge_cls)}</td>'
            f'<td>{mode_html}</td>'
            f'<td>{review_html}</td>'
            f'<td>{qa_html}</td>'
            f'<td><div style="display:flex;align-items:center;gap:4px">{dur_html}{bar_html}</div></td>'
            f'<td>{dots_html}</td>'
            f'<td>{pr_html}</td>'
            f'<td class="meta">{_e(started)}</td>'
            f'</tr>'
        )

    total_pages = max(1, (total + per_page - 1) // per_page)

    filter_js = """
<script>
function filterTable() {
  var search = document.getElementById('search').value.toLowerCase();
  var status = document.getElementById('f-status').value;
  var mode = document.getElementById('f-mode').value;
  document.querySelectorAll('tbody tr').forEach(function(row) {
    var show = true;
    if (search && row.dataset.ticket.toLowerCase().indexOf(search) < 0) show = false;
    if (status && row.dataset.status !== status) show = false;
    if (mode && row.dataset.mode !== mode) show = false;
    row.style.display = show ? '' : 'none';
  });
}
</script>"""

    # Soft auto-refresh: fetch new HTML and replace tbody without losing scroll/filters
    auto_refresh_js = """
<script>
setInterval(function() {
  fetch(location.href).then(function(r) { return r.text(); }).then(function(html) {
    var parser = new DOMParser();
    var doc = parser.parseFromString(html, 'text/html');
    var newBody = doc.querySelector('tbody');
    var newStats = doc.querySelector('[data-stats]');
    if (newBody) document.querySelector('tbody').innerHTML = newBody.innerHTML;
    if (newStats) {
      var cur = document.querySelector('[data-stats]');
      if (cur) cur.innerHTML = newStats.innerHTML;
    }
    filterTable();
  }).catch(function() {});
}, 10000);
</script>"""

    return f"""<!DOCTYPE html><html><head>
<title>Traces</title>
<style>{_LANGFUSE_STYLES}
table {{ width:100%;border-collapse:separate;border-spacing:0;border:1px solid #E2E8F0;border-radius:8px;overflow:hidden }}
thead th {{ background:#F7F9FB;color:#64748B;font-weight:500;font-size:11.2px;text-align:left;padding:10px 12px;border-bottom:1px solid #E2E8F0;white-space:nowrap }}
tbody tr {{ transition:background 0.1s }}
tbody tr:hover {{ background:rgba(241,245,249,0.5) }}
tbody td {{ padding:8px 12px;border-bottom:1px solid #E2E8F0;vertical-align:middle }}
tbody tr:last-child td {{ border-bottom:none }}
.view-toggle {{ display:inline-flex;border:1px solid #E2E8F0;border-radius:6px;overflow:hidden }}
.view-btn {{ padding:4px 12px;font-size:11.2px;cursor:pointer;border:none;background:#FFF;color:#64748B;font-weight:500;border-right:1px solid #E2E8F0;font-family:inherit }}
.view-btn:last-child {{ border-right:none }}
.view-btn.active {{ background:#0F172A;color:#F7F9FB }}
.view-btn:hover:not(.active) {{ background:#F1F5F9 }}
</style></head><body><div class="page">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
  <h1>Traces</h1>
  <div class="view-toggle">
    <button class="view-btn active" onclick="location.href='/traces'">Table</button>
    <button class="view-btn" onclick="location.href='/traces?view=board'">Board</button>
  </div>
</div>
{stats}
{filters}
<table>
<thead><tr>
  <th style="width:110px">Ticket</th><th style="width:130px">Status</th>
  <th style="width:80px">Mode</th><th style="width:90px">Review</th>
  <th style="width:80px">QA</th><th style="width:120px">Duration</th>
  <th style="width:100px">Phases</th><th style="width:50px">PR</th>
  <th>Started</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 0;font-size:11.2px;color:#64748B">
  <span>Page {page} of {total_pages}</span>
  <div style="display:flex;gap:4px">
    {'<a href="/traces?page=' + str(page-1) + '" style="padding:4px 8px;border:1px solid #E2E8F0;border-radius:6px">&larr; Prev</a>' if page > 1 else ''}
    {'<a href="/traces?page=' + str(page+1) + '" style="padding:4px 8px;border:1px solid #E2E8F0;border-radius:6px">Next &rarr;</a>' if page < total_pages else ''}
  </div>
</div>
{filter_js}
{auto_refresh_js}
</div></body></html>"""


# --- Trace Detail (Span Tree View) ---


def _render_span_row(
    entry: dict, icon_type: str, duration: float | None = None,
    indent: int = 0, artifacts: list[dict] | None = None,
) -> str:
    """Render a single span row in the tree."""
    phase = entry.get("phase", "")
    event = entry.get("event", "")
    ts = entry.get("timestamp", "")[:19]
    color = _PHASE_COLORS.get(phase, "#64748B")
    letter = _ICON_LETTERS.get(icon_type, "S")

    # Icon
    icon_html = (
        f'<div style="width:24px;height:20px;display:flex;align-items:center;'
        f'justify-content:center;border-radius:4px;border:2px solid {color};'
        f'font-size:10px;font-weight:700;color:{color};background:#FFF;'
        f'flex-shrink:0;margin-right:8px">{letter}</div>'
    )

    # Metadata line
    skip = {"trace_id", "ticket_id", "timestamp", "phase", "event", "source", "content"}
    extra = {k: v for k, v in entry.items() if k not in skip and v}
    meta_parts = []
    if phase and phase != event:
        meta_parts.append(f'<span>{_e(phase)}</span>')
    for k, v in extra.items():
        val = str(v)
        if val.startswith("http"):
            meta_parts.append(f'<a href="{_e(_safe_url(val))}" target="_blank">{_e(val[:60])}</a>')
        elif k == "commit":
            meta_parts.append(
                f'<span>commit: <code style="font-size:11px;background:#F1F5F9;'
                f'padding:1px 4px;border-radius:3px">{_e(val[:8])}</code></span>')
        else:
            meta_parts.append(f'<span>{_e(k)}: {_e(val[:100])}</span>')
    meta_html = f'<div style="display:flex;gap:12px;font-size:11.2px;color:#64748B;margin-top:2px">{"".join(meta_parts)}</div>' if meta_parts else ""

    # Status badges inline
    verdict = extra.get("verdict", "")
    overall = extra.get("overall", "")
    inline_badges = ""
    if verdict:
        cls = "badge-success" if verdict == "APPROVED" else "badge-warning"
        inline_badges += f' {_badge(str(verdict), cls)}'
    if overall:
        criteria = f'{extra.get("criteria_passed", "")}/{extra.get("criteria_total", "")}'
        cls = "badge-success" if overall == "PASS" else "badge-error"
        inline_badges += f' {_badge(f"{overall} {criteria}", cls)}'

    # Duration
    dur_html = ""
    if duration and duration > 0:
        dur_html = f'<div style="flex-shrink:0;text-align:right;font-size:11.2px;color:#64748B;margin-left:12px;white-space:nowrap">{_fmt_dur(duration)}</div>'
    elif ts:
        dur_html = f'<div style="flex-shrink:0;text-align:right;font-size:11.2px;color:#94A3B8;margin-left:12px">{ts[11:]}</div>'

    # Indent
    indent_html = f'<div style="width:{indent * 20}px;flex-shrink:0"></div>' if indent else ""

    row = (
        f'<div style="display:flex;align-items:flex-start;padding:8px 16px;'
        f'border-bottom:1px solid #E2E8F0;transition:background 0.1s" '
        f'onmouseover="this.style.background=\'rgba(241,245,249,0.5)\'" '
        f'onmouseout="this.style.background=\'transparent\'">'
        f'{indent_html}{icon_html}'
        f'<div style="flex:1;min-width:0">'
        f'<div style="font-size:13.2px;font-weight:500;color:#0F172A">'
        f'{_e(event)}{inline_badges}</div>'
        f'{meta_html}</div>'
        f'{dur_html}</div>'
    )

    # Artifact children
    art_html = ""
    if artifacts:
        for art in artifacts:
            content = str(art.get("content", ""))
            art_event = art.get("event", "artifact")
            label = art_event.replace("_artifact", "").replace("_", " ")
            art_html += (
                f'<div style="padding:0 16px 0 {(indent + 1) * 20 + 32}px;'
                f'border-bottom:1px solid #E2E8F0">'
                f'<div style="display:flex;align-items:center;gap:6px;padding:6px 0;'
                f'font-size:11.2px;color:#4D45E5;cursor:pointer" '
                f'onclick="var c=this.nextElementSibling;c.style.display=c.style.display===\'none\'?\'block\':\'none\';'
                f'this.querySelector(\'svg\').style.transform=c.style.display===\'none\'?\'\':\' rotate(90deg)\'">'
                f'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
                f'<path d="M9 18l6-6-6-6"/></svg>'
                f'View {_e(label)} ({len(content):,} chars)</div>'
                f'<div style="display:none;padding:8px 12px;margin-bottom:8px;'
                f'background:#F7F9FB;border:1px solid #E2E8F0;border-radius:6px;'
                f'font-size:12px;white-space:pre-wrap;max-height:400px;overflow-y:auto;'
                f'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#334155;'
                f'line-height:1.6">{_e(content)}</div></div>'
            )

    return row + art_html


def _render_detail(ticket_id: str) -> str:
    """Render the Langfuse-style trace detail view."""
    entries = read_trace(ticket_id)

    if not entries:
        return (
            f'<!DOCTYPE html><html><head><style>{_LANGFUSE_STYLES}</style></head>'
            f'<body><div class="page"><h1>No trace found for {_e(ticket_id)}</h1>'
            f'<a href="/traces">&larr; Back</a></div></body></html>'
        )

    tree = build_span_tree(entries)
    s = tree["summary"]
    durations = compute_phase_durations(entries)

    # Breadcrumb
    breadcrumb = f'<div class="meta" style="margin-bottom:8px"><a href="/traces">Traces</a> / {_e(ticket_id)}</div>'

    # Title + status
    status = s.get("status", "Unknown")
    badge_cls = _STATUS_BADGE.get(status, "badge-secondary")
    title = f'<h1 style="margin-bottom:16px">{_e(ticket_id)} {_badge(status, badge_cls)}</h1>'

    # Summary bar
    tokens_in = s.get("tokens_in", 0)
    tokens_out = s.get("tokens_out", 0)
    token_str = f'{tokens_in:,} in / {tokens_out:,} out' if tokens_in else 'N/A'
    review = s.get("review_verdict", "")
    qa = s.get("qa_result", "")
    qa_detail = ""
    if s.get("qa_passed") or s.get("qa_total"):
        qa_detail = f' {s.get("qa_passed", 0)}/{s.get("qa_total", 0)}'
    pr_url = s.get("pr_url", "")
    mode = s.get("pipeline_mode", "")

    summary_items = [
        f'<div><span class="meta">Duration</span> <strong>{_e(s.get("duration", "N/A"))}</strong></div>',
    ]
    if mode:
        summary_items.append(f'<div><span class="meta">Mode</span> {_badge(mode, "badge-secondary")}</div>')
    if review:
        r_cls = "badge-success" if review == "APPROVED" else "badge-warning"
        summary_items.append(f'<div><span class="meta">Review</span> {_badge(review, r_cls)}</div>')
    if qa:
        q_cls = "badge-success" if qa == "PASS" else "badge-error"
        summary_items.append(f'<div><span class="meta">QA</span> {_badge(qa + qa_detail, q_cls)}</div>')
    summary_items.append(f'<div><span class="meta">Analyst</span> <span style="font-weight:500">{_e(token_str)}</span></div>')
    if pr_url:
        summary_items.append(f'<div><span class="meta">PR</span> <a href="{_e(_safe_url(pr_url))}" target="_blank">#{pr_url.split("/")[-1]}</a></div>')

    summary_bar = (
        f'<div style="display:flex;gap:24px;flex-wrap:wrap;align-items:center;'
        f'padding:12px 16px;background:#F7F9FB;border:1px solid #E2E8F0;'
        f'border-radius:8px;margin-bottom:20px">{"".join(summary_items)}</div>'
    )

    # Phase duration bar
    dur_bar = ""
    if durations:
        total_secs = sum(d["duration_seconds"] for d in durations)
        if total_secs > 0:
            segs = ""
            for d in durations:
                pct = max(1, (d["duration_seconds"] / total_secs) * 100)
                color = _PHASE_COLORS.get(d["phase"], "#64748B")
                label = d["phase"].replace("_", " ")
                segs += (
                    f'<div style="width:{pct:.1f}%;background:{color};display:flex;'
                    f'align-items:center;justify-content:center;font-size:11.2px;'
                    f'color:white;font-weight:500;white-space:nowrap;overflow:hidden;'
                    f'padding:0 8px" title="{_e(label)}: {_fmt_dur(d["duration_seconds"])}">'
                    f'{_e(label)} {_fmt_dur(d["duration_seconds"])}</div>'
                )
            dur_bar = (
                f'<div style="display:flex;border-radius:4px;overflow:hidden;'
                f'border:1px solid #E2E8F0;margin-bottom:20px;height:32px">{segs}</div>'
            )

    # Error/failure box
    failure_box = ""
    if tree["errors"]:
        diag = extract_diagnostic_info(entries)
        hint = diag.get("hint", "")
        err_items = ""
        for err in tree["errors"]:
            e = err["entry"]
            err_items += (
                f'<div style="margin-top:4px"><strong>{_e(e.get("error_type", "Error"))}</strong>'
                f' <span class="meta">at {_e(e.get("timestamp", "")[:19])}</span>'
                f'<div style="margin-left:12px;color:#334155">{_e(e.get("error_message", ""))}</div></div>'
            )
        hint_html = f'<div style="margin-bottom:6px"><strong>Hint:</strong> {_e(hint)}</div>' if hint else ""
        failure_box = (
            f'<div style="margin-bottom:20px;padding:12px 16px;background:#FBE6F1;'
            f'border:1px solid #F5C6CB;border-left:4px solid #DB2626;border-radius:8px">'
            f'{hint_html}{err_items}</div>'
        )

    # --- Span tree sections ---
    def _section(title: str, icon_type: str, color: str, nodes: list, default_open: bool = True) -> str:
        if not nodes:
            return ""
        count = len(nodes)
        display = "" if default_open else "display:none;"
        chevron_rot = "rotate(90deg)" if default_open else ""
        return (
            f'<div style="border:1px solid #E2E8F0;border-radius:8px;margin-bottom:16px;overflow:hidden">'
            f'<div style="display:flex;align-items:center;gap:8px;padding:10px 16px;'
            f'background:#F7F9FB;border-bottom:1px solid #E2E8F0;font-size:13.2px;'
            f'font-weight:600;cursor:pointer" '
            f'onclick="var b=this.nextElementSibling;b.style.display=b.style.display===\'none\'?\'\':\' none\';'
            f'this.querySelector(\'svg\').style.transform=b.style.display===\'none\'?\'\':\' rotate(90deg)\'">'
            f'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#64748B" stroke-width="2" '
            f'style="transition:transform 0.2s;transform:{chevron_rot}"><path d="M9 18l6-6-6-6"/></svg>'
            f'<div style="width:24px;height:20px;display:flex;align-items:center;justify-content:center;'
            f'border-radius:4px;border:2px solid {color};font-size:10px;font-weight:700;color:{color};'
            f'background:#FFF">{_ICON_LETTERS.get(icon_type, "S")}</div>'
            f'{_e(title)}'
            f'<span style="flex:1"></span>'
            f'<span class="meta" style="font-weight:400">{count} {"event" if count == 1 else "events"}</span>'
            f'</div>'
            f'<div style="{display}">'
        )

    # L1 section
    l1_html = _section("L1: Ticket Intake", "trace", "#124D49", tree["l1"], default_open=False)
    for node in tree["l1"]:
        l1_html += _render_span_row(node["entry"], node.get("icon", "event"))
    if tree["l1"]:
        l1_html += '</div></div>'

    # L2 section
    l2_html = _section("L2: Agent Pipeline", "agent", "#9333EA", tree["l2"], default_open=True)
    for node in tree["l2"]:
        l2_html += _render_span_row(
            node["entry"], node.get("icon", "span"),
            duration=node.get("duration_seconds"),
            indent=1, artifacts=node.get("artifacts"),
        )
    if tree["l2"]:
        l2_html += '</div></div>'

    # L3 section
    l3_html = ""
    if tree["l3"]:
        l3_html = _section("L3: Post-PR Events", "event", "#6466F1", tree["l3"], default_open=False)
        for node in tree["l3"]:
            l3_html += _render_span_row(node["entry"], "event")
        l3_html += '</div></div>'

    # Raw events (collapsed)
    raw_html = (
        f'<div style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-top:8px">'
        f'<div style="padding:8px 16px;font-size:11.2px;color:#64748B;cursor:pointer;'
        f'border-bottom:1px solid #E2E8F0" '
        f'onclick="var b=this.nextElementSibling;b.style.display=b.style.display===\'none\'?\'\':\' none\';'
        f'this.querySelector(\'svg\').style.transform=b.style.display===\'none\'?\'\':\' rotate(90deg)\'">'
        f'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        f'<path d="M9 18l6-6-6-6"/></svg> Raw Events ({len(entries)})</div>'
        f'<div style="display:none">'
    )
    for e in entries:
        ts = e.get("timestamp", "")[:19]
        phase = e.get("phase", "")
        event = e.get("event", "")
        raw_html += (
            f'<div style="display:flex;gap:12px;padding:4px 16px;font-size:11.2px;color:#64748B;'
            f'border-bottom:1px solid #F1F5F9;font-family:ui-monospace,SFMono-Regular,Menlo,monospace">'
            f'<span style="color:#94A3B8;min-width:60px">{_e(ts[11:])}</span>'
            f'<span style="color:#6466F1;min-width:110px">{_e(phase)}</span>'
            f'<span style="color:#334155">{_e(event[:80])}</span></div>'
        )
    raw_html += '</div></div>'

    return f"""<!DOCTYPE html><html><head>
<title>Trace &mdash; {_e(ticket_id)}</title>
<style>{_LANGFUSE_STYLES}</style>
</head><body><div class="page">
{breadcrumb}{title}{summary_bar}{dur_bar}{failure_box}
{l1_html}{l2_html}{l3_html}{raw_html}
</div></body></html>"""


# --- Board View (Kanban) ---


def _classify_traces(
    traces: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split traces into in-flight, completed, and stuck/failed buckets."""
    in_flight: list[dict] = []
    completed: list[dict] = []
    stuck: list[dict] = []
    now = datetime.now(UTC)

    for t in traces:
        status = t.get("status", "Unknown")
        if status in _FAILED_STATUSES:
            stuck.append(t)
            continue
        if status in _COMPLETED_STATUSES:
            completed.append(t)
            continue
        threshold = _STUCK_THRESHOLDS.get(status)
        if threshold is not None:
            run_started = t.get("run_started_at", t.get("started_at", ""))
            if run_started:
                try:
                    age_hours = (now - datetime.fromisoformat(run_started)).total_seconds() / 3600
                    if age_hours > threshold:
                        stuck.append(t)
                        continue
                except (ValueError, TypeError):
                    pass
        in_flight.append(t)

    return in_flight, completed, stuck


def _render_board_column(title: str, color: str, traces: list[dict], count: int) -> str:
    """Render a single Kanban column."""
    cards = ""
    for t in traces:
        tid = _e(t["ticket_id"])
        status = t.get("status", "")
        duration = _e(t.get("duration", ""))
        badge_cls = _STATUS_BADGE.get(status, "badge-secondary")

        extra = ""
        if status in _FAILED_STATUSES or status in _STUCK_THRESHOLDS:
            entries = t.pop("_raw_entries", None) or read_trace(t["ticket_id"])
            diag = extract_diagnostic_info(entries)
            hint = diag.get("hint", "")
            if hint:
                extra = (
                    f'<div style="margin-top:6px;font-size:11.2px;color:#64748B">{_e(hint[:120])}</div>'
                )

        review = t.get("review_verdict", "")
        qa = t.get("qa_result", "")
        badges = ""
        if review:
            r_cls = "badge-success" if review == "APPROVED" else "badge-warning"
            badges += _badge(review, r_cls) + " "
        if qa:
            q_cls = "badge-success" if qa == "PASS" else "badge-error"
            badges += _badge(qa, q_cls) + " "
        pr = t.get("pr_url", "")
        pr_link = f'<a href="{_e(_safe_url(pr))}" target="_blank" style="font-size:11.2px">PR</a>' if pr else ""

        cards += (
            f'<div style="padding:10px;margin:6px 0;background:white;border-radius:6px;'
            f'border:1px solid #E2E8F0;cursor:pointer" onclick="location.href=\'/traces/{tid}\'">'
            f'<div style="display:flex;justify-content:space-between;align-items:center">'
            f'<a href="/traces/{tid}" style="font-weight:500;font-size:13.2px">{tid}</a>'
            f'{_badge(status, badge_cls)}</div>'
            f'<div style="margin-top:4px;font-size:11.2px;color:#64748B">{duration} {badges}{pr_link}</div>'
            f'{extra}</div>'
        )

    empty = '<div style="color:#94A3B8;text-align:center;padding:20px;font-size:11.2px">None</div>'
    return (
        f'<div style="flex:1;min-width:280px;max-width:400px">'
        f'<div style="padding:8px 12px;background:{color};color:white;border-radius:6px 6px 0 0;'
        f'font-weight:600;font-size:13.2px;display:flex;justify-content:space-between;align-items:center">'
        f'<span>{_e(title)}</span>'
        f'<span style="background:rgba(255,255,255,0.25);padding:1px 8px;border-radius:10px;font-size:11.2px">{count}</span></div>'
        f'<div style="background:#F7F9FB;border:1px solid #E2E8F0;border-top:none;border-radius:0 0 6px 6px;'
        f'padding:8px;min-height:100px;max-height:75vh;overflow-y:auto">'
        f'{cards or empty}</div></div>'
    )


def _render_board(traces: list[dict], total: int) -> str:
    """Render Kanban board view."""
    in_flight, completed, stuck = _classify_traces(traces)
    board = (
        f'<div style="display:flex;gap:16px;align-items:flex-start">'
        f'{_render_board_column("In-Flight", "#EA580C", in_flight, len(in_flight))}'
        f'{_render_board_column("Stuck / Failed", "#DB2626", stuck, len(stuck))}'
        f'{_render_board_column("Completed", "#124D49", completed, len(completed))}'
        f'</div>'
    )
    if not traces:
        board = '<div style="color:#94A3B8;text-align:center;padding:40px;font-size:13.2px">No tickets yet.</div>'

    return f"""<!DOCTYPE html><html><head>
<title>Status Board</title>
<meta http-equiv="refresh" content="10">
<style>{_LANGFUSE_STYLES}</style>
</head><body><div class="page">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
  <h1>Status Board <span class="meta" style="margin-left:8px">{total} tickets</span></h1>
  <div style="display:inline-flex;border:1px solid #E2E8F0;border-radius:6px;overflow:hidden">
    <button style="padding:4px 12px;font-size:11.2px;cursor:pointer;border:none;background:#FFF;color:#64748B;font-weight:500;border-right:1px solid #E2E8F0;font-family:inherit" onclick="location.href='/traces'">Table</button>
    <button style="padding:4px 12px;font-size:11.2px;cursor:pointer;border:none;background:#0F172A;color:#F7F9FB;font-weight:500;font-family:inherit">Board</button>
  </div>
</div>
{board}
</div></body></html>"""


# --- Routes ---


@router.get("/traces", response_class=HTMLResponse)
async def traces_list(
    pr: str = "", page: int = 1, per_page: int = 50, view: str = "table",
) -> str:
    """List ticket traces — table (default) or board (?view=board)."""
    if view == "board":
        traces = list_traces(limit=200)
        return _render_board(traces, len(traces))

    if pr:
        traces = list_traces(limit=0)
        traces = [t for t in traces if pr in t.get("pr_url", "")]
        total = len(traces)
    else:
        total = count_traces()
        offset = (page - 1) * per_page
        traces = list_traces(offset=offset, limit=per_page)

    return _render_trace_table(traces, total, page, per_page)


@router.get("/traces/{ticket_id}", response_class=HTMLResponse)
async def trace_detail(ticket_id: str) -> str:
    """Show Langfuse-style span tree for a ticket."""
    return _render_detail(ticket_id)


@router.get("/api/traces", response_model=None)
async def traces_api(
    pr: str = "", page: int = 1, per_page: int = 50,
) -> dict[str, object]:
    """JSON API for traces list."""
    if pr:
        traces = list_traces(limit=0)
        traces = [t for t in traces if pr in t.get("pr_url", "")]
        total = len(traces)
    else:
        total = count_traces()
        offset = (page - 1) * per_page
        traces = list_traces(offset=offset, limit=per_page)
    # Strip cached entries from JSON response (internal dashboard field)
    clean = [{k: v for k, v in t.items() if k != "_raw_entries"} for t in traces]
    return {"total": total, "page": page, "per_page": per_page, "traces": clean}


@router.get("/api/traces/{ticket_id}", response_model=None)
async def trace_api(ticket_id: str) -> list[dict[str, object]]:
    """JSON API for a single trace."""
    return read_trace(ticket_id)
