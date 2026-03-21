"""Tests for basic conflict detection between concurrent tickets."""

from __future__ import annotations

from pathlib import Path

from conflict_detector import ConflictDetector


class TestConflictDetector:
    def test_no_conflicts_when_no_active_tickets(self, tmp_path: Path) -> None:
        detector = ConflictDetector(storage_path=tmp_path / "active.json")
        conflicts = detector.check_conflicts("NEW-1", ["src/auth.ts"])
        assert conflicts == []

    def test_detects_file_overlap(self, tmp_path: Path) -> None:
        detector = ConflictDetector(storage_path=tmp_path / "active.json")
        detector.register("OLD-1", "Refactor auth", ["src/auth.ts", "src/middleware.ts"])

        conflicts = detector.check_conflicts("NEW-1", ["src/auth.ts", "src/api.ts"])
        assert len(conflicts) == 1
        assert conflicts[0]["conflicting_ticket_id"] == "OLD-1"
        assert "src/auth.ts" in conflicts[0]["overlapping_files"]  # type: ignore[operator]

    def test_no_conflict_with_disjoint_files(self, tmp_path: Path) -> None:
        detector = ConflictDetector(storage_path=tmp_path / "active.json")
        detector.register("OLD-1", "Auth work", ["src/auth.ts"])

        conflicts = detector.check_conflicts("NEW-1", ["src/api.ts"])
        assert conflicts == []

    def test_ignores_same_ticket(self, tmp_path: Path) -> None:
        detector = ConflictDetector(storage_path=tmp_path / "active.json")
        detector.register("SAME-1", "Work", ["src/auth.ts"])

        conflicts = detector.check_conflicts("SAME-1", ["src/auth.ts"])
        assert conflicts == []

    def test_multiple_conflicts(self, tmp_path: Path) -> None:
        detector = ConflictDetector(storage_path=tmp_path / "active.json")
        detector.register("OLD-1", "Auth", ["src/auth.ts"])
        detector.register("OLD-2", "Middleware", ["src/middleware.ts"])

        conflicts = detector.check_conflicts(
            "NEW-1", ["src/auth.ts", "src/middleware.ts"]
        )
        assert len(conflicts) == 2

    def test_unregister_removes_ticket(self, tmp_path: Path) -> None:
        detector = ConflictDetector(storage_path=tmp_path / "active.json")
        detector.register("OLD-1", "Auth", ["src/auth.ts"])
        detector.unregister("OLD-1")

        conflicts = detector.check_conflicts("NEW-1", ["src/auth.ts"])
        assert conflicts == []

    def test_register_replaces_existing(self, tmp_path: Path) -> None:
        detector = ConflictDetector(storage_path=tmp_path / "active.json")
        detector.register("TICKET-1", "V1", ["src/old.ts"])
        detector.register("TICKET-1", "V2", ["src/new.ts"])

        # Should have new files, not old
        conflicts = detector.check_conflicts("OTHER-1", ["src/old.ts"])
        assert conflicts == []
        conflicts = detector.check_conflicts("OTHER-1", ["src/new.ts"])
        assert len(conflicts) == 1


class TestFormatWarning:
    def test_formats_readable_warning(self, tmp_path: Path) -> None:
        detector = ConflictDetector(storage_path=tmp_path / "active.json")
        conflicts = [
            {
                "conflicting_ticket_id": "OLD-1",
                "conflicting_ticket_title": "Refactor auth",
                "overlapping_files": ["src/auth.ts", "src/middleware.ts"],
            }
        ]
        warning = detector.format_warning("NEW-1", conflicts)
        assert "OLD-1" in warning
        assert "Refactor auth" in warning
        assert "src/auth.ts" in warning
        assert "warning, not a block" in warning

    def test_truncates_long_file_lists(self, tmp_path: Path) -> None:
        detector = ConflictDetector(storage_path=tmp_path / "active.json")
        conflicts = [
            {
                "conflicting_ticket_id": "OLD-1",
                "conflicting_ticket_title": "Big change",
                "overlapping_files": [f"src/file{i}.ts" for i in range(10)],
            }
        ]
        warning = detector.format_warning("NEW-1", conflicts)
        assert "+5 more" in warning
