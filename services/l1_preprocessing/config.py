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

    # Jira bug webhook
    jira_implemented_ticket_field_id: str = ""  # e.g. "customfield_10050"
    jira_bug_link_types: str = "is caused by,relates to,is blocked by"
    jira_qa_confirmed_field_id: str = ""
    # Fallback bearer auth for Jira Automation (can't compute HMAC)
    jira_bug_webhook_token: str = ""

    # ADO
    ado_org_url: str = ""
    ado_pat: str = ""
    ado_webhook_token: str = ""  # Shared secret for ADO webhook auth (X-ADO-Webhook-Token header)

    # GitHub
    github_token: str = ""
    agent_gh_token: str = ""  # Dedicated agent GitHub PAT (injected as GH_TOKEN in agent sessions)

    # Webhook
    webhook_secret: str = ""

    # Internal API auth (protects /api/* endpoints)
    api_key: str = ""  # Set to require X-API-Key header on control-plane endpoints

    # Figma (optional — extraction skipped if empty)
    figma_api_token: str = ""

    # L2 Dispatch
    default_client_repo: str = ""  # Path to the client repo for spawn-team.sh
    default_client_profile: str = ""  # Client profile name (loads from runtime/client-profiles/)

    # Queue (optional — falls back to in-process background tasks if empty)
    redis_url: str = ""  # e.g., redis://localhost:6379/0

    # Autonomy metrics
    l1_internal_api_token: str = ""
    autonomy_admin_token: str = ""  # Phase 3 — admin write endpoints (env plumbing only in Phase 1)
    autonomy_db_path: str = ""  # Empty = defaults to <repo>/data/autonomy.db
    autonomy_internal_rate_bucket_capacity: int = 20
    autonomy_internal_rate_refill_per_sec: float = 1.0
    autonomy_internal_max_body_bytes: int = 262_144  # 256 KB

    # Service
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
