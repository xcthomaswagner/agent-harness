"""Tests for the graduated autonomy engine."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from autonomy import AutonomyEngine, PROutcome


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class TestRecordAndMetrics:
    def test_empty_metrics(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        metrics = engine.get_metrics()
        assert metrics["sample_size"] == 0
        assert metrics["recommended_mode"] == "conservative"

    def test_record_and_retrieve(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        engine.record_outcome(PROutcome(
            ticket_id="T-1", pr_url="https://pr/1", ticket_type="story",
            created_at=_now_iso(), first_pass_accepted=True, merged=True,
        ))
        metrics = engine.get_metrics()
        assert metrics["sample_size"] == 1
        assert metrics["first_pass_acceptance_rate"] == 1.0

    def test_replaces_duplicate_ticket(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        engine.record_outcome(PROutcome(
            ticket_id="T-1", pr_url="https://pr/1", ticket_type="story",
            created_at=_now_iso(), first_pass_accepted=False,
        ))
        engine.record_outcome(PROutcome(
            ticket_id="T-1", pr_url="https://pr/1", ticket_type="story",
            created_at=_now_iso(), first_pass_accepted=True,
        ))
        metrics = engine.get_metrics()
        assert metrics["sample_size"] == 1
        assert metrics["first_pass_acceptance_rate"] == 1.0


class TestRecommendMode:
    def test_conservative_with_few_samples(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        for i in range(10):
            engine.record_outcome(PROutcome(
                ticket_id=f"T-{i}", pr_url=f"https://pr/{i}", ticket_type="story",
                created_at=_now_iso(), first_pass_accepted=True, merged=True,
            ))
        metrics = engine.get_metrics()
        assert metrics["recommended_mode"] == "conservative"  # < 20 samples

    def test_semi_autonomous_at_threshold(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        for i in range(20):
            engine.record_outcome(PROutcome(
                ticket_id=f"T-{i}", pr_url=f"https://pr/{i}", ticket_type="story",
                created_at=_now_iso(), first_pass_accepted=True, merged=True,
            ))
        metrics = engine.get_metrics()
        assert metrics["recommended_mode"] == "semi_autonomous"

    def test_stays_conservative_with_low_acceptance(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        for i in range(20):
            engine.record_outcome(PROutcome(
                ticket_id=f"T-{i}", pr_url=f"https://pr/{i}", ticket_type="story",
                created_at=_now_iso(),
                first_pass_accepted=(i < 15),  # 75% — below 90% threshold
                merged=True,
            ))
        metrics = engine.get_metrics()
        assert metrics["recommended_mode"] == "conservative"

    def test_full_autonomous_at_threshold(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        for i in range(50):
            engine.record_outcome(PROutcome(
                ticket_id=f"T-{i}", pr_url=f"https://pr/{i}", ticket_type="story",
                created_at=_now_iso(), first_pass_accepted=True, merged=True,
                human_issues_found=2, ai_issues_found=2,  # 100% catch rate
            ))
        metrics = engine.get_metrics()
        assert metrics["recommended_mode"] == "full_autonomous"


    def test_catch_rate_zero_when_no_human_baseline(self, tmp_path: Path) -> None:
        """When humans find 0 issues, catch rate should be 0 (unreliable), not 1.0."""
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        for i in range(25):
            engine.record_outcome(PROutcome(
                ticket_id=f"T-{i}", pr_url=f"https://pr/{i}", ticket_type="story",
                created_at=_now_iso(), first_pass_accepted=True, merged=True,
                human_issues_found=0, ai_issues_found=3,
            ))
        metrics = engine.get_metrics()
        assert metrics["self_review_catch_rate"] == 0.0
        # Without human baseline, should NOT recommend full_autonomous
        assert metrics["recommended_mode"] != "full_autonomous"


class TestShouldAutoMerge:
    def test_conservative_never_auto_merges(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        assert engine.should_auto_merge("bug") is False
        assert engine.should_auto_merge("story") is False

    def test_semi_autonomous_merges_low_risk(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        for i in range(20):
            engine.record_outcome(PROutcome(
                ticket_id=f"T-{i}", pr_url=f"https://pr/{i}", ticket_type="story",
                created_at=_now_iso(), first_pass_accepted=True, merged=True,
            ))
        assert engine.should_auto_merge("bug") is True
        assert engine.should_auto_merge("config") is True
        assert engine.should_auto_merge("story") is False  # Not low-risk

    def test_full_autonomous_merges_everything(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        for i in range(50):
            engine.record_outcome(PROutcome(
                ticket_id=f"T-{i}", pr_url=f"https://pr/{i}", ticket_type="story",
                created_at=_now_iso(), first_pass_accepted=True, merged=True,
                human_issues_found=1, ai_issues_found=1,
            ))
        assert engine.should_auto_merge("story") is True
        assert engine.should_auto_merge("bug") is True


class TestDefectEscapeRate:
    def test_defect_blocks_semi_autonomous(self, tmp_path: Path) -> None:
        engine = AutonomyEngine(metrics_path=tmp_path / "metrics.json")
        for i in range(20):
            engine.record_outcome(PROutcome(
                ticket_id=f"T-{i}", pr_url=f"https://pr/{i}", ticket_type="story",
                created_at=_now_iso(), first_pass_accepted=True, merged=True,
                defect_escaped=(i < 2),  # 10% defect rate — above 5% threshold
            ))
        metrics = engine.get_metrics()
        assert metrics["recommended_mode"] == "conservative"
        assert metrics["defect_escape_rate"] == 0.1
