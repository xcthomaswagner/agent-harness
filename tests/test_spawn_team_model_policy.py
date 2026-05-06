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
    qa = agents / "qa.md"
    developer.write_text("---\nname: developer\nmodel: opus\n---\n")
    qa.write_text("---\nname: qa\nmodel: sonnet\n---\n")

    spawn_team.apply_model_policy_to_agents(worktree)

    assert "model: sonnet" in developer.read_text()
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

    assert qa.model == "opus"
    assert qa.reasoning == "high"
    assert challenger.model == "opus"
    assert challenger.reasoning == "high"
