"""Tests for client profile loader — YAML loading, defaults, error handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from client_profile import (
    ClientProfile,
    find_profile_by_ado_project,
    find_profile_by_repo,
    list_profiles,
    load_profile,
)


@pytest.fixture
def profiles_dir(tmp_path: Path) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    return d


class TestClientProfile:
    def test_basic_properties(self) -> None:
        data = {
            "client": "Acme Corp",
            "platform_profile": "sitecore",
            "ticket_source": {
                "instance": "acme.atlassian.net",
                "project_key": "ACME",
                "ai_label": "ai-build",
                "done_status": "Closed",
            },
            "client_repo": {"local_path": "/path/to/repo"},
        }
        profile = ClientProfile(data, name="acme")
        assert profile.name == "acme"
        assert profile.platform_profile == "sitecore"
        assert profile.jira_instance == "acme.atlassian.net"
        assert profile.project_key == "ACME"
        assert profile.ai_label == "ai-build"
        assert profile.done_status == "Closed"
        assert profile.client_repo_path == "/path/to/repo"

    def test_defaults(self) -> None:
        profile = ClientProfile({})
        assert profile.name == ""
        assert profile.platform_profile == ""
        assert profile.ai_label == "ai-implement"
        assert profile.quick_label == "ai-quick"
        assert profile.done_status == "Done"
        assert profile.client_repo_path == ""

    def test_name_from_data(self) -> None:
        profile = ClientProfile({"client": "From Data"})
        assert profile.name == "From Data"

    def test_name_override(self) -> None:
        profile = ClientProfile({"client": "From Data"}, name="override")
        assert profile.name == "override"

    def test_empty_sections(self) -> None:
        profile = ClientProfile({})
        assert profile.ticket_source == {}
        assert profile.source_control == {}
        assert profile.ci_pipeline == {}


class TestLoadProfile:
    def test_loads_yaml(self, profiles_dir: Path) -> None:
        (profiles_dir / "test.yaml").write_text(
            "client: Test Corp\nplatform_profile: salesforce\n"
        )
        profile = load_profile("test", profiles_dir=profiles_dir)
        assert profile is not None
        assert profile.name == "test"
        assert profile.platform_profile == "salesforce"

    def test_returns_none_for_missing(self, profiles_dir: Path) -> None:
        profile = load_profile("nonexistent", profiles_dir=profiles_dir)
        assert profile is None

    def test_handles_empty_yaml(self, profiles_dir: Path) -> None:
        (profiles_dir / "empty.yaml").write_text("")
        # yaml.safe_load returns None for empty file → returns None gracefully
        profile = load_profile("empty", profiles_dir=profiles_dir)
        assert profile is None

    def test_handles_minimal_yaml(self, profiles_dir: Path) -> None:
        (profiles_dir / "minimal.yaml").write_text("client: Minimal\n")
        profile = load_profile("minimal", profiles_dir=profiles_dir)
        assert profile is not None
        assert profile.name == "minimal"

    def test_rejects_path_traversal(
        self, tmp_path: Path, profiles_dir: Path
    ) -> None:
        """Bug regression: names with path separators, ``..``, or
        absolute paths used to reach arbitrary .yaml files on disk via
        ``directory / f"{name}.yaml"`` (pathlib happily resolves
        traversal segments). Fixed by validating the name against a
        conservative regex before any filesystem access."""
        # Plant a file one directory up from the profiles dir — an
        # attacker would have to get through the regex to see it.
        outside = tmp_path / "captured.yaml"
        outside.write_text("client: Captured\n")

        # Every variant must return None and never touch the outside file.
        for evil in (
            "../captured",
            "../../etc/passwd",
            "/etc/passwd",
            "name/with/slashes",
            "..",
            "",
            ".hidden",
            "has space",
            "name*glob",
        ):
            assert load_profile(evil, profiles_dir=profiles_dir) is None, (
                f"{evil!r} must be rejected by the name validator"
            )

    def test_accepts_conservative_names(self, profiles_dir: Path) -> None:
        """Names that match the allow regex must still work — otherwise
        the fix would break every legitimate client like ``xcsf30`` or
        ``fleet-pride``."""
        for good in ("xcsf30", "fleet-pride", "abc_123", "Sitecore", "a"):
            (profiles_dir / f"{good}.yaml").write_text(
                f"client: {good}\n"
            )
            profile = load_profile(good, profiles_dir=profiles_dir)
            assert profile is not None
            assert profile.name == good


class TestListProfiles:
    def test_lists_yaml_files(self, profiles_dir: Path) -> None:
        (profiles_dir / "alpha.yaml").write_text("client: A\n")
        (profiles_dir / "beta.yaml").write_text("client: B\n")
        (profiles_dir / "schema.yaml").write_text("# schema\n")

        result = list_profiles(profiles_dir=profiles_dir)
        assert "alpha" in result
        assert "beta" in result
        assert "schema" not in result

    def test_returns_empty_for_missing_dir(self, tmp_path: Path) -> None:
        result = list_profiles(profiles_dir=tmp_path / "nonexistent")
        assert result == []

    def test_returns_empty_for_no_yaml(self, profiles_dir: Path) -> None:
        (profiles_dir / "readme.md").write_text("not yaml")
        result = list_profiles(profiles_dir=profiles_dir)
        assert result == []


class TestFindProfileByRepo:
    def test_find_profile_by_repo_matches_exact(self, profiles_dir: Path) -> None:
        (profiles_dir / "alpha.yaml").write_text(
            "client: Alpha\nclient_repo:\n  github_repo: acme/widgets\n"
        )
        (profiles_dir / "beta.yaml").write_text(
            "client: Beta\nclient_repo:\n  github_repo: other/repo\n"
        )
        profile = find_profile_by_repo("acme/widgets", profiles_dir=profiles_dir)
        assert profile is not None
        assert profile.name == "alpha"

    def test_find_profile_by_repo_matches_case_insensitive(
        self, profiles_dir: Path
    ) -> None:
        (profiles_dir / "alpha.yaml").write_text(
            "client: Alpha\nclient_repo:\n  github_repo: Acme/Widgets\n"
        )
        profile = find_profile_by_repo("acme/widgets", profiles_dir=profiles_dir)
        assert profile is not None
        assert profile.name == "alpha"

    def test_find_profile_by_repo_matches_url(self, profiles_dir: Path) -> None:
        (profiles_dir / "alpha.yaml").write_text(
            "client: Alpha\n"
            "client_repo:\n"
            "  url: https://github.com/acme/widgets.git\n"
        )
        profile = find_profile_by_repo("acme/widgets", profiles_dir=profiles_dir)
        assert profile is not None
        assert profile.name == "alpha"

    def test_find_profile_by_repo_returns_none_when_no_match(
        self, profiles_dir: Path
    ) -> None:
        (profiles_dir / "alpha.yaml").write_text(
            "client: Alpha\nclient_repo:\n  github_repo: acme/widgets\n"
        )
        profile = find_profile_by_repo("nope/nada", profiles_dir=profiles_dir)
        assert profile is None

    def test_find_profile_by_repo_skips_schema(self, profiles_dir: Path) -> None:
        (profiles_dir / "schema.yaml").write_text(
            "client: X\nclient_repo:\n  github_repo: acme/widgets\n"
        )
        profile = find_profile_by_repo("acme/widgets", profiles_dir=profiles_dir)
        assert profile is None

    def test_find_profile_by_repo_empty_input(self, profiles_dir: Path) -> None:
        assert find_profile_by_repo("", profiles_dir=profiles_dir) is None


class TestAdoProperties:
    def test_ticket_source_type(self) -> None:
        data = {"ticket_source": {"type": "ado"}}
        profile = ClientProfile(data)
        assert profile.ticket_source_type == "ado"

    def test_ticket_source_type_defaults_empty(self) -> None:
        profile = ClientProfile({})
        assert profile.ticket_source_type == ""

    def test_ado_project_name(self) -> None:
        data = {"ticket_source": {"type": "ado", "ado_project_name": "XC-SF-30in30"}}
        profile = ClientProfile(data)
        assert profile.ado_project_name == "XC-SF-30in30"

    def test_ado_project_name_defaults_empty(self) -> None:
        profile = ClientProfile({})
        assert profile.ado_project_name == ""


class TestFindProfileByAdoProject:
    def test_matches(self, profiles_dir: Path) -> None:
        (profiles_dir / "xcsf30.yaml").write_text(
            "client: XCSF\n"
            "ticket_source:\n"
            "  type: ado\n"
            "  project_key: XCSF30\n"
            "  ado_project_name: XC-SF-30in30\n"
        )
        profile = find_profile_by_ado_project("XC-SF-30in30", profiles_dir=profiles_dir)
        assert profile is not None
        assert profile.name == "xcsf30"
        assert profile.project_key == "XCSF30"

    def test_case_insensitive(self, profiles_dir: Path) -> None:
        (profiles_dir / "xcsf30.yaml").write_text(
            "client: XCSF\n"
            "ticket_source:\n"
            "  type: ado\n"
            "  project_key: XCSF30\n"
            "  ado_project_name: XC-SF-30in30\n"
        )
        profile = find_profile_by_ado_project("xc-sf-30in30", profiles_dir=profiles_dir)
        assert profile is not None
        assert profile.name == "xcsf30"

    def test_skips_jira_profiles(self, profiles_dir: Path) -> None:
        (profiles_dir / "jira_client.yaml").write_text(
            "client: Jira Client\n"
            "ticket_source:\n"
            "  type: jira\n"
            "  project_key: JIRA\n"
            "  ado_project_name: XC-SF-30in30\n"  # Even if this field exists
        )
        profile = find_profile_by_ado_project("XC-SF-30in30", profiles_dir=profiles_dir)
        assert profile is None

    def test_returns_none_when_no_match(self, profiles_dir: Path) -> None:
        (profiles_dir / "xcsf30.yaml").write_text(
            "client: XCSF\n"
            "ticket_source:\n"
            "  type: ado\n"
            "  project_key: XCSF30\n"
            "  ado_project_name: XC-SF-30in30\n"
        )
        profile = find_profile_by_ado_project("NonexistentProject", profiles_dir=profiles_dir)
        assert profile is None

    def test_returns_none_for_empty_input(self, profiles_dir: Path) -> None:
        assert find_profile_by_ado_project("", profiles_dir=profiles_dir) is None

    def test_skips_schema_yaml(self, profiles_dir: Path) -> None:
        (profiles_dir / "schema.yaml").write_text(
            "client: Schema\n"
            "ticket_source:\n"
            "  type: ado\n"
            "  ado_project_name: XC-SF-30in30\n"
        )
        profile = find_profile_by_ado_project("XC-SF-30in30", profiles_dir=profiles_dir)
        assert profile is None


class TestAutonomyAccessors:
    def test_auto_merge_enabled_defaults_false(self) -> None:
        profile = ClientProfile({})
        assert profile.auto_merge_enabled is False

    def test_auto_merge_enabled_respects_yaml(self) -> None:
        profile = ClientProfile({"autonomy": {"auto_merge_enabled": True}})
        assert profile.auto_merge_enabled is True

    def test_auto_merge_enabled_truthy_values(self) -> None:
        profile = ClientProfile({"autonomy": {"auto_merge_enabled": "yes"}})
        assert profile.auto_merge_enabled is True

    def test_low_risk_ticket_types_defaults(self) -> None:
        profile = ClientProfile({})
        assert profile.low_risk_ticket_types == [
            "bug",
            "chore",
            "config",
            "dependency",
            "docs",
        ]

    def test_low_risk_ticket_types_custom_yaml(self) -> None:
        profile = ClientProfile(
            {"autonomy": {"low_risk_ticket_types": ["Bug", "Task"]}}
        )
        assert profile.low_risk_ticket_types == ["bug", "task"]

    def test_low_risk_ticket_types_empty_list_falls_back(self) -> None:
        profile = ClientProfile({"autonomy": {"low_risk_ticket_types": []}})
        # empty list → fall back to defaults
        assert "bug" in profile.low_risk_ticket_types
