"""Tests for config — settings loading, defaults."""

from __future__ import annotations

from unittest.mock import patch

from config import Settings


class TestSettings:
    def test_default_field_values(self) -> None:
        """Field defaults are correct when no env vars or file."""
        # Clear env to test pure defaults
        with patch.dict("os.environ", {}, clear=True):
            s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.jira_ac_field_id == "customfield_10429"
        assert s.jira_story_points_field_id == "customfield_10040"
        assert s.log_level == "INFO"

    def test_jira_ac_field_customizable(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            s = Settings(
                jira_ac_field_id="customfield_99999",
                _env_file=None,  # type: ignore[call-arg]
            )
        assert s.jira_ac_field_id == "customfield_99999"

    def test_env_override(self) -> None:
        """Env vars should override defaults."""
        with patch.dict("os.environ", {
            "ANTHROPIC_API_KEY": "test-key-123",
            "LOG_LEVEL": "DEBUG",
        }, clear=True):
            s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.anthropic_api_key == "test-key-123"
        assert s.log_level == "DEBUG"

    def test_all_secret_fields_in_sanitize_list(self) -> None:
        """Every Settings field with 'key', 'token', 'secret', or 'pat' in
        its name should be stripped from agent environments."""
        import sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
        from shared.env_sanitize import SECRET_VARS

        secret_keywords = {"key", "token", "secret", "pat"}
        settings_fields = Settings.model_fields
        missing = []
        for field_name in settings_fields:
            env_name = field_name.upper()
            # Match on word boundaries so e.g. "path" doesn't match "pat"
            parts = set(field_name.lower().split("_"))
            has_secret_keyword = bool(parts & secret_keywords)
            if has_secret_keyword and env_name not in SECRET_VARS:
                missing.append(env_name)
        assert not missing, f"Secret fields missing from SECRET_VARS: {missing}"
