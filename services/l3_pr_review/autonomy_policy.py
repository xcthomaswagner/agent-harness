"""Auto-merge policy evaluation. Pure decision logic; no I/O except L1 reads."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

DEFAULT_LOW_RISK_TYPES = ("bug", "chore", "config", "dependency", "docs")
_CACHE_TTL_SEC = 60.0

# Reason codes returned by evaluate_policy_gates
REASON_OK = "ok"
REASON_MODE_CONSERVATIVE = "mode_conservative"
REASON_NOT_LOW_RISK_IN_SEMI = "not_low_risk_in_semi"
REASON_CI_NOT_PASSED = "ci_not_passed"
REASON_CHANGES_REQUESTED = "changes_requested_outstanding"
REASON_HUMAN_AUTHORED = "human_authored_pr"
REASON_NOT_MERGEABLE = "not_mergeable"
REASON_ALREADY_MERGED = "already_merged"
REASON_NO_APPROVAL = "no_approval"
REASON_KILL_SWITCH_OFF = "kill_switch_off"
REASON_GLOBAL_DISABLED = "global_disabled"
REASON_DATA_QUALITY_DEGRADED = "data_quality_degraded"


@dataclass
class AutoMergeContext:
    """Policy inputs resolved from L1 + profile YAML + environment."""

    recommended_mode: str
    data_quality_status: str
    ticket_type: str
    low_risk_types: list[str]
    profile_enabled: bool  # from L1 toggle endpoint (YAML or runtime override)
    global_enabled: bool  # AUTO_MERGE_ENABLED env var
    bot_github_username: str  # Expected PR author
    dry_run: bool  # True when global_enabled=False (evaluate but don't merge)


# Simple in-process TTL cache
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expiry, value = entry
    if time.monotonic() >= expiry:
        return None
    return value


def _cache_set(key: str, value: Any, ttl: float = _CACHE_TTL_SEC) -> None:
    _cache[key] = (time.monotonic() + ttl, value)


def _cache_clear() -> None:
    """Test hook."""
    _cache.clear()


async def fetch_recommended_mode(
    client_profile: str,
    *,
    l1_url: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """GET /api/autonomy?client_profile=. Returns (mode, dq_status).

    Fail-closed to ('conservative', 'unknown') on error.
    """
    key = f"mode:{client_profile}"
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    url = f"{l1_url.rstrip('/')}/api/autonomy"
    owns_client = client is None
    c = client or httpx.AsyncClient(timeout=5.0)
    try:
        resp = await c.get(url, params={"client_profile": client_profile})
        if resp.status_code != 200:
            logger.warning("l1_autonomy_fetch_non_200", status=resp.status_code)
            return ("conservative", "unknown")
        data = resp.json()
        mode = str(data.get("recommended_mode") or "conservative")
        dq = str((data.get("data_quality") or {}).get("status") or "unknown")
        result = (mode, dq)
        _cache_set(key, result)
        return result
    except (httpx.RequestError, ValueError):
        logger.warning("l1_autonomy_fetch_failed", client_profile=client_profile)
        return ("conservative", "unknown")
    finally:
        if owns_client:
            await c.aclose()


async def fetch_auto_merge_enabled(
    client_profile: str,
    *,
    l1_url: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """GET /api/autonomy/auto-merge-toggle?client_profile=. Fail-closed to False."""
    key = f"toggle:{client_profile}"
    cached = _cache_get(key)
    if cached is not None:
        return bool(cached)
    url = f"{l1_url.rstrip('/')}/api/autonomy/auto-merge-toggle"
    owns_client = client is None
    c = client or httpx.AsyncClient(timeout=5.0)
    try:
        resp = await c.get(url, params={"client_profile": client_profile})
        if resp.status_code != 200:
            return False
        enabled = bool(resp.json().get("enabled", False))
        _cache_set(key, enabled)
        return enabled
    except (httpx.RequestError, ValueError):
        return False
    finally:
        if owns_client:
            await c.aclose()


async def fetch_profile_by_repo(
    repo_full_name: str,
    *,
    l1_url: str,
    internal_token: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Resolve repo -> client_profile via L1. Returns dict (may have empty profile)."""
    key = f"profile:{repo_full_name}"
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    if not internal_token:
        logger.warning("l1_profile_by_repo_no_token")
        return {
            "client_profile": "",
            "low_risk_ticket_types": [],
            "auto_merge_enabled_yaml": False,
        }
    url = f"{l1_url.rstrip('/')}/api/internal/autonomy/profile-by-repo"
    owns_client = client is None
    c = client or httpx.AsyncClient(timeout=5.0)
    try:
        resp = await c.get(
            url,
            params={"repo_full_name": repo_full_name},
            headers={"X-Internal-Api-Token": internal_token},
        )
        if resp.status_code != 200:
            return {
                "client_profile": "",
                "low_risk_ticket_types": [],
                "auto_merge_enabled_yaml": False,
            }
        data: dict[str, Any] = resp.json()
        _cache_set(key, data)
        return data
    except (httpx.RequestError, ValueError):
        return {
            "client_profile": "",
            "low_risk_ticket_types": [],
            "auto_merge_enabled_yaml": False,
        }
    finally:
        if owns_client:
            await c.aclose()


def evaluate_policy_gates(
    ctx: AutoMergeContext, pr_state: dict[str, Any]
) -> tuple[bool, str, dict[str, bool]]:
    """Pure function: apply all gates. Returns (should_merge, reason_code, gate_map).

    pr_state keys required: author, merged, mergeable, mergeable_state,
    approvals_count, changes_requested_count, checks_passed.
    """
    gates: dict[str, bool] = {}
    # Record global switch state for audit but do NOT fail gates on it —
    # the orchestrator decides whether to execute the merge vs. record dry_run.
    gates["global_enabled"] = ctx.global_enabled

    # 1. Per-profile kill switch
    gates["profile_enabled"] = ctx.profile_enabled
    if not ctx.profile_enabled:
        return False, REASON_KILL_SWITCH_OFF, gates

    # 3. Data quality must be 'good'
    dq_ok = ctx.data_quality_status == "good"
    gates["data_quality_good"] = dq_ok
    if not dq_ok:
        return False, REASON_DATA_QUALITY_DEGRADED, gates

    # 4. Mode must be semi or full autonomous
    mode_ok = ctx.recommended_mode in ("semi_autonomous", "full_autonomous")
    gates["mode_allows_merge"] = mode_ok
    if not mode_ok:
        return False, REASON_MODE_CONSERVATIVE, gates

    # 5. If semi_autonomous, ticket_type must be low-risk
    if ctx.recommended_mode == "semi_autonomous":
        low_risk = (ctx.ticket_type or "").lower() in {
            t.lower() for t in ctx.low_risk_types
        }
        gates["low_risk_ticket_type"] = low_risk
        if not low_risk:
            return False, REASON_NOT_LOW_RISK_IN_SEMI, gates
    else:
        gates["low_risk_ticket_type"] = True  # not required

    # 6. Author must be the bot
    author_ok = (pr_state.get("author") or "").lower() == ctx.bot_github_username.lower()
    gates["bot_authored"] = author_ok
    if not author_ok:
        return False, REASON_HUMAN_AUTHORED, gates

    # 7. Not already merged
    already_merged = bool(pr_state.get("merged"))
    gates["not_already_merged"] = not already_merged
    if already_merged:
        return False, REASON_ALREADY_MERGED, gates

    # 8. At least one approval
    approvals = int(pr_state.get("approvals_count") or 0)
    gates["has_approval"] = approvals > 0
    if approvals < 1:
        return False, REASON_NO_APPROVAL, gates

    # 9. No outstanding changes_requested
    changes_req = int(pr_state.get("changes_requested_count") or 0)
    gates["no_changes_requested"] = changes_req == 0
    if changes_req > 0:
        return False, REASON_CHANGES_REQUESTED, gates

    # 10. CI passed
    ci_ok = bool(pr_state.get("checks_passed"))
    gates["ci_passed"] = ci_ok
    if not ci_ok:
        return False, REASON_CI_NOT_PASSED, gates

    # 11. Mergeable per GitHub (clean state incorporates required checks/reviews)
    mergeable = bool(pr_state.get("mergeable"))
    clean = (pr_state.get("mergeable_state") or "").lower() == "clean"
    gates["mergeable"] = mergeable and clean
    if not mergeable or not clean:
        return False, REASON_NOT_MERGEABLE, gates

    return True, REASON_OK, gates
