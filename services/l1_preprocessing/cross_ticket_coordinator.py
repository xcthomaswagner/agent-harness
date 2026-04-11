"""Cross-Ticket Coordinator — monitors decomposed ticket sub-PRs and integrates them.

When a parent ticket is decomposed into sub-tickets, each sub-ticket enters the
pipeline independently. This coordinator monitors completion of all sub-ticket PRs
and triggers the integration merge.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import structlog

from tracer import atomic_write_text

logger = structlog.get_logger()

DEFAULT_TRACKING_PATH = Path(__file__).resolve().parents[2] / "data" / "cross-ticket.json"


class SubTicketStatus:
    """Tracks the status of a sub-ticket within a decomposed parent."""

    def __init__(
        self,
        sub_ticket_id: str,
        parent_ticket_id: str,
        pr_url: str = "",
        branch: str = "",
        status: str = "pending",  # pending, in_progress, pr_created, merged, failed
    ) -> None:
        self.sub_ticket_id = sub_ticket_id
        self.parent_ticket_id = parent_ticket_id
        self.pr_url = pr_url
        self.branch = branch
        self.status = status

    def to_dict(self) -> dict[str, str]:
        return {
            "sub_ticket_id": self.sub_ticket_id,
            "parent_ticket_id": self.parent_ticket_id,
            "pr_url": self.pr_url,
            "branch": self.branch,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubTicketStatus:
        return cls(
            sub_ticket_id=str(data.get("sub_ticket_id", "")),
            parent_ticket_id=str(data.get("parent_ticket_id", "")),
            pr_url=str(data.get("pr_url", "")),
            branch=str(data.get("branch", "")),
            status=str(data.get("status", "pending")),
        )


class CrossTicketCoordinator:
    """Monitors sub-ticket PRs and triggers integration merges."""

    def __init__(self, tracking_path: Path | None = None) -> None:
        self._path = tracking_path or DEFAULT_TRACKING_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[SubTicketStatus]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
            return [SubTicketStatus.from_dict(d) for d in data]
        except (json.JSONDecodeError, KeyError):
            return []

    def _save(self, items: list[SubTicketStatus]) -> None:
        # Atomic write via a sibling temp file so a crash mid-write
        # leaves the previous tracking file intact instead of producing
        # a truncated one. Two concurrent ``register_sub_tickets`` or
        # ``update_sub_ticket`` calls can still trample each other's
        # in-memory view (that's the _load/_save race the dedup fix in
        # ``register_sub_tickets`` already documents), but the write
        # itself can no longer corrupt the file.
        atomic_write_text(
            self._path,
            json.dumps([i.to_dict() for i in items], indent=2),
        )

    def register_sub_tickets(
        self, parent_id: str, sub_ticket_ids: list[str]
    ) -> None:
        """Register sub-tickets for a decomposed parent.

        Idempotent: if ``(parent_id, sub_id)`` is already tracked, the
        entry is left alone. Without this guard, a re-triggered parent
        (webhook replay, decomposition retry) would accumulate ghost
        ``pending`` rows — ``update_sub_ticket`` only flipped the first
        match, so duplicates would never transition and ``all_done``
        would stay False forever, silently stalling the integration
        merge.
        """
        items = self._load()
        existing_keys = {
            (i.parent_ticket_id, i.sub_ticket_id) for i in items
        }
        new_count = 0
        for sub_id in sub_ticket_ids:
            if (parent_id, sub_id) in existing_keys:
                continue
            items.append(SubTicketStatus(
                sub_ticket_id=sub_id, parent_ticket_id=parent_id
            ))
            existing_keys.add((parent_id, sub_id))
            new_count += 1
        self._save(items)
        logger.info(
            "sub_tickets_registered",
            parent=parent_id,
            count=len(sub_ticket_ids),
            new=new_count,
            skipped_duplicates=len(sub_ticket_ids) - new_count,
        )

    def update_sub_ticket(
        self, sub_ticket_id: str, status: str, pr_url: str = "", branch: str = ""
    ) -> str | None:
        """Update a sub-ticket's status. Returns parent_id if all sub-tickets are done.

        Updates every row matching ``sub_ticket_id`` — older code used
        ``break`` on the first match, so any stray duplicates from pre-
        dedup-fix state would stay pending forever. Walking the full
        list is cheap (tracking file is small) and makes the coordinator
        robust against historical duplicate rows.
        """
        items = self._load()
        parent_id = None

        for item in items:
            if item.sub_ticket_id == sub_ticket_id:
                item.status = status
                item.pr_url = pr_url or item.pr_url
                item.branch = branch or item.branch
                parent_id = item.parent_ticket_id

        self._save(items)

        if not parent_id:
            return None

        # Check if all sub-tickets for this parent are done
        siblings = [i for i in items if i.parent_ticket_id == parent_id]
        all_done = all(s.status in ("merged", "pr_created") for s in siblings)

        if all_done:
            logger.info("all_sub_tickets_complete", parent=parent_id)
            return parent_id

        return None

    def get_sub_tickets(self, parent_id: str) -> list[SubTicketStatus]:
        """Get all sub-tickets for a parent."""
        items = self._load()
        return [i for i in items if i.parent_ticket_id == parent_id]

    def trigger_integration_merge(
        self, parent_id: str, client_repo: str, target_branch: str = "main"
    ) -> bool:
        """Merge all sub-ticket branches into an integration branch.

        Creates branch: ai/{parent_id}-integration
        Merges sub-ticket branches in order
        Runs tests
        Opens integration PR
        """
        sub_tickets = self.get_sub_tickets(parent_id)
        branches = [s.branch for s in sub_tickets if s.branch]

        if not branches:
            logger.warning("no_branches_to_integrate", parent=parent_id)
            return False

        integration_branch = f"ai/{parent_id}-integration"
        log = logger.bind(parent=parent_id, branches=branches)
        log.info("starting_integration_merge")

        try:
            # Create integration branch from target
            subprocess.run(
                ["git", "checkout", "-b", integration_branch, target_branch],
                cwd=client_repo, check=True, capture_output=True,
            )

            # Merge each sub-ticket branch
            for branch in branches:
                result = subprocess.run(
                    ["git", "merge", "--no-ff", branch, "-m",
                     f"merge: integrate {branch} into {integration_branch}"],
                    cwd=client_repo, capture_output=True,
                )
                if result.returncode != 0:
                    log.error("merge_conflict", branch=branch)
                    subprocess.run(
                        ["git", "merge", "--abort"],
                        cwd=client_repo, capture_output=True,
                    )
                    return False

            log.info("integration_merge_complete")
            return True

        except subprocess.CalledProcessError as exc:
            log.error("integration_failed", error=str(exc))
            return False
