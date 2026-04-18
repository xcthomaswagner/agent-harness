"""Shared adapter protocols for ticket-source write-back.

Callers like ``pipeline.py`` used to accept the concrete union
``JiraAdapter | AdoAdapter`` from ``Pipeline._get_adapter``, which
grew every time a new ticket source was added and forced mypy to
look at the full public surface of both adapters (each has 6-7
async methods). In practice the pipeline only ever uses three
methods — ``write_comment``, ``transition_status``, ``add_label`` —
so this structural protocol documents that contract and makes
adding a new adapter a matter of "implement these three methods."
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TicketWriteBackAdapter(Protocol):
    """Minimum surface every ticket-source adapter must expose to L1.

    The L1 pipeline writes an AI enrichment comment, transitions the
    ticket to an in-progress state, and may add a label — nothing
    else. Adapters are free to implement additional source-specific
    methods (attachment download, work-item typing, etc.) but those
    are NOT part of the cross-source contract.
    """

    async def write_comment(self, ticket_id: str, comment: str) -> None:
        """Post a plain-text comment on the ticket."""

    async def transition_status(
        self, ticket_id: str, target_status: str
    ) -> None:
        """Move the ticket to ``target_status`` (free-form; each
        adapter maps this to its own workflow transition ID)."""

    async def add_label(self, ticket_id: str, label: str) -> None:
        """Tag the ticket with ``label`` (no-op if already present)."""
