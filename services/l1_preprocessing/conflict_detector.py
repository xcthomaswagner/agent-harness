"""Basic conflict detection for concurrent tickets.

Tracks in-progress tickets and their likely affected file scopes.
When a new ticket enters the pipeline, checks for overlapping scopes
and warns via Jira/ADO comments.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Active tickets are stored in a JSON file for simplicity.
# Phase 4 upgrades this to a shared database/Redis for multi-worker support.
DEFAULT_ACTIVE_TICKETS_PATH = Path("/tmp/harness-active-tickets.json")


class ActiveTicket:
    """A ticket currently being processed by the pipeline."""

    def __init__(
        self,
        ticket_id: str,
        title: str,
        affected_files: list[str],
        branch: str = "",
    ) -> None:
        self.ticket_id = ticket_id
        self.title = title
        self.affected_files = affected_files
        self.branch = branch

    def to_dict(self) -> dict[str, object]:
        return {
            "ticket_id": self.ticket_id,
            "title": self.title,
            "affected_files": self.affected_files,
            "branch": self.branch,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ActiveTicket:
        return cls(
            ticket_id=str(data.get("ticket_id", "")),
            title=str(data.get("title", "")),
            affected_files=[str(f) for f in (data.get("affected_files") or [])],  # type: ignore[attr-defined]
            branch=str(data.get("branch", "")),
        )


class ConflictDetector:
    """Detects file scope overlaps between concurrent tickets."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self._path = storage_path or DEFAULT_ACTIVE_TICKETS_PATH

    def _load_active(self) -> list[ActiveTicket]:
        """Load active tickets from storage."""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
            return [ActiveTicket.from_dict(t) for t in data]
        except (json.JSONDecodeError, KeyError):
            logger.warning("active_tickets_corrupt", path=str(self._path))
            return []

    def _save_active(self, tickets: list[ActiveTicket]) -> None:
        """Save active tickets to storage."""
        data = [t.to_dict() for t in tickets]
        self._path.write_text(json.dumps(data, indent=2))

    def register(
        self, ticket_id: str, title: str, affected_files: list[str], branch: str = ""
    ) -> None:
        """Register a ticket as actively in-progress."""
        active = self._load_active()
        # Remove existing entry for this ticket (in case of re-processing)
        active = [t for t in active if t.ticket_id != ticket_id]
        active.append(ActiveTicket(ticket_id, title, affected_files, branch))
        self._save_active(active)
        logger.info("ticket_registered", ticket_id=ticket_id, file_count=len(affected_files))

    def unregister(self, ticket_id: str) -> None:
        """Remove a ticket from the active list (completed or abandoned)."""
        active = self._load_active()
        active = [t for t in active if t.ticket_id != ticket_id]
        self._save_active(active)
        logger.info("ticket_unregistered", ticket_id=ticket_id)

    def check_conflicts(
        self, ticket_id: str, affected_files: list[str]
    ) -> list[dict[str, object]]:
        """Check if a new ticket's file scope overlaps with active tickets.

        Returns a list of conflicts, each with:
        - conflicting_ticket_id
        - conflicting_ticket_title
        - overlapping_files
        """
        active = self._load_active()
        new_files = set(affected_files)
        conflicts: list[dict[str, object]] = []

        for ticket in active:
            if ticket.ticket_id == ticket_id:
                continue
            existing_files = set(ticket.affected_files)
            overlap = new_files & existing_files
            if overlap:
                conflicts.append({
                    "conflicting_ticket_id": ticket.ticket_id,
                    "conflicting_ticket_title": ticket.title,
                    "overlapping_files": sorted(overlap),
                })

        if conflicts:
            logger.warning(
                "file_scope_conflicts_detected",
                ticket_id=ticket_id,
                conflict_count=len(conflicts),
            )

        return conflicts

    def format_warning(self, ticket_id: str, conflicts: list[dict[str, object]]) -> str:
        """Format a human-readable warning message for Jira/ADO."""
        lines = [
            f"*AI Pipeline — File Scope Conflict Warning for {ticket_id}:*\n",
            "The following in-progress tickets may overlap with this ticket's scope:\n",
        ]
        for conflict in conflicts:
            files = conflict.get("overlapping_files", [])
            assert isinstance(files, list)
            file_list = ", ".join(str(f) for f in files[:5])
            if len(files) > 5:
                file_list += f" (+{len(files) - 5} more)"
            lines.append(
                f"- **{conflict['conflicting_ticket_id']}** "
                f"({conflict['conflicting_ticket_title']}): "
                f"overlaps on {file_list}"
            )
        lines.append(
            "\n*Note:* This is a warning, not a block. "
            "Consider coordinating with the other ticket's developer."
        )
        return "\n".join(lines)
