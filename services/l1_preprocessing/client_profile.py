"""Client Profile loader — reads per-client YAML configuration."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger()

PROFILES_DIR = Path(__file__).resolve().parents[2] / "runtime" / "client-profiles"

# Profile names are filesystem-backed (``<name>.yaml``) and show up in HTTP
# query parameters (``/api/autonomy/auto-merge-toggle?client_profile=...``),
# so restrict them to a conservative charset that cannot escape the profiles
# directory via traversal (``..``), absolute paths, or glob characters.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ClientProfile:
    """A loaded client profile with typed accessors."""

    def __init__(self, data: dict[str, Any], name: str = "") -> None:
        self._data = data
        self.name = name or str(data.get("client", ""))
        self.platform_profile = str(data.get("platform_profile", ""))

    @property
    def ticket_source(self) -> dict[str, Any]:
        val = self._data.get("ticket_source", {})
        return val if isinstance(val, dict) else {}

    @property
    def source_control(self) -> dict[str, Any]:
        val = self._data.get("source_control", {})
        return val if isinstance(val, dict) else {}

    @property
    def ci_pipeline(self) -> dict[str, Any]:
        val = self._data.get("ci_pipeline", {})
        return val if isinstance(val, dict) else {}

    @property
    def client_repo_path(self) -> str:
        val = self._data.get("client_repo", {})
        return str(val.get("local_path", "")) if isinstance(val, dict) else ""

    @property
    def ai_label(self) -> str:
        return str(self.ticket_source.get("ai_label", "ai-implement"))

    @property
    def quick_label(self) -> str:
        return str(self.ticket_source.get("quick_label", "ai-quick"))

    @property
    def done_status(self) -> str:
        return str(self.ticket_source.get("done_status", "Done"))

    @property
    def in_progress_status(self) -> str:
        return str(self.ticket_source.get("in_progress_status", "In Progress"))

    @property
    def jira_instance(self) -> str:
        return str(self.ticket_source.get("instance", ""))

    @property
    def project_key(self) -> str:
        return str(self.ticket_source.get("project_key", ""))

    @property
    def ticket_source_type(self) -> str:
        """Read ticket_source.type from YAML (jira | ado)."""
        return str(self.ticket_source.get("type", ""))

    @property
    def ado_project_name(self) -> str:
        """Read ticket_source.ado_project_name from YAML."""
        return str(self.ticket_source.get("ado_project_name", ""))

    # --- Source control properties ---

    @property
    def source_control_type(self) -> str:
        """Read source_control.type from YAML (github | azure-repos)."""
        return str(self.source_control.get("type", "github"))

    @property
    def is_azure_repos(self) -> bool:
        """True when source_control.type is azure-repos."""
        return self.source_control_type == "azure-repos"

    @property
    def ado_project(self) -> str:
        """Read source_control.ado_project from YAML (Azure Repos only)."""
        return str(self.source_control.get("ado_project", ""))

    @property
    def ado_repository_id(self) -> str:
        """Read source_control.ado_repository_id from YAML (Azure Repos only)."""
        return str(self.source_control.get("ado_repository_id", ""))

    @property
    def auto_merge_enabled(self) -> bool:
        """Read autonomy.auto_merge_enabled from YAML (default False)."""
        autonomy = self._data.get("autonomy", {})
        if not isinstance(autonomy, dict):
            return False
        return bool(autonomy.get("auto_merge_enabled", False))

    @property
    def low_risk_ticket_types(self) -> list[str]:
        """Read autonomy.low_risk_ticket_types from YAML.

        Defaults to a hardcoded set when the YAML does not specify a list.
        """
        autonomy = self._data.get("autonomy", {})
        if isinstance(autonomy, dict):
            custom = autonomy.get("low_risk_ticket_types")
            if isinstance(custom, list) and custom:
                return [str(t).lower() for t in custom]
        return ["bug", "chore", "config", "dependency", "docs"]


def load_profile(name: str, profiles_dir: Path | None = None) -> ClientProfile | None:
    """Load a client profile by name.

    Looks for `<name>.yaml` in the profiles directory.
    Returns None if not found.

    Rejects names that don't match ``^[A-Za-z0-9][A-Za-z0-9_-]*$`` — any
    attempt at path traversal (``..``), absolute paths, or glob characters
    is dropped at the entry point. Callers get None with a
    ``client_profile_invalid_name`` warning instead of a filesystem hit.
    The parse-error branch no longer echoes YAML error text (which could
    contain file contents) into the log, in case a caller does manage to
    pass the regex via a legitimate-looking but unexpected file.
    """
    if not isinstance(name, str) or not _PROFILE_NAME_RE.match(name):
        logger.warning("client_profile_invalid_name", name=name)
        return None

    directory = profiles_dir or PROFILES_DIR
    path = directory / f"{name}.yaml"

    if not path.exists():
        logger.warning("client_profile_not_found", name=name, path=str(path))
        return None

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        logger.warning("client_profile_parse_error", name=name)
        return None
    if not isinstance(data, dict):
        logger.warning("client_profile_invalid", name=name, reason="YAML is not a dict")
        return None
    logger.info("client_profile_loaded", name=name, client=data.get("client", ""))
    return ClientProfile(data, name)


def find_profile_by_project_key(
    project_key: str, profiles_dir: Path | None = None
) -> ClientProfile | None:
    """Find a client profile whose project_key matches the given key.

    Scans all YAML profiles and returns the first match.
    Returns None if no profile matches.
    """
    directory = profiles_dir or PROFILES_DIR
    if not directory.exists():
        return None

    for path in sorted(directory.glob("*.yaml")):
        if path.stem == "schema":
            continue
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        profile_key = data.get("ticket_source", {}).get("project_key", "")
        if profile_key and profile_key.upper() == project_key.upper():
            logger.info(
                "client_profile_matched_by_project_key",
                name=path.stem,
                project_key=project_key,
            )
            return ClientProfile(data, path.stem)

    return None


def find_profile_by_ado_project(
    ado_project_name: str, profiles_dir: Path | None = None
) -> ClientProfile | None:
    """Find a client profile whose ado_project_name matches (case-insensitive).

    Only considers profiles with ticket_source.type == 'ado'.
    Returns None if no profile matches.
    """
    directory = profiles_dir or PROFILES_DIR
    if not directory.exists() or not ado_project_name:
        return None

    target = ado_project_name.strip().lower()
    if not target:
        return None

    for path in sorted(directory.glob("*.yaml")):
        if path.stem == "schema":
            continue
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        ts = data.get("ticket_source", {})
        if not isinstance(ts, dict):
            continue
        if str(ts.get("type", "")).lower() != "ado":
            continue
        profile_ado_name = str(ts.get("ado_project_name", "")).strip().lower()
        if profile_ado_name and profile_ado_name == target:
            logger.info(
                "client_profile_matched_by_ado_project",
                name=path.stem,
                ado_project_name=ado_project_name,
            )
            return ClientProfile(data, path.stem)

    return None


def find_profile_by_ado_repo(
    ado_repository_id: str, profiles_dir: Path | None = None
) -> ClientProfile | None:
    """Find a client profile whose source_control.ado_repository_id matches.

    Used by L3 to resolve an ADO PR webhook to a client profile.
    Only considers profiles with source_control.type == 'azure-repos'.
    Returns None if no profile matches.
    """
    directory = profiles_dir or PROFILES_DIR
    if not directory.exists() or not ado_repository_id:
        return None

    target = ado_repository_id.strip().lower()
    if not target:
        return None

    for path in sorted(directory.glob("*.yaml")):
        if path.stem == "schema":
            continue
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        sc = data.get("source_control", {})
        if not isinstance(sc, dict):
            continue
        if str(sc.get("type", "")).lower() != "azure-repos":
            continue
        profile_repo_id = str(sc.get("ado_repository_id", "")).strip().lower()
        if profile_repo_id and profile_repo_id == target:
            logger.info(
                "client_profile_matched_by_ado_repo",
                name=path.stem,
                ado_repository_id=ado_repository_id,
            )
            return ClientProfile(data, path.stem)

    return None


def find_profile_by_repo(
    repo_full_name: str, profiles_dir: Path | None = None
) -> ClientProfile | None:
    """Find a client profile whose client_repo matches `repo_full_name`.

    Matches either `client_repo.github_repo` ("owner/repo") or
    `client_repo.url` (a full URL containing the repo path). Comparison is
    case-insensitive. Scans all *.yaml in profiles_dir (skipping schema.yaml);
    returns the first match or None.
    """
    directory = profiles_dir or PROFILES_DIR
    if not directory.exists() or not repo_full_name:
        return None

    target = repo_full_name.strip().lower()
    if not target:
        return None

    for path in sorted(directory.glob("*.yaml")):
        if path.stem == "schema":
            continue
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        client_repo = data.get("client_repo", {})
        if not isinstance(client_repo, dict):
            continue
        github_repo = str(client_repo.get("github_repo", "")).strip().lower()
        url = str(client_repo.get("url", "")).strip().lower()
        if github_repo and github_repo == target:
            logger.info(
                "client_profile_matched_by_repo",
                name=path.stem,
                repo_full_name=repo_full_name,
            )
            return ClientProfile(data, path.stem)
        if url:
            # URL may be https://github.com/owner/repo[.git] — match suffix.
            # Require the owner/repo segment to be preceded by a slash so
            # ``alpha/service`` does NOT match an unrelated profile whose
            # URL ends with ``team-alpha/service``. The previous bare
            # ``endswith(target)`` clause admitted any suffix match and
            # could route webhooks to the wrong client profile (wrong
            # credentials, wrong callbacks).
            url_clean = url.rstrip("/").removesuffix(".git")
            if url_clean == target or url_clean.endswith("/" + target):
                logger.info(
                    "client_profile_matched_by_repo",
                    name=path.stem,
                    repo_full_name=repo_full_name,
                )
                return ClientProfile(data, path.stem)

    return None


def list_profiles(profiles_dir: Path | None = None) -> list[str]:
    """List available client profile names."""
    directory = profiles_dir or PROFILES_DIR
    if not directory.exists():
        return []
    return [
        p.stem
        for p in sorted(directory.glob("*.yaml"))
        if p.stem != "schema"
    ]
