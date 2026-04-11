"""Tests for cross-ticket coordinator — sub-ticket tracking, integration merge."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cross_ticket_coordinator import (
    CrossTicketCoordinator,
    SubTicketStatus,
)


@pytest.fixture
def coordinator(tmp_path: Path) -> CrossTicketCoordinator:
    return CrossTicketCoordinator(tracking_path=tmp_path / "tracking.json")


class TestSubTicketStatus:
    def test_to_dict(self) -> None:
        s = SubTicketStatus("SUB-1", "PARENT-1", pr_url="https://pr", status="pr_created")
        d = s.to_dict()
        assert d["sub_ticket_id"] == "SUB-1"
        assert d["parent_ticket_id"] == "PARENT-1"
        assert d["pr_url"] == "https://pr"
        assert d["status"] == "pr_created"

    def test_from_dict(self) -> None:
        d = {
            "sub_ticket_id": "SUB-2",
            "parent_ticket_id": "PARENT-2",
            "pr_url": "",
            "branch": "ai/SUB-2",
            "status": "in_progress",
        }
        s = SubTicketStatus.from_dict(d)
        assert s.sub_ticket_id == "SUB-2"
        assert s.branch == "ai/SUB-2"

    def test_from_dict_missing_keys(self) -> None:
        """Missing keys should default to empty strings."""
        s = SubTicketStatus.from_dict({})
        assert s.sub_ticket_id == ""
        assert s.status == "pending"

    def test_round_trip(self) -> None:
        original = SubTicketStatus("X-1", "P-1", "https://pr", "ai/X-1", "merged")
        restored = SubTicketStatus.from_dict(original.to_dict())
        assert restored.sub_ticket_id == original.sub_ticket_id
        assert restored.status == original.status


class TestCrossTicketCoordinator:
    def test_register_sub_tickets(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        coordinator.register_sub_tickets("P-1", ["S-1", "S-2", "S-3"])
        subs = coordinator.get_sub_tickets("P-1")
        assert len(subs) == 3
        assert all(s.parent_ticket_id == "P-1" for s in subs)
        assert all(s.status == "pending" for s in subs)

    def test_register_is_idempotent_for_same_parent_and_sub(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        """Bug regression: before the fix, calling register_sub_tickets
        twice for the same parent (webhook replay, decomposition retry)
        appended duplicate rows. update_sub_ticket only flipped the
        first match on each call, so the ghost pending rows kept the
        coordinator in a state where ``all_done`` was permanently False
        and the integration merge never fired. After the fix, a second
        call is a no-op."""
        coordinator.register_sub_tickets("P-DUP", ["S-1", "S-2"])
        coordinator.register_sub_tickets("P-DUP", ["S-1", "S-2"])

        subs = coordinator.get_sub_tickets("P-DUP")
        assert len(subs) == 2  # Not 4 — dedup worked.
        assert sorted(s.sub_ticket_id for s in subs) == ["S-1", "S-2"]

    def test_register_merges_new_sub_tickets_with_existing(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        """A retry that adds a new sub-ticket on top of existing ones
        must keep the existing entries untouched AND add the new one."""
        coordinator.register_sub_tickets("P-PART", ["S-1", "S-2"])
        coordinator.update_sub_ticket(
            "S-1", "pr_created", pr_url="https://pr/1"
        )
        # Second registration repeats S-1 and adds S-3.
        coordinator.register_sub_tickets("P-PART", ["S-1", "S-3"])

        subs = {
            s.sub_ticket_id: s for s in coordinator.get_sub_tickets("P-PART")
        }
        assert set(subs) == {"S-1", "S-2", "S-3"}
        # S-1's prior update must survive.
        assert subs["S-1"].status == "pr_created"
        assert subs["S-1"].pr_url == "https://pr/1"
        # The untouched sibling is still pending.
        assert subs["S-2"].status == "pending"

    def test_update_sub_ticket_flips_all_duplicates(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        """Belt-and-braces regression: if a pre-fix tracking file still
        has stray duplicate rows for the same (parent, sub) pair, the
        updater must transition all of them so all_done can eventually
        return true. Simulates the bad state by writing duplicates
        directly to the tracking file."""
        import json

        coordinator.register_sub_tickets("P-GHOST", ["S-1", "S-2"])
        # Inject a ghost duplicate of S-1 as if from a pre-fix run.
        raw = json.loads(coordinator._path.read_text())
        raw.append({
            "sub_ticket_id": "S-1",
            "parent_ticket_id": "P-GHOST",
            "pr_url": "",
            "branch": "",
            "status": "pending",
        })
        coordinator._path.write_text(json.dumps(raw))

        # Mark S-1 merged — both the original and the ghost row must
        # transition, otherwise all_done can never be True.
        coordinator.update_sub_ticket("S-1", "merged", branch="ai/S-1")
        result = coordinator.update_sub_ticket(
            "S-2", "pr_created", pr_url="https://pr/2"
        )
        assert result == "P-GHOST"

    def test_update_sub_ticket_returns_none_when_not_all_done(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        coordinator.register_sub_tickets("P-2", ["S-4", "S-5"])
        result = coordinator.update_sub_ticket("S-4", "pr_created", pr_url="https://pr/4")
        assert result is None  # S-5 still pending

    def test_update_sub_ticket_returns_parent_when_all_done(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        coordinator.register_sub_tickets("P-3", ["S-6", "S-7"])
        coordinator.update_sub_ticket("S-6", "merged", branch="ai/S-6")
        result = coordinator.update_sub_ticket("S-7", "pr_created", pr_url="https://pr/7")
        assert result == "P-3"

    def test_update_unknown_sub_ticket(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        result = coordinator.update_sub_ticket("NONEXISTENT", "merged")
        assert result is None

    def test_get_sub_tickets_empty(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        subs = coordinator.get_sub_tickets("NO-PARENT")
        assert subs == []

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "persist.json"
        c1 = CrossTicketCoordinator(tracking_path=path)
        c1.register_sub_tickets("P-4", ["S-8"])

        c2 = CrossTicketCoordinator(tracking_path=path)
        subs = c2.get_sub_tickets("P-4")
        assert len(subs) == 1

    def test_handles_corrupt_json(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.json"
        path.write_text("not json{{{")
        c = CrossTicketCoordinator(tracking_path=path)
        subs = c.get_sub_tickets("ANY")
        assert subs == []

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.json"
        c = CrossTicketCoordinator(tracking_path=path)
        subs = c.get_sub_tickets("ANY")
        assert subs == []


class TestIntegrationMerge:
    def test_returns_false_with_no_branches(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        coordinator.register_sub_tickets("P-5", ["S-9"])
        result = coordinator.trigger_integration_merge("P-5", "/tmp/repo")
        assert result is False

    def test_merge_success(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        coordinator.register_sub_tickets("P-6", ["S-10"])
        coordinator.update_sub_ticket("S-10", "merged", branch="ai/S-10")

        with patch("cross_ticket_coordinator.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = coordinator.trigger_integration_merge("P-6", "/tmp/repo")

        assert result is True
        assert mock_run.call_count >= 2  # checkout + merge

    def test_merge_conflict_returns_false(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        coordinator.register_sub_tickets("P-7", ["S-11"])
        coordinator.update_sub_ticket("S-11", "merged", branch="ai/S-11")

        call_count = {"n": 0}

        def side_effect(*args: object, **kwargs: object) -> MagicMock:
            call_count["n"] += 1
            m = MagicMock()
            if call_count["n"] == 1:
                m.returncode = 0  # checkout succeeds
            else:
                m.returncode = 1  # merge fails
            return m

        with patch(
            "cross_ticket_coordinator.subprocess.run", side_effect=side_effect
        ):
            result = coordinator.trigger_integration_merge("P-7", "/tmp/repo")

        assert result is False

    def test_checkout_failure(
        self, coordinator: CrossTicketCoordinator
    ) -> None:
        coordinator.register_sub_tickets("P-8", ["S-12"])
        coordinator.update_sub_ticket("S-12", "merged", branch="ai/S-12")

        import subprocess

        with patch(
            "cross_ticket_coordinator.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "git"),
        ):
            result = coordinator.trigger_integration_merge("P-8", "/tmp/repo")

        assert result is False
