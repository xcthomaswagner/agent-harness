"""Tests for the diagnostic checklist analyzer."""

from __future__ import annotations

from typing import Any

from diagnostic import render_diagnostic_checklist, run_diagnostic_checklist
from tracer import ARTIFACT_TOOL_INDEX

# ---------- helpers ----------


def _tool_index_entry(**index_fields: Any) -> dict[str, Any]:
    base = {
        "tool_counts": {},
        "tool_errors": {},
        "mcp_servers_used": [],
        "mcp_servers_available": [],
        "mcp_servers_unused": [],
        "first_tool_error": None,
        "assistant_turns": 0,
        "tool_call_count": 0,
    }
    base.update(index_fields)
    return {"event": ARTIFACT_TOOL_INDEX, "phase": "artifact", "index": base}


def _pipeline_complete_entry(review: str = "APPROVED", qa: str = "PASS") -> dict[str, Any]:
    return {
        "trace_id": "x",
        "timestamp": "2026-01-01T10:00:00Z",
        "phase": "complete",
        "event": "Pipeline complete",
        "source": "agent",
        "pr_url": "https://github.com/test/pr/1",
        "review_verdict": review,
        "qa_result": qa,
        "pipeline_mode": "simple",
        "units": 1,
    }


def _get_check(checks: list[dict[str, Any]], check_id: str) -> dict[str, Any]:
    for c in checks:
        if c.get("id") == check_id:
            return c
    raise AssertionError(f"check {check_id} not found")


# ---------- platform_detected ----------


class TestPlatformDetected:
    def test_green_both_agree(self) -> None:
        entries = [
            {"event": "PLATFORM: SALESFORCE detected"},
            {"event": "ticket_read", "platform_profile": "salesforce"},
            _pipeline_complete_entry(),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "platform_detected")
        assert check["status"] == "green"
        assert "salesforce" in check["evidence"].lower()

    def test_yellow_marker_only(self) -> None:
        entries = [{"event": "PLATFORM: SALESFORCE noted"}, _pipeline_complete_entry()]
        check = _get_check(run_diagnostic_checklist(entries), "platform_detected")
        assert check["status"] == "yellow"

    def test_yellow_profile_only(self) -> None:
        entries = [
            {"event": "ticket_read", "platform_profile": "sitecore"},
            _pipeline_complete_entry(),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "platform_detected")
        assert check["status"] == "yellow"

    def test_red_mismatch(self) -> None:
        entries = [
            {"event": "PLATFORM: sitecore"},
            {"event": "ticket_read", "platform_profile": "salesforce"},
            _pipeline_complete_entry(),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "platform_detected")
        assert check["status"] == "red"
        assert "mismatch" in check["evidence"].lower()

    def test_yellow_no_signals(self) -> None:
        entries = [_pipeline_complete_entry()]
        check = _get_check(run_diagnostic_checklist(entries), "platform_detected")
        assert check["status"] == "yellow"


# ---------- skill_invoked ----------


class TestSkillInvoked:
    def test_yellow_skill_called(self) -> None:
        entries = [_tool_index_entry(tool_counts={"Skill": 2, "Read": 1})]
        check = _get_check(run_diagnostic_checklist(entries), "skill_invoked")
        # Structurally limited to yellow max — tool_index can't verify which skill.
        assert check["status"] == "yellow"
        assert check["details"]["skill_calls"] == 2
        # Per reviewer fix #4: read_calls is not consumed by the check and
        # is no longer stashed in details.
        assert "read_calls" not in check["details"]

    def test_red_no_skill_calls(self) -> None:
        entries = [_tool_index_entry(tool_counts={"Bash": 3})]
        check = _get_check(run_diagnostic_checklist(entries), "skill_invoked")
        assert check["status"] == "red"

    def test_yellow_on_absent_index(self) -> None:
        # No tool_index artifact at all.
        entries = [_pipeline_complete_entry()]
        check = _get_check(run_diagnostic_checklist(entries), "skill_invoked")
        assert check["status"] == "yellow"
        assert "tool_index not available" in check["evidence"]


# ---------- mcp_preferred ----------


class TestMcpPreferred:
    def test_green_mcp_plus_minimal_bash(self) -> None:
        entries = [
            _tool_index_entry(tool_counts={"mcp__salesforce__sf_org_use": 3, "Bash": 2}),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "mcp_preferred")
        assert check["status"] == "green"
        assert check["details"]["mcp_count"] == 3
        assert check["details"]["bash_count"] == 2

    def test_yellow_mixed(self) -> None:
        entries = [
            _tool_index_entry(tool_counts={"mcp__salesforce__sf_org_use": 1, "Bash": 12}),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "mcp_preferred")
        assert check["status"] == "yellow"

    def test_red_all_shell(self) -> None:
        entries = [_tool_index_entry(tool_counts={"Bash": 7})]
        check = _get_check(run_diagnostic_checklist(entries), "mcp_preferred")
        assert check["status"] == "red"

    def test_yellow_no_activity(self) -> None:
        entries = [_tool_index_entry(tool_counts={})]
        check = _get_check(run_diagnostic_checklist(entries), "mcp_preferred")
        assert check["status"] == "yellow"


# ---------- first_deviation ----------


class TestFirstDeviation:
    def test_green_no_errors(self) -> None:
        entries = [
            _tool_index_entry(tool_counts={"Bash": 1}, first_tool_error=None),
            _pipeline_complete_entry(),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "first_deviation")
        assert check["status"] == "green"

    def test_yellow_only_tool_error(self) -> None:
        entries = [
            _tool_index_entry(
                first_tool_error={"tool": "Bash", "line": 42, "message": "oops"},
            ),
            _pipeline_complete_entry(),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "first_deviation")
        assert check["status"] == "yellow"
        assert "Bash" in check["evidence"]
        assert "42" in check["evidence"]

    def test_yellow_only_pipeline_error(self) -> None:
        entries = [
            _tool_index_entry(),
            {
                "trace_id": "x",
                "phase": "pipeline",
                "event": "error",
                "error_type": "RuntimeError",
                "error_message": "exploded",
            },
        ]
        check = _get_check(run_diagnostic_checklist(entries), "first_deviation")
        assert check["status"] == "yellow"
        assert "RuntimeError" in check["evidence"]

    def test_red_both_errors(self) -> None:
        entries = [
            _tool_index_entry(
                first_tool_error={"tool": "Bash", "line": 10, "message": "x"},
            ),
            {
                "trace_id": "x",
                "phase": "pipeline",
                "event": "error",
                "error_type": "RuntimeError",
                "error_message": "boom",
            },
        ]
        check = _get_check(run_diagnostic_checklist(entries), "first_deviation")
        assert check["status"] == "red"

    def test_dedup_skipped_trailing_webhook_does_not_hide_current_run_error(
        self,
    ) -> None:
        """Improvement regression: diagnostic used to use its own
        ``_run_start_idx`` that just walked back to the last
        ``webhook_received`` — so a dedup-skipped trailing webhook
        (no subsequent ``processing_started`` or agent activity) would
        become the scan boundary and the legitimate error from the real
        current run would drop off (silently becoming "no pipeline
        error" → green). diagnostic now calls
        ``tracer.find_run_start_idx``, which skips dedup-only webhooks
        and picks the real boundary. This test proves the fix: the
        current run's error must surface through first_deviation even
        when a dedup-skipped webhook appears later in the trace.

        first_deviation's two-signal discipline maps "pipeline error
        only, no tool error" → yellow, so we assert yellow + the error
        text, not red. Before the fix, the same input would have
        returned green (the stale-webhook boundary hid the error).
        """
        entries = [
            # --- real current run ---
            {"event": "webhook_received", "phase": "intake"},
            {"event": "processing_started"},
            {
                "trace_id": "x",
                "phase": "pipeline",
                "event": "error",
                "error_type": "RuntimeError",
                "error_message": "real error from current run",
            },
            # --- dedup-skipped trailing webhook (no processing_started
            # after it, no agent activity) ---
            {"event": "webhook_received", "phase": "intake"},
        ]
        check = _get_check(
            run_diagnostic_checklist(entries), "first_deviation"
        )
        # With the fix, the pipeline error surfaces → yellow with the
        # error text in evidence. Without the fix, the scan boundary
        # was set AFTER the error so it returned green.
        assert check["status"] == "yellow", (
            f"expected yellow (pipeline error must surface through the "
            f"dedup-skipped trailing webhook), got {check['status']}"
        )
        assert "RuntimeError" in check["evidence"]

    def test_stale_error_from_prior_run_ignored(self) -> None:
        """Fix #1: error from a prior run must not surface in current run.

        Trace has two ``webhook_received`` events (two pipeline runs). The
        FIRST run errored out; the SECOND run is clean. The first_deviation
        check must scope its error scan to entries after the latest
        ``webhook_received``, so it returns green — not red.
        """
        entries = [
            # --- run 1 (errored) ---
            {"event": "webhook_received", "phase": "intake"},
            {"event": "processing_started"},
            {
                "trace_id": "x",
                "phase": "pipeline",
                "event": "error",
                "error_type": "RuntimeError",
                "error_message": "stale boom — from run 1",
            },
            # --- run 2 (clean re-run) ---
            {"event": "webhook_received", "phase": "intake"},
            {"event": "processing_started"},
            _tool_index_entry(tool_counts={"Bash": 1}, first_tool_error=None),
            _pipeline_complete_entry(),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "first_deviation")
        assert check["status"] == "green", (
            f"expected green (stale error from run 1 should be ignored), "
            f"got {check['status']}: {check['evidence']}"
        )
        assert "stale" not in check["evidence"].lower()


# ---------- scratch_org ----------


class TestScratchOrg:
    def test_green_both_calls(self) -> None:
        entries = [
            {"event": "PLATFORM: SALESFORCE"},
            _tool_index_entry(
                tool_counts={
                    "mcp__salesforce__sf_scratch_create": 1,
                    "mcp__salesforce__sf_org_use": 1,
                },
            ),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "scratch_org")
        assert check["status"] == "green"

    def test_yellow_only_create(self) -> None:
        entries = [
            {"event": "ticket_read", "platform_profile": "salesforce"},
            _tool_index_entry(
                tool_counts={"mcp__salesforce__sf_scratch_create": 1},
            ),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "scratch_org")
        assert check["status"] == "yellow"

    def test_red_neither_call(self) -> None:
        entries = [
            {"event": "PLATFORM: SALESFORCE"},
            _tool_index_entry(tool_counts={"Bash": 1}),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "scratch_org")
        assert check["status"] == "red"

    def test_green_neutral_non_salesforce(self) -> None:
        entries = [
            {"event": "PLATFORM: sitecore"},
            _tool_index_entry(tool_counts={"Bash": 10}),
        ]
        check = _get_check(run_diagnostic_checklist(entries), "scratch_org")
        assert check["status"] == "green"
        assert "not applicable" in check["evidence"].lower()


# ---------- review_qa_verdict ----------


class TestReviewQaVerdict:
    def test_green_approved_pass(self) -> None:
        entries = [_pipeline_complete_entry("APPROVED", "PASS")]
        check = _get_check(run_diagnostic_checklist(entries), "review_qa_verdict")
        assert check["status"] == "green"

    def test_yellow_notes(self) -> None:
        entries = [_pipeline_complete_entry("APPROVED", "PASS_WITH_NOTES")]
        check = _get_check(run_diagnostic_checklist(entries), "review_qa_verdict")
        assert check["status"] == "yellow"

    def test_red_qa_fail(self) -> None:
        entries = [_pipeline_complete_entry("APPROVED", "FAIL")]
        check = _get_check(run_diagnostic_checklist(entries), "review_qa_verdict")
        assert check["status"] == "red"

    def test_red_review_rejected(self) -> None:
        entries = [_pipeline_complete_entry("REJECTED", "PASS")]
        check = _get_check(run_diagnostic_checklist(entries), "review_qa_verdict")
        assert check["status"] == "red"

    def test_yellow_pipeline_never_completed(self) -> None:
        """Fix #5: no Pipeline complete entry -> yellow, not crash."""
        entries = [
            {"event": "webhook_received"},
            {"event": "processing_started"},
        ]
        check = _get_check(run_diagnostic_checklist(entries), "review_qa_verdict")
        assert check["status"] == "yellow"
        assert "did not complete" in check["evidence"].lower()


# ---------- graceful degradation + end-to-end ----------


class TestDegradation:
    def test_all_absent_trace_no_crash(self) -> None:
        checks = run_diagnostic_checklist([])
        assert len(checks) == 6
        # No check should be green (no evidence). Platform + review are yellow;
        # scratch_org is yellow (platform unknown); skill/MCP/deviation are yellow.
        for c in checks:
            assert c["status"] in {"yellow", "red"}, f"check {c['id']}: {c}"

    def test_all_checks_present_and_labeled(self) -> None:
        checks = run_diagnostic_checklist([_pipeline_complete_entry()])
        ids = {c["id"] for c in checks}
        assert ids == {
            "platform_detected",
            "skill_invoked",
            "mcp_preferred",
            "first_deviation",
            "scratch_org",
            "review_qa_verdict",
        }
        for c in checks:
            assert c.get("label"), f"check {c['id']} missing label"
            assert c.get("evidence"), f"check {c['id']} missing evidence"


# ---------- rendering ----------


class TestRender:
    def test_empty_checks(self) -> None:
        assert render_diagnostic_checklist([]) == ""

    def test_all_green_dimmed(self) -> None:
        checks = [
            {
                "id": "platform_detected",
                "label": "Platform detected correctly?",
                "status": "green",
                "evidence": "looks fine",
                "details": {},
            }
        ]
        html_out = render_diagnostic_checklist(checks)
        assert "opacity:0.55" in html_out
        assert "looks fine" in html_out
        assert "Diagnostic Checklist" in html_out

    def test_sort_order_red_first(self) -> None:
        checks = [
            {"id": "platform_detected", "label": "A", "status": "green", "evidence": "green-ev"},
            {"id": "skill_invoked", "label": "B", "status": "yellow", "evidence": "yellow-ev"},
            {"id": "mcp_preferred", "label": "C", "status": "red", "evidence": "red-ev"},
        ]
        html_out = render_diagnostic_checklist(checks)
        red_idx = html_out.index("red-ev")
        yellow_idx = html_out.index("yellow-ev")
        green_idx = html_out.index("green-ev")
        assert red_idx < yellow_idx < green_idx

    def test_xss_in_evidence(self) -> None:
        checks = [
            {
                "id": "platform_detected",
                "label": "Platform detected correctly?",
                "status": "red",
                "evidence": "<script>alert('x')</script>",
                "details": {},
            }
        ]
        html_out = render_diagnostic_checklist(checks)
        assert "<script>alert" not in html_out
        assert "&lt;script&gt;" in html_out
