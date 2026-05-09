"""Tests for operator project setup helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

import project_setup
from project_setup import (
    delete_project_setup,
    inspect_project_path,
    save_project_setup,
    setup_options,
)


def _patch_paths(tmp_path: Path, monkeypatch) -> Path:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    env_path = tmp_path / ".env"
    monkeypatch.setattr(project_setup, "PROFILES_DIR", profiles)

    import client_profile as cp_module

    monkeypatch.setattr(cp_module, "PROFILES_DIR", profiles)
    monkeypatch.setattr(project_setup, "L1_ENV_PATH", env_path)
    return profiles


def _git_repo(path: Path, remote: str = "git@github.com:acme/widgets.git") -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-B", "main"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", remote],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_inspect_project_path_detects_git_remote_and_platform(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_paths(tmp_path, monkeypatch)
    repo = tmp_path / "widgets"
    _git_repo(repo)
    (repo / "sfdx-project.json").write_text("{}", encoding="utf-8")

    result = inspect_project_path(str(repo))

    assert result["exists"] is True
    assert result["is_git_repo"] is True
    assert result["github_repo"] == "acme/widgets"
    assert result["detected_platform"] == "salesforce"
    assert result["suggested_profile_id"] == "widgets"


def test_save_project_setup_writes_profile_and_env_separately(
    tmp_path: Path, monkeypatch
) -> None:
    profiles = _patch_paths(tmp_path, monkeypatch)
    repo = tmp_path / "widgets"
    _git_repo(repo)

    result = save_project_setup(
        {
            "profile_id": "acme-widgets",
            "client_name": "Acme Widgets",
            "project_path": str(repo),
            "platform_profile": "contentstack",
            "ticket_source_type": "ado",
            "ticket_instance": "https://dev.azure.com/acme",
            "project_key": "ACME",
            "ado_project_name": "Acme Project",
            "source_control_type": "github",
            "github_repo": "acme/widgets",
            "test_command": "pnpm test",
            "build_command": "pnpm build",
            "platform_settings": {
                "stack_name": "Acme CMS",
                "frontend_framework": "Next.js App Router",
            },
            "env": {
                "CONTENTSTACK_API_KEY": "stack-key",
                "CONTENTSTACK_REGION": "NA",
            },
            "actions": {},
        }
    )

    assert result["saved"] is True
    body = yaml.safe_load((profiles / "acme-widgets.yaml").read_text())
    assert body["client"] == "Acme Widgets"
    assert body["platform_profile"] == "contentstack"
    assert body["client_repo"]["local_path"] == str(repo.resolve())
    assert body["client_repo"]["github_repo"] == "acme/widgets"
    assert body["ticket_source"]["type"] == "ado"
    assert body["platform_settings"]["frontend_framework"] == "Next.js App Router"
    assert "stack-key" not in (profiles / "acme-widgets.yaml").read_text()
    assert "CONTENTSTACK_API_KEY=stack-key" in project_setup.L1_ENV_PATH.read_text()


def test_save_project_setup_can_create_directory_and_init_git(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_paths(tmp_path, monkeypatch)
    repo = tmp_path / "new-client"

    result = save_project_setup(
        {
            "profile_id": "new-client",
            "client_name": "New Client",
            "project_path": str(repo),
            "platform_profile": "generic",
            "ticket_source_type": "jira",
            "project_key": "NEW",
            "source_control_type": "github",
            "github_repo": "acme/new-client",
            "actions": {"create_directory": True, "init_git": True},
        }
    )

    assert result["saved"] is True
    assert repo.is_dir()
    assert (repo / ".git").exists()


def test_save_project_setup_defaults_validation_commands_from_package_scripts(
    tmp_path: Path, monkeypatch
) -> None:
    profiles = _patch_paths(tmp_path, monkeypatch)
    repo = tmp_path / "next-app"
    _git_repo(repo)
    (repo / "package.json").write_text(
        """
        {
          "scripts": {
            "test": "vitest",
            "lint": "next lint",
            "build": "next build",
            "e2e": "playwright test"
          },
          "dependencies": {"next": "latest"},
          "devDependencies": {"vitest": "latest", "@playwright/test": "latest"}
        }
        """,
        encoding="utf-8",
    )

    save_project_setup(
        {
            "profile_id": "next-app",
            "client_name": "Next App",
            "project_path": str(repo),
            "platform_profile": "contentstack",
            "source_control_type": "github",
            "github_repo": "acme/next-app",
            "actions": {},
        }
    )

    body = yaml.safe_load((profiles / "next-app.yaml").read_text())
    assert body["ci_pipeline"] == {
        "test_command": "npm run test",
        "lint_command": "npm run lint",
        "build_command": "npm run build",
        "e2e_command": "npm run e2e",
    }


def test_delete_project_setup_removes_profile_and_optionally_directory(
    tmp_path: Path, monkeypatch
) -> None:
    profiles = _patch_paths(tmp_path, monkeypatch)
    repo = tmp_path / "delete-me"
    save_project_setup(
        {
            "profile_id": "delete-me",
            "client_name": "Delete Me",
            "project_path": str(repo),
            "platform_profile": "generic",
            "source_control_type": "github",
            "github_repo": "acme/delete-me",
            "actions": {"create_directory": True, "init_git": True},
        }
    )

    result = delete_project_setup("delete-me", delete_directory=True)

    assert result["deleted"] is True
    assert result["deleted_directory"] is True
    assert not (profiles / "delete-me.yaml").exists()
    assert not repo.exists()


def test_delete_project_setup_blocks_shared_directory_delete(
    tmp_path: Path, monkeypatch
) -> None:
    profiles = _patch_paths(tmp_path, monkeypatch)
    repo = tmp_path / "shared-client"
    _git_repo(repo)

    for profile_id in ("alpha", "bravo"):
        save_project_setup(
            {
                "profile_id": profile_id,
                "client_name": profile_id.title(),
                "project_path": str(repo),
                "platform_profile": "generic",
                "source_control_type": "github",
                "github_repo": f"acme/{profile_id}",
                "actions": {},
            }
        )

    with pytest.raises(project_setup.ProjectSetupError, match="still used"):
        delete_project_setup("alpha", delete_directory=True)

    assert repo.exists()
    assert (profiles / "alpha.yaml").exists()
    assert (profiles / "bravo.yaml").exists()


def test_setup_options_reports_supported_platforms_and_env_presence(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_paths(tmp_path, monkeypatch)
    project_setup.L1_ENV_PATH.write_text("CONTENTSTACK_API_KEY=present\n")

    result = setup_options()

    assert "generic" in result["platforms"]
    assert "contentstack" in result["platform_settings"]
    contentstack = {
        item["key"]: item for item in result["platform_settings"]["contentstack"]
    }
    assert contentstack["CONTENTSTACK_API_KEY"]["present"] is True
