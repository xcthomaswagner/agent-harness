"""Diagnostic checklist — six auto-computed trace health checks.

Reads ONLY the precomputed ``tool_index`` artifact (see tool_index.py) and
trace summary fields. Never re-parses session-stream.jsonl.

Two-signal-green discipline: no check can return green without two
independent pieces of evidence. Single-signal evidence produces yellow.
Checks that are structurally limited to yellow-or-red (e.g., skill-
invocation verification without per-call argument data) flag the gap in
the ``evidence`` field.

Render function sorts red -> yellow -> green so the dev's eye lands on
problems first. Greens render dimmed so an all-green panel does not
produce false confidence.
"""

from __future__ import annotations

import html
import re
from typing import Any

from tracer import ARTIFACT_TOOL_INDEX

_CHECK_ORDER = [
    "platform_detected",
    "skill_invoked",
    "mcp_preferred",
    "first_deviation",
    "scratch_org",
    "review_qa_verdict",
]

_CHECK_LABELS = {
    "platform_detected": "Platform detected correctly?",
    "skill_invoked": "Expected skill(s) invoked?",
    "mcp_preferred": "MCP tools preferred over shell?",
    "first_deviation": "First deviation point",
    "scratch_org": "Scratch org / environment correct?",
    "review_qa_verdict": "Review / QA verdict",
}

# Most real SF/ADO work should flow through MCP tools. A handful of Bash
# calls is fine for orchestration (cd, ls, echo) but more suggests shell
# drift.
_BASH_SOFT_THRESHOLD = 5

_STATUS_ORDER = {"red": 0, "yellow": 1, "green": 2}


def _result(
    check_id: str,
    status: str,
    evidence: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "evidence": evidence,
        "details": details or {},
    }


def _find_tool_index(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for e in entries:
        if e.get("event") == ARTIFACT_TOOL_INDEX:
            idx = e.get("index")
            if isinstance(idx, dict):
                return idx
    return None


def _find_platform_marker(entries: list[dict[str, Any]]) -> str | None:
    # TODO: The literal ``PLATFORM: <name>`` marker string is not currently
    # written into structured trace entries anywhere in the repo. This regex
    # will only match if a harness skill/agent is updated to emit that marker
    # (out of scope for commit 3). Until then, the platform_detected check
    # will rely on the ``platform_profile`` signal alone, capping it at
    # yellow. See harness-CLAUDE.md / runtime skills for future work.
    pattern = re.compile(r"PLATFORM\s*:\s*([A-Za-z0-9_-]+)", re.IGNORECASE)
    for e in entries:
        for key in ("event", "message", "content", "text"):
            val = e.get(key)
            if isinstance(val, str):
                m = pattern.search(val)
                if m:
                    return m.group(1).strip().lower()
    return None


def _find_profile_platform(entries: list[dict[str, Any]]) -> str | None:
    for e in entries:
        val = e.get("platform_profile")
        if isinstance(val, str) and val:
            return val.strip().lower()
    return None


def _run_start_idx(entries: list[dict[str, Any]]) -> int:
    """Index of the most recent ``webhook_received`` event, or 0.

    Mirrors ``tracer._find_run_start_idx`` at a simpler level: we only need
    to slice off stale entries from prior pipeline runs for error scanning.
    Kept self-contained (no private tracer import) so diagnostic.py has a
    minimal surface area.
    """
    for i in range(len(entries) - 1, -1, -1):
        ev = entries[i].get("event", "")
        if isinstance(ev, str) and "webhook_received" in ev:
            return i
    return 0


def _pipeline_error_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    # Only scan the current run's entries — a stale error from a prior run
    # (e.g. a re-triggered trace) must not surface as a "first deviation"
    # for the latest run.
    start = _run_start_idx(entries)
    for e in entries[start:]:
        if e.get("event") == "error":
            return e
    return None


def _find_pipeline_complete_entry(
    entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the most recent ``Pipeline complete`` entry, or None.

    Walks in reverse so that if the trace has multiple runs, we see the
    latest completion. Returns None if the pipeline never completed — in
    that case review/QA verdict is unknown.
    """
    for e in reversed(entries):
        if e.get("event") == "Pipeline complete":
            return e
    return None


# ---------- individual checks ----------


def _check_platform_detected(entries: list[dict[str, Any]]) -> dict[str, Any]:
    marker = _find_platform_marker(entries)
    profile = _find_profile_platform(entries)
    details = {"marker": marker, "profile": profile}

    if marker and profile:
        if marker == profile:
            return _result(
                "platform_detected",
                "green",
                f"Agent marker 'PLATFORM: {marker}' matches client profile '{profile}'.",
                details,
            )
        return _result(
            "platform_detected",
            "red",
            f"Agent detected '{marker}' but client profile is '{profile}' — mismatch.",
            details,
        )
    if marker or profile:
        source = "agent marker" if marker else "client profile"
        value = marker or profile
        return _result(
            "platform_detected",
            "yellow",
            f"Only one signal present ({source}: '{value}'); cannot cross-verify.",
            details,
        )
    return _result(
        "platform_detected",
        "yellow",
        "No platform marker and no client profile recorded in trace.",
        details,
    )


def _check_skill_invoked(index: dict[str, Any] | None) -> dict[str, Any]:
    if index is None:
        return _result(
            "skill_invoked", "yellow", "tool_index not available for this trace."
        )
    counts = index.get("tool_counts") or {}
    skill_calls = int(counts.get("Skill", 0) or 0)
    details = {"skill_calls": skill_calls}

    # Structurally limited: tool_index stores counts only, not per-call
    # arguments, so we cannot verify WHICH skill was invoked. Any Skill
    # call is yellow; zero is red. Green requires per-call data that
    # stage-1 tool_index does not capture.
    if skill_calls > 0:
        return _result(
            "skill_invoked",
            "yellow",
            (
                f"{skill_calls} Skill tool call(s) observed; tool_index cannot"
                " identify which skill (per-call args not captured)."
            ),
            details,
        )
    return _result(
        "skill_invoked",
        "red",
        "No Skill tool calls observed in session stream.",
        details,
    )


def _check_mcp_preferred(index: dict[str, Any] | None) -> dict[str, Any]:
    if index is None:
        return _result(
            "mcp_preferred", "yellow", "tool_index not available for this trace."
        )
    counts = index.get("tool_counts") or {}
    mcp_count = sum(
        int(v or 0) for k, v in counts.items() if isinstance(k, str) and k.startswith("mcp__")
    )
    bash_count = int(counts.get("Bash", 0) or 0)
    ratio = (mcp_count / bash_count) if bash_count else None
    details = {"mcp_count": mcp_count, "bash_count": bash_count, "ratio": ratio}

    if mcp_count == 0 and bash_count > 0:
        return _result(
            "mcp_preferred",
            "red",
            f"{bash_count} Bash call(s) and zero mcp__* calls — all shell, no MCP.",
            details,
        )
    if mcp_count > 0 and bash_count <= _BASH_SOFT_THRESHOLD:
        return _result(
            "mcp_preferred",
            "green",
            (
                f"{mcp_count} mcp__* call(s) and only {bash_count} Bash call(s)"
                f" (<= {_BASH_SOFT_THRESHOLD} threshold)."
            ),
            details,
        )
    if mcp_count > 0 and bash_count > _BASH_SOFT_THRESHOLD:
        return _result(
            "mcp_preferred",
            "yellow",
            (
                f"Mixed: {mcp_count} mcp__* call(s) but {bash_count} Bash call(s)"
                f" (> {_BASH_SOFT_THRESHOLD} threshold)."
            ),
            details,
        )
    return _result(
        "mcp_preferred",
        "yellow",
        "No tool calls observed — nothing to classify.",
        details,
    )


def _check_first_deviation(
    entries: list[dict[str, Any]],
    index: dict[str, Any] | None,
) -> dict[str, Any]:
    first_tool_error = index.get("first_tool_error") if index else None
    pipeline_err = _pipeline_error_entry(entries)
    details: dict[str, Any] = {
        "first_tool_error": first_tool_error,
        "pipeline_error": (
            {
                "event": pipeline_err.get("event"),
                "error_type": pipeline_err.get("error_type"),
                "error_message": pipeline_err.get("error_message"),
            }
            if pipeline_err
            else None
        ),
    }

    if first_tool_error is None and pipeline_err is None:
        if index is None:
            return _result(
                "first_deviation",
                "yellow",
                "tool_index not available and no pipeline errors — cannot fully verify.",
                details,
            )
        return _result(
            "first_deviation",
            "green",
            "No tool errors and no pipeline error events recorded.",
            details,
        )
    if first_tool_error is not None and pipeline_err is not None:
        tool = first_tool_error.get("tool", "?")
        line = first_tool_error.get("line", "?")
        return _result(
            "first_deviation",
            "red",
            (
                f"Tool error ({tool} at stream line {line}) AND pipeline error"
                f" ({pipeline_err.get('error_type', 'error')})."
            ),
            details,
        )
    if first_tool_error is not None:
        tool = first_tool_error.get("tool", "?")
        line = first_tool_error.get("line", "?")
        msg = (first_tool_error.get("message") or "")[:120]
        return _result(
            "first_deviation",
            "yellow",
            f"First tool error: {tool} at stream line {line}. {msg}".strip(),
            details,
        )
    etype = pipeline_err.get("error_type", "error") if pipeline_err else "error"
    emsg = (pipeline_err.get("error_message") or "")[:120] if pipeline_err else ""
    return _result(
        "first_deviation",
        "yellow",
        f"Pipeline error: {etype}. {emsg}".strip(),
        details,
    )


def _check_scratch_org(
    entries: list[dict[str, Any]],
    index: dict[str, Any] | None,
) -> dict[str, Any]:
    marker = _find_platform_marker(entries)
    profile = _find_profile_platform(entries)
    platform = (marker or profile or "").lower()

    if platform and platform != "salesforce":
        return _result(
            "scratch_org",
            "green",
            f"Not applicable (non-salesforce platform: {platform}).",
            {"platform": platform},
        )
    if index is None:
        return _result(
            "scratch_org", "yellow", "tool_index not available for this trace."
        )

    counts = index.get("tool_counts") or {}
    create = int(counts.get("mcp__salesforce__sf_scratch_create", 0) or 0)
    use = int(counts.get("mcp__salesforce__sf_org_use", 0) or 0)
    details = {
        "sf_scratch_create": create,
        "sf_org_use": use,
        "platform": platform or "unknown",
    }

    if not platform:
        return _result(
            "scratch_org",
            "yellow",
            "Platform not identified; cannot confirm scratch-org requirement.",
            details,
        )
    if create > 0 and use > 0:
        return _result(
            "scratch_org",
            "green",
            (
                f"sf_scratch_create ({create}) and sf_org_use ({use}) both"
                " called (cannot verify alias prefix or success from"
                " tool_index)."
            ),
            details,
        )
    if create > 0 or use > 0:
        which = "sf_scratch_create" if create > 0 else "sf_org_use"
        return _result(
            "scratch_org",
            "yellow",
            f"Only {which} observed (create={create}, use={use}).",
            details,
        )
    return _result(
        "scratch_org",
        "red",
        "No sf_scratch_create or sf_org_use calls — scratch org never bootstrapped.",
        details,
    )


def _check_review_qa_verdict(entries: list[dict[str, Any]]) -> dict[str, Any]:
    # Read review/QA verdict directly from the latest "Pipeline complete"
    # entry. These fields live top-level on that entry (tracer.py:587-593).
    # Avoids calling build_span_tree just to read two fields from summary.
    complete = _find_pipeline_complete_entry(entries)
    if complete is None:
        return _result(
            "review_qa_verdict",
            "yellow",
            "Pipeline did not complete; no review/QA verdict recorded.",
            {"review_verdict": "", "qa_result": ""},
        )
    review = str(complete.get("review_verdict", "") or "").upper()
    qa = str(complete.get("qa_result", "") or "").upper()
    details = {"review_verdict": review, "qa_result": qa}

    fail_tokens = {"FAIL", "REJECTED"}
    if review in fail_tokens or qa in fail_tokens:
        return _result(
            "review_qa_verdict",
            "red",
            f"review={review or 'N/A'}, qa={qa or 'N/A'} — failure recorded.",
            details,
        )
    if review == "APPROVED" and qa == "PASS":
        return _result(
            "review_qa_verdict",
            "green",
            "Review APPROVED and QA PASS.",
            details,
        )
    return _result(
        "review_qa_verdict",
        "yellow",
        f"review={review or 'N/A'}, qa={qa or 'N/A'} — partial or missing verdict.",
        details,
    )


# ---------- public API ----------


def run_diagnostic_checklist(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute the six-item diagnostic checklist for a trace."""
    index = _find_tool_index(entries)
    results = [
        _check_platform_detected(entries),
        _check_skill_invoked(index),
        _check_mcp_preferred(index),
        _check_first_deviation(entries, index),
        _check_scratch_org(entries, index),
        _check_review_qa_verdict(entries),
    ]
    for check in results:
        check["label"] = _CHECK_LABELS.get(check["id"], check["id"])
    return results


def render_diagnostic_checklist(checks: list[dict[str, Any]]) -> str:
    """Render the checklist as an HTML fragment.

    Sorts red -> yellow -> green so problems appear first. Greens render
    with dimmed styling so an all-green panel does not produce false
    "all clear" confidence.
    """
    if not checks:
        return ""

    def _sort_key(c: dict[str, Any]) -> tuple[int, int]:
        status_rank = _STATUS_ORDER.get(c.get("status", "yellow"), 1)
        cid = c.get("id")
        order_rank = _CHECK_ORDER.index(cid) if cid in _CHECK_ORDER else 99
        return (status_rank, order_rank)

    sorted_checks = sorted(checks, key=_sort_key)

    colors = {"red": "#DB2626", "yellow": "#D97706", "green": "#16A34A"}
    icon = "\u25CF"

    rows: list[str] = []
    for c in sorted_checks:
        status = c.get("status", "yellow")
        color = colors.get(status, "#64748B")
        label = html.escape(str(c.get("label", c.get("id", ""))), quote=True)
        evidence = html.escape(str(c.get("evidence", "")), quote=True)
        dim = "opacity:0.55;" if status == "green" else ""
        rows.append(
            f'<div style="display:flex;align-items:flex-start;gap:10px;'
            f'padding:8px 12px;border-bottom:1px solid #F1F5F9;{dim}">'
            f'<span style="color:{color};font-size:14px;line-height:1.4">{icon}</span>'
            f'<span style="font-weight:600;color:#0F172A;min-width:240px">{label}</span>'
            f'<span style="color:#475569;flex:1">{evidence}</span>'
            f"</div>"
        )

    return (
        '<div class="diagnostic-checklist" style="border:1px solid #E2E8F0;'
        'border-radius:8px;margin-bottom:20px;background:#FFFFFF;overflow:hidden">'
        '<div style="padding:8px 12px;background:#F7F9FB;border-bottom:1px solid #E2E8F0;'
        'font-size:12px;font-weight:700;color:#0F172A;letter-spacing:0.04em;'
        'text-transform:uppercase">Diagnostic Checklist</div>'
        + "".join(rows)
        + "</div>"
    )
