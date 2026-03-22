"""Trace dashboard — serves HTML views of ticket traces at /traces."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from tracer import list_traces, read_trace

router = APIRouter()


def _escape(text: str) -> str:
    """HTML-escape a string."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@router.get("/traces", response_class=HTMLResponse)
async def traces_list(pr: str = "") -> str:
    """List all ticket traces, optionally filtered by PR URL."""
    traces = list_traces()

    if pr:
        traces = [t for t in traces if pr in t.get("pr_url", "")]

    rows = ""
    for t in traces:
        status_color = "#2D8B57" if "complete" in t["status"].lower() else "#E8792F"
        review_badge = ""
        if t["review_verdict"]:
            rc = "#2D8B57" if t["review_verdict"] == "APPROVED" else "#E8792F"
            review_badge = f'<span style="color:{rc}">{_escape(t["review_verdict"])}</span>'
        qa_badge = ""
        if t["qa_result"]:
            qc = "#2D8B57" if t["qa_result"] == "PASS" else "#c0392b"
            qa_badge = f'<span style="color:{qc}">{_escape(t["qa_result"])}</span>'
        pr_link = f'<a href="{_escape(t["pr_url"])}" target="_blank">PR</a>' if t["pr_url"] else "—"

        rows += f"""<tr>
            <td><a href="/traces/{_escape(t['ticket_id'])}">{_escape(t['ticket_id'])}</a></td>
            <td><span style="color:{status_color}">{_escape(t['status'][:40])}</span></td>
            <td>{_escape(t.get('pipeline_mode', ''))}</td>
            <td>{review_badge}</td>
            <td>{qa_badge}</td>
            <td>{pr_link}</td>
            <td>{t['entries']}</td>
            <td style="font-size:0.85em">{_escape(t['started_at'][:19])}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Agent Harness — Traces</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; margin: 40px; background: #fafafa; color: #333; }}
        h1 {{ color: #1B2A4A; border-bottom: 3px solid #2E6CA4; padding-bottom: 10px; }}
        table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        th {{ background: #1B2A4A; color: white; padding: 10px 14px; text-align: left; font-size: 0.9em; }}
        td {{ padding: 8px 14px; border-bottom: 1px solid #eee; font-size: 0.9em; }}
        tr:hover {{ background: #f5f5f5; }}
        a {{ color: #2E6CA4; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .count {{ color: #888; margin-left: 8px; font-size: 0.9em; }}
    </style>
</head>
<body>
    <h1>Agent Harness — Traces<span class="count">{len(traces)} tickets</span></h1>
    <table>
        <thead>
            <tr><th>Ticket</th><th>Status</th><th>Mode</th><th>Review</th><th>QA</th><th>PR</th><th>Events</th><th>Started</th></tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
</body>
</html>"""


@router.get("/traces/{ticket_id}", response_class=HTMLResponse)
async def trace_detail(ticket_id: str) -> str:
    """Show the full trace timeline for a ticket."""
    entries = read_trace(ticket_id)

    if not entries:
        return f"""<!DOCTYPE html><html><body>
            <h1>No trace found for {_escape(ticket_id)}</h1>
            <a href="/traces">← Back to traces</a>
        </body></html>"""

    # Build timeline
    timeline = ""
    for i, e in enumerate(entries):
        phase = e.get("phase", "")
        event = e.get("event", "")
        ts = e.get("timestamp", "")[:19]
        source = e.get("source", "l1")

        # Color by phase
        phase_colors: dict[str, str] = {
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
        }
        color = phase_colors.get(phase, "#555")

        # Format details
        details = ""
        skip_keys = {"trace_id", "ticket_id", "timestamp", "phase", "event", "source"}
        extra = {k: v for k, v in e.items() if k not in skip_keys and v}

        if "content" in extra:
            content = str(extra.pop("content"))
            details += f'<details><summary>View content ({len(content)} chars)</summary>'
            details += f'<pre style="white-space:pre-wrap;max-height:400px;overflow:auto;background:#f5f5f5;padding:10px;border-radius:4px;font-size:0.85em">{_escape(content)}</pre></details>'

        if extra:
            for k, v in extra.items():
                val = str(v)
                if val.startswith("http"):
                    details += f'<div style="margin:2px 0"><strong>{_escape(k)}:</strong> <a href="{_escape(val)}" target="_blank">{_escape(val)}</a></div>'
                else:
                    details += f'<div style="margin:2px 0"><strong>{_escape(k)}:</strong> {_escape(val[:200])}</div>'

        source_badge = f'<span style="font-size:0.75em;color:#888;margin-left:6px">[{_escape(source)}]</span>' if source != "l1" else ""

        timeline += f"""
        <div style="display:flex;gap:16px;padding:10px 0;border-bottom:1px solid #eee;">
            <div style="min-width:60px;text-align:right;color:#888;font-size:0.85em">{ts[11:]}</div>
            <div style="min-width:12px">
                <div style="width:12px;height:12px;border-radius:50%;background:{color};margin-top:3px"></div>
                {'<div style="width:1px;height:100%;background:#ddd;margin-left:5px"></div>' if i < len(entries) - 1 else ''}
            </div>
            <div style="flex:1">
                <div><strong style="color:{color}">{_escape(phase)}</strong>: {_escape(event)}{source_badge}</div>
                {details}
            </div>
        </div>"""

    # Extract summary info
    pr_url = ""
    review_verdict = ""
    qa_result = ""
    for e in entries:
        if e.get("pr_url"):
            pr_url = str(e["pr_url"])
        if e.get("event") == "Pipeline complete":
            review_verdict = str(e.get("review_verdict", ""))
            qa_result = str(e.get("qa_result", ""))

    summary = f"""
    <div style="display:flex;gap:20px;margin:20px 0;padding:16px;background:white;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <div><strong>Trace ID:</strong> {_escape(entries[0].get('trace_id', ''))}</div>
        <div><strong>Events:</strong> {len(entries)}</div>
        {f'<div><strong>Review:</strong> {_escape(review_verdict)}</div>' if review_verdict else ''}
        {f'<div><strong>QA:</strong> {_escape(qa_result)}</div>' if qa_result else ''}
        {f'<div><strong>PR:</strong> <a href="{_escape(pr_url)}" target="_blank">View PR</a></div>' if pr_url else ''}
    </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Trace — {_escape(ticket_id)}</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; margin: 40px; background: #fafafa; color: #333; }}
        h1 {{ color: #1B2A4A; }}
        a {{ color: #2E6CA4; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <a href="/traces">← All traces</a>
    <h1>Trace — {_escape(ticket_id)}</h1>
    {summary}
    <div style="background:white;border-radius:8px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        {timeline}
    </div>
</body>
</html>"""


@router.get("/api/traces", response_model=None)
async def traces_api(pr: str = "") -> list[dict[str, object]]:
    """JSON API for traces list."""
    traces = list_traces()
    if pr:
        traces = [t for t in traces if pr in t.get("pr_url", "")]
    return traces


@router.get("/api/traces/{ticket_id}", response_model=None)
async def trace_api(ticket_id: str) -> list[dict[str, object]]:
    """JSON API for a single trace."""
    return read_trace(ticket_id)
