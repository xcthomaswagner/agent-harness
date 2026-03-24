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
