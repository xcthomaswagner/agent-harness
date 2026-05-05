"""Repo-local WORKFLOW.md scanner and generator.

The central harness profile explains the platform. A repo workflow is the
optional overlay that explains how this specific client repo wants work done:
validation commands, local hazards, docs, CI shape, and platform conventions.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

WORKFLOW_FILENAME = "WORKFLOW.md"
MAX_WORKFLOW_BYTES = 200_000


class RepoWorkflowError(ValueError):
    """Raised when a requested repo workflow operation is invalid."""


def profile_options(profile_names: Iterable[str], load: Any) -> list[dict[str, Any]]:
    """Return dashboard profile options with repo/workflow availability."""

    options: list[dict[str, Any]] = []
    for name in profile_names:
        profile = load(name)
        if profile is None:
            continue
        raw_path = str(getattr(profile, "client_repo_path", "") or "")
        repo_path = ""
        repo_exists = False
        workflow_exists = False
        if raw_path:
            candidate = Path(raw_path).expanduser()
            repo_exists = candidate.exists()
            repo_path = str(candidate)
            workflow_exists = (candidate / WORKFLOW_FILENAME).is_file()
        options.append(
            {
                "client_profile": name,
                "platform_profile": str(getattr(profile, "platform_profile", "") or ""),
                "repo_path": repo_path,
                "repo_exists": repo_exists,
                "workflow_exists": workflow_exists,
            }
        )
    return options


def generate_repo_workflow(
    repo_path: str | Path,
    *,
    client_profile: str = "",
    platform_profile: str = "",
) -> dict[str, Any]:
    """Scan a repo and return a generated WORKFLOW.md draft plus evidence."""

    repo = _resolve_git_repo(repo_path)
    existing_path = repo / WORKFLOW_FILENAME
    existing_text = _read_text(existing_path) if existing_path.is_file() else ""
    package_json = _read_package_json(repo)
    package_jsons = _find_package_jsons(repo)
    tracked = _tracked_files(repo)
    deps = _package_deps(package_json)
    scripts = _package_scripts(package_json)
    package_manager = _detect_package_manager(repo, tracked)
    ci_files = _detect_ci_files(repo)
    docs = _detect_docs(repo)
    env_examples = _detect_env_examples(repo)
    frameworks = _detect_frameworks(repo, deps, package_jsons, platform_profile)
    test_tools = _detect_test_tools(repo, deps, tracked)
    validation_commands = _validation_commands(
        scripts=scripts,
        package_manager=package_manager,
        frameworks=frameworks,
        repo=repo,
    )
    evidence = _evidence(
        repo=repo,
        package_json=package_json,
        package_jsons=package_jsons,
        scripts=scripts,
        package_manager=package_manager,
        ci_files=ci_files,
        docs=docs,
        env_examples=env_examples,
        frameworks=frameworks,
        test_tools=test_tools,
        validation_commands=validation_commands,
    )
    warnings = _warnings(
        repo=repo,
        package_json=package_json,
        scripts=scripts,
        package_manager=package_manager,
        ci_files=ci_files,
        frameworks=frameworks,
        test_tools=test_tools,
    )
    validation = _validate_existing(existing_text, validation_commands, frameworks)
    detected = {
        "repo_name": repo.name,
        "git_branch": _git_value(repo, "branch", "--show-current"),
        "git_remote": _git_value(repo, "config", "--get", "remote.origin.url"),
        "package_manager": package_manager,
        "frameworks": frameworks,
        "test_tools": test_tools,
        "ci_files": ci_files,
        "docs": docs,
        "env_examples": env_examples,
        "validation_commands": validation_commands,
        "package_json_count": len(package_jsons),
    }
    draft = _render_workflow(
        repo=repo,
        client_profile=client_profile,
        platform_profile=platform_profile,
        detected=detected,
        warnings=warnings,
    )
    return {
        "repo_path": str(repo),
        "client_profile": client_profile,
        "platform_profile": platform_profile,
        "workflow_path": str(existing_path),
        "workflow_exists": bool(existing_text),
        "existing_text": existing_text,
        "draft_text": draft,
        "detected": detected,
        "evidence": evidence,
        "warnings": warnings,
        "validation": validation,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def save_repo_workflow(repo_path: str | Path, content: str) -> dict[str, Any]:
    """Atomically write WORKFLOW.md in the resolved git repo root."""

    repo = _resolve_git_repo(repo_path)
    body = content.strip()
    if not body:
        raise RepoWorkflowError("WORKFLOW.md content cannot be empty")
    encoded = (body + "\n").encode("utf-8")
    if len(encoded) > MAX_WORKFLOW_BYTES:
        raise RepoWorkflowError(
            f"WORKFLOW.md is too large ({len(encoded)} bytes; max {MAX_WORKFLOW_BYTES})"
        )
    workflow_path = repo / WORKFLOW_FILENAME
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(workflow_path.parent),
        delete=False,
    ) as tmp:
        tmp.write(body + "\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(workflow_path)
    return {
        "saved": True,
        "repo_path": str(repo),
        "workflow_path": str(workflow_path),
        "workflow_exists": True,
        "bytes": len(encoded),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def copy_repo_workflow_overlay(worktree_dir: Path) -> dict[str, Any]:
    """Copy WORKFLOW.md into .harness for stable agent/runtime reference."""

    source = worktree_dir / WORKFLOW_FILENAME
    if not source.is_file():
        return {
            "available": False,
            "source_path": "",
            "overlay_path": "",
            "bytes": 0,
        }
    content = source.read_text(encoding="utf-8", errors="replace")
    harness_dir = worktree_dir / ".harness"
    harness_dir.mkdir(parents=True, exist_ok=True)
    overlay = harness_dir / "repo-workflow.md"
    metadata = harness_dir / "repo-workflow.json"
    overlay.write_text(content.rstrip() + "\n", encoding="utf-8")
    payload = {
        "available": True,
        "source_path": str(source),
        "overlay_path": str(overlay),
        "bytes": len(content.encode("utf-8")),
        "copied_at": datetime.now(UTC).isoformat(),
    }
    metadata.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _resolve_git_repo(path: str | Path) -> Path:
    raw = str(path).strip()
    if not raw:
        raise RepoWorkflowError("repo_path is required")
    candidate = Path(raw).expanduser()
    if not candidate.exists():
        raise RepoWorkflowError(f"repo_path does not exist: {candidate}")
    if not candidate.is_dir():
        raise RepoWorkflowError(f"repo_path must be a directory: {candidate}")
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepoWorkflowError(f"unable to inspect git repo: {exc}") from exc
    if result.returncode != 0:
        raise RepoWorkflowError(f"not a git repository: {candidate}")
    return Path(result.stdout.strip()).resolve()


def _git_value(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
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


def _tracked_files(repo: Path) -> set[str]:
    raw = _git_value(repo, "ls-files")
    return {line.strip() for line in raw.splitlines() if line.strip()}


def _read_text(path: Path, *, max_bytes: int = MAX_WORKFLOW_BYTES) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[:max_bytes].decode("utf-8", errors="replace")


def _read_package_json(repo: Path) -> dict[str, Any]:
    path = repo / "package.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _find_package_jsons(repo: Path) -> list[str]:
    paths = ["package.json"] if (repo / "package.json").is_file() else []
    for pattern in ("apps/*/package.json", "packages/*/package.json"):
        paths.extend(
            str(path.relative_to(repo))
            for path in sorted(repo.glob(pattern))
            if "node_modules" not in path.parts
        )
    return sorted(set(paths))


def _package_deps(package_json: Mapping[str, Any]) -> set[str]:
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        value = package_json.get(key)
        if isinstance(value, dict):
            deps.update(str(name) for name in value)
    return deps


def _package_scripts(package_json: Mapping[str, Any]) -> dict[str, str]:
    value = package_json.get("scripts")
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _detect_package_manager(repo: Path, tracked: set[str]) -> str:
    checks = (
        ("pnpm", "pnpm-lock.yaml"),
        ("yarn", "yarn.lock"),
        ("npm", "package-lock.json"),
        ("bun", "bun.lockb"),
    )
    for manager, filename in checks:
        if filename in tracked or (repo / filename).is_file():
            return manager
    return "npm" if (repo / "package.json").is_file() else ""


def _detect_ci_files(repo: Path) -> list[str]:
    paths: list[Path] = []
    workflow_dir = repo / ".github" / "workflows"
    if workflow_dir.is_dir():
        paths.extend(sorted(workflow_dir.glob("*.yml")))
        paths.extend(sorted(workflow_dir.glob("*.yaml")))
    for name in ("azure-pipelines.yml", "azure-pipelines.yaml", ".gitlab-ci.yml"):
        path = repo / name
        if path.is_file():
            paths.append(path)
    return [str(path.relative_to(repo)) for path in sorted(set(paths))]


def _detect_docs(repo: Path) -> list[str]:
    names = (
        "README.md",
        "CONTRIBUTING.md",
        "ARCHITECTURE.md",
        "CLAUDE.md",
        "AGENTS.md",
        "docs/architecture.md",
    )
    return [name for name in names if (repo / name).is_file()]


def _detect_env_examples(repo: Path) -> list[str]:
    names = (
        ".env.example",
        ".env.local.example",
        ".env.development.example",
        "example.env",
    )
    return [name for name in names if (repo / name).is_file()]


def _detect_frameworks(
    repo: Path,
    deps: set[str],
    package_jsons: list[str],
    platform_profile: str,
) -> list[str]:
    found: list[str] = []
    if "next" in deps or _has_any(repo, ("next.config.js", "next.config.mjs", "next.config.ts")):
        found.append("Next.js")
    if "react" in deps and "Next.js" not in found:
        found.append("React")
    if "vite" in deps or _has_any(repo, ("vite.config.ts", "vite.config.js")):
        found.append("Vite")
    if (repo / "sfdx-project.json").is_file() or (repo / "force-app").is_dir():
        found.append("Salesforce")
    if _has_any(repo, ("sitecore.json", "xmcloud.build.json")) or (repo / "rendering").is_dir():
        found.append("Sitecore")
    if platform_profile == "contentstack" or _has_any(
        repo,
        (
            "contentstack.config.*",
            "contentstack-migration",
            "content-types",
            "contentstack",
        ),
    ):
        found.append("ContentStack")
    if package_jsons and "JavaScript" not in found:
        found.append("JavaScript")
    return found


def _detect_test_tools(repo: Path, deps: set[str], tracked: set[str]) -> list[str]:
    found: list[str] = []
    if "jest" in deps or _has_any(repo, ("jest.config.*",)):
        found.append("Jest")
    if "vitest" in deps or _has_any(repo, ("vitest.config.*",)):
        found.append("Vitest")
    if "@playwright/test" in deps or _has_any(repo, ("playwright.config.*",)):
        found.append("Playwright")
    if "cypress" in deps or _has_any(repo, ("cypress.config.*",)):
        found.append("Cypress")
    if "storybook" in deps or "@storybook/react" in deps or ".storybook/main.ts" in tracked:
        found.append("Storybook")
    return found


def _validation_commands(
    *,
    scripts: Mapping[str, str],
    package_manager: str,
    frameworks: list[str],
    repo: Path,
) -> list[str]:
    commands: list[str] = []
    if package_manager and scripts:
        for script in ("typecheck", "lint", "test", "build"):
            if script in scripts:
                commands.append(_script_command(package_manager, script))
    if "Salesforce" in frameworks and (repo / "sfdx-project.json").is_file():
        commands.append("sf project deploy validate --source-dir force-app")
    return _dedupe(commands)


def _script_command(package_manager: str, script: str) -> str:
    if package_manager == "pnpm":
        return f"pnpm {script}"
    if package_manager == "yarn":
        return f"yarn {script}"
    if package_manager == "bun":
        return f"bun run {script}"
    return f"npm run {script}"


def _evidence(
    *,
    repo: Path,
    package_json: Mapping[str, Any],
    package_jsons: list[str],
    scripts: Mapping[str, str],
    package_manager: str,
    ci_files: list[str],
    docs: list[str],
    env_examples: list[str],
    frameworks: list[str],
    test_tools: list[str],
    validation_commands: list[str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def add(area: str, source: str, message: str, value: str = "") -> None:
        rows.append({"area": area, "source": source, "message": message, "value": value})

    if package_json:
        add("package", "package.json", "Detected root package.json")
    for package_file in package_jsons:
        if package_file != "package.json":
            add("package", package_file, "Detected workspace package")
    if package_manager:
        add(
            "package",
            _package_manager_source(package_manager),
            "Detected package manager",
            package_manager,
        )
    for name, command in scripts.items():
        if name in {"build", "lint", "test", "typecheck", "format", "storybook"}:
            add("scripts", "package.json", f"Detected {name} script", command)
    for ci_file in ci_files:
        add("ci", ci_file, "Detected CI workflow")
        for command in _workflow_run_commands(repo / ci_file):
            add("ci", ci_file, "Detected CI run command", command)
    for doc in docs:
        add("docs", doc, "Detected repository guidance")
    for env_file in env_examples:
        add("env", env_file, "Detected environment example")
    for framework in frameworks:
        add("stack", "repo scan", "Detected framework/platform", framework)
    for tool in test_tools:
        add("testing", "repo scan", "Detected test tool", tool)
    for command in validation_commands:
        add("validation", "generated", "Recommended validation command", command)
    return rows


def _package_manager_source(package_manager: str) -> str:
    return {
        "pnpm": "pnpm-lock.yaml",
        "yarn": "yarn.lock",
        "npm": "package-lock.json",
        "bun": "bun.lockb",
    }.get(package_manager, "package.json")


def _workflow_run_commands(path: Path) -> list[str]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    commands: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            run = value.get("run")
            if isinstance(run, str):
                for line in run.splitlines():
                    stripped = line.strip()
                    if stripped:
                        commands.append(stripped)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return _dedupe(commands)[:12]


def _warnings(
    *,
    repo: Path,
    package_json: Mapping[str, Any],
    scripts: Mapping[str, str],
    package_manager: str,
    ci_files: list[str],
    frameworks: list[str],
    test_tools: list[str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def warn(check_id: str, area: str, severity: str, message: str, recommendation: str) -> None:
        rows.append(
            {
                "id": check_id,
                "area": area,
                "severity": severity,
                "message": message,
                "recommendation": recommendation,
            }
        )

    if package_json and not package_manager:
        warn(
            "package_manager_missing",
            "dependencies",
            "warning",
            "package.json exists but no known lockfile was detected.",
            "Confirm the expected package manager before agents install dependencies.",
        )
    if package_json and "test" not in scripts and not test_tools:
        warn(
            "test_command_missing",
            "validation",
            "warning",
            "No obvious test command or test framework was detected.",
            "Add a repo-local testing expectation or ticket-specific validation rule.",
        )
    if not ci_files:
        warn(
            "ci_missing",
            "ci",
            "warning",
            "No common CI workflow file was detected.",
            "Do not treat missing checks as a pass; require local validation evidence in the PR.",
        )
    if "Next.js" in frameworks:
        lint_script = scripts.get("lint", "")
        has_eslint = _has_any(repo, (".eslintrc", ".eslintrc.*", "eslint.config.*"))
        if "next lint" in lint_script and not has_eslint:
            warn(
                "next_lint_can_prompt",
                "validation",
                "warning",
                "The lint script uses next lint without an obvious ESLint config.",
                "Prefer a non-interactive validation command or add a committed ESLint config.",
            )
        warn(
            "next_security_baseline",
            "security",
            "info",
            "Next.js dependency advisories may include pre-existing framework vulnerabilities.",
            "Separate baseline vulnerabilities from ticket-introduced dependency changes.",
        )
    if "ContentStack" in frameworks:
        warn(
            "contentstack_write_scope",
            "cms",
            "info",
            "ContentStack schema/content changes need explicit environment and branch discipline.",
            "Use the configured non-production branch/environment and document schema changes.",
        )
    return rows


def _validate_existing(
    existing_text: str,
    validation_commands: list[str],
    frameworks: list[str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not existing_text:
        return [
            {
                "id": "workflow_missing",
                "area": "workflow",
                "severity": "info",
                "message": "No WORKFLOW.md exists yet.",
                "recommendation": "Generate a draft, edit it, then save it to the repo root.",
            }
        ]
    lowered = existing_text.lower()
    for heading in (
        "repository context",
        "agent instructions",
        "validation commands",
        "pull request requirements",
    ):
        if heading not in lowered:
            rows.append(
                {
                    "id": f"missing_{heading.replace(' ', '_')}",
                    "area": "workflow",
                    "severity": "warning",
                    "message": f"Existing WORKFLOW.md does not mention '{heading}'.",
                    "recommendation": "Add a short section for a predictable repo-local contract.",
                }
            )
    for command in validation_commands:
        if command.lower() not in lowered:
            rows.append(
                {
                    "id": "validation_command_missing",
                    "area": "validation",
                    "severity": "warning",
                    "message": f"Existing WORKFLOW.md does not mention `{command}`.",
                    "recommendation": "Add the expected command or explain why it is not required.",
                }
            )
    if "ContentStack" in frameworks and "contentstack" not in lowered:
        rows.append(
            {
                "id": "contentstack_guidance_missing",
                "area": "cms",
                "severity": "warning",
                "message": "ContentStack was detected but the workflow does not mention it.",
                "recommendation": "Add CMS branch, schema, and validation expectations.",
            }
        )
    return rows


def _render_workflow(
    *,
    repo: Path,
    client_profile: str,
    platform_profile: str,
    detected: Mapping[str, Any],
    warnings: list[dict[str, str]],
) -> str:
    validation_commands = _as_list(detected.get("validation_commands"))
    frameworks = _as_list(detected.get("frameworks"))
    docs = _as_list(detected.get("docs"))
    ci_files = _as_list(detected.get("ci_files"))
    env_examples = _as_list(detected.get("env_examples"))
    test_tools = _as_list(detected.get("test_tools"))
    package_manager = str(detected.get("package_manager") or "")

    lines = [
        "# WORKFLOW.md",
        "",
        "<!-- Generated by Agentic Harness. Review and edit before relying on it. -->",
        "",
        "## Repository Context",
        "",
        f"- Repository: `{repo.name}`",
        f"- Client profile: `{client_profile or 'unspecified'}`",
        f"- Platform profile: `{platform_profile or 'unspecified'}`",
        f"- Detected stack: {_inline_list(frameworks)}",
        f"- Package manager: `{package_manager or 'not detected'}`",
        f"- Test tools: {_inline_list(test_tools)}",
        f"- CI files: {_inline_list(ci_files)}",
        f"- Repo docs to read first: {_inline_list(docs)}",
        f"- Environment examples: {_inline_list(env_examples)}",
        "",
        "## Agent Instructions",
        "",
        "- Treat this file as repo-local guidance layered under the central harness policy.",
        "- Do not weaken review, QA, security, approval, or secret-handling gates.",
        "- Keep changes scoped to ticket acceptance criteria and the existing architecture.",
        "- Prefer established components, utilities, scripts, and folder boundaries.",
        "- Record validation evidence and any skipped checks in the PR summary.",
        "",
        "## Validation Commands",
        "",
    ]
    if validation_commands:
        lines.extend(
            ["Run from the repository root unless the ticket narrows scope:", "", "```bash"]
        )
        lines.extend(validation_commands)
        lines.extend(["```"])
    else:
        lines.append(
            "No standard validation command was detected. Add commands agents should run."
        )
    lines.extend(
        [
            "",
            "## Coding Standards",
            "",
            "- Match the existing formatting, naming, component, and test style.",
            "- Do not edit generated files directly unless this repo documents that workflow.",
            "- Avoid broad refactors unless the ticket explicitly requires them.",
            "- Keep secrets out of code, logs, screenshots, generated docs, and PR descriptions.",
            "",
        ]
    )

    if "Next.js" in frameworks:
        lines.extend(
            [
                "## Next.js Rules",
                "",
                "- Use the repo's router, rendering model, styling, and data-fetching pattern.",
                "- Keep CMS/API tokens server-side; do not expose secrets to client components.",
                "- Include accessible labels, alt text, empty states, and responsive checks.",
                "- Prefer the existing design system or CSS approach.",
                "",
            ]
        )

    if "ContentStack" in frameworks:
        lines.extend(
            [
                "## ContentStack Rules",
                "",
                "- Confirm the target stack, environment, and branch before making CMS writes.",
                "- Prefer healthy ContentStack MCP; document REST fallbacks when used.",
                "- Record content type, global field, entry, environment, and branch changes.",
                "- Use non-production branches/environments unless explicitly approved.",
                "- Align Next.js rendering code with the ContentStack model and preview needs.",
                "",
            ]
        )

    if "Salesforce" in frameworks:
        lines.extend(
            [
                "## Salesforce Rules",
                "",
                "- Validate metadata changes with Salesforce CLI when credentials are available.",
                "- Keep permission, object, and Experience Cloud assumptions explicit.",
                "- Add or run targeted Apex/LWC tests for changed behavior.",
                "",
            ]
        )

    if "Sitecore" in frameworks:
        lines.extend(
            [
                "## Sitecore Rules",
                "",
                "- Keep rendering, item serialization, and content schema changes aligned.",
                "- Document any required content item or template updates in the PR.",
                "- Run the repo's Sitecore/XM Cloud validation commands when available.",
                "",
            ]
        )

    lines.extend(["## Known Hazards", ""])
    if warnings:
        for item in warnings:
            lines.append(
                f"- `{item['id']}` ({item['severity']}): {item['message']} "
                f"Recommendation: {item['recommendation']}"
            )
    else:
        lines.append("- No generator warnings were detected. Add project-specific hazards here.")

    lines.extend(
        [
            "",
            "## Pull Request Requirements",
            "",
            "- Summarize the ticket goal, implementation, changed files, and validation.",
            "- Include screenshots or preview notes for UI-visible changes.",
            "- Call out baseline failures separately from failures introduced by the ticket.",
            "- Mention follow-up manual steps, migrations, CMS publishing, or config.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _as_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _inline_list(items: list[str]) -> str:
    if not items:
        return "`none detected`"
    return ", ".join(f"`{item}`" for item in items)


def _has_any(repo: Path, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if "*" in pattern:
            if any(repo.glob(pattern)):
                return True
        elif (repo / pattern).exists():
            return True
    return False


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
