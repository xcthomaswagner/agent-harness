"""Tests for the v5 lesson-candidate / evidence / outcome repository helpers."""

from __future__ import annotations

import pytest

from autonomy_store import (
    LESSON_EVIDENCE_CAP,
    LessonCandidateUpsert,
    PrRunUpsert,
    get_lesson_by_id,
    insert_lesson_evidence,
    list_lesson_candidates,
    list_lesson_evidence,
    set_lesson_status_reason,
    update_lesson_status,
    upsert_lesson_candidate,
    upsert_pr_run,
)
from learning_miner.detectors.base import compute_lesson_id


@pytest.fixture
def conn(learning_conn):
    return learning_conn


def _base_candidate(**overrides: object) -> LessonCandidateUpsert:
    scope = str(overrides.pop("scope_key", "xcsf30|salesforce|security|foo.cls"))
    det = str(overrides.pop("detector_name", "human_issue_cluster"))
    pat = str(overrides.pop("pattern_key", "security|*.cls"))
    data: dict[str, object] = {
        "lesson_id": compute_lesson_id(det, pat, scope),
        "detector_name": det,
        "pattern_key": pat,
        "client_profile": "xcsf30",
        "platform_profile": "salesforce",
        "scope_key": scope,
        "proposed_delta_json": '{"edit_type": "append_section"}',
        "severity": "warn",
        "window_frequency": 1,
    }
    data.update(overrides)
    return LessonCandidateUpsert(**data)  # type: ignore[arg-type]


class TestUpsertLessonCandidate:
    def test_inserts_new_row_with_status_proposed_and_frequency_1(
        self, conn
    ) -> None:
        cid = upsert_lesson_candidate(
            conn, _base_candidate(), now="2026-04-10T00:00:00+00:00"
        )
        assert cid > 0
        row = conn.execute(
            "SELECT * FROM lesson_candidates WHERE id = ?", (cid,)
        ).fetchone()
        assert row["status"] == "proposed"
        assert row["frequency"] == 1
        assert row["detected_at"] == "2026-04-10T00:00:00+00:00"
        assert row["last_seen_at"] == "2026-04-10T00:00:00+00:00"
        assert row["severity"] == "warn"
        assert row["client_profile"] == "xcsf30"

    def test_repeat_detection_bumps_last_seen(self, conn) -> None:
        cid = upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=4),
            now="2026-04-10T00:00:00+00:00",
        )
        upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=4),
            now="2026-04-11T00:00:00+00:00",
        )
        upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=4),
            now="2026-04-12T00:00:00+00:00",
        )
        row = conn.execute(
            "SELECT * FROM lesson_candidates WHERE id = ?", (cid,)
        ).fetchone()
        # Frequency is MAX of observed cluster sizes, not scan count.
        assert row["frequency"] == 4
        # detected_at pinned to first detection, last_seen_at rolls.
        assert row["detected_at"] == "2026-04-10T00:00:00+00:00"
        assert row["last_seen_at"] == "2026-04-12T00:00:00+00:00"

    def test_window_frequency_seeds_initial_value(self, conn) -> None:
        cid = upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=7),
            now="2026-04-10T00:00:00+00:00",
        )
        row = conn.execute(
            "SELECT frequency FROM lesson_candidates WHERE id = ?",
            (cid,),
        ).fetchone()
        assert row["frequency"] == 7

    def test_upsert_uses_max_on_frequency(self, conn) -> None:
        cid = upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=5),
            now="2026-04-10T00:00:00+00:00",
        )
        # Narrower rescan — fewer evidence rows in the new window.
        upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=2),
            now="2026-04-11T00:00:00+00:00",
        )
        row = conn.execute(
            "SELECT frequency FROM lesson_candidates WHERE id = ?",
            (cid,),
        ).fetchone()
        # MAX(5, 2) = 5 — the narrower scan must not regress frequency.
        assert row["frequency"] == 5

        # Wider rescan — grows the number.
        upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=9),
            now="2026-04-12T00:00:00+00:00",
        )
        row = conn.execute(
            "SELECT frequency FROM lesson_candidates WHERE id = ?",
            (cid,),
        ).fetchone()
        assert row["frequency"] == 9

    def test_repeat_detection_does_not_overwrite_status_or_pr_url(
        self, conn
    ) -> None:
        upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=3),
            now="2026-04-10T00:00:00+00:00",
        )
        lid = compute_lesson_id(
            "human_issue_cluster",
            "security|*.cls",
            "xcsf30|salesforce|security|foo.cls",
        )
        # Simulate the approval flow advancing the status + assigning a PR.
        update_lesson_status(
            conn,
            lid,
            "draft_ready",
            reason="drafter done",
            pr_url="https://github.com/x/y/pull/1",
            now="2026-04-10T01:00:00+00:00",
        )
        # Another nightly rescan re-fires the same pattern with a
        # larger cluster than before.
        upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=5),
            now="2026-04-11T00:00:00+00:00",
        )
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        # Status + pr_url preserved, frequency widened.
        assert row["status"] == "draft_ready"
        assert row["pr_url"] == "https://github.com/x/y/pull/1"
        assert row["frequency"] == 5

    def test_upsert_preserves_drafted_delta_past_proposed(
        self, conn
    ) -> None:
        """Regression: once /draft has merged a ``unified_diff`` into
        ``proposed_delta_json``, a nightly rescan must NOT overwrite
        it with the mechanical starter. Losing the drafted diff would
        force the operator to re-draft (and re-spend Anthropic tokens)
        on every scan.
        """
        import json

        # Initial detection — mechanical starter delta.
        upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=3),
            now="2026-04-10T00:00:00+00:00",
        )
        lid = compute_lesson_id(
            "human_issue_cluster",
            "security|*.cls",
            "xcsf30|salesforce|security|foo.cls",
        )
        # Simulate /draft merging a Claude-drafted diff into the delta,
        # then transitioning to draft_ready.
        drafted = json.dumps({
            "target_path": "runtime/platform-profiles/salesforce/CODE_REVIEW_SUPPLEMENT.md",
            "unified_diff": "--- a/x\n+++ b/x\n@@\n+rule\n",
            "drafter_origin": "markdown_drafter",
        }, sort_keys=True)
        update_lesson_status(
            conn, lid, "draft_ready",
            reason="drafter done",
            proposed_delta_json=drafted,
            now="2026-04-10T01:00:00+00:00",
        )
        # Nightly rescan with the fresh mechanical delta (no unified_diff).
        upsert_lesson_candidate(
            conn,
            _base_candidate(window_frequency=5),
            now="2026-04-11T00:00:00+00:00",
        )
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        # Drafted delta (with unified_diff) must survive the rescan.
        stored = json.loads(row["proposed_delta_json"])
        assert "unified_diff" in stored
        assert stored["drafter_origin"] == "markdown_drafter"
        # frequency still widens — only the delta is frozen.
        assert row["frequency"] == 5

    def test_upsert_refreshes_delta_while_proposed(self, conn) -> None:
        """For a still-``proposed`` lesson, the mechanical delta MUST
        refresh on rescan so an evolved detector output shows up.
        """
        import json

        upsert_lesson_candidate(
            conn,
            _base_candidate(
                window_frequency=3,
                proposed_delta_json='{"anchor": "v1"}',
            ),
            now="2026-04-10T00:00:00+00:00",
        )
        lid = compute_lesson_id(
            "human_issue_cluster",
            "security|*.cls",
            "xcsf30|salesforce|security|foo.cls",
        )
        # Second scan emits a refined delta.
        upsert_lesson_candidate(
            conn,
            _base_candidate(
                window_frequency=4,
                proposed_delta_json='{"anchor": "v2"}',
            ),
            now="2026-04-11T00:00:00+00:00",
        )
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        assert row["status"] == "proposed"
        assert json.loads(row["proposed_delta_json"]) == {"anchor": "v2"}


class TestInsertLessonEvidence:
    def _seed_lesson(self, conn) -> str:
        upsert_lesson_candidate(
            conn, _base_candidate(), now="2026-04-10T00:00:00+00:00"
        )
        return compute_lesson_id(
            "human_issue_cluster",
            "security|*.cls",
            "xcsf30|salesforce|security|foo.cls",
        )

    def test_insert_creates_row(self, conn) -> None:
        lid = self._seed_lesson(conn)
        pr_run_id = upsert_pr_run(
            conn,
            PrRunUpsert(
                ticket_id="SCRUM-42",
                pr_number=1,
                repo_full_name="acme/app",
                head_sha="sha-1",
            ),
        )
        new_id = insert_lesson_evidence(
            conn,
            lesson_id=lid,
            trace_id="SCRUM-42",
            source_ref="review_issues#101",
            observed_at="2026-04-10T05:00:00+00:00",
            snippet="SOQL injection in foo.cls",
            pr_run_id=pr_run_id,
        )
        assert new_id is not None and new_id > 0
        rows = list_lesson_evidence(conn, lid)
        assert len(rows) == 1
        assert rows[0]["trace_id"] == "SCRUM-42"
        assert rows[0]["pr_run_id"] == pr_run_id

    def test_insert_accepts_null_pr_run_id(self, conn) -> None:
        # Some detectors (e.g. tool_index-only patterns) don't always tie
        # to a specific pr_run. pr_run_id must remain nullable.
        lid = self._seed_lesson(conn)
        new_id = insert_lesson_evidence(
            conn,
            lesson_id=lid,
            trace_id="T-noprrun",
            source_ref="tool_index.json#x",
            observed_at="2026-04-10T05:00:00+00:00",
            snippet="no pr_run",
        )
        assert new_id is not None and new_id > 0
        rows = list_lesson_evidence(conn, lid)
        assert len(rows) == 1
        assert rows[0]["pr_run_id"] is None

    def test_unique_collision_is_noop(self, conn) -> None:
        lid = self._seed_lesson(conn)
        insert_lesson_evidence(
            conn,
            lesson_id=lid,
            trace_id="T-1",
            source_ref="r",
            observed_at="2026-04-10T05:00:00+00:00",
            snippet="first",
        )
        second = insert_lesson_evidence(
            conn,
            lesson_id=lid,
            trace_id="T-1",
            source_ref="r",
            observed_at="2026-04-10T06:00:00+00:00",
            snippet="should be ignored",
        )
        assert second is None
        rows = list_lesson_evidence(conn, lid)
        assert len(rows) == 1
        # First snippet preserved — no update path for evidence.
        assert rows[0]["snippet"] == "first"

    def test_trims_oldest_when_over_cap(self, conn) -> None:
        lid = self._seed_lesson(conn)
        # Insert LESSON_EVIDENCE_CAP + 3 unique rows; expect oldest 3 gone.
        total = LESSON_EVIDENCE_CAP + 3
        for i in range(total):
            insert_lesson_evidence(
                conn,
                lesson_id=lid,
                trace_id=f"T-{i:03d}",
                source_ref=f"r{i}",
                observed_at="2026-04-10T05:00:00+00:00",
                snippet=f"ev{i}",
            )
        rows = list_lesson_evidence(conn, lid, limit=LESSON_EVIDENCE_CAP + 10)
        assert len(rows) == LESSON_EVIDENCE_CAP
        # Newest kept: trace_ids T-022..T-003 (assuming CAP=20 and total=23).
        trace_ids = {r["trace_id"] for r in rows}
        # Everything with i >= 3 survives.
        assert all(f"T-{i:03d}" in trace_ids for i in range(3, total))
        # Everything with i < 3 got trimmed.
        assert all(f"T-{i:03d}" not in trace_ids for i in range(3))

    def test_cap_zero_rejected(self, conn) -> None:
        lid = self._seed_lesson(conn)
        with pytest.raises(ValueError, match="cap must be"):
            insert_lesson_evidence(
                conn,
                lesson_id=lid,
                trace_id="T-x",
                source_ref="r",
                observed_at="2026-04-10T05:00:00+00:00",
                cap=0,
            )

    def test_respects_custom_cap(self, conn) -> None:
        lid = self._seed_lesson(conn)
        for i in range(5):
            insert_lesson_evidence(
                conn,
                lesson_id=lid,
                trace_id=f"T-{i}",
                source_ref=f"r{i}",
                observed_at="2026-04-10T05:00:00+00:00",
                snippet=f"ev{i}",
                cap=2,
            )
        rows = list_lesson_evidence(conn, lid, limit=10)
        assert len(rows) == 2
        trace_ids = {r["trace_id"] for r in rows}
        assert trace_ids == {"T-3", "T-4"}


class TestListEvidenceForLessons:
    def _seed(self, conn, lid: str, n: int) -> None:
        for i in range(n):
            insert_lesson_evidence(
                conn,
                lesson_id=lid,
                trace_id=f"T-{lid}-{i}",
                source_ref=f"ref-{i}",
                observed_at=f"2026-04-{i+1:02d}",
                snippet="",
            )

    def _seed_candidate(self, conn, scope_key: str) -> str:
        upsert_lesson_candidate(conn, _base_candidate(scope_key=scope_key))
        return compute_lesson_id(
            "human_issue_cluster", "security|*.cls", scope_key
        )

    def test_empty_lesson_ids_returns_empty_dict(self, conn) -> None:
        from autonomy_store import list_evidence_for_lessons
        assert list_evidence_for_lessons(conn, []) == {}

    def test_buckets_evidence_by_lesson_id(self, conn) -> None:
        from autonomy_store import list_evidence_for_lessons
        l1 = self._seed_candidate(conn, "A")
        l2 = self._seed_candidate(conn, "B")
        self._seed(conn, l1, 2)
        self._seed(conn, l2, 3)
        out = list_evidence_for_lessons(conn, [l1, l2])
        assert len(out[l1]) == 2
        assert len(out[l2]) == 3

    def test_missing_lesson_absent_from_result(self, conn) -> None:
        from autonomy_store import list_evidence_for_lessons
        lid = self._seed_candidate(conn, "X")
        out = list_evidence_for_lessons(conn, [lid, "LSN-absent"])
        assert "LSN-absent" not in out
        assert lid not in out  # no evidence seeded → also absent

    def test_per_lesson_cap_respected(self, conn) -> None:
        from autonomy_store import list_evidence_for_lessons
        lid = self._seed_candidate(conn, "Y")
        self._seed(conn, lid, 5)
        out = list_evidence_for_lessons(conn, [lid], limit_per_lesson=2)
        assert len(out[lid]) == 2


class TestListLessonCandidates:
    def test_filters_combine(self, conn) -> None:
        upsert_lesson_candidate(
            conn,
            _base_candidate(
                scope_key="s1",
                pattern_key="p1",
                detector_name="det_a",
                client_profile="a",
            ),
            now="2026-04-10T00:00:00+00:00",
        )
        upsert_lesson_candidate(
            conn,
            _base_candidate(
                scope_key="s2",
                pattern_key="p2",
                detector_name="det_a",
                client_profile="b",
            ),
            now="2026-04-11T00:00:00+00:00",
        )
        upsert_lesson_candidate(
            conn,
            _base_candidate(
                scope_key="s3",
                pattern_key="p3",
                detector_name="det_b",
                client_profile="a",
            ),
            now="2026-04-12T00:00:00+00:00",
        )
        only_a = list_lesson_candidates(conn, client_profile="a")
        assert {r["scope_key"] for r in only_a} == {"s1", "s3"}

        only_det_a = list_lesson_candidates(conn, detector_name="det_a")
        assert {r["scope_key"] for r in only_det_a} == {"s1", "s2"}

        # Default status filter (None) returns all.
        assert len(list_lesson_candidates(conn)) == 3

    def test_orders_by_detected_at_desc(self, conn) -> None:
        upsert_lesson_candidate(
            conn,
            _base_candidate(scope_key="s1", pattern_key="p1"),
            now="2026-04-10T00:00:00+00:00",
        )
        upsert_lesson_candidate(
            conn,
            _base_candidate(scope_key="s2", pattern_key="p2"),
            now="2026-04-12T00:00:00+00:00",
        )
        upsert_lesson_candidate(
            conn,
            _base_candidate(scope_key="s3", pattern_key="p3"),
            now="2026-04-11T00:00:00+00:00",
        )
        rows = list_lesson_candidates(conn)
        assert [r["scope_key"] for r in rows] == ["s2", "s3", "s1"]

    def test_offset_paginates_after_ordering(self, conn) -> None:
        for scope, pattern, now in (
            ("s1", "p1", "2026-04-10T00:00:00+00:00"),
            ("s2", "p2", "2026-04-12T00:00:00+00:00"),
            ("s3", "p3", "2026-04-11T00:00:00+00:00"),
        ):
            upsert_lesson_candidate(
                conn,
                _base_candidate(scope_key=scope, pattern_key=pattern),
                now=now,
            )

        rows = list_lesson_candidates(conn, limit=1, offset=1)

        assert [r["scope_key"] for r in rows] == ["s3"]


class TestUpdateLessonStatus:
    def _seed(self, conn) -> str:
        upsert_lesson_candidate(
            conn, _base_candidate(), now="2026-04-10T00:00:00+00:00"
        )
        return compute_lesson_id(
            "human_issue_cluster",
            "security|*.cls",
            "xcsf30|salesforce|security|foo.cls",
        )

    def test_valid_transition_proposed_to_draft_ready(self, conn) -> None:
        lid = self._seed(conn)
        row = update_lesson_status(
            conn,
            lid,
            "draft_ready",
            reason="drafter produced diff",
            now="2026-04-10T01:00:00+00:00",
        )
        assert row["status"] == "draft_ready"
        assert row["status_reason"] == "drafter produced diff"
        assert row["updated_at"] == "2026-04-10T01:00:00+00:00"

    def test_invalid_transition_raises(self, conn) -> None:
        lid = self._seed(conn)
        with pytest.raises(ValueError, match="invalid transition"):
            # proposed -> applied is not allowed (must go through draft_ready+approved)
            update_lesson_status(conn, lid, "applied")

    def test_unknown_lesson_raises(self, conn) -> None:
        with pytest.raises(ValueError, match="unknown lesson_id"):
            update_lesson_status(conn, "LSN-deadbeef", "draft_ready")

    def test_side_channel_fields_only_set_when_supplied(self, conn) -> None:
        lid = self._seed(conn)
        update_lesson_status(
            conn, lid, "draft_ready", pr_url="https://example.com/pull/1"
        )
        # Transition again without pr_url — should not clobber.
        update_lesson_status(conn, lid, "approved", reason="ok")
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        assert row["pr_url"] == "https://example.com/pull/1"
        assert row["status"] == "approved"
        # Merged SHA still empty because we never set it.
        assert row["merged_commit_sha"] == ""

    def test_terminal_states_reject_further_transitions(self, conn) -> None:
        lid = self._seed(conn)
        update_lesson_status(conn, lid, "rejected", reason="not useful")
        with pytest.raises(ValueError, match="invalid transition"):
            update_lesson_status(conn, lid, "proposed")

    def test_truncates_long_reason_to_cap(self, conn) -> None:
        """Regression: set_lesson_status_reason truncated to 500 but
        update_lesson_status didn't — so a verbose pr_opener error
        could survive one writer and not the other. Both now apply
        the same LESSON_REASON_MAX_LEN cap.
        """
        from autonomy_store import LESSON_REASON_MAX_LEN
        lid = self._seed(conn)
        long = "x" * (LESSON_REASON_MAX_LEN + 200)
        update_lesson_status(conn, lid, "draft_ready", reason=long)
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        assert len(row["status_reason"]) == LESSON_REASON_MAX_LEN

    def test_snooze_cycle_allowed(self, conn) -> None:
        lid = self._seed(conn)
        update_lesson_status(
            conn,
            lid,
            "snoozed",
            reason="waiting for more data",
            next_review_at="2026-05-01T00:00:00+00:00",
        )
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        assert row["status"] == "snoozed"
        assert row["next_review_at"] == "2026-05-01T00:00:00+00:00"
        # snoozed -> proposed is allowed (wake-up path).
        update_lesson_status(conn, lid, "proposed", reason="review due")
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        assert row["status"] == "proposed"


class TestSetLessonStatusReason:
    def _seed(self, conn) -> str:
        upsert_lesson_candidate(
            conn, _base_candidate(), now="2026-04-10T00:00:00+00:00"
        )
        return compute_lesson_id(
            "human_issue_cluster",
            "security|*.cls",
            "xcsf30|salesforce|security|foo.cls",
        )

    def test_updates_reason_without_changing_status(self, conn) -> None:
        lid = self._seed(conn)
        set_lesson_status_reason(conn, lid, "drafter: git apply failed")
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        assert row["status"] == "proposed"
        assert row["status_reason"] == "drafter: git apply failed"

    def test_truncates_to_500_chars(self, conn) -> None:
        lid = self._seed(conn)
        long = "x" * 700
        set_lesson_status_reason(conn, lid, long)
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        assert len(row["status_reason"]) == 500


class TestSetLessonMergedCommitSha:
    """Guards against empty sha — writing empty would CLEAR existing
    merge state and force outcomes.py to re-poll gh. Raise on misuse
    instead of silently reverting merge state.
    """

    def _seed(self, conn) -> str:
        from autonomy_store import update_lesson_status
        upsert_lesson_candidate(
            conn, _base_candidate(), now="2026-04-10T00:00:00+00:00"
        )
        lid = compute_lesson_id(
            "human_issue_cluster",
            "security|*.cls",
            "xcsf30|salesforce|security|foo.cls",
        )
        # Walk to applied so the sha write semantically makes sense.
        update_lesson_status(conn, lid, "draft_ready", reason="ok")
        update_lesson_status(conn, lid, "approved", reason="ok")
        update_lesson_status(conn, lid, "applied", reason="ok")
        return lid

    def test_rejects_empty_sha(self, conn) -> None:
        from autonomy_store import set_lesson_merged_commit_sha
        lid = self._seed(conn)
        with pytest.raises(ValueError, match="non-empty"):
            set_lesson_merged_commit_sha(conn, lid, "")

    def test_writes_valid_sha(self, conn) -> None:
        from autonomy_store import set_lesson_merged_commit_sha
        lid = self._seed(conn)
        set_lesson_merged_commit_sha(conn, lid, "abcdef12")
        row = get_lesson_by_id(conn, lid)
        assert row is not None
        assert row["merged_commit_sha"] == "abcdef12"
