"""Trace dashboard — serves HTML views of ticket traces at /traces."""

from __future__ import annotations

import html
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from tracer import (
    compute_phase_durations,
    count_traces,
    extract_diagnostic_info,
    extract_escalation_reason,
    list_traces,
    read_trace,
)

router = APIRouter()

# --- Shared constants ---

_STATUS_COLORS: dict[str, str] = {
    "Complete": "#2D8B57",
    "PR Created": "#2D8B57",
    "Escalated": "#c0392b",
    "QA Done": "#2E6CA4",
    "Review Done": "#2E6CA4",
    "Merged": "#2E6CA4",
    "Implementing": "#E8792F",
    "Planned": "#E8792F",
    "Enriched": "#888",
    "Processing": "#888",
    "Received": "#888",
    "Dispatched": "#E8792F",
    "CI Fix": "#E8792F",
    "Agent Done (no PR)": "#c0392b",
}

_PHASE_COLORS: dict[str, str] = {
    "webhook": "#888",
    "analyst": "#2E6CA4",
    "pipeline": "#2E6CA4",
    "ticket_read": "#1B2A4A",
    "planning": "#6c5ce7",
    "plan_review": "#6c5ce7",
    "implementation": "#E8792F",
    "merge": "#E8792F",
    "code_review": "#2D8B57",
    "qa_validation": "#2D8B57",
    "pr_created": "#1B2A4A",
    "complete": "#2D8B57",
    "artifact": "#888",
    "l3_pr_review": "#6c5ce7",
    "l3_ci_fix": "#c0392b",
    "l3_comment": "#2E6CA4",
    "l3_changes_requested": "#c0392b",
    "spawn": "#c0392b",
}

_COMPLETED_STATUSES = {"Complete"}
_STUCK_ELIGIBLE_STATUSES = {"Received", "Processing", "Enriched", "Dispatched"}
_FAILED_STATUSES = {"Escalated", "Agent Done (no PR)"}

_BASE_STYLES = """
    body { font-family: -apple-system, sans-serif; margin: 40px; background: #fafafa; color: #333; }
    h1 { color: #1B2A4A; border-bottom: 3px solid #2E6CA4; padding-bottom: 10px; }
    a { color: #2E6CA4; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .count { color: #888; margin-left: 8px; font-size: 0.9em; }
"""


def _escape(text: str) -> str:
    """HTML-escape a string to prevent XSS."""
    return html.escape(text, quote=True)


def _safe_url(url: str) -> str:
    """Return the URL only if it uses a safe scheme, otherwise return '#'."""
    stripped = url.strip().lower()
    if stripped.startswith("https://") or stripped.startswith("http://"):
        return url
    return "#"


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable Nm Ss string."""
    if seconds <= 0:
        return "0s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _badge(text: str, color: str) -> str:
    """Render a colored badge span."""
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
        f'font-size:0.8em;font-weight:600;color:white;background:{color}">'
        f'{_escape(text)}</span>'
    )


def _classify_traces(
    traces: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split traces into in-flight, completed, and stuck/failed buckets."""
    in_flight: list[dict] = []
    completed: list[dict] = []
    stuck: list[dict] = []

    now = datetime.now(UTC)

    for t in traces:
        status = t["status"]

        if status in _FAILED_STATUSES:
            stuck.append(t)
            continue

        if status in _COMPLETED_STATUSES:
            completed.append(t)
            continue

        # Check for stuck: early-stage status and >1 hour since run started
        if status in _STUCK_ELIGIBLE_STATUSES:
            run_started = t.get("run_started_at", t.get("started_at", ""))
            if run_started:
                try:
                    started_dt = datetime.fromisoformat(run_started)
                    age_hours = (now - started_dt).total_seconds() / 3600
                    if age_hours > 1:
                        stuck.append(t)
                        continue
                except (ValueError, TypeError):
                    pass

        in_flight.append(t)

    return in_flight, completed, stuck


# --- Status Board View (default) ---


def _render_card(t: dict, card_type: str) -> str:
    """Render a single ticket card for the status board."""
    status = t["status"]
    status_color = _STATUS_COLORS.get(status, "#888")
    ticket_link = f'<a href="/traces/{_escape(t["ticket_id"])}">{_escape(t["ticket_id"])}</a>'
    duration = _escape(t.get("duration", ""))
    mode = _escape(t.get("pipeline_mode", ""))

    if card_type == "completed":
        review = t.get("review_verdict", "")
        qa = t.get("qa_result", "")
        pr_url = t.get("pr_url", "")
        badges = ""
        if review:
            rc = "#2D8B57" if review == "APPROVED" else "#E8792F"
            badges += _badge(review, rc) + " "
        if qa:
            qc = "#2D8B57" if qa == "PASS" else "#c0392b"
            badges += _badge(qa, qc) + " "
        pr_link = f'<a href="{_escape(_safe_url(pr_url))}" target="_blank">PR</a>' if pr_url else ""
        return (
            f'<div style="padding:12px;margin:6px 0;background:white;border-radius:6px;'
            f'border-left:4px solid #2D8B57;box-shadow:0 1px 2px rgba(0,0,0,0.06)">'
            f'<div style="display:flex;justify-content:space-between;align-items:center">'
            f'<span style="font-weight:600">{ticket_link}</span>'
            f'<span style="font-size:0.85em;color:#888">{duration}</span></div>'
            f'<div style="margin-top:6px">{badges}{pr_link}</div>'
            f'</div>'
        )

    if card_type == "stuck":
        # Get escalation reason and diagnostics from trace entries
        entries = read_trace(t["ticket_id"])
        reason = extract_escalation_reason(entries)
        diag = extract_diagnostic_info(entries)

        reason_html = (
            f'<div style="margin-top:6px;font-size:0.85em;color:#c0392b">'
            f'{_escape(reason)}</div>'
            if reason else ""
        )

        # Build expandable diagnostics section
        diag_html = ""
        hint = diag.get("hint", "")
        errors = diag.get("errors", [])
        if hint or errors:
            error_items = ""
            for err in errors:
                ts = _escape(err.get("timestamp", ""))
                etype = _escape(err.get("error_type", ""))
                emsg = _escape(err.get("error_message", ""))
                stderr = err.get("error_context", {}).get("stderr", "")
                error_items += (
                    f'<div style="margin-top:4px">'
                    f'<strong>{etype}</strong>'
                    f'<span style="color:#888"> at {ts}</span>'
                    f'<div style="margin-left:12px;white-space:pre-wrap">'
                    f'{emsg}</div>'
                    f'</div>'
                )
                if stderr:
                    error_items += (
                        f'<pre style="margin:4px 0 0 12px;padding:6px;'
                        f'background:#fff0f0;border-radius:3px;font-size:0.9em;'
                        f'max-height:150px;overflow:auto">'
                        f'{_escape(str(stderr)[:500])}</pre>'
                    )

            hint_html = (
                f'<div style="margin-bottom:6px;color:#333">'
                f'<strong>Hint:</strong> {_escape(hint)}</div>'
                if hint else ""
            )

            diag_html = (
                f'<details style="margin-top:8px">'
                f'<summary style="cursor:pointer;font-size:0.85em;'
                f'color:#555;font-weight:600">Diagnostics</summary>'
                f'<div style="margin-top:6px;padding:8px;background:#fff0f0;'
                f'border-radius:4px;font-size:0.82em">'
                f'{hint_html}{error_items}'
                f'</div></details>'
            )

        return (
            f'<div class="stuck-card" style="padding:12px;margin:6px 0;background:#fff5f5;'
            f'border-radius:6px;border-left:4px solid #c0392b;'
            f'box-shadow:0 1px 2px rgba(0,0,0,0.06)">'
            f'<div style="display:flex;justify-content:space-between;align-items:center">'
            f'<span style="font-weight:600">{ticket_link}'
            f'<span class="ticket-id" style="display:none">{_escape(t["ticket_id"])}</span></span>'
            f'{_badge(status, "#c0392b")}</div>'
            f'<div style="margin-top:4px;font-size:0.85em;color:#888">{duration}</div>'
            f'{reason_html}'
            f'{diag_html}'
            f'</div>'
        )

    # in_flight
    return (
        f'<div style="padding:12px;margin:6px 0;background:white;border-radius:6px;'
        f'border-left:4px solid {status_color};box-shadow:0 1px 2px rgba(0,0,0,0.06)">'
        f'<div style="display:flex;justify-content:space-between;align-items:center">'
        f'<span style="font-weight:600">{ticket_link}</span>'
        f'{_badge(status, status_color)}</div>'
        f'<div style="margin-top:4px;font-size:0.85em;color:#888">'
        f'{mode} {duration}</div>'
        f'</div>'
    )


def _render_board(traces: list[dict], total: int) -> str:
    """Render the status board HTML."""
    in_flight, completed, stuck = _classify_traces(traces)

    sections = ""

    if stuck:
        cards = "".join(_render_card(t, "stuck") for t in stuck)
        sections += (
            f'<div style="margin-bottom:30px">'
            f'<h2 style="color:#c0392b;font-size:1.1em;margin-bottom:8px">'
            f'Stuck / Failed <span class="count">{len(stuck)}</span></h2>'
            f'{cards}</div>'
        )

    if in_flight:
        cards = "".join(_render_card(t, "in_flight") for t in in_flight)
        sections += (
            f'<div style="margin-bottom:30px">'
            f'<h2 style="color:#E8792F;font-size:1.1em;margin-bottom:8px">'
            f'In-Flight <span class="count">{len(in_flight)}</span></h2>'
            f'{cards}</div>'
        )

    if completed:
        cards = "".join(_render_card(t, "completed") for t in completed)
        sections += (
            f'<div style="margin-bottom:30px">'
            f'<h2 style="color:#2D8B57;font-size:1.1em;margin-bottom:8px">'
            f'Completed <span class="count">{len(completed)}</span></h2>'
            f'{cards}</div>'
        )

    if not sections:
        sections = '<p style="color:#888;text-align:center;margin:40px 0">No tickets yet.</p>'

    # Stuck notification JS (fires once per set of stuck tickets via sessionStorage)
    notification_js = """
<script>
(function() {
  var stuckCards = document.querySelectorAll('.stuck-card');
  if (!stuckCards.length) return;
  var ids = [];
  stuckCards.forEach(function(c) {
    var el = c.querySelector('.ticket-id');
    if (el) ids.push(el.textContent);
  });
  ids.sort();
  var key = 'notified_stuck';
  var sig = ids.join(',');
  if (sessionStorage.getItem(key) === sig) return;
  if (Notification.permission === 'denied') return;
  Notification.requestPermission().then(function(p) {
    if (p === 'granted') {
      new Notification('Stuck tickets', {body: sig});
      sessionStorage.setItem(key, sig);
    }
  });
})();
</script>"""

    view_toggle = '<div style="text-align:right;margin-bottom:10px;font-size:0.85em"><a href="/traces?view=table">Table view</a></div>'

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Agent Harness — Status Board</title>
    <meta http-equiv="refresh" content="30">
    <style>
        {_BASE_STYLES}
        h2 {{ margin-top: 0; }}
    </style>
</head>
<body>
    <h1>Agent Harness — Status Board<span class="count">{total} tickets</span></h1>
    {view_toggle}
    {sections}
    {notification_js}
</body>
</html>"""


# --- Table View (legacy, via ?view=table) ---


def _render_table(
    traces: list[dict], total: int, page: int, per_page: int, total_pages: int
) -> str:
    """Render the legacy table HTML."""
    rows = ""
    for t in traces:
        _status = t["status"]
        status_color = _STATUS_COLORS.get(_status, "#888")
        review_badge = ""
        if t["review_verdict"]:
            rc = "#2D8B57" if t["review_verdict"] == "APPROVED" else "#E8792F"
            review_badge = f'<span style="color:{rc}">{_escape(t["review_verdict"])}</span>'
        qa_badge = ""
        if t["qa_result"]:
            qc = "#2D8B57" if t["qa_result"] == "PASS" else "#c0392b"
            qa_badge = f'<span style="color:{qc}">{_escape(t["qa_result"])}</span>'
        pr_link = f'<a href="{_escape(_safe_url(t["pr_url"]))}" target="_blank">PR</a>' if t["pr_url"] else "\u2014"
        duration = _escape(t.get("duration", ""))

        rows += f"""<tr>
            <td><a href="/traces/{_escape(t['ticket_id'])}">{_escape(t['ticket_id'])}</a></td>
            <td><span style="color:{status_color}">{_escape(t['status'][:40])}</span></td>
            <td>{_escape(t.get('pipeline_mode', ''))}</td>
            <td>{review_badge}</td>
            <td>{qa_badge}</td>
            <td>{pr_link}</td>
            <td>{duration}</td>
            <td>{int(t.get('entries', 0))}</td>
            <td style="font-size:0.85em">{_escape(str(t.get('started_at', ''))[:19])}</td>
        </tr>"""

    view_toggle = '<div style="text-align:right;margin-bottom:10px;font-size:0.85em"><a href="/traces">Board view</a></div>'

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Agent Harness — Traces</title>
    <style>
        {_BASE_STYLES}
        table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        th {{ background: #1B2A4A; color: white; padding: 10px 14px; text-align: left; font-size: 0.9em; }}
        td {{ padding: 8px 14px; border-bottom: 1px solid #eee; font-size: 0.9em; }}
        tr:hover {{ background: #f5f5f5; }}
    </style>
</head>
<body>
    <h1>Agent Harness — Traces<span class="count">{total} tickets</span></h1>
    {view_toggle}
    <table>
        <thead>
            <tr><th>Ticket</th><th>Status</th><th>Mode</th><th>Review</th><th>QA</th><th>PR</th><th>Duration</th><th>Events</th><th>Started</th></tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    <div style="margin:20px 0;text-align:center">
        {'<a href="/traces?view=table&page=' + str(page - 1) + '">← Prev</a> ' if page > 1 else ''}
        Page {page} of {total_pages}
        {' <a href="/traces?view=table&page=' + str(page + 1) + '">Next →</a>' if page < total_pages else ''}
    </div>
</body>
</html>"""


# --- Routes ---


@router.get("/traces", response_class=HTMLResponse)
async def traces_list(
    pr: str = "", page: int = 1, per_page: int = 50, view: str = "board",
) -> str:
    """List ticket traces — board view (default) or table view (?view=table)."""
    if view == "board" and not pr:
        # Board view: load all (capped at 200) for bucketing
        traces = list_traces(limit=200)
        total = len(traces)
        return _render_board(traces, total)

    # Table view (or PR-filtered)
    if pr:
        traces = list_traces(limit=0)
        traces = [t for t in traces if pr in t.get("pr_url", "")]
        total = len(traces)
    else:
        total = count_traces()
        offset = (page - 1) * per_page
        traces = list_traces(offset=offset, limit=per_page)

    total_pages = max(1, (total + per_page - 1) // per_page)
    return _render_table(traces, total, page, per_page, total_pages)


@router.get("/traces/{ticket_id}", response_class=HTMLResponse)
async def trace_detail(ticket_id: str) -> str:
    """Show the full trace timeline for a ticket with phase durations and summary."""
    entries = read_trace(ticket_id)

    if not entries:
        return f"""<!DOCTYPE html><html><body>
            <h1>No trace found for {_escape(ticket_id)}</h1>
            <a href="/traces">\u2190 Back to traces</a>
        </body></html>"""

    # --- Phase duration bar ---
    durations = compute_phase_durations(entries)
    duration_bar = ""
    if durations:
        total_secs = sum(d["duration_seconds"] for d in durations)
        if total_secs > 0:
            segments = ""
            for d in durations:
                pct = max(1, (d["duration_seconds"] / total_secs) * 100)
                color = _PHASE_COLORS.get(d["phase"], "#888")
                label = d["phase"].replace("_", " ")
                dur_str = _format_duration(d["duration_seconds"])
                segments += (
                    f'<div style="width:{pct:.1f}%;background:{color};padding:6px 4px;'
                    f'color:white;font-size:0.75em;text-align:center;overflow:hidden;'
                    f'white-space:nowrap" title="{_escape(label)}: {dur_str}">'
                    f'{_escape(label)}<br>{dur_str}</div>'
                )
            duration_bar = (
                f'<div style="display:flex;border-radius:6px;overflow:hidden;'
                f'margin:16px 0;box-shadow:0 1px 2px rgba(0,0,0,0.1)">'
                f'{segments}</div>'
            )

    # --- Summary box ---
    pr_url = ""
    review_verdict = ""
    qa_result = ""
    tokens_in = 0
    tokens_out = 0
    has_tokens = False
    for e in entries:
        if e.get("pr_url"):
            pr_url = str(e["pr_url"])
        if e.get("event") == "Pipeline complete":
            review_verdict = str(e.get("review_verdict", ""))
            qa_result = str(e.get("qa_result", ""))
        if e.get("event") == "analyst_completed":
            ti = e.get("tokens_in", 0)
            to = e.get("tokens_out", 0)
            if isinstance(ti, int) and ti > 0:
                tokens_in = ti
                tokens_out = to if isinstance(to, int) else 0
                has_tokens = True

    # Compute total wall-clock time
    first_ts = entries[0].get("timestamp", "")
    last_ts = entries[-1].get("timestamp", "")
    wall_clock = ""
    try:
        start_dt = datetime.fromisoformat(first_ts)
        end_dt = datetime.fromisoformat(last_ts)
        wall_clock = _format_duration((end_dt - start_dt).total_seconds())
    except (ValueError, TypeError):
        pass

    token_info = f"{tokens_in:,} in / {tokens_out:,} out" if has_tokens else "N/A"

    # Phase breakdown text
    phase_text = ""
    if durations:
        parts = []
        for d in durations:
            label = d["phase"].replace("_", " ")
            # Abbreviate common phase names
            abbrevs = {
                "ticket read": "read", "implementation": "impl",
                "code review": "review", "qa validation": "QA",
                "pr created": "PR",
            }
            label = abbrevs.get(label, label)
            parts.append(f"{_escape(label)}: {_format_duration(d['duration_seconds'])}")
        phase_text = " &middot; ".join(parts)

    summary = f"""
    <div style="display:flex;flex-wrap:wrap;gap:20px;margin:20px 0;padding:16px;
         background:white;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <div><strong>Total:</strong> {_escape(wall_clock) if wall_clock else 'N/A'}</div>
        <div><strong>Analyst tokens:</strong> {token_info}</div>
        {f'<div><strong>Review:</strong> {_escape(review_verdict)}</div>' if review_verdict else ''}
        {f'<div><strong>QA:</strong> {_escape(qa_result)}</div>' if qa_result else ''}
        {f'<div><strong>PR:</strong> <a href="{_escape(_safe_url(pr_url))}" target="_blank">View PR</a></div>' if pr_url else ''}
    </div>
    {f'<div style="font-size:0.85em;color:#555;margin-bottom:16px">{phase_text}</div>' if phase_text else ''}"""

    # --- Failure reason box ---
    failure_box = ""
    # Derive status for this ticket
    events = [e.get("event", "") for e in entries]
    is_failed = "Escalated" in events or "Agent Done (no PR)" in [e.get("status", "") for e in entries]
    if is_failed:
        reason = extract_escalation_reason(entries)
        if reason:
            # Check for full escalation artifact content
            esc_content = ""
            for e in entries:
                if e.get("event") == "escalation_artifact":
                    esc_content = str(e.get("content", ""))
                    break
            details_html = ""
            if esc_content:
                details_html = (
                    f'<details style="margin-top:8px"><summary>Full escalation report</summary>'
                    f'<pre style="white-space:pre-wrap;max-height:300px;overflow:auto;'
                    f'background:#fff5f5;padding:10px;border-radius:4px;font-size:0.85em">'
                    f'{_escape(esc_content)}</pre></details>'
                )
            failure_box = (
                f'<div style="margin:16px 0;padding:12px 16px;background:#fff5f5;'
                f'border:1px solid #f5c6cb;border-left:4px solid #c0392b;border-radius:6px">'
                f'<strong style="color:#c0392b">Failure:</strong> {_escape(reason)}'
                f'{details_html}</div>'
            )

    # --- Timeline ---
    timeline = ""
    for i, e in enumerate(entries):
        phase = e.get("phase", "")
        event = e.get("event", "")
        ts = e.get("timestamp", "")[:19]
        source = e.get("source", "l1")

        color = _PHASE_COLORS.get(phase, "#555")

        details = ""
        skip_keys = {"trace_id", "ticket_id", "timestamp", "phase", "event", "source"}
        extra = {k: v for k, v in e.items() if k not in skip_keys and v}

        if "content" in extra:
            content = str(extra.pop("content"))
            details += (
                f'<details><summary>View content ({len(content)} chars)</summary>'
                f'<pre style="white-space:pre-wrap;max-height:400px;overflow:auto;'
                f'background:#f5f5f5;padding:10px;border-radius:4px;font-size:0.85em">'
                f'{_escape(content)}</pre></details>'
            )

        if extra:
            for k, v in extra.items():
                val = str(v)
                if val.startswith("http"):
                    details += (
                        f'<div style="margin:2px 0"><strong>{_escape(k)}:</strong> '
                        f'<a href="{_escape(val)}" target="_blank">{_escape(val)}</a></div>'
                    )
                else:
                    details += (
                        f'<div style="margin:2px 0"><strong>{_escape(k)}:</strong> '
                        f'{_escape(val[:200])}</div>'
                    )

        source_badge = (
            f'<span style="font-size:0.75em;color:#888;margin-left:6px">'
            f'[{_escape(source)}]</span>'
            if source != "l1" else ""
        )

        connector = (
            '<div style="width:1px;height:100%;background:#ddd;margin-left:5px"></div>'
            if i < len(entries) - 1 else ""
        )

        timeline += f"""
        <div style="display:flex;gap:16px;padding:10px 0;border-bottom:1px solid #eee;">
            <div style="min-width:60px;text-align:right;color:#888;font-size:0.85em">{ts[11:]}</div>
            <div style="min-width:12px">
                <div style="width:12px;height:12px;border-radius:50%;background:{color};margin-top:3px"></div>
                {connector}
            </div>
            <div style="flex:1">
                <div><strong style="color:{color}">{_escape(phase)}</strong>: {_escape(event)}{source_badge}</div>
                {details}
            </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Trace \u2014 {_escape(ticket_id)}</title>
    <style>
        {_BASE_STYLES}
        h1 {{ border-bottom: none; }}
    </style>
</head>
<body>
    <a href="/traces">\u2190 All traces</a>
    <h1>Trace \u2014 {_escape(ticket_id)}</h1>
    {duration_bar}
    {summary}
    {failure_box}
    <div style="background:white;border-radius:8px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        {timeline}
    </div>
</body>
</html>"""


@router.get("/api/traces", response_model=None)
async def traces_api(
    pr: str = "", page: int = 1, per_page: int = 50
) -> dict[str, object]:
    """JSON API for traces list with pagination."""
    if pr:
        traces = list_traces(limit=0)
        traces = [t for t in traces if pr in t.get("pr_url", "")]
        total = len(traces)
    else:
        total = count_traces()
        offset = (page - 1) * per_page
        traces = list_traces(offset=offset, limit=per_page)

    return {"total": total, "page": page, "per_page": per_page, "traces": traces}


@router.get("/api/traces/{ticket_id}", response_model=None)
async def trace_api(ticket_id: str) -> list[dict[str, object]]:
    """JSON API for a single trace."""
    return read_trace(ticket_id)
