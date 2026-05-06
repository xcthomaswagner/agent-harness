"""Tests for applying operator model policy to injected agent files."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SERVICES_DIR = Path(__file__).resolve().parents[1] / "services"

if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))


def _load_spawn_team_module():
    module_name = "_spawn_team_model_policy_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPTS_DIR / "spawn_team.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_apply_model_policy_rewrites_injected_agent_frontmatter(
    tmp_path: Path, monkeypatch
) -> None:
    policy_path = tmp_path / "operator_model_policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "roles": [
                    {
                        "role": "developer",
                        "label": "Developer",
                        "model": "sonnet",
                        "reasoning": "standard",
                    },
                    {
                        "role": "plan_reviewer",
                        "label": "Plan Reviewer",
                        "model": "opus",
                        "reasoning": "high",
                    },
                    {
                        "role": "qa",
                        "label": "QA",
                        "model": "opus",
                        "reasoning": "high",
                    },
                ],
            }
        )
    )
    monkeypatch.setenv("HARNESS_MODEL_POLICY", str(policy_path))

    spawn_team = _load_spawn_team_module()
    worktree = tmp_path / "worktree"
    agents = worktree / ".claude" / "agents"
    agents.mkdir(parents=True)
    developer = agents / "developer.md"
    plan_reviewer = agents / "plan-reviewer.md"
    qa = agents / "qa.md"
    developer.write_text("---\nname: developer\nmodel: opus\n---\n")
    plan_reviewer.write_text("---\nname: plan-reviewer\nmodel: sonnet\n---\n")
    qa.write_text("---\nname: qa\nmodel: sonnet\n---\n")

    spawn_team.apply_model_policy_to_agents(worktree)

    assert "model: sonnet" in developer.read_text()
    assert "model: opus" in plan_reviewer.read_text()
    assert "model: opus" in qa.read_text()


def test_claude_cli_model_args_passes_explicit_opus_and_sonnet() -> None:
    from shared.model_policy import ModelSelection, claude_cli_model_args

    assert claude_cli_model_args(
        ModelSelection("team_lead", "Team Lead", "opus", "high")
    ) == ["--model", "opus"]
    assert claude_cli_model_args("sonnet") == ["--model", "sonnet"]
    assert claude_cli_model_args("claude-opus-4-20250514") == []


def test_default_policy_uses_high_reasoning_qa_and_challenger(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_MODEL_POLICY", raising=False)

    from shared.model_policy import resolve_model

    qa = resolve_model("qa")
    challenger = resolve_model("challenger")
    plan_reviewer = resolve_model("plan_reviewer")

    assert qa.model == "opus"
    assert qa.reasoning == "high"
    assert challenger.model == "opus"
    assert challenger.reasoning == "high"
    assert plan_reviewer.model == "opus"
    assert plan_reviewer.reasoning == "high"


def test_archive_run_artifacts_uses_learning_detector_layout(tmp_path: Path) -> None:
    spawn_team = _load_spawn_team_module()
    worktree = tmp_path / "worktrees" / "ai" / "PROJ-1"
    harness = worktree / ".harness"
    logs = harness / "logs"
    plans = harness / "plans"
    logs.mkdir(parents=True)
    plans.mkdir(parents=True)
    (logs / "pipeline.jsonl").write_text('{"phase":"planning"}\n')
    (logs / "retrospective.json").write_text('{"status":"ok"}\n')
    (harness / "ticket.json").write_text('{"id":"PROJ-1"}\n')
    (plans / "plan-v1.json").write_text('{"units":[]}\n')
    (harness / "client-readiness.md").write_text("ready\n")

    archive = tmp_path / "trace-archive" / "PROJ-1"
    result = spawn_team.archive_run_artifacts(worktree, archive)

    assert result == {"logs": 2, "plans": 1, "root": 2}
    assert (archive / "logs" / "pipeline.jsonl").read_text() == '{"phase":"planning"}\n'
    assert (archive / "logs" / "retrospective.json").read_text() == '{"status":"ok"}\n'
    assert (archive / "ticket.json").read_text() == '{"id":"PROJ-1"}\n'
    assert (archive / "plans" / "plan-v1.json").read_text() == '{"units":[]}\n'
    assert (archive / "client-readiness.md").read_text() == "ready\n"
    assert not (archive / "pipeline.jsonl").exists()
