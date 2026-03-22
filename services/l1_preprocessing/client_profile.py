"""Client Profile loader — reads per-client YAML configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger()

PROFILES_DIR = Path(__file__).resolve().parents[2] / "runtime" / "client-profiles"


class ClientProfile:
    """A loaded client profile with typed accessors."""

    def __init__(self, data: dict[str, Any], name: str = "") -> None:
        self._data = data
        self.name = name or str(data.get("client", ""))
        self.platform_profile = str(data.get("platform_profile", ""))

    @property
    def ticket_source(self) -> dict[str, Any]:
        result: dict[str, Any] = self._data.get("ticket_source", {})
        return result

    @property
    def source_control(self) -> dict[str, Any]:
        result: dict[str, Any] = self._data.get("source_control", {})
        return result

    @property
    def ci_pipeline(self) -> dict[str, Any]:
        result: dict[str, Any] = self._data.get("ci_pipeline", {})
        return result

    @property
    def client_repo_path(self) -> str:
        return str(self._data.get("client_repo", {}).get("local_path", ""))

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
    def jira_instance(self) -> str:
        return str(self.ticket_source.get("instance", ""))

    @property
    def project_key(self) -> str:
        return str(self.ticket_source.get("project_key", ""))


def load_profile(name: str, profiles_dir: Path | None = None) -> ClientProfile | None:
    """Load a client profile by name.

    Looks for `<name>.yaml` in the profiles directory.
    Returns None if not found.
    """
    directory = profiles_dir or PROFILES_DIR
    path = directory / f"{name}.yaml"

    if not path.exists():
        logger.warning("client_profile_not_found", name=name, path=str(path))
        return None

    data: dict[str, Any] = yaml.safe_load(path.read_text())
    logger.info("client_profile_loaded", name=name, client=data.get("client", ""))
    return ClientProfile(data, name)


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
