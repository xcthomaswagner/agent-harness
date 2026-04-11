"""Shared constants and helpers for dashboard HTML rendering.

Single source of truth for the status → badge-class mapping AND the
small set of HTML escaping / formatting helpers that every dashboard
module needs. Previously the status map had drifted between
``trace_dashboard.py`` (17 entries) and ``unified_dashboard.py`` (13
entries, missing ``Failed``, ``Timed Out``, ``Cleaned Up``) — so the
unified dashboard silently fell back to ``badge-secondary`` on statuses
the main dashboard styled correctly. The HTML helpers below had the
same duplication hazard: ``_e``/``_safe_url``/``_fmt_dur``/``_badge``/
``_fmt_ts`` were reimplemented across four dashboard files, identical
in behavior, with minor signature variations that invited drift.

All dashboard modules now import from here. Any new status, new URL
scheme allowlisted, or new timestamp display format lands in exactly
one place.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

# Status → badge class mapping. Extend here; every dashboard reading this
# dict will pick up the change.
STATUS_BADGE: dict[str, str] = {
    "Complete": "badge-success",
    "PR Created": "badge-success",
    "Escalated": "badge-error",
    "Failed": "badge-error",
    "Timed Out": "badge-error",
    "Agent Done (no PR)": "badge-error",
    "Cleaned Up": "badge-secondary",
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


def escape_html(text: Any) -> str:
    """HTML-escape a value, coercing None and non-strings to ``str`` first.

    This is the canonical ``_e`` helper that every dashboard previously
    defined locally. Using the None-coercing form (``"" if None else ...``)
    so callers can safely pass through optional fields without guarding.
    """
    return html.escape("" if text is None else str(text), quote=True)


def safe_url(url: str) -> str:
    """Return URL only if it uses an http(s) scheme — otherwise ``#``.

    Protects the dashboard's anchor ``href`` attributes from
    ``javascript:`` / ``data:`` schemes sneaking in via trace entries.
    The case-insensitive scheme check mirrors what every dashboard used
    to do locally.
    """
    s = url.strip().lower()
    return url if s.startswith("https://") or s.startswith("http://") else "#"


def fmt_dur(seconds: float) -> str:
    """Format a duration in seconds as ``Nm Ss`` (or ``Ss`` under a minute)."""
    if seconds <= 0:
        return "0s"
    m, s = int(seconds // 60), int(seconds % 60)
    return f"{m}m {s}s" if m else f"{s}s"


def fmt_ts(ts: str) -> str:
    """Format an ISO-8601 timestamp as ``Mon DD, HH:MM`` for dashboard display.

    Returns empty string on empty input, and falls back to the first 16
    characters of the raw string on parse failure (matches the legacy
    local-copy behavior — keeps malformed timestamps visible but clipped).
    """
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        return ts[:16]


def badge(text: str, cls: str) -> str:
    """Render a badge span with escaped text."""
    return f'<span class="badge {cls}">{escape_html(text)}</span>'


def fmt_pct(value: float | None) -> str:
    """Format a 0..1 fraction as a rounded percent string, or em-dash.

    ``None`` renders as a single em-dash (``\u2014``) so dashboards
    have a consistent placeholder for "no data". Previously this
    helper was duplicated in ``unified_dashboard`` and
    ``autonomy_dashboard`` with a subtle drift — one copy used the
    literal ``\u2014`` escape and the other used the raw ``—``
    character, which is visually identical but exactly the drift
    hazard this module was created to prevent.
    """
    if value is None:
        return "\u2014"
    return f"{value * 100:.0f}%"


# Base CSS used by every dashboard module. Previously duplicated
# verbatim in trace_dashboard, unified_dashboard, and autonomy_dashboard
# — any future palette or typography tweak had to be made in three
# places at once. Each dashboard concatenates this block with any
# module-specific styles (card grid sizing, table chrome, etc.) that
# differ across views.
LANGFUSE_BASE_CSS = """
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
