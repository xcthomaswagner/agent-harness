"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """L1 Pre-Processing Service configuration.

    All values can be set via environment variables or .env file.
    """

    # Anthropic
    anthropic_api_key: str = ""

    # Jira
    jira_base_url: str = ""
    jira_api_token: str = ""
    jira_user_email: str = ""
    jira_ac_field_id: str = "customfield_10429"
    jira_story_points_field_id: str = "customfield_10040"

    # ADO (Phase 2)
    ado_org_url: str = ""
    ado_pat: str = ""

    # GitHub
    github_token: str = ""

    # Webhook
    webhook_secret: str = ""

    # Service
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
