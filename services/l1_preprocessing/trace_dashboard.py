"""Trace dashboard — Langfuse-style observability views at /traces.

Provides three views:
- Table view (default): filterable trace list with phase dots and duration bars
- Board view (?view=board): Kanban columns (In-Flight / Completed / Stuck)
- Detail view (/traces/<id>): L1/L2/L3 span tree with artifact expansion
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from dashboard_common import (
    LANGFUSE_BASE_CSS,
)
from dashboard_common import (
    badge as _badge,
)
from dashboard_common import (
    escape_html as _e,
)
from dashboard_common import (
    fmt_dur as _fmt_dur,
)
from dashboard_common import (
    fmt_ts as _fmt_ts,
)
from dashboard_common import (
    safe_url as _safe_url,
)
from diagnostic import render_diagnostic_checklist, run_diagnostic_checklist
from investigate_command import build_investigate_command
from tracer import (
    build_span_tree,
    build_trace_list_row,
    compute_phase_durations,
    count_traces,
    extract_diagnostic_info,
    find_run_start_idx,
    list_traces,
    read_trace,
)

router = APIRouter()


def _safe_ticket_id(ticket_id: str) -> str:
    """Validate ticket_id against path traversal. Mirrors main._validate_ticket_id."""
    if not ticket_id or not re.fullmatch(r"[A-Za-z0-9_-]+", ticket_id):
        raise HTTPException(status_code=400, detail="Invalid ticket_id")
    return ticket_id


# --- Langfuse Design System ---
# Base CSS (body/typography/badge) imported from dashboard_common so
# all dashboards stay in lockstep on palette and typography.
_LANGFUSE_STYLES = LANGFUSE_BASE_CSS

# Status → badge class mapping — imported from the shared module so
# trace_dashboard and unified_dashboard cannot drift. Aliased to the
# existing private name so call sites below are unchanged.
from dashboard_common import STATUS_BADGE as _STATUS_BADGE  # noqa: E402

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
_FAILED_STATUSES = {"Escalated", "Agent Done (no PR)", "Failed", "Timed Out"}
_COMPLETED_STATUSES = {"Complete"}

# Statuses where the pipeline has finished running (no live updates expected).
# Derived from the two existing sets plus a few post-pipeline states so any
# future addition to _FAILED_STATUSES or _COMPLETED_STATUSES propagates
# automatically to the detail-page auto-refresh gate.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    _FAILED_STATUSES | _COMPLETED_STATUSES | {"PR Created", "Merged", "Cleaned Up"}
)


# HTML helpers (_e, _badge, _fmt_dur, _fmt_ts, _safe_url) are imported
# from dashboard_common at the top of this file. Historically they were
# duplicated across four dashboard modules with subtle drift risk.

# --- Trace List (Table View) ---


def _render_trace_table(traces: list[dict[str, Any]], total: int, page: int, per_page: int) -> str:
    """Render the Langfuse-style trace list table."""
    # Enrich each trace with phase dots (use cached entries + run-start
    # index from list_traces so build_trace_list_row doesn't re-scan).
    enriched: list[dict[str, Any]] = []
    for t in traces:
        entries = t.pop("_raw_entries", None) or read_trace(t["ticket_id"])
        run_start_idx = t.pop("_run_start_idx", None)
        enriched.append(
            build_trace_list_row(t, entries, run_start_idx=run_start_idx)
        )

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
        '<option>Failed</option><option>Timed Out</option><option>Cleaned Up</option>'
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
        title = t.get("ticket_title", "")
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
            f'#{_e(pr.split("/")[-1])}</a>' if pr else '<span class="meta">&mdash;</span>')
        mode_html = _badge(mode, "badge-secondary") if mode else '<span class="meta">&mdash;</span>'

        # Shorten ">24h (multi-run)" to ">24h" for table display
        dur_display = duration.replace(" (multi-run)", "") if duration else ""
        dur_html = f'<span style="color:{dur_color};white-space:nowrap">{_e(dur_display)}</span>' if dur_display else '<span class="meta">&mdash;</span>'
        bar_html = (
            f'<div style="width:80px;height:5px;border-radius:3px;'
            f'background:#F1F5F9;margin-top:3px">'
            f'<div style="width:{dur_pct}%;background:{dur_color};border-radius:3px;height:100%"></div>'
            f'</div>' if dur_pct else ""
        )

        _PHASE_LABELS = {
            "ticket_read": "Read Ticket",
            "planning": "Planning",
            "plan_review": "Plan Review",
            "implementation": "Implementation",
            "merge": "Merge",
            "security_scan": "Security Scan",
            "code_review": "Code Review",
            "judge": "Judge",
            "qa_validation": "QA Validation",
            "simplify": "Simplify",
            "pr_created": "PR Created",
            "complete": "Complete",
        }
        tooltip_parts = [_PHASE_LABELS.get(d["phase"], d["phase"].replace("_", " ").title()) for d in dots]
        tooltip = " → ".join(tooltip_parts)
        dots_html = f'<div style="display:flex;gap:2px" title="{_e(tooltip)}">'
        for d in dots:
            label = _PHASE_LABELS.get(d["phase"], d["phase"].replace("_", " ").title())
            dots_html += f'<div style="width:8px;height:8px;border-radius:2px;background:{d["color"]}" title="{_e(label)}"></div>'
        dots_html += '</div>'

        rows += (
            f'<tr data-status="{_e(status)}" data-mode="{_e(mode)}" data-ticket="{tid}" '
            f'onclick="location.href=\'/traces/{tid}\'" style="cursor:pointer">'
            f'<td><a href="/traces/{tid}" style="font-weight:500">{tid}</a></td>'
            f'<td style="overflow:hidden;text-overflow:ellipsis;max-width:300px" '
            f'title="{_e(title)}">'
            f'<span class="meta">{_e(title)}</span></td>'
            f'<td>{_badge(status, badge_cls)}</td>'
            f'<td>{mode_html}</td>'
            f'<td>{review_html}</td>'
            f'<td>{qa_html}</td>'
            f'<td><div>{dur_html}{bar_html}</div></td>'
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
tbody td {{ padding:8px 12px;border-bottom:1px solid #E2E8F0;vertical-align:middle;white-space:nowrap }}
tbody tr:last-child td {{ border-bottom:none }}
.view-toggle {{ display:inline-flex;border:1px solid #E2E8F0;border-radius:6px;overflow:hidden }}
.view-btn {{ padding:4px 12px;font-size:11.2px;cursor:pointer;border:none;background:#FFF;color:#64748B;font-weight:500;border-right:1px solid #E2E8F0;font-family:inherit }}
.view-btn:last-child {{ border-right:none }}
.view-btn.active {{ background:#0F172A;color:#F7F9FB }}
.view-btn:hover:not(.active) {{ background:#F1F5F9 }}
</style></head><body><div class="page">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
  <h1>Traces <span class="meta" style="margin-left:12px;font-weight:400"><a href="/dashboard">Dashboard</a> | <a href="/autonomy">Autonomy</a></span></h1>
  <div class="view-toggle">
    <button class="view-btn active" onclick="location.href='/traces'">Table</button>
    <button class="view-btn" onclick="location.href='/traces?view=board'">Board</button>
  </div>
</div>
{stats}
{filters}
<table>
<thead><tr>
  <th style="width:100px">Ticket</th><th>Title</th><th style="width:110px">Status</th>
  <th style="width:65px">Mode</th><th style="width:80px">Review</th>
  <th style="width:55px">QA</th><th style="width:140px">Duration</th>
  <th style="width:100px">Phases</th><th style="width:55px">PR</th>
  <th style="width:100px">Started</th>
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
    entry: dict[str, Any], icon_type: str, duration: float | None = None,
    indent: int = 0, artifacts: list[dict[str, Any]] | None = None,
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

    # Duration — always show relative duration when available, never raw timestamps
    dur_html = ""
    if duration is not None:
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


def _diag_checklist_html(entries: list[dict[str, Any]]) -> str:
    return render_diagnostic_checklist(run_diagnostic_checklist(entries))


# ---------------------------------------------------------------------------
# Detail-view section renderers
# ---------------------------------------------------------------------------
#
# ``_render_detail`` used to be a ~250-line function that inlined every
# visual section (summary bar, investigate / discuss disclosures,
# phase duration bar, failure box, span trees, raw events). Each
# section is extracted below so the function body becomes a thin
# orchestration of fragment assembly and each helper is individually
# testable with plain dict fixtures instead of having to go through
# the full ``read_trace`` path.


def _render_investigate_box(ticket_id: str) -> str:
    """Copy-investigation-command disclosure — native ``<details>``.

    Builds the ready-to-paste shell snippet from the canonical
    template in ``investigate_command.py`` so the dashboard and the
    ``/traces/{id}/discuss`` endpoint cannot drift.
    """
    investigate_cmd = build_investigate_command(ticket_id)
    return (
        '<details style="margin-bottom:20px;padding:10px 14px;background:#F7F9FB;'
        'border:1px solid #E2E8F0;border-radius:8px">'
        '<summary style="cursor:pointer;font-weight:600;font-size:12px;color:#334155">'
        'Investigate this trace locally (copy command)</summary>'
        '<pre style="margin-top:10px;padding:10px 12px;background:#0F172A;color:#E2E8F0;'
        'border-radius:6px;font-size:11.5px;line-height:1.55;white-space:pre-wrap;'
        'word-break:break-all;font-family:ui-monospace,SFMono-Regular,Menlo,monospace">'
        f'{_e(investigate_cmd)}</pre></details>'
    )


def _render_discuss_box(ticket_id: str) -> str:
    """Audited 'Discuss with Claude' disclosure — three-step command.

    Mints a session token via ``POST /traces/{id}/discuss`` (writes
    to ``discuss-audit.jsonl``), runs the returned investigate
    command, then feeds the Claude transcript to
    ``capture_discuss_output.py``. Both this and the cheaper
    ``_render_investigate_box`` live inside ``<details>`` elements
    so they expand without JavaScript.
    """
    discuss_cmd = (
        "# Step 1: request a session token (writes to discuss-audit.jsonl)\n"
        f"curl -sSf -X POST http://localhost:8000/traces/{ticket_id}/discuss \\\n"
        "  -H 'X-API-Key: ...' | jq -r .investigate_command > /tmp/investigate.sh\n"
        "\n"
        "# Step 2: run the investigation\n"
        "bash /tmp/investigate.sh\n"
        "\n"
        "# Step 3: when Claude's output has the three sections\n"
        "# (## Root cause, ## Proposed fix, ## Memory entry):\n"
        "python scripts/capture_discuss_output.py --transcript /tmp/transcript.md"
    )
    return (
        '<details style="margin-bottom:20px;padding:10px 14px;background:#F7F9FB;'
        'border:1px solid #E2E8F0;border-radius:8px">'
        '<summary style="cursor:pointer;font-weight:600;font-size:12px;color:#334155">'
        '\U0001f50d Open in Claude for investigation</summary>'
        '<pre style="margin-top:10px;padding:10px 12px;background:#0F172A;color:#E2E8F0;'
        'border-radius:6px;font-size:11.5px;line-height:1.55;white-space:pre-wrap;'
        'word-break:break-all;font-family:ui-monospace,SFMono-Regular,Menlo,monospace">'
        f'{_e(discuss_cmd)}</pre></details>'
    )


def _render_duration_bar(durations: list[dict[str, Any]]) -> str:
    """Phase-duration bar — proportional segments colored by phase.

    Returns empty string when ``durations`` is empty or the total
    duration is zero (avoids a divide-by-zero inside the per-segment
    percentage computation).
    """
    if not durations:
        return ""
    total_secs = sum(d["duration_seconds"] for d in durations)
    if total_secs <= 0:
        return ""
    segs = ""
    for d in durations:
        if d["duration_seconds"] <= 0:
            continue  # skip zero-duration phases (e.g., pr_created logged same instant)
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
    return (
        f'<div style="display:flex;border-radius:4px;overflow:hidden;'
        f'border:1px solid #E2E8F0;margin-bottom:20px;height:32px">{segs}</div>'
    )


def _render_failure_box(
    entries: list[dict[str, Any]],
    tree: dict[str, Any],
    run_start_idx: int,
) -> str:
    """Error/failure box — only rendered when ``tree['errors']`` is non-empty."""
    if not tree["errors"]:
        return ""
    diag = extract_diagnostic_info(entries, run_start_idx=run_start_idx)
    hint = diag.get("hint", "")
    err_items = ""
    for err in tree["errors"]:
        e = err["entry"]
        err_items += (
            f'<div style="margin-top:4px"><strong>{_e(e.get("error_type", "Error"))}</strong>'
            f' <span class="meta">at {_e(e.get("timestamp", "")[:19])}</span>'
            f'<div style="margin-left:12px;color:#334155">{_e(e.get("error_message", ""))}</div></div>'
        )
    hint_html = (
        f'<div style="margin-bottom:6px"><strong>Hint:</strong> {_e(hint)}</div>'
        if hint else ""
    )
    return (
        f'<div style="margin-bottom:20px;padding:12px 16px;background:#FBE6F1;'
        f'border:1px solid #F5C6CB;border-left:4px solid #DB2626;border-radius:8px">'
        f'{hint_html}{err_items}</div>'
    )


def _render_detail(ticket_id: str) -> str:
    """Render the Langfuse-style trace detail view."""
    entries = read_trace(ticket_id)

    if not entries:
        return (
            f'<!DOCTYPE html><html><head><style>{_LANGFUSE_STYLES}</style></head>'
            f'<body><div class="page"><h1>No trace found for {_e(ticket_id)}</h1>'
            f'<a href="/traces">&larr; Back</a></div></body></html>'
        )

    # Compute the run-start index once and thread it into every consumer
    # below. Previously, each of build_span_tree, compute_phase_durations,
    # and extract_diagnostic_info was doing its own full reverse scan.
    run_start_idx = find_run_start_idx(entries)
    tree = build_span_tree(entries, run_start_idx=run_start_idx)
    s = tree["summary"]
    durations = compute_phase_durations(entries, run_start_idx=run_start_idx)

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
    summary_items.append(f'<div><span class="meta">Analyst (API)</span> <span style="font-weight:500">{_e(token_str)}</span></div>')
    max_in = s.get("billing_max_tokens_in", 0)
    max_out = s.get("billing_max_tokens_out", 0)
    if max_in or max_out:
        max_str = f'{max_in:,} in / {max_out:,} out'
        summary_items.append(f'<div><span class="meta">Agent (Max)</span> <span style="font-weight:500">{_e(max_str)}</span></div>')
    if pr_url:
        summary_items.append(f'<div><span class="meta">PR</span> <a href="{_e(_safe_url(pr_url))}" target="_blank">#{_e(pr_url.split("/")[-1])}</a></div>')

    summary_bar = (
        f'<div style="display:flex;gap:24px;flex-wrap:wrap;align-items:center;'
        f'padding:12px 16px;background:#F7F9FB;border:1px solid #E2E8F0;'
        f'border-radius:8px;margin-bottom:20px">{"".join(summary_items)}</div>'
    )

    # Section builders live at module scope for testability — see the
    # header comment block above _render_investigate_box.
    investigate_box = _render_investigate_box(ticket_id)
    discuss_box = _render_discuss_box(ticket_id)
    dur_bar = _render_duration_bar(durations)
    failure_box = _render_failure_box(entries, tree, run_start_idx)

    # --- Span tree sections ---
    def _section(title: str, icon_type: str, color: str, nodes: list[dict[str, Any]], default_open: bool = True) -> str:
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

    # Raw events (collapsed) — same accordion pattern as L1/L2/panels
    raw_html = (
        f'<div style="border:1px solid #E2E8F0;border-radius:8px;overflow:hidden;margin-bottom:16px">'
        f'<div style="display:flex;align-items:center;gap:8px;padding:10px 16px;'
        f'background:#F7F9FB;border-bottom:1px solid #E2E8F0;'
        f'font-weight:600;font-size:13.2px;cursor:pointer" '
        f'onclick="var b=this.nextElementSibling;b.style.display=b.style.display===\'none\'?\'\':\' none\';'
        f'this.querySelector(\'svg\').style.transform=b.style.display===\'none\'?\'\':\' rotate(90deg)\'">'
        f'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#64748B" stroke-width="2" '
        f'style="transition:transform 0.2s"><path d="M9 18l6-6-6-6"/></svg>'
        f'Raw Events ({len(entries)})</div>'
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

    # Auto-refresh while the pipeline is still running so users watching a
    # live run see new events without manually reloading. Stops refreshing
    # once the run reaches a terminal state (Complete, PR Created, Failed,
    # etc.) to avoid hammering L1 indefinitely on long-finished traces.
    refresh_meta = (
        '<meta http-equiv="refresh" content="5">'
        if status not in _TERMINAL_STATUSES
        else ""
    )

    # Session observability panels (commit 2 of post-mortem observability plan).
    # Isolated in trace_dashboard_panels.py to avoid merge conflicts with
    # commits 3 and 4 also modifying this file.
    from trace_dashboard_panels import render_session_panels, render_tool_usage_panel
    session_html = render_session_panels(entries)
    tool_usage_html = render_tool_usage_panel(entries)
    diag_html = _diag_checklist_html(entries)

    return f"""<!DOCTYPE html><html><head>
{refresh_meta}
<title>Trace &mdash; {_e(ticket_id)}</title>
<style>{_LANGFUSE_STYLES}</style>
</head><body><div class="page">
{breadcrumb}{title}{summary_bar}{investigate_box}{discuss_box}{dur_bar}{failure_box}
{l1_html}{l2_html}{l3_html}
{session_html}
{raw_html}
{diag_html}
{tool_usage_html}
</div></body></html>"""


# --- Board View (Kanban) ---


def _classify_traces(
    traces: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split traces into in-flight, completed, and stuck/failed buckets."""
    in_flight: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    stuck: list[dict[str, Any]] = []
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


def _render_board_column(title: str, color: str, traces: list[dict[str, Any]], count: int) -> str:
    """Render a single Kanban column."""
    # In-flight statuses get the pulsing "running" indicator
    in_flight_statuses = {
        "Dispatched", "Processing", "Enriched", "Planned",
        "Implementing", "Review Done", "QA Done", "Merged", "CI Fix",
    }
    cards = ""
    for t in traces:
        tid = _e(t["ticket_id"])
        status = t.get("status", "")
        duration = _e(t.get("duration", ""))
        current_phase = t.get("current_phase", "")
        badge_cls = _STATUS_BADGE.get(status, "badge-secondary")
        is_running = status in in_flight_statuses

        extra = ""
        if status in _FAILED_STATUSES or status in _STUCK_THRESHOLDS:
            entries = t.pop("_raw_entries", None) or read_trace(t["ticket_id"])
            run_start_idx = t.pop("_run_start_idx", None)
            diag = extract_diagnostic_info(
                entries, run_start_idx=run_start_idx
            )
            hint = diag.get("hint", "")
            if hint:
                extra = (
                    f'<div style="margin-top:6px;font-size:11.2px;color:#64748B">{_e(hint[:120])}</div>'
                )

        # Live progress row (running tickets only)
        progress_html = ""
        if is_running and current_phase:
            progress_html = (
                f'<div style="margin-top:6px;display:flex;align-items:center;gap:6px;'
                f'font-size:11.2px;color:#EA580C">'
                f'<span class="pulse-dot" style="width:8px;height:8px;border-radius:50%;'
                f'background:#EA580C;display:inline-block;animation:pulse 1.5s ease-in-out infinite"></span>'
                f'<span>Running: <strong>{_e(current_phase)}</strong></span>'
                f'</div>'
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
            f'{progress_html}'
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


def _render_board(traces: list[dict[str, Any]], total: int) -> str:
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
<meta http-equiv="refresh" content="5">
<style>{_LANGFUSE_STYLES}
@keyframes pulse {{
  0%, 100% {{ opacity: 1; transform: scale(1); }}
  50% {{ opacity: 0.4; transform: scale(0.85); }}
}}
</style>
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
    _safe_ticket_id(ticket_id)
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
    # Strip cached internal fields from JSON response — these are only
    # for dashboard-side performance (avoiding reread + rescan) and are
    # not part of the public API surface.
    internal_fields = {"_raw_entries", "_run_start_idx"}
    clean = [
        {k: v for k, v in t.items() if k not in internal_fields}
        for t in traces
    ]
    return {"total": total, "page": page, "per_page": per_page, "traces": clean}


@router.get("/api/traces/{ticket_id}", response_model=None)
async def trace_api(ticket_id: str) -> list[dict[str, Any]]:
    """JSON API for a single trace."""
    _safe_ticket_id(ticket_id)
    entries = read_trace(ticket_id)
    if not entries:
        raise HTTPException(status_code=404, detail=f"No trace found for {ticket_id}")
    return entries
