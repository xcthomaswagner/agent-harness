"""Static checks for L2 runtime prompt contracts.

These tests make prompt-only orchestration changes visible to CI. The L2
pipeline is executed by Claude Code, so regressions here are usually missing
runtime instructions rather than Python behavior.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"


def _read(path: str) -> str:
    return (RUNTIME / path).read_text(encoding="utf-8")


def test_risk_challenge_runtime_contract_exists() -> None:
    harness = _read("harness-CLAUDE.md")
    challenger = _read("agents/challenger.md")
    skill = _read("skills/risk-challenge/SKILL.md")

    assert "### Step 2b: Risk Challenge Gate" in harness
    assert "risk_challenge" in harness
    assert ".harness/logs/risk-challenge.json" in harness
    assert ".harness/logs/plan-decision.json" in harness
    assert "estimated_units >= 3" in harness
    assert "Salesforce/ContentStack metadata" in harness

    assert "name: challenger" in challenger
    assert "model: opus" in challenger
    assert "requires_plan_revision" in challenger

    assert "High-Risk Triggers" in skill
    assert "SAP, Oracle, ERP" in skill
    assert "risk-challenge.json" in skill


def test_structured_handoff_contract_is_mandatory() -> None:
    harness = _read("harness-CLAUDE.md")
    handoff = _read("skills/structured-handoff/SKILL.md")
    messaging = _read("lib/messaging/MESSAGE_PROTOCOL.md")

    assert "Structured Shared State Contract" in harness
    assert "Chat summaries are not authoritative" in harness
    assert ".harness/logs/implementation-result-<unit_id>.json" in harness
    assert ".harness/logs/plan-review.json" in harness
    assert ".harness/logs/merge-report.json" in harness

    assert "Chat summaries are not authoritative" in handoff
    assert "implementation-result-<unit_id>.json" in handoff
    assert "risk-challenge.json" in handoff

    assert "risk_challenge_result" in messaging
    assert "judge_result" in messaging
    assert "Canonical files are authoritative" in messaging


def test_qa_agent_default_is_strong_model() -> None:
    qa = _read("agents/qa.md")

    assert "name: qa" in qa
    assert "model: opus" in qa
