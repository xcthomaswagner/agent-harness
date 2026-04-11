"""Shared constants and helpers for dashboard HTML rendering.

Single source of truth for status → badge-class mapping. Previously
duplicated (with drift) between ``trace_dashboard.py`` (17 entries) and
``unified_dashboard.py`` (13 entries, missing ``Failed``, ``Timed Out``,
``Cleaned Up``) — so the unified dashboard silently fell back to
``badge-secondary`` on statuses the main dashboard styled correctly.
Importing from here prevents future drift when new statuses land.

Kept deliberately small: this module only contains values that multiple
dashboard modules would otherwise hand-copy. HTML helpers like
``_e``/``_badge``/``_fmt_ts`` are still local to each dashboard because
they vary subtly across callers (quote policy, whitespace) and
extracting them would be higher churn than this narrow status map.
"""

from __future__ import annotations

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
