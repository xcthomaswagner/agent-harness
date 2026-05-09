"""Operator project setup helpers.

This module owns the local setup flow behind the dashboard:

* inspect a directory on the operator's machine,
* detect enough repo/project facts to prefill a client profile,
* write reviewable client profile YAML, and
* write local-only runtime settings to ``services/l1_preprocessing/.env``.

Secrets deliberately do not go into ``runtime/client-profiles``. Profiles are
intended to be versioned and reviewed; tokens and local MCP paths are not.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from client_profile import PROFILES_DIR, list_profiles, load_profile
from repo_workflow import RepoWorkflowError, generate_repo_workflow

REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_PROFILES_DIR = REPO_ROOT / "runtime" / "platform-profiles"
L1_ENV_PATH = Path(__file__).resolve().parent / ".env"

_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_GITHUB_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ProjectSetupError(ValueError):
    """Raised when a setup request is invalid or cannot be completed."""


@dataclass(frozen=True)
class EnvSetting:
    key: str
    label: str
    secret: bool = False
    required: bool = False
    help: str = ""
    default: str = ""


PLATFORM_SETTINGS: dict[str, list[EnvSetting]] = {
    "contentstack": [
        EnvSetting("CONTENTSTACK_API_KEY", "Stack API key", secret=True, required=True),
        EnvSetting(
            "CONTENTSTACK_DELIVERY_TOKEN",
            "Delivery token",
            secret=True,
            required=True,
        ),
        EnvSetting(
            "CONTENTSTACK_MANAGEMENT_TOKEN",
            "Management token",
            secret=True,
            required=True,
        ),
        EnvSetting("CONTENTSTACK_REGION", "Region", required=True, default="NA"),
        EnvSetting(
            "CONTENTSTACK_ENVIRONMENT",
            "Environment",
            required=True,
            default="development",
        ),
        EnvSetting("CONTENTSTACK_BRANCH", "Branch", required=True, default="ai"),
        EnvSetting("CONTENTSTACK_MCP_GROUPS", "MCP groups", default="cma,cda"),
    ],
    "salesforce": [
        EnvSetting(
            "SALESFORCE_MCP_PATH",
            "Salesforce MCP path",
            help="Local path to the checked-out Salesforce MCP server.",
        ),
        EnvSetting(
            "SF_USE_GENERIC_UNIX_KEYCHAIN",
            "Use generic Unix keychain",
            default="true",
            help="Recommended on macOS when sf CLI auth hits keychain decrypt errors.",
        ),
    ],
    "sitecore": [],
    "generic": [],
}

PROFILE_PLATFORM_FIELDS: dict[str, list[dict[str, str]]] = {
    "salesforce": [
        {
            "key": "default_org_alias",
            "label": "Default org alias",
            "placeholder": "scratch-or-sandbox-alias",
        },
        {
            "key": "login_url",
            "label": "Login URL",
            "placeholder": "https://test.salesforce.com",
        },
    ],
    "sitecore": [
        {
            "key": "xmcloud_environment_id",
            "label": "XM Cloud environment ID",
            "placeholder": "",
        },
        {"key": "site_name", "label": "Site name", "placeholder": "website"},
    ],
    "contentstack": [
        {"key": "stack_name", "label": "Stack name", "placeholder": ""},
        {
            "key": "frontend_framework",
            "label": "Frontend framework",
            "placeholder": "Next.js App Router",
        },
    ],
    "generic": [],
}


def setup_options() -> dict[str, Any]:
    """Return selectable setup options and current profile/env status."""

    platforms = ["generic"] + [
        p.name for p in sorted(PLATFORM_PROFILES_DIR.iterdir()) if p.is_dir()
    ]
    profiles = []
    for name in list_profiles():
        profile = load_profile(name)
        if profile is None:
            continue
        profiles.append(
            {
                "id": name,
                "client": profile.name,
                "platform_profile": profile.platform_profile or "generic",
                "repo_path": profile.client_repo_path,
                "repo_exists": bool(
                    profile.client_repo_path
                    and Path(profile.client_repo_path).expanduser().is_dir()
                ),
                "ticket_source_type": profile.ticket_source_type or "jira",
                "source_control_type": profile.source_control_type or "github",
            }
        )

    env = _read_env_file(L1_ENV_PATH)
    platform_settings = {
        platform: [
            {
                "key": setting.key,
                "label": setting.label,
                "secret": setting.secret,
                "required": setting.required,
                "help": setting.help,
                "default": setting.default,
                "present": bool(env.get(setting.key) or os.environ.get(setting.key)),
            }
            for setting in settings
        ]
        for platform, settings in PLATFORM_SETTINGS.items()
    }

    return {
        "platforms": platforms,
        "profiles": profiles,
        "ticket_sources": ["jira", "ado"],
        "source_controls": ["github", "azure-repos"],
        "platform_settings": platform_settings,
        "profile_platform_fields": PROFILE_PLATFORM_FIELDS,
    }


def inspect_project_path(path: str) -> dict[str, Any]:
    """Inspect a local directory and return facts useful for setup."""

    raw = path.strip()
    if not raw:
        raise ProjectSetupError("project_path is required")
    candidate = Path(raw).expanduser()
    exists = candidate.exists()
    is_dir = candidate.is_dir() if exists else False
    result: dict[str, Any] = {
        "input_path": raw,
        "path": str(candidate),
        "exists": exists,
        "is_dir": is_dir,
        "git_root": "",
        "is_git_repo": False,
        "git_branch": "",
        "git_remote": "",
        "github_repo": "",
        "suggested_profile_id": _slug(candidate.name or "client-project"),
        "suggested_client_name": _title_from_slug(candidate.name or "Client Project"),
        "detected_platform": "",
        "detected": {},
        "matching_profiles": [],
        "notes": [],
    }
    if not exists:
        result["notes"].append(
            {
                "severity": "warning",
                "message": "Directory does not exist yet.",
                "recommendation": "Create the directory during save or choose an existing clone.",
            }
        )
        return result
    if not is_dir:
        raise ProjectSetupError("project_path must be a directory")

    git_root = _git_value(candidate, "rev-parse", "--show-toplevel")
    if git_root:
        repo = Path(git_root).resolve()
        result["git_root"] = str(repo)
        result["path"] = str(repo)
        result["is_git_repo"] = True
        result["git_branch"] = _git_value(repo, "branch", "--show-current")
        remote = _git_value(repo, "config", "--get", "remote.origin.url")
        result["git_remote"] = remote
        result["github_repo"] = _github_repo_from_remote(remote)
        result["suggested_profile_id"] = _slug(repo.name)
        result["suggested_client_name"] = _title_from_slug(repo.name)
        try:
            workflow = generate_repo_workflow(repo)
            result["detected"] = workflow.get("detected", {})
            frameworks = {
                str(item).strip().lower()
                for item in result["detected"].get("frameworks", [])
            }
            result["detected_platform"] = _detect_platform(repo, frameworks)
        except RepoWorkflowError as exc:
            result["notes"].append(
                {
                    "severity": "warning",
                    "message": f"Repo scan failed: {exc}",
                    "recommendation": (
                        "Save is still possible, but validation commands may need "
                        "manual entry."
                    ),
                }
            )
    else:
        result["notes"].append(
            {
                "severity": "warning",
                "message": "Directory is not a git repository.",
                "recommendation": (
                    "Enable Git initialization during save before running the harness."
                ),
            }
        )
        result["detected_platform"] = _detect_platform(candidate, set())

    result["matching_profiles"] = _matching_profiles(Path(result["path"]))
    return result


def save_project_setup(payload: dict[str, Any]) -> dict[str, Any]:
    """Create/update a client profile and optional local setup artifacts."""

    profile_id = str(payload.get("profile_id") or "").strip()
    if not _PROFILE_ID_RE.match(profile_id):
        raise ProjectSetupError("profile_id must use letters, numbers, dash, or underscore")

    project_path = str(payload.get("project_path") or "").strip()
    if not project_path:
        raise ProjectSetupError("project_path is required")
    repo = Path(project_path).expanduser()

    actions = payload.get("actions") if isinstance(payload.get("actions"), dict) else {}
    if actions.get("create_directory"):
        repo.mkdir(parents=True, exist_ok=True)
    if not repo.exists() or not repo.is_dir():
        raise ProjectSetupError("project_path must exist or create_directory must be enabled")

    if actions.get("init_git") and not _git_value(repo, "rev-parse", "--show-toplevel"):
        _run(["git", "init"], cwd=repo)
        branch = str(payload.get("default_branch") or "main").strip() or "main"
        _run(["git", "checkout", "-B", branch], cwd=repo, check=False)

    git_root = _git_value(repo, "rev-parse", "--show-toplevel")
    if git_root:
        repo = Path(git_root).resolve()

    source_control_type = str(payload.get("source_control_type") or "github").strip()
    source_control = _source_control(payload, repo, source_control_type)
    created_remote = False
    if actions.get("create_github_repo"):
        created_remote = _ensure_github_repo(source_control, repo)

    profile_data = _profile_data(payload, repo, source_control)
    profile_path = _write_profile(profile_id, profile_data)

    env_updates = payload.get("env") if isinstance(payload.get("env"), dict) else {}
    env_written = _write_env_updates(env_updates)

    return {
        "saved": True,
        "profile_id": profile_id,
        "profile_path": str(profile_path),
        "project_path": str(repo),
        "platform_profile": profile_data.get("platform_profile") or "generic",
        "source_control_type": source_control_type,
        "github_repo_created": created_remote,
        "env_written": env_written,
        "readiness": _readiness(profile_data, repo),
    }


def delete_project_setup(profile_id: str, *, delete_directory: bool = False) -> dict[str, Any]:
    """Delete a configured client profile and optionally its local project directory."""

    clean_id = profile_id.strip()
    if not _PROFILE_ID_RE.match(clean_id):
        raise ProjectSetupError("profile_id must use letters, numbers, dash, or underscore")

    profile = load_profile(clean_id)
    if profile is None:
        raise ProjectSetupError(f"profile not found: {clean_id}")

    repo_path = str(profile.client_repo_path or "").strip()
    profile_path = PROFILES_DIR / f"{clean_id}.yaml"
    if not profile_path.is_file():
        raise ProjectSetupError(f"profile file not found: {clean_id}")

    deleted_directory = False
    if delete_directory:
        if not repo_path:
            raise ProjectSetupError("profile has no local project directory to delete")
        shared = _profiles_sharing_repo_path(clean_id, repo_path)
        if shared:
            names = ", ".join(shared)
            raise ProjectSetupError(
                "project directory is still used by other profile(s): "
                f"{names}. Delete those profiles first or leave the directory in place."
            )
        deleted_directory = _delete_project_directory(Path(repo_path).expanduser())

    profile_path.unlink()
    return {
        "deleted": True,
        "profile_id": clean_id,
        "profile_path": str(profile_path),
        "project_path": repo_path,
        "deleted_directory": deleted_directory,
    }


def _profiles_sharing_repo_path(profile_id: str, repo_path: str) -> list[str]:
    target = Path(repo_path).expanduser().resolve()
    shared: list[str] = []
    for name in list_profiles():
        if name == profile_id:
            continue
        profile = load_profile(name)
        if profile is None or not profile.client_repo_path:
            continue
        candidate = Path(profile.client_repo_path).expanduser().resolve()
        if candidate == target:
            shared.append(name)
    return shared


def _profile_data(
    payload: dict[str, Any],
    repo: Path,
    source_control: dict[str, Any],
) -> dict[str, Any]:
    platform = str(payload.get("platform_profile") or "generic").strip()
    if platform == "generic":
        platform = ""
    ticket_source_type = str(payload.get("ticket_source_type") or "jira").strip()
    platform_settings = payload.get("platform_settings")
    if not isinstance(platform_settings, dict):
        platform_settings = {}
    validation = _default_ci_commands(payload, repo, platform)

    data: dict[str, Any] = {
        "client": str(payload.get("client_name") or payload.get("profile_id") or "").strip(),
        "platform_profile": platform,
        "ticket_source": {
            "type": ticket_source_type,
            "instance": str(payload.get("ticket_instance") or "").strip(),
            "project_key": str(payload.get("project_key") or "").strip(),
            "ado_project_name": str(payload.get("ado_project_name") or "").strip(),
            "ai_label": str(payload.get("ai_label") or "ai-implement").strip(),
            "quick_label": str(payload.get("quick_label") or "ai-quick").strip(),
            "clarification_status": str(
                payload.get("clarification_status") or "Needs Info"
            ).strip(),
            "in_progress_status": str(
                payload.get("in_progress_status")
                or ("Active" if ticket_source_type == "ado" else "In Progress")
            ).strip(),
            "done_status": str(payload.get("done_status") or "Done").strip(),
            "custom_fields": {},
        },
        "source_control": source_control,
        "ci_pipeline": {
            "test_command": validation["test_command"],
            "lint_command": validation["lint_command"],
            "build_command": validation["build_command"],
            "e2e_command": validation["e2e_command"],
        },
        "test_framework": {
            "unit": str(payload.get("unit_test_framework") or "").strip(),
            "integration": str(payload.get("integration_test_framework") or "").strip(),
            "e2e": str(payload.get("e2e_test_framework") or "").strip(),
        },
        "client_repo": {
            "local_path": str(repo),
            "github_repo": str(source_control.get("github_repo") or "").strip(),
            "url": str(payload.get("repo_url") or "").strip(),
        },
        "autonomy": {
            "auto_merge_enabled": bool(payload.get("auto_merge_enabled", False)),
            "low_risk_ticket_types": _csv_list(
                payload.get("low_risk_ticket_types"),
                default=["bug", "chore", "config", "dependency", "docs"],
            ),
        },
    }
    clean_platform_settings = {
        str(k): str(v).strip()
        for k, v in platform_settings.items()
        if str(k).strip() and str(v).strip()
    }
    if clean_platform_settings:
        data["platform_settings"] = clean_platform_settings
    return data


def _default_ci_commands(
    payload: dict[str, Any],
    repo: Path,
    platform: str,
) -> dict[str, str]:
    """Return validation commands, preferring explicit input then repo/tool defaults."""

    commands = {
        "test_command": str(payload.get("test_command") or "").strip(),
        "lint_command": str(payload.get("lint_command") or "").strip(),
        "build_command": str(payload.get("build_command") or "").strip(),
        "e2e_command": str(payload.get("e2e_command") or "").strip(),
    }
    if all(commands.values()):
        return commands

    detected = _detected_validation_defaults(repo, platform)
    for key, value in detected.items():
        if not commands[key]:
            commands[key] = value

    if platform == "salesforce":
        commands.setdefault("test_command", "")
        if not commands["test_command"]:
            commands["test_command"] = "sf apex run test --result-format human --code-coverage"
        if not commands["build_command"]:
            commands["build_command"] = "sf project deploy validate --source-dir force-app"
    return commands


def _detected_validation_defaults(repo: Path, platform: str) -> dict[str, str]:
    defaults = {
        "test_command": "",
        "lint_command": "",
        "build_command": "",
        "e2e_command": "",
    }
    if not repo.exists() or not repo.is_dir():
        return defaults

    try:
        workflow = generate_repo_workflow(repo, platform_profile=platform)
    except RepoWorkflowError:
        workflow = {}

    detected = workflow.get("detected") if isinstance(workflow, dict) else {}
    validation_commands = (
        detected.get("validation_commands", []) if isinstance(detected, dict) else []
    )
    for raw in validation_commands if isinstance(validation_commands, list) else []:
        command = str(raw).strip()
        lower = command.lower()
        if "lint" in lower and not defaults["lint_command"]:
            defaults["lint_command"] = command
        elif "build" in lower and not defaults["build_command"]:
            defaults["build_command"] = command
        elif "test" in lower and "e2e" not in lower and not defaults["test_command"]:
            defaults["test_command"] = command

    package_json = _read_json(repo / "package.json")
    scripts = package_json.get("scripts") if isinstance(package_json.get("scripts"), dict) else {}
    package_manager = (
        str(detected.get("package_manager") or "") if isinstance(detected, dict) else ""
    ) or _detect_package_manager_from_files(repo)
    if scripts and package_manager:
        script_map = {
            "test_command": ("test",),
            "lint_command": ("lint",),
            "build_command": ("build",),
            "e2e_command": ("e2e", "test:e2e"),
        }
        for key, names in script_map.items():
            if defaults[key]:
                continue
            for name in names:
                if name in scripts:
                    defaults[key] = _script_command(package_manager, name)
                    break
    return defaults


def _source_control(payload: dict[str, Any], repo: Path, kind: str) -> dict[str, Any]:
    default_branch = str(payload.get("default_branch") or "main").strip() or "main"
    branch_prefix = str(payload.get("branch_prefix") or "ai/").strip() or "ai/"
    reviewers = _csv_list(payload.get("pr_reviewers"), default=[])
    github_repo = str(payload.get("github_repo") or "").strip()
    if not github_repo:
        github_repo = _github_repo_from_remote(
            _git_value(repo, "config", "--get", "remote.origin.url")
        )

    if kind == "azure-repos":
        return {
            "type": "azure-repos",
            "org": str(payload.get("ado_org") or payload.get("source_org") or "").strip(),
            "repo": str(payload.get("repo_name") or repo.name).strip(),
            "default_branch": default_branch,
            "branch_prefix": branch_prefix,
            "pr_reviewers": reviewers,
            "ado_project": str(payload.get("ado_project") or "").strip(),
            "ado_repository_id": str(payload.get("ado_repository_id") or "").strip(),
        }

    owner, name = _split_github_repo(github_repo)
    return {
        "type": "github",
        "org": owner or str(payload.get("source_org") or "").strip(),
        "repo": name or str(payload.get("repo_name") or repo.name).strip(),
        "default_branch": default_branch,
        "branch_prefix": branch_prefix,
        "pr_reviewers": reviewers,
        "github_repo": github_repo,
    }


def _readiness(profile_data: dict[str, Any], repo: Path) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []
    if not _git_value(repo, "rev-parse", "--show-toplevel"):
        notes.append(
            {
                "severity": "error",
                "message": "Project directory is not a git repository.",
                "recommendation": (
                    "Initialize git or point at an existing clone before running tickets."
                ),
            }
        )
    source_control = profile_data.get("source_control", {})
    if isinstance(source_control, dict):
        if source_control.get("type") == "github" and not source_control.get("github_repo"):
            notes.append(
                {
                    "severity": "warning",
                    "message": "GitHub repo is not configured.",
                    "recommendation": "Set owner/repo or create the repo before PR delivery.",
                }
            )
        if source_control.get("type") == "azure-repos":
            for key in ("org", "ado_project", "ado_repository_id", "repo"):
                if not source_control.get(key):
                    notes.append(
                        {
                            "severity": "warning",
                            "message": f"Azure Repos setting missing: {key}.",
                            "recommendation": (
                                "Fill all Azure Repos fields before running "
                                "ADO-backed tickets."
                            ),
                        }
                    )
    platform = str(profile_data.get("platform_profile") or "generic")
    env = _read_env_file(L1_ENV_PATH)
    for setting in PLATFORM_SETTINGS.get(platform, []):
        if setting.required and not env.get(setting.key) and not os.environ.get(setting.key):
            notes.append(
                {
                    "severity": "warning",
                    "message": f"Runtime setting missing: {setting.key}.",
                    "recommendation": (
                        "Save the local setting or export it before running this "
                        "project type."
                    ),
                }
            )
    if not notes:
        notes.append(
            {
                "severity": "ok",
                "message": "Profile is ready for harness runs from this local directory.",
                "recommendation": (
                    "Add the trigger label on a matching ticket to start the pipeline."
                ),
            }
        )
    return notes


def _write_profile(profile_id: str, data: dict[str, Any]) -> Path:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    path = PROFILES_DIR / f"{profile_id}.yaml"
    body = "# Generated by Operator Project Setup. Review before committing.\n"
    body += yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    _atomic_write(path, body)
    return path


def _write_env_updates(updates: dict[str, Any]) -> list[str]:
    allowed = {
        setting.key
        for settings in PLATFORM_SETTINGS.values()
        for setting in settings
    } | {
        "GITHUB_TOKEN",
        "AGENT_GH_TOKEN",
        "ADO_PAT",
        "ADO_ORG_URL",
        "JIRA_BASE_URL",
        "JIRA_USER_EMAIL",
        "JIRA_API_TOKEN",
    }
    clean: dict[str, str] = {}
    for key, value in updates.items():
        key_s = str(key).strip().upper()
        value_s = str(value).strip()
        if not value_s:
            continue
        if key_s not in allowed or not _ENV_KEY_RE.match(key_s):
            raise ProjectSetupError(f"Unsupported local setting: {key_s}")
        clean[key_s] = value_s
    if not clean:
        return []
    _merge_env_file(L1_ENV_PATH, clean)
    return sorted(clean)


def _merge_env_file(path: Path, updates: dict[str, str]) -> None:
    existing = (
        path.read_text(encoding="utf-8", errors="replace").splitlines()
        if path.is_file()
        else []
    )
    remaining = dict(updates)
    output: list[str] = []
    for raw in existing:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw:
            output.append(raw)
            continue
        key = raw.split("=", 1)[0].strip()
        if key in remaining:
            output.append(f"{key}={_quote_env(remaining.pop(key))}")
        else:
            output.append(raw)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.extend(f"{key}={_quote_env(value)}" for key, value in sorted(remaining.items()))
    _atomic_write(path, "\n".join(output).rstrip() + "\n")


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if _ENV_KEY_RE.match(key):
            env[key] = value
    return env


def _quote_env(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ensure_github_repo(source_control: dict[str, Any], repo: Path) -> bool:
    full = str(source_control.get("github_repo") or "").strip()
    if not _GITHUB_FULL_NAME_RE.match(full):
        raise ProjectSetupError("github_repo must be owner/repo before creation")
    if shutil.which("gh") is None:
        raise ProjectSetupError("GitHub CLI is required to create a missing repo")
    view = _run(["gh", "repo", "view", full], cwd=repo, check=False)
    if view.returncode == 0:
        return False
    create = _run(
        ["gh", "repo", "create", full, "--private", "--source", str(repo), "--remote", "origin"],
        cwd=repo,
        check=False,
    )
    if create.returncode != 0:
        detail = (create.stderr or create.stdout or "").strip()
        raise ProjectSetupError(f"GitHub repo creation failed: {detail}")
    return True


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ProjectSetupError(f"{cmd[0]} failed: {detail}")
    return result


def _delete_project_directory(path: Path) -> bool:
    resolved = path.resolve()
    home = Path.home().resolve()
    temp_roots = {
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp").resolve(),
        Path("/private/tmp").resolve(),
    }
    blocked = {
        Path("/").resolve(),
        home,
        REPO_ROOT.resolve(),
        PROFILES_DIR.resolve(),
        *temp_roots,
    }
    if resolved in blocked:
        raise ProjectSetupError(f"refusing to delete protected directory: {resolved}")
    try:
        resolved.relative_to(home)
        under_home = True
    except ValueError:
        under_home = False
    under_tmp = any(str(resolved).startswith(str(root) + os.sep) for root in temp_roots)
    if not under_home and not under_tmp:
        raise ProjectSetupError(
            "project directory deletion is limited to the operator home directory or /tmp"
        )
    if not resolved.exists():
        return False
    if not resolved.is_dir():
        raise ProjectSetupError("project path is not a directory")
    shutil.rmtree(resolved)
    return True


def _git_value(path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _detect_package_manager_from_files(repo: Path) -> str:
    checks = (
        ("pnpm", "pnpm-lock.yaml"),
        ("yarn", "yarn.lock"),
        ("npm", "package-lock.json"),
        ("bun", "bun.lockb"),
    )
    for manager, filename in checks:
        if (repo / filename).is_file():
            return manager
    return "npm" if (repo / "package.json").is_file() else ""


def _script_command(package_manager: str, script: str) -> str:
    if package_manager == "pnpm":
        return f"pnpm {script}"
    if package_manager == "yarn":
        return f"yarn {script}"
    if package_manager == "bun":
        return f"bun run {script}"
    return f"npm run {script}"


def _github_repo_from_remote(remote: str) -> str:
    if not remote:
        return ""
    clean = remote.strip().removesuffix(".git")
    if clean.startswith("git@github.com:"):
        return clean.split(":", 1)[1]
    match = re.search(r"github\.com[:/]([^/]+/[^/]+)$", clean)
    return match.group(1) if match else ""


def _split_github_repo(value: str) -> tuple[str, str]:
    if "/" not in value:
        return "", ""
    owner, repo = value.split("/", 1)
    return owner.strip(), repo.strip()


def _detect_platform(repo: Path, frameworks: set[str]) -> str:
    if (repo / "sfdx-project.json").is_file() or (repo / "force-app").is_dir():
        return "salesforce"
    if (repo / "sitecore.json").is_file() or any("sitecore" in item for item in frameworks):
        return "sitecore"
    if any("contentstack" in item for item in frameworks):
        return "contentstack"
    return "generic"


def _matching_profiles(repo: Path) -> list[str]:
    out: list[str] = []
    target = str(repo.resolve())
    for name in list_profiles():
        profile = load_profile(name)
        if profile and str(Path(profile.client_repo_path).expanduser()) == target:
            out.append(name)
    return out


def _csv_list(value: Any, *, default: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value or "").strip()
    if not raw:
        return default
    return [part.strip() for part in raw.split(",") if part.strip()]


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip().lower()).strip("-_")
    return slug or "client-project"


def _title_from_slug(value: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_ ]+", value) if part) or value


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)
