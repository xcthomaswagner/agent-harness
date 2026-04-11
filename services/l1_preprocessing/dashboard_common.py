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
