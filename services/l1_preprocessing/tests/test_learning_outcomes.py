"""Tests for learning_miner/outcomes.py — merge poll + outcomes measurement.

The outcomes job touches three external surfaces:

- ``gh pr view`` for the merge-state poll
- ``git clone`` + ``git log`` for human-reedit detection
- ``autonomy.db`` for pre/post metric windows

Metric-window math is exercised against a real sqlite DB populated
with synthetic pr_runs rows. The merge poll + clone paths are mocked
via monkeypatch on ``learning_miner._subprocess.run_bin``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autonomy_store import (
    LessonCandidateUpsert,
    PrRunUpsert,
    autonomy_conn,
    get_latest_outcome,
    update_lesson_status,
    upsert_lesson_candidate,
    upsert_pr_run,
)
from learning_miner import outcomes as outcomes_mod
from learning_miner.outcomes import (
    _agent_email_lower,
    _classify_verdict,
    _detect_human_reedits,
    _lesson_edited_paths,
    _poll_merge_state,
    _scoped_metrics,
    run_outcomes,
)

# ---- verdict classifier ----------------------------------------------


class TestClassifyVerdict:
    def test_human_reedit_trumps_metrics(self) -> None:
        v = _classify_verdict(
            pre={"fpa": 0.9, "escape_rate": 0.02, "catch_rate": 0.8},
            post={"fpa": 0.95, "escape_rate": 0.01, "catch_rate": 0.85},
            pattern_recurrence=0,
            human_reedit_count=1,
        )
        assert v == "human_reedit"

    def test_pattern_recurrence_flags_regressed(self) -> None:
        v = _classify_verdict(
            pre={"fpa": 0.9, "escape_rate": 0.02, "catch_rate": 0.8},
            post={"fpa": 0.9, "escape_rate": 0.02, "catch_rate": 0.8},
            pattern_recurrence=5,
            human_reedit_count=0,
        )
        assert v == "regressed"

    def test_confirmed_on_improvement(self) -> None:
        v = _classify_verdict(
            pre={"fpa": 0.80, "escape_rate": 0.10, "catch_rate": 0.50},
            post={"fpa": 0.90, "escape_rate": 0.05, "catch_rate": 0.70},
            pattern_recurrence=0,
            human_reedit_count=0,
        )
        assert v == "confirmed"

    def test_regressed_on_decline(self) -> None:
        v = _classify_verdict(
            pre={"fpa": 0.90, "escape_rate": 0.02, "catch_rate": 0.85},
            post={"fpa": 0.70, "escape_rate": 0.10, "catch_rate": 0.50},
            pattern_recurrence=0,
            human_reedit_count=0,
        )
        assert v == "regressed"

    def test_no_change_within_epsilon(self) -> None:
        v = _classify_verdict(
            pre={"fpa": 0.90, "escape_rate": 0.02, "catch_rate": 0.80},
            post={"fpa": 0.905, "escape_rate": 0.02, "catch_rate": 0.80},
            pattern_recurrence=0,
            human_reedit_count=0,
        )
        assert v == "no_change"

    def test_pending_when_both_windows_empty(self) -> None:
        v = _classify_verdict(
            pre={"fpa": None, "escape_rate": None, "catch_rate": None},
            post={"fpa": None, "escape_rate": None, "catch_rate": None},
            pattern_recurrence=0,
            human_reedit_count=0,
        )
        assert v == "pending"


# ---- scoped metrics --------------------------------------------------


class TestScopedMetrics:
    def _seed(self, conn, n: int, accepted: int, opened_at: str) -> None:
        for i in range(n):
            upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id=f"T-{opened_at}-{i}",
                    pr_number=hash((opened_at, i)) & 0xFFFF,
                    repo_full_name="acme/app",
                    head_sha=f"sha-{opened_at}-{i}",
                    client_profile="xcsf30",
                    opened_at=opened_at,
                    first_pass_accepted=1 if i < accepted else 0,
                ),
            )

    def test_fpa_windows_on_open_at_bounds(
        self, learning_conn
    ) -> None:
        conn = learning_conn
        self._seed(conn, n=10, accepted=8, opened_at="2026-03-01T00:00:00+00:00")
        self._seed(conn, n=10, accepted=5, opened_at="2026-04-01T00:00:00+00:00")
        pre = _scoped_metrics(
            conn,
            client_profile="xcsf30",
            since_iso="2026-02-15T00:00:00+00:00",
            until_iso="2026-03-15T00:00:00+00:00",
        )
        post = _scoped_metrics(
            conn,
            client_profile="xcsf30",
            since_iso="2026-03-15T00:00:00+00:00",
            until_iso="2026-04-15T00:00:00+00:00",
        )
        assert pre["fpa"] == 0.8
        assert post["fpa"] == 0.5

    def test_empty_window_returns_none(self, learning_conn) -> None:
        conn = learning_conn
        out = _scoped_metrics(
            conn,
            client_profile="xcsf30",
            since_iso="2030-01-01T00:00:00+00:00",
            until_iso="2030-02-01T00:00:00+00:00",
        )
        assert out == {"fpa": None, "escape_rate": None, "catch_rate": None}

    def test_empty_profile_returns_none(self, learning_conn) -> None:
        assert _scoped_metrics(
            learning_conn,
            client_profile="",
            since_iso="2026-01-01T00:00:00+00:00",
            until_iso="2026-02-01T00:00:00+00:00",
        ) == {"fpa": None, "escape_rate": None, "catch_rate": None}


# ---- _poll_merge_state -----------------------------------------------


class TestPollMergeState:
    def test_returns_none_when_gh_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd, **kw):
            proc = MagicMock()
            proc.returncode = 1
            proc.stderr = "gh: not authenticated"
            proc.stdout = ""
            return proc

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _poll_merge_state("https://github.com/x/y/pull/1") is None

    def test_returns_none_when_pr_not_merged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd, **kw):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = json.dumps({"state": "OPEN", "mergeCommit": None})
            proc.stderr = ""
            return proc

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _poll_merge_state("https://github.com/x/y/pull/1") is None

    def test_returns_info_when_merged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd, **kw):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = json.dumps({
                "state": "MERGED",
                "mergeCommit": {"oid": "deadbeef"},
                "mergedAt": "2026-04-17T12:00:00Z",
            })
            proc.stderr = ""
            return proc

        monkeypatch.setattr(subprocess, "run", fake_run)
        info = _poll_merge_state("https://github.com/x/y/pull/1")
        assert info is not None
        assert info.commit_sha == "deadbeef"
        assert info.merged_at == "2026-04-17T12:00:00Z"

    def test_malformed_json_is_tolerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd, **kw):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "{broken json"
            proc.stderr = ""
            return proc

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert _poll_merge_state("https://github.com/x/y/pull/1") is None


# ---- _lesson_edited_paths -------------------------------------------


class TestLessonEditedPaths:
    def _fake_row(self, delta: dict) -> MagicMock:
        row = MagicMock()
        row.__getitem__ = lambda self, k: (
            json.dumps(delta) if k == "proposed_delta_json" else None
        )
        return row

    def test_parses_unified_diff(self) -> None:
        row = self._fake_row({
            "target_path": "runtime/skills/a.md",
            "unified_diff": (
                "--- a/runtime/skills/a.md\n"
                "+++ b/runtime/skills/a.md\n"
                "@@\n+x\n"
            ),
        })
        assert _lesson_edited_paths(row) == ["runtime/skills/a.md"]

    def test_falls_back_to_target_path(self) -> None:
        row = self._fake_row({"target_path": "runtime/skills/b.md"})
        assert _lesson_edited_paths(row) == ["runtime/skills/b.md"]

    def test_handles_missing_delta(self) -> None:
        row = MagicMock()
        row.__getitem__ = lambda self, k: ""
        assert _lesson_edited_paths(row) == []


# ---- _detect_human_reedits with real git ----------------------------


@pytest.fixture
def origin_with_merge(tmp_path: Path) -> tuple[Path, str]:
    """A file-backed origin with one merge commit + one human commit after."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=origin, check=True, capture_output=True,
    )
    skill = origin / "runtime" / "skills" / "a.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("initial\n")
    subprocess.run(
        ["git", "add", "."], cwd=origin, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=init@t", "-c", "user.name=init",
         "commit", "-m", "init"],
        cwd=origin, check=True, capture_output=True,
    )
    # "Merge" commit — authored by the agent.
    skill.write_text("initial\nagent added rule\n")
    subprocess.run(
        ["git", "add", "."], cwd=origin, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=xcagent.rockwell@xcentium.com",
         "-c", "user.name=XCentium Agent",
         "commit", "-m", "chore(learning): LSN-1"],
        cwd=origin, check=True, capture_output=True,
    )
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=origin, check=True, capture_output=True, text=True,
    )
    merge_sha = proc.stdout.strip()
    # Human commit on top.
    skill.write_text("initial\nagent added rule\nhuman edit\n")
    subprocess.run(
        ["git", "add", "."], cwd=origin, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=alice@example.com", "-c", "user.name=Alice",
         "commit", "-m", "fix typo"],
        cwd=origin, check=True, capture_output=True,
    )
    return origin, merge_sha


def _lesson_with_diff(target: str, diff: str) -> MagicMock:
    row = MagicMock()
    payload = {
        "target_path": target,
        "unified_diff": diff,
    }
    def getitem(self, k):
        if k == "lesson_id":
            return "LSN-test"
        if k == "proposed_delta_json":
            return json.dumps(payload)
        return None
    row.__getitem__ = getitem
    return row


class TestDetectHumanReedits:
    def test_detects_human_commit_after_merge(
        self,
        origin_with_merge: tuple[Path, str],
    ) -> None:
        origin, merge_sha = origin_with_merge
        lesson = _lesson_with_diff(
            target="runtime/skills/a.md",
            diff=(
                "--- a/runtime/skills/a.md\n"
                "+++ b/runtime/skills/a.md\n"
                "@@\n+rule\n"
            ),
        )
        count, refs = _detect_human_reedits(
            lesson=lesson,
            merged_commit_sha=merge_sha,
            scratch_root=origin,
        )
        assert count == 1
        assert refs[0]["author"].startswith("Alice")

    def test_respects_agent_git_email_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When AGENT_GIT_EMAIL is set, pr_opener commits with that email.

        outcomes must recognize that email as agent-authored — otherwise
        every agent commit trips the HUMAN_REEDIT verdict.
        """
        monkeypatch.setenv("AGENT_GIT_EMAIL", "bot@example.com")
        assert _agent_email_lower() == "bot@example.com"
        origin = tmp_path / "origin"
        origin.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=origin,
            check=True, capture_output=True,
        )
        f = origin / "runtime" / "skills" / "a.md"
        f.parent.mkdir(parents=True)
        f.write_text("v1\n")
        subprocess.run(["git", "add", "."], cwd=origin, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=bot@example.com",
             "-c", "user.name=Bot", "commit", "-m", "first"],
            cwd=origin, check=True, capture_output=True,
        )
        merge_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=origin,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Same bot email on the post-merge commit — must be ignored.
        f.write_text("v2\n")
        subprocess.run(["git", "add", "."], cwd=origin, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=bot@example.com",
             "-c", "user.name=Bot", "commit", "-m", "bot follow-up"],
            cwd=origin, check=True, capture_output=True,
        )
        lesson = _lesson_with_diff(
            target="runtime/skills/a.md",
            diff="--- a/runtime/skills/a.md\n+++ b/runtime/skills/a.md\n@@\n+rule\n",
        )
        count, refs = _detect_human_reedits(
            lesson=lesson, merged_commit_sha=merge_sha, scratch_root=origin,
        )
        assert count == 0
        assert refs == []

    def test_dedupes_commit_touching_multiple_edited_files(
        self,
        tmp_path: Path,
    ) -> None:
        """A single human commit touching N edited files must count once.

        Regression guard: the loop iterates per-file, so without sha
        dedup a cross-file commit inflates human_reedit_count and
        pollutes refs.
        """
        origin = tmp_path / "origin"
        origin.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=origin, check=True, capture_output=True,
        )
        a = origin / "runtime" / "skills" / "a.md"
        b = origin / "runtime" / "skills" / "b.md"
        a.parent.mkdir(parents=True)
        a.write_text("a1\n")
        b.write_text("b1\n")
        subprocess.run(["git", "add", "."], cwd=origin, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=xcagent.rockwell@xcentium.com",
             "-c", "user.name=A", "commit", "-m", "agent merge"],
            cwd=origin, check=True, capture_output=True,
        )
        merge_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=origin,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # One human commit modifying BOTH files.
        a.write_text("a2\n")
        b.write_text("b2\n")
        subprocess.run(["git", "add", "."], cwd=origin, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=bob@example.com", "-c", "user.name=Bob",
             "commit", "-m", "cross-file human fix"],
            cwd=origin, check=True, capture_output=True,
        )
        lesson = _lesson_with_diff(
            target="runtime/skills/a.md",
            diff=(
                "--- a/runtime/skills/a.md\n"
                "+++ b/runtime/skills/a.md\n"
                "@@\n+x\n"
                "--- a/runtime/skills/b.md\n"
                "+++ b/runtime/skills/b.md\n"
                "@@\n+y\n"
            ),
        )
        count, refs = _detect_human_reedits(
            lesson=lesson, merged_commit_sha=merge_sha, scratch_root=origin,
        )
        assert count == 1
        assert len(refs) == 1
        assert refs[0]["author"].startswith("Bob")

    def test_ignores_agent_only_commits(
        self,
        tmp_path: Path,
    ) -> None:
        # Build an origin where the only post-merge commit is also by
        # the agent — should yield count=0.
        origin = tmp_path / "origin"
        origin.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=origin,
            check=True, capture_output=True,
        )
        f = origin / "runtime" / "skills" / "x.md"
        f.parent.mkdir(parents=True)
        f.write_text("v1\n")
        subprocess.run(["git", "add", "."], cwd=origin, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=xcagent.rockwell@xcentium.com",
             "-c", "user.name=A", "commit", "-m", "first"],
            cwd=origin, check=True, capture_output=True,
        )
        sha1 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=origin,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        f.write_text("v2\n")
        subprocess.run(["git", "add", "."], cwd=origin, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=xcagent.rockwell@xcentium.com",
             "-c", "user.name=A", "commit", "-m", "second"],
            cwd=origin, check=True, capture_output=True,
        )
        lesson = _lesson_with_diff(
            target="runtime/skills/x.md",
            diff="--- a/runtime/skills/x.md\n+++ b/runtime/skills/x.md\n@@\n+rule\n",
        )
        count, refs = _detect_human_reedits(
            lesson=lesson, merged_commit_sha=sha1, scratch_root=origin,
        )
        assert count == 0
        assert refs == []


# ---- run_outcomes end-to-end (with real metrics, mocked gh) ---------


class TestRunOutcomes:
    def _seed_applied_lesson(
        self,
        *,
        days_since_merge: int,
        merged_commit_sha: str,
        pr_url: str = "https://github.com/x/y/pull/1",
    ) -> str:
        """Seed a lesson at status='applied' with merged_commit_sha set
        and updated_at dated to be past the window boundary."""
        from learning_miner.detectors.base import compute_lesson_id

        scope = "xcsf30|salesforce|security|*.cls"
        lid = compute_lesson_id("human_issue_cluster", "p|k", scope)
        with autonomy_conn() as conn:
            upsert_lesson_candidate(
                conn,
                LessonCandidateUpsert(
                    lesson_id=lid,
                    detector_name="human_issue_cluster",
                    pattern_key="p|k",
                    client_profile="xcsf30",
                    platform_profile="salesforce",
                    scope_key=scope,
                    proposed_delta_json=json.dumps(
                        {
                            "target_path": "runtime/skills/a.md",
                            "unified_diff": (
                                "--- a/runtime/skills/a.md\n"
                                "+++ b/runtime/skills/a.md\n"
                                "@@\n+rule\n"
                            ),
                        }
                    ),
                ),
            )
            update_lesson_status(conn, lid, "draft_ready", reason="drafter")
            update_lesson_status(conn, lid, "approved", reason="ok")
            update_lesson_status(
                conn, lid, "applied",
                reason="pr opened",
                pr_url=pr_url,
                merged_commit_sha=merged_commit_sha,
            )
            pivot = (
                datetime.now(UTC) - timedelta(days=days_since_merge)
            ).isoformat()
            conn.execute(
                "UPDATE lesson_candidates SET updated_at = ? "
                "WHERE lesson_id = ?",
                (pivot, lid),
            )
            conn.commit()
        return lid

    def test_applied_lesson_outside_window_is_skipped(
        self,
        learning_conn,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from config import settings

        monkeypatch.setattr(
            settings, "autonomy_db_path", str(tmp_path / "a.db")
        )
        monkeypatch.setattr(settings, "learning_outcomes_window_days", 14)
        lid = self._seed_applied_lesson(
            days_since_merge=1, merged_commit_sha="abc123"
        )
        stats = run_outcomes()
        assert stats.outcomes_measured == 0
        with autonomy_conn() as conn:
            assert get_latest_outcome(conn, lid) is None

    def test_applied_lesson_inside_window_writes_outcome(
        self,
        learning_conn,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from config import settings

        monkeypatch.setattr(
            settings, "autonomy_db_path", str(tmp_path / "a.db")
        )
        monkeypatch.setattr(settings, "learning_outcomes_window_days", 14)
        # Human-reedit detection needs a live origin; stub to "no reedits".
        monkeypatch.setattr(
            outcomes_mod,
            "_detect_human_reedits",
            lambda **_kw: (0, []),
        )
        lid = self._seed_applied_lesson(
            days_since_merge=20, merged_commit_sha="feeddead"
        )
        stats = run_outcomes()
        assert stats.outcomes_measured == 1
        with autonomy_conn() as conn:
            outcome = get_latest_outcome(conn, lid)
        assert outcome is not None
        assert outcome["verdict"] in {
            "pending", "confirmed", "no_change", "regressed",
        }
        assert outcome["human_reedit_count"] == 0

    def test_scratch_clone_lazy_when_no_measurement(
        self,
        learning_conn,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When no lesson reaches measurement this tick, don't clone.

        A tick where every applied lesson is still merge-polling (or
        has no PR url) should not pay the harness-clone cost. Previously
        run_outcomes cloned eagerly before iterating, so a fleet of
        half-built lessons burned a full clone per tick for nothing.
        """
        from config import settings
        from learning_miner.detectors.base import compute_lesson_id

        monkeypatch.setattr(
            settings, "autonomy_db_path", str(tmp_path / "a.db")
        )
        monkeypatch.setattr(
            settings,
            "learning_harness_repo_url",
            "https://example.invalid/harness.git",
        )
        # One applied lesson with pr_url but no merge sha; gh poll will
        # be stubbed to report "not merged yet" — measurement is skipped,
        # and the clone should never happen.
        lid = compute_lesson_id("det", "p", "s|p|k")
        with autonomy_conn() as conn:
            upsert_lesson_candidate(
                conn,
                LessonCandidateUpsert(
                    lesson_id=lid,
                    detector_name="det",
                    pattern_key="p",
                    client_profile="xcsf30",
                    platform_profile="salesforce",
                    scope_key="s|p|k",
                    proposed_delta_json="{}",
                ),
            )
            update_lesson_status(conn, lid, "draft_ready", reason="d")
            update_lesson_status(conn, lid, "approved", reason="a")
            update_lesson_status(
                conn, lid, "applied", reason="pr opened",
                pr_url="https://github.com/x/y/pull/99",
            )

        clone_calls: list[list[str]] = []

        def fake_run_bin(binary, args, **_kw):
            # gh poll → "not merged yet".
            proc = MagicMock()
            if binary == "gh":
                proc.returncode = 0
                proc.stdout = json.dumps({"state": "OPEN"})
                proc.stderr = ""
            elif binary == "git" and args[:1] == ["clone"]:
                clone_calls.append(args)
                proc.returncode = 0
                proc.stdout = proc.stderr = ""
            else:
                proc.returncode = 0
                proc.stdout = proc.stderr = ""
            return proc

        monkeypatch.setattr(
            "learning_miner._subprocess.subprocess.run",
            lambda *a, **kw: fake_run_bin(a[0][0], a[0][1:], **kw),
        )
        stats = run_outcomes()
        # No measurement, so no clone.
        assert stats.outcomes_measured == 0
        assert clone_calls == []

    def test_merge_poll_writes_commit_sha(
        self,
        learning_conn,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from config import settings

        monkeypatch.setattr(
            settings, "autonomy_db_path", str(tmp_path / "a.db")
        )
        # Seed an applied lesson with pr_url but NO merge sha yet.
        from learning_miner.detectors.base import compute_lesson_id
        lid = compute_lesson_id("det", "p", "s|p|k")
        with autonomy_conn() as conn:
            upsert_lesson_candidate(
                conn,
                LessonCandidateUpsert(
                    lesson_id=lid,
                    detector_name="det",
                    pattern_key="p",
                    client_profile="xcsf30",
                    platform_profile="salesforce",
                    scope_key="s|p|k",
                    proposed_delta_json="{}",
                ),
            )
            update_lesson_status(conn, lid, "draft_ready", reason="d")
            update_lesson_status(conn, lid, "approved", reason="a")
            update_lesson_status(
                conn, lid, "applied", reason="pr opened",
                pr_url="https://github.com/x/y/pull/7",
            )

        def fake_gh(cmd, **_kw):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = json.dumps({
                "state": "MERGED",
                "mergeCommit": {"oid": "merged-sha-7"},
                "mergedAt": "2026-04-17T00:00:00Z",
            })
            proc.stderr = ""
            return proc

        monkeypatch.setattr(subprocess, "run", fake_gh)
        stats = run_outcomes()
        assert stats.merge_polls_resolved == 1
        with autonomy_conn() as conn:
            row = conn.execute(
                "SELECT merged_commit_sha FROM lesson_candidates "
                "WHERE lesson_id = ?",
                (lid,),
            ).fetchone()
        assert row["merged_commit_sha"] == "merged-sha-7"


class TestPatternRecurrence:
    """The outcomes helper delegates to the lesson's detector and
    tolerates missing / broken detectors without blocking."""

    def _make_lesson(
        self,
        *,
        detector_name: str = "human_issue_cluster",
        pattern_key: str = "security|*.cls",
        client_profile: str = "xcsf30",
    ):
        row = MagicMock()
        def getitem(self, k):
            return {
                "detector_name": detector_name,
                "pattern_key": pattern_key,
                "client_profile": client_profile,
            }.get(k, "")
        row.__getitem__ = getitem
        return row

    def test_unknown_detector_returns_zero(self, learning_conn) -> None:
        from learning_miner.outcomes import _pattern_recurrence
        out = _pattern_recurrence(
            learning_conn,
            lesson=self._make_lesson(detector_name="nonexistent"),
            since_iso="2026-01-01T00:00:00+00:00",
            until_iso="2026-02-01T00:00:00+00:00",
        )
        assert out == 0

    def test_empty_detector_name_returns_zero(self, learning_conn) -> None:
        from learning_miner.outcomes import _pattern_recurrence
        out = _pattern_recurrence(
            learning_conn,
            lesson=self._make_lesson(detector_name=""),
            since_iso="2026-01-01T00:00:00+00:00",
            until_iso="2026-02-01T00:00:00+00:00",
        )
        assert out == 0

    def test_mcp_drift_has_no_recurrence_impl_yet(
        self, learning_conn
    ) -> None:
        """Detector 1 doesn't override recurrence_for, so it falls
        through to 0 via count_pattern_recurrence.
        """
        from learning_miner.outcomes import _pattern_recurrence
        out = _pattern_recurrence(
            learning_conn,
            lesson=self._make_lesson(detector_name="mcp_drift"),
            since_iso="2026-01-01T00:00:00+00:00",
            until_iso="2026-02-01T00:00:00+00:00",
        )
        assert out == 0
