"""Application configuration loaded from environment variables."""

import os

from pydantic import field_validator
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

    # Phase 1 fail-closed default: L1 webhooks reject unauthenticated
    # traffic unless this is explicitly set true. Set in .env for local
    # dev only.
    allow_unsigned_webhooks: bool = False

    # Internal API auth (protects /api/* endpoints)
    api_key: str = ""  # Set to require X-API-Key header on control-plane endpoints

    # Phase 1 fail-closed default: dashboard GETs require auth unless
    # explicitly opened for anonymous access. Set true in .env for
    # local dev only.
    dashboard_allow_anonymous: bool = False

    # Figma (optional — extraction skipped if empty)
    figma_api_token: str = ""

    # L2 Dispatch
    default_client_repo: str = ""  # Path to the client repo for spawn-team.sh
    default_client_profile: str = ""  # Client profile name (loads from runtime/client-profiles/)

    # Queue (optional — falls back to in-process background tasks if empty)
    redis_url: str = ""  # e.g., redis://localhost:6379/0

    # Seconds to keep a ticket claimed after /api/agent-complete fires. The
    # window absorbs self-triggered ADO webhooks from our own comment-post
    # and status-transition write-backs so they don't cascade into re-runs.
    # Edge-detection state is cleared on the same schedule.
    agent_complete_release_delay_sec: int = 60

    # Autonomy metrics
    l1_internal_api_token: str = ""
    autonomy_admin_token: str = ""  # Phase 3 — admin write endpoints (env plumbing only in Phase 1)
    autonomy_db_path: str = ""  # Empty = defaults to <repo>/data/autonomy.db
    autonomy_internal_rate_bucket_capacity: int = 20
    autonomy_internal_rate_refill_per_sec: float = 1.0
    autonomy_internal_max_body_bytes: int = 262_144  # 256 KB

    # Self-learning miner + drafter
    learning_miner_enabled: bool = False
    learning_consistency_check_enabled: bool = True
    # PR opener: OFF by default. When enabled, dry-run stops at the
    # local commit (no push, no gh pr create) so operators can exercise
    # the full flow before allowing real PRs.
    learning_pr_opener_enabled: bool = False
    learning_pr_opener_dry_run: bool = True
    learning_harness_repo_url: str = (
        "https://github.com/xcthomaswagner/agent-harness.git"
    )
    learning_harness_base_branch: str = "main"
    # Comma-separated GitHub handles (no @) — passed as --reviewer to
    # ``gh pr create`` on every lesson PR (and revert PR). Empty =
    # rely on CODEOWNERS or manual assignment. Example:
    # LEARNING_PR_OPENER_REVIEWERS=xcthomaswagner,xcentium/platform-reviewers
    learning_pr_opener_reviewers: str = ""
    # Outcomes measurement: polls applied lessons for PR-merge state,
    # then measures pre/post metrics once the window has elapsed.
    # OFF by default — flip when the PR opener starts creating real
    # PRs that merge.
    learning_outcomes_enabled: bool = False
    learning_outcomes_interval_hours: int = 24
    learning_outcomes_window_days: int = 14

    # Service
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("default_client_repo")
    @classmethod
    def _expand_user_default_client_repo(cls, v: str) -> str:
        """Expand ~ in DEFAULT_CLIENT_REPO so operators can use tilde
        paths in .env without Path() turning ``~/foo`` into ``<cwd>/~/foo``.
        Matches the expansion ClientProfile.client_repo_path does for
        profile-scoped paths.
        """
        return os.path.expanduser(v) if v else v


settings = Settings()
