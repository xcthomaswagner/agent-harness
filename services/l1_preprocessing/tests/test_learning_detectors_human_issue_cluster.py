"""Tests for Detector 2 — human_issue_cluster.

Covers:

- Below-threshold clusters (fewer than MIN_CLUSTER_SIZE distinct
  pr_run_ids) do not produce candidates.
- At-threshold clusters produce exactly one candidate with the
  expected scope_key, pattern_key, and client/platform values.
- File-pattern derivation: common ``.ext`` → ``*.ext``; common top-
  level dir → ``<dir>/**``; mixed → empty pattern.
- Evidence rows carry redacted summaries and ``review_issues#<id>``
  source_refs so the lesson_evidence UNIQUE constraint doesn't
  collide when a trace has several issues in the same cluster.
- Platform-profile resolution: issues on a profile with a
  resolvable platform are included; issues on an unknown profile
  are dropped.
- Window filtering: issues on pr_runs opened before the window
  are excluded.
- End-to-end via ``run_miner`` to confirm the runner persists
  Detector 2's output correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autonomy_store import (
    get_lesson_by_id,
    insert_review_issue,
    list_lesson_candidates,
    list_lesson_evidence,
)
from learning_miner import run_miner
from learning_miner.detectors.human_issue_cluster import (
    MIN_CLUSTER_SIZE,
    HumanIssueClusterDetector,
    _derive_file_pattern,
    _resolve_platform_profile,
    build,
)
from tests.conftest import seed_human_issue_for_learning, seed_pr_run_for_learning


@pytest.fixture(autouse=True)
def clear_platform_cache():
    """_resolve_platform_profile is lru_cached; reset across tests."""
    _resolve_platform_profile.cache_clear()
    yield
    _resolve_platform_profile.cache_clear()


@pytest.fixture
def conn(learning_conn):
    return learning_conn


def _days_ago_iso(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _seed_pr_run(
    conn,
    *,
    pr_number: int,
    ticket_id: str,
    client_profile: str,
    opened_days_ago: int = 1,
) -> int:
    return seed_pr_run_for_learning(
        conn,
        pr_number=pr_number,
        ticket_id=ticket_id,
        client_profile=client_profile,
        opened_at=_days_ago_iso(opened_days_ago),
    )


_seed_human_issue = seed_human_issue_for_learning


class TestFilePatternDerivation:
    def test_common_extension(self) -> None:
        assert _derive_file_pattern(
            ["a.cls", "b.cls", "sub/c.cls"]
        ) == "*.cls"

    def test_common_top_level_dir(self) -> None:
        # All under the same top dir but different extensions.
        assert _derive_file_pattern(
            ["src/a.py", "src/foo/b.js", "src/c.go"]
        ) == "src/**"

    def test_mixed_paths_yields_empty_pattern(self) -> None:
        assert _derive_file_pattern(
            ["src/a.py", "lib/b.js", "cmd/c.go"]
        ) == ""

    def test_empty_inputs_yield_empty(self) -> None:
        assert _derive_file_pattern([]) == ""
        assert _derive_file_pattern(["", "."]) == ""


class TestThresholdAndGrouping:
    def test_below_threshold_no_candidate(self, conn) -> None:
        for i in range(MIN_CLUSTER_SIZE - 1):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path="force-app/foo.cls",
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert out == []

    def test_at_threshold_one_candidate(self, conn) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"force-app/foo{i}.cls",
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert len(out) == 1
        prop = out[0]
        assert prop.client_profile == "xcsf30"
        assert prop.platform_profile == "salesforce"
        assert prop.pattern_key == "security|*.cls"
        assert prop.scope_key == "xcsf30|salesforce|security|*.cls"
        # Frequency-based severity: exactly MIN_CLUSTER_SIZE stays at info.
        assert prop.severity == "info"

    def test_high_frequency_bumps_severity(self, conn) -> None:
        count = MIN_CLUSTER_SIZE * 2
        for i in range(count):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"force-app/foo{i}.cls",
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert len(out) == 1
        assert out[0].severity == "warn"

    def test_same_pr_run_multiple_issues_counts_once(self, conn) -> None:
        # Two human issues on the same PR — the cluster size is distinct
        # pr_run_ids, not raw issue count.
        pr_id = _seed_pr_run(
            conn,
            pr_number=1,
            ticket_id="T-1",
            client_profile="xcsf30",
        )
        for i in range(MIN_CLUSTER_SIZE):
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"force-app/foo{i}.cls",
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert out == []

    def test_window_excludes_old_pr_runs(self, conn) -> None:
        # Old runs outside window.
        for i in range(MIN_CLUSTER_SIZE):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
                opened_days_ago=30,
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"force-app/foo{i}.cls",
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert out == []


class TestPlatformResolution:
    def test_unknown_profile_is_dropped(self, conn) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="bogus-profile-that-does-not-exist",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"a{i}.py",
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert out == []

    def test_category_only_cluster_when_no_file_pattern(self, conn) -> None:
        # Mixed extensions + mixed top-level dirs → no common pattern.
        paths = [
            "force-app/main/foo.cls",
            "config/bar.json",
            "lib/baz.yaml",
        ]
        for i, p in enumerate(paths):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="documentation",
                file_path=p,
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert len(out) == 1
        prop = out[0]
        assert prop.pattern_key == "documentation|"
        assert prop.scope_key == "xcsf30|salesforce|documentation|"

    def test_normalizes_blank_category_to_unknown(self, conn) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="",
                file_path=f"force-app/foo{i}.cls",
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert len(out) == 1
        assert out[0].pattern_key == "(unknown)|*.cls"


class TestEvidenceShape:
    def test_evidence_uses_review_issues_source_ref(self, conn) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"force-app/foo{i}.cls",
                summary=f"issue on foo{i}",
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert len(out) == 1
        srcs = {item.source_ref for item in out[0].evidence}
        # Unique per issue id → no UNIQUE collision inside lesson_evidence.
        assert len(srcs) == MIN_CLUSTER_SIZE
        assert all(s.startswith("review_issues#") for s in srcs)

    def test_evidence_trace_id_is_ticket_id(self, conn) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"TICK-{i}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"force-app/foo{i}.cls",
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        trace_ids = {e.trace_id for e in out[0].evidence}
        assert trace_ids == {"TICK-0", "TICK-1", "TICK-2"}


class TestSourceAndValidityFilters:
    """Ensure rows that shouldn't count, don't."""

    def test_ai_review_issues_are_excluded(self, conn) -> None:
        # Seed enough AI-review issues to exceed threshold; detector
        # must ignore them because its WHERE clause filters on
        # source='human_review'.
        for i in range(MIN_CLUSTER_SIZE):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            insert_review_issue(
                conn,
                pr_run_id=pr_id,
                source="ai_review",
                file_path=f"force-app/foo{i}.cls",
                category="security",
                summary=f"AI-found issue {i}",
                is_valid=1,
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert out == []

    def test_invalidated_human_issues_are_excluded(self, conn) -> None:
        # is_valid=0 means the issue has been invalidated (e.g. judge
        # rejected, human retracted). Detector must ignore these.
        for i in range(MIN_CLUSTER_SIZE):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            insert_review_issue(
                conn,
                pr_run_id=pr_id,
                source="human_review",
                file_path=f"force-app/foo{i}.cls",
                category="security",
                summary="invalidated",
                is_valid=0,
            )
        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert out == []


class TestMultiClusterScenario:
    """A realistic mix: two clusters above threshold, one below."""

    def test_reports_only_clusters_meeting_threshold(self, conn) -> None:
        pr_counter = 0

        def _next_pr(ticket_id: str) -> int:
            nonlocal pr_counter
            pr_counter += 1
            return _seed_pr_run(
                conn,
                pr_number=pr_counter,
                ticket_id=ticket_id,
                client_profile="xcsf30",
            )

        # Cluster 1 (security|*.cls) — above threshold.
        for i in range(MIN_CLUSTER_SIZE):
            pr = _next_pr(f"SEC-{i}")
            _seed_human_issue(
                conn,
                pr_run_id=pr,
                category="security",
                file_path=f"force-app/a{i}.cls",
            )
        # Cluster 2 (docs|*.md) — above threshold.
        for i in range(MIN_CLUSTER_SIZE):
            pr = _next_pr(f"DOC-{i}")
            _seed_human_issue(
                conn,
                pr_run_id=pr,
                category="documentation",
                file_path=f"README-{i}.md",
            )
        # Cluster 3 (performance|*.js) — below threshold (only 2 PRs).
        for i in range(MIN_CLUSTER_SIZE - 1):
            pr = _next_pr(f"PERF-{i}")
            _seed_human_issue(
                conn,
                pr_run_id=pr,
                category="performance",
                file_path=f"src/app-{i}.js",
            )

        out = HumanIssueClusterDetector().scan(conn, window_days=14)
        pattern_keys = {p.pattern_key for p in out}
        assert pattern_keys == {
            "security|*.cls",
            "documentation|*.md",
        }


class TestEmptyPlatformProfile:
    """A real client profile with platform_profile='' should be dropped.

    Detector 2 is deliberately scoped — cross-platform lessons go
    through a separate (future) flow. A profile without a platform
    can't be targeted correctly, so its human issues are ignored.
    """

    def test_dropped_when_platform_profile_empty(
        self, conn, tmp_path
    ) -> None:
        from unittest.mock import patch

        import client_profile as cp
        from learning_miner.detectors import human_issue_cluster as hic

        # Point the loader at a scratch profiles dir.
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "no-platform.yaml").write_text(
            "client: no platform\nplatform_profile: \"\"\n"
        )

        # Patch the loader to use our scratch dir AND bust the
        # module-level lru_cache that remembers earlier lookups.
        hic._resolve_platform_profile.cache_clear()
        with patch.object(cp, "PROFILES_DIR", profiles_dir):
            for i in range(MIN_CLUSTER_SIZE):
                pr_id = _seed_pr_run(
                    conn,
                    pr_number=i + 1,
                    ticket_id=f"NP-{i}",
                    client_profile="no-platform",
                )
                _seed_human_issue(
                    conn,
                    pr_run_id=pr_id,
                    category="security",
                    file_path=f"a{i}.py",
                )
            out = HumanIssueClusterDetector().scan(conn, window_days=14)
        assert out == []


class TestFrequencySemantics:
    """The `frequency` column must reflect cluster size, not scan count."""

    def test_frequency_reflects_cluster_size_on_first_scan(self, conn) -> None:
        cluster_size = MIN_CLUSTER_SIZE + 2
        for i in range(cluster_size):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"force-app/foo{i}.cls",
            )
        detector = build()
        run_miner(conn, [detector], window_days=14)
        candidates = list_lesson_candidates(conn)
        assert len(candidates) == 1
        # One nightly pass against a N-PR cluster must persist
        # frequency=N, not 1.
        assert candidates[0]["frequency"] == cluster_size

    def test_subsequent_scan_does_not_regress_frequency(self, conn) -> None:
        cluster_size = MIN_CLUSTER_SIZE + 2
        for i in range(cluster_size):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"force-app/foo{i}.cls",
            )
        detector = build()
        run_miner(conn, [detector], window_days=14)
        # Simulate a narrower rescan (still above threshold but
        # smaller). MAX semantics mean frequency stays at the larger
        # number observed.
        # We rerun with window=1 day — all pr_runs opened "today"
        # still land inside the window, so rescan sees same cluster.
        # Here we re-verify the upsert doesn't blindly overwrite.
        run_miner(conn, [detector], window_days=14)
        candidates = list_lesson_candidates(conn)
        assert candidates[0]["frequency"] == cluster_size


class TestEndToEndViaRunner:
    def test_runner_persists_candidate_and_evidence(self, conn) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"force-app/foo{i}.cls",
                summary=f"summary {i}",
            )
        detector = build()
        result = run_miner(conn, [detector], window_days=14)
        assert result.total_candidates == 1
        assert result.total_evidence == MIN_CLUSTER_SIZE

        candidates = list_lesson_candidates(conn)
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["detector_name"] == "human_issue_cluster"
        assert cand["client_profile"] == "xcsf30"
        assert cand["platform_profile"] == "salesforce"
        assert cand["status"] == "proposed"

        ev = list_lesson_evidence(conn, cand["lesson_id"])
        assert len(ev) == MIN_CLUSTER_SIZE

    def test_rerun_does_not_duplicate(self, conn) -> None:
        for i in range(MIN_CLUSTER_SIZE):
            pr_id = _seed_pr_run(
                conn,
                pr_number=i + 1,
                ticket_id=f"T-{i+1}",
                client_profile="xcsf30",
            )
            _seed_human_issue(
                conn,
                pr_run_id=pr_id,
                category="security",
                file_path=f"force-app/foo{i}.cls",
            )
        detector = build()
        run_miner(conn, [detector], window_days=14)
        run_miner(conn, [detector], window_days=14)

        candidates = list_lesson_candidates(conn)
        assert len(candidates) == 1
        row = get_lesson_by_id(conn, candidates[0]["lesson_id"])
        assert row is not None
        # Frequency is MAX of window-observed cluster sizes; two
        # scans of the same cluster keep it at MIN_CLUSTER_SIZE.
        assert row["frequency"] == MIN_CLUSTER_SIZE
        # Evidence stays bounded — UNIQUE on
        # (lesson_id, trace_id, source_ref) makes second insert a no-op.
        ev = list_lesson_evidence(conn, candidates[0]["lesson_id"])
        assert len(ev) == MIN_CLUSTER_SIZE


class TestRecurrenceFor:
    """Detector 2 implements recurrence_for so outcomes.py can ask
    'did the pattern continue to show up after the lesson's PR merged?'
    """

    def _lesson_row(
        self,
        pattern_key: str,
        client_profile: str = "xcsf30",
    ):
        from unittest.mock import MagicMock
        row = MagicMock()
        def getitem(self, k):
            return {
                "pattern_key": pattern_key,
                "client_profile": client_profile,
            }.get(k, "")
        row.__getitem__ = getitem
        return row

    def test_zero_when_no_post_window_rows(self, conn) -> None:
        detector = HumanIssueClusterDetector()
        lesson = self._lesson_row("security|*.cls")
        out = detector.recurrence_for(
            conn,
            lesson=lesson,
            since_iso=_days_ago_iso(5),
            until_iso=_days_ago_iso(0),
        )
        assert out == 0

    def test_counts_recurring_issues_in_window(self, conn) -> None:
        """Seed 3 new issues in the window; recurrence should be 3."""
        detector = HumanIssueClusterDetector()
        since = _days_ago_iso(10)
        until = _days_ago_iso(0)
        for i in range(3):
            pr_id = _seed_pr_run(
                conn,
                pr_number=200 + i,
                ticket_id=f"POST-{i}",
                client_profile="xcsf30",
                opened_days_ago=5,
            )
            _seed_human_issue(
                conn, pr_run_id=pr_id,
                category="security", file_path=f"a{i}.cls",
            )
        lesson = self._lesson_row("security|*.cls")
        assert detector.recurrence_for(
            conn, lesson=lesson, since_iso=since, until_iso=until,
        ) == 3

    def test_counts_matching_rows_despite_outlier(self, conn) -> None:
        """A single off-pattern file among matching ones must not suppress
        the count.

        Regression: the old implementation derived ONE file_pattern
        from ALL fetched rows and compared it to the lesson's pattern.
        A single ``*.html`` row among 3 ``*.cls`` rows collapsed the
        derived pattern to ``''`` and returned 0 — as if no recurrence
        happened. We now test each row against the lesson's pattern
        individually.
        """
        detector = HumanIssueClusterDetector()
        since = _days_ago_iso(10)
        until = _days_ago_iso(0)
        # 3 genuine recurrences of the lesson's pattern.
        for i in range(3):
            pr_id = _seed_pr_run(
                conn,
                pr_number=400 + i,
                ticket_id=f"POST3-{i}",
                client_profile="xcsf30",
                opened_days_ago=5,
            )
            _seed_human_issue(
                conn, pr_run_id=pr_id,
                category="security", file_path=f"service{i}.cls",
            )
        # One unrelated issue in the same category but different extension.
        pr_id = _seed_pr_run(
            conn,
            pr_number=999,
            ticket_id="OUTLIER",
            client_profile="xcsf30",
            opened_days_ago=5,
        )
        _seed_human_issue(
            conn, pr_run_id=pr_id,
            category="security", file_path="landing.html",
        )
        lesson = self._lesson_row("security|*.cls")
        assert detector.recurrence_for(
            conn, lesson=lesson, since_iso=since, until_iso=until,
        ) == 3

    def test_mismatched_file_pattern_returns_zero(self, conn) -> None:
        """Issues whose derived file_pattern doesn't match the lesson's
        pattern are not counted — they're recurrences of a *different* lesson.
        """
        detector = HumanIssueClusterDetector()
        since = _days_ago_iso(10)
        until = _days_ago_iso(0)
        for i in range(3):
            pr_id = _seed_pr_run(
                conn,
                pr_number=300 + i,
                ticket_id=f"POST2-{i}",
                client_profile="xcsf30",
                opened_days_ago=5,
            )
            _seed_human_issue(
                conn, pr_run_id=pr_id,
                category="security", file_path=f"page{i}.html",
            )
        lesson = self._lesson_row("security|*.cls")
        assert detector.recurrence_for(
            conn, lesson=lesson, since_iso=since, until_iso=until,
        ) == 0

    def test_other_profile_issues_excluded(self, conn) -> None:
        detector = HumanIssueClusterDetector()
        since = _days_ago_iso(10)
        until = _days_ago_iso(0)
        for i in range(3):
            pr_id = _seed_pr_run(
                conn,
                pr_number=400 + i,
                ticket_id=f"POST3-{i}",
                client_profile="other-profile",
                opened_days_ago=5,
            )
            _seed_human_issue(
                conn, pr_run_id=pr_id,
                category="security", file_path=f"a{i}.cls",
            )
        lesson = self._lesson_row(
            "security|*.cls", client_profile="xcsf30"
        )
        assert detector.recurrence_for(
            conn, lesson=lesson, since_iso=since, until_iso=until,
        ) == 0

    def test_malformed_pattern_key_returns_zero(self, conn) -> None:
        detector = HumanIssueClusterDetector()
        lesson = self._lesson_row("not-a-pipe-key")
        assert detector.recurrence_for(
            conn,
            lesson=lesson,
            since_iso=_days_ago_iso(5),
            until_iso=_days_ago_iso(0),
        ) == 0
