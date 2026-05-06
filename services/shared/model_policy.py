"""Operator-local model policy resolution.

The dashboard persists ``data/operator_model_policy.json``. This helper
lets L1, L2 spawn scripts, and L3 read the same single-operator policy
without introducing multi-user configuration.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = REPO_ROOT / "data" / "operator_model_policy.json"

MODEL_OPTIONS: tuple[str, ...] = (
    "default",
    "opus",
    "sonnet",
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
)
REASONING_OPTIONS: tuple[str, ...] = ("default", "low", "standard", "high")

DEFAULT_ROLES: tuple[dict[str, str], ...] = (
    {
        "role": "analyst",
        "label": "Analyst",
        "model": "claude-opus-4-20250514",
        "reasoning": "high",
    },
    {"role": "team_lead", "label": "Team Lead", "model": "opus", "reasoning": "high"},
    {"role": "planner", "label": "Planner", "model": "opus", "reasoning": "high"},
    {
        "role": "developer",
        "label": "Developer",
        "model": "opus",
        "reasoning": "high",
    },
    {
        "role": "code_reviewer",
        "label": "Code Reviewer",
        "model": "opus",
        "reasoning": "high",
    },
    {
        "role": "challenger",
        "label": "Challenger",
        "model": "opus",
        "reasoning": "high",
    },
    {"role": "judge", "label": "Judge", "model": "sonnet", "reasoning": "standard"},
    {"role": "qa", "label": "QA", "model": "opus", "reasoning": "high"},
    {
        "role": "merge_coordinator",
        "label": "Merge Coordinator",
        "model": "sonnet",
        "reasoning": "standard",
    },
    {
        "role": "run_reflector",
        "label": "Run Reflector",
        "model": "opus",
        "reasoning": "high",
    },
    {
        "role": "l3_pr_review",
        "label": "L3 PR Review",
        "model": "opus",
        "reasoning": "high",
    },
    {
        "role": "l3_ci_fix",
        "label": "L3 CI Fix",
        "model": "sonnet",
        "reasoning": "standard",
    },
    {
        "role": "l3_comment_response",
        "label": "L3 Comment Response",
        "model": "sonnet",
        "reasoning": "standard",
    },
)

_DEFAULT_BY_ROLE = {row["role"]: row for row in DEFAULT_ROLES}


@dataclass(frozen=True)
class ModelSelection:
    role: str
    label: str
    model: str
    reasoning: str

    @property
    def claude_code_model(self) -> str:
        """Return the model token accepted by Claude Code CLI."""
        if self.model in {"opus", "sonnet"}:
            return self.model
        if self.model.startswith("claude-opus"):
            return "opus"
        if self.model.startswith("claude-sonnet"):
            return "sonnet"
        default = _DEFAULT_BY_ROLE.get(self.role, {})
        fallback = default.get("model", "sonnet")
        return fallback if fallback in {"opus", "sonnet"} else "sonnet"

    @property
    def anthropic_model(self) -> str:
        """Return a full model id for Anthropic API calls."""
        if self.model == "opus":
            return "claude-opus-4-20250514"
        if self.model == "sonnet":
            return "claude-sonnet-4-20250514"
        if self.model == "default":
            default = _DEFAULT_BY_ROLE.get(self.role, {})
            fallback = default.get("model", "claude-opus-4-20250514")
            if fallback == "opus":
                return "claude-opus-4-20250514"
            if fallback == "sonnet":
                return "claude-sonnet-4-20250514"
            return fallback
        return self.model


def claude_cli_model_args(selection_or_model: ModelSelection | str) -> list[str]:
    """Return explicit Claude Code CLI model args for harness sessions."""
    model = (
        selection_or_model.claude_code_model
        if isinstance(selection_or_model, ModelSelection)
        else selection_or_model
    )
    return ["--model", model] if model in {"opus", "sonnet"} else []


def policy_path() -> Path:
    raw = os.getenv("HARNESS_MODEL_POLICY", "")
    return Path(raw).expanduser() if raw else DEFAULT_POLICY_PATH


def default_policy() -> dict[str, Any]:
    return {
        "version": 1,
        "source": "default",
        "model_options": list(MODEL_OPTIONS),
        "reasoning_options": list(REASONING_OPTIONS),
        "roles": [dict(row) for row in DEFAULT_ROLES],
    }


def read_policy() -> dict[str, Any]:
    path = policy_path()
    if not path.is_file():
        return default_policy()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default_policy()
    if not isinstance(raw, dict):
        return default_policy()

    configured = raw.get("roles")
    if not isinstance(configured, list):
        configured = []

    configured_by_role: dict[str, dict[str, str]] = {}
    for row in configured:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "")
        default = _DEFAULT_BY_ROLE.get(role)
        if default is None:
            continue
        model = str(row.get("model") or default["model"])
        reasoning = str(row.get("reasoning") or default["reasoning"])
        configured_by_role[role] = {
            "role": role,
            "label": default["label"],
            "model": model if model in MODEL_OPTIONS else default["model"],
            "reasoning": reasoning if reasoning in REASONING_OPTIONS else default["reasoning"],
        }

    roles = [
        configured_by_role.get(row["role"], dict(row))
        for row in DEFAULT_ROLES
    ]
    return {
        **default_policy(),
        "source": "local",
        "updated_at": str(raw.get("updated_at") or ""),
        "roles": roles,
    }


def resolve_model(role: str) -> ModelSelection:
    normalized = role.replace("-", "_")
    for row in read_policy()["roles"]:
        if row["role"] == normalized:
            return ModelSelection(
                role=normalized,
                label=row["label"],
                model=row["model"],
                reasoning=row["reasoning"],
            )
    default = _DEFAULT_BY_ROLE.get(normalized, _DEFAULT_BY_ROLE["developer"])
    return ModelSelection(
        role=normalized,
        label=default["label"],
        model=default["model"],
        reasoning=default["reasoning"],
    )
