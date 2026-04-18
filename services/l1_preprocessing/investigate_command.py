"""Canonical template for the copy-paste trace investigation command.

Shared by the ``/traces/{ticket_id}/discuss`` endpoint in ``main.py`` and
the copy-investigate disclosure rendered by ``trace_dashboard.py``. Keeping
a single source here prevents the two renderings from drifting — the
discuss endpoint used to take a ``{base}`` URL parameter while the dashboard
hardcoded ``http://localhost:8000``, so an operator changing the base URL
via the discuss path would see the dashboard disclosure still pointing at
loopback.

The base URL is intentionally hardcoded at the default — Tier 1 post-mortem
observability is single-dev, local-only. Do not derive the base from the
request host; ngrok-style public forwards to loopback break that.
"""

from __future__ import annotations

import re

DISCUSS_BASE_URL = "http://localhost:8000"
_SAFE_TICKET_ID = re.compile(r"^[A-Za-z0-9_-]+$")

INVESTIGATE_COMMAND_TEMPLATE = (
    "mkdir -p /tmp/trace-{ticket_id} && \\\n"
    "curl -sSf {base}/traces/{ticket_id}/bundle | "
    "tar xz -C /tmp/trace-{ticket_id} && \\\n"
    "cd /tmp/trace-{ticket_id} && \\\n"
    "claude -p \"I'm investigating a failed agent run. Read all the files "
    "in this directory. Start by reading diagnostic.json (if it exists) "
    "and tool-index.json, then tell me what the first deviation point was. "
    "Cite specific line numbers for every claim.\""
)


def build_investigate_command(ticket_id: str, base_url: str = DISCUSS_BASE_URL) -> str:
    """Render the copy-paste shell snippet for a local post-mortem session."""
    if not _SAFE_TICKET_ID.match(ticket_id):
        raise ValueError(f"Invalid ticket_id for shell command: {ticket_id!r}")
    return INVESTIGATE_COMMAND_TEMPLATE.format(ticket_id=ticket_id, base=base_url)
