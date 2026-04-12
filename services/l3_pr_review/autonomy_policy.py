"""Auto-merge policy evaluation. Pure decision logic; no I/O except L1 reads."""
from __future__ import annotations

import time
from collections.abc import Callable
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


async def _cached_l1_get(
    cache_key: str,
    url: str,
    *,
    fail_closed: Any,
    parse: Callable[[dict[str, Any]], Any],
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    log_event: str | None = None,
    log_context: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> Any:
    """Shared GET-with-cache for the three ``fetch_*`` helpers.

    Each caller used to duplicate ~25 lines of cache check → owns_client
    httpx dance → non-200 fail-closed → JSON parse → RequestError /
    ValueError fail-closed → aclose. Centralising it here makes the
    fail-closed default explicit and harder to accidentally forget
    when new L1 endpoints are added (the default-open hazard that bit
    us in ado_api.py).

    ``parse`` maps the JSON body to the cached value. ``fail_closed``
    is returned (and NOT cached) on any failure. ``log_event`` / ``log_context``
    are logged as warnings on failure so callers don't each duplicate
    a structured log line.
    """
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    owns_client = client is None
    c = client or httpx.AsyncClient(timeout=5.0)
    try:
        resp = await c.get(url, params=params, headers=headers)
        if resp.status_code != 200:
            if log_event:
                logger.warning(
                    log_event, status=resp.status_code, **(log_context or {})
                )
            return fail_closed
        value = parse(resp.json())
        _cache_set(cache_key, value)
        return value
    except (httpx.RequestError, ValueError):
        if log_event:
            logger.warning(log_event, **(log_context or {}))
        return fail_closed
    finally:
        if owns_client:
            await c.aclose()


def _parse_recommended_mode(data: dict[str, Any]) -> tuple[str, str]:
    mode = str(data.get("recommended_mode") or "conservative")
    dq = str((data.get("data_quality") or {}).get("status") or "unknown")
    return (mode, dq)


async def fetch_recommended_mode(
    client_profile: str,
    *,
    l1_url: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """GET /api/autonomy?client_profile=. Returns (mode, dq_status).

    Fail-closed to ('conservative', 'unknown') on error.
    """
    return await _cached_l1_get(
        f"mode:{client_profile}",
        f"{l1_url.rstrip('/')}/api/autonomy",
        fail_closed=("conservative", "unknown"),
        parse=_parse_recommended_mode,
        params={"client_profile": client_profile},
        log_event="l1_autonomy_fetch_failed",
        log_context={"client_profile": client_profile},
        client=client,
    )


async def fetch_auto_merge_enabled(
    client_profile: str,
    *,
    l1_url: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """GET /api/autonomy/auto-merge-toggle?client_profile=. Fail-closed to False."""
    return await _cached_l1_get(
        f"toggle:{client_profile}",
        f"{l1_url.rstrip('/')}/api/autonomy/auto-merge-toggle",
        fail_closed=False,
        parse=lambda data: bool(data.get("enabled", False)),
        params={"client_profile": client_profile},
        client=client,
    )


_EMPTY_PROFILE: dict[str, Any] = {
    "client_profile": "",
    "low_risk_ticket_types": [],
    "auto_merge_enabled_yaml": False,
}


async def fetch_profile_by_repo(
    repo_full_name: str,
    *,
    l1_url: str,
    internal_token: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Resolve repo -> client_profile via L1. Returns dict (may have empty profile)."""
    if not internal_token:
        logger.warning("l1_profile_by_repo_no_token")
        return _EMPTY_PROFILE
    return await _cached_l1_get(
        f"profile:{repo_full_name}",
        f"{l1_url.rstrip('/')}/api/internal/autonomy/profile-by-repo",
        fail_closed=_EMPTY_PROFILE,
        parse=lambda data: data,
        params={"repo_full_name": repo_full_name},
        headers={"X-Internal-Api-Token": internal_token},
        client=client,
    )


@dataclass(frozen=True)
class _Gate:
    """One policy gate — name, reason code on failure, predicate.

    ``predicate`` returns True when the gate PASSES. ``applies_when``
    is an optional precondition: when it returns False the gate is
    skipped and recorded as True (pass-through, e.g. ``low_risk`` only
    applies in semi_autonomous mode).

    Keeping the table ordered and explicit means the gate sequence is
    auditable in one place and new gates are a single line addition —
    the previous 11-branch if/return ladder forced every new rule to
    find the right insertion point and copy 3-line scaffolding.
    """
    name: str
    reason: str
    predicate: Any  # Callable[[AutoMergeContext, dict], bool]
    applies_when: Any = None  # Optional[Callable[[AutoMergeContext, dict], bool]]


def _low_risk_predicate(
    ctx: AutoMergeContext, _pr: dict[str, Any]
) -> bool:
    return (ctx.ticket_type or "").lower() in {
        t.lower() for t in ctx.low_risk_types
    }


_GATES: list[_Gate] = [
    _Gate(
        "profile_enabled",
        REASON_KILL_SWITCH_OFF,
        lambda ctx, pr: ctx.profile_enabled,
    ),
    _Gate(
        "data_quality_good",
        REASON_DATA_QUALITY_DEGRADED,
        lambda ctx, pr: ctx.data_quality_status == "good",
    ),
    _Gate(
        "mode_allows_merge",
        REASON_MODE_CONSERVATIVE,
        lambda ctx, pr: ctx.recommended_mode in ("semi_autonomous", "full_autonomous"),
    ),
    # Only applies in semi_autonomous mode — full_autonomous skips the
    # low-risk check entirely (pass-through).
    _Gate(
        "low_risk_ticket_type",
        REASON_NOT_LOW_RISK_IN_SEMI,
        _low_risk_predicate,
        applies_when=lambda ctx, pr: ctx.recommended_mode == "semi_autonomous",
    ),
    _Gate(
        "bot_authored",
        REASON_HUMAN_AUTHORED,
        lambda ctx, pr: (pr.get("author") or "").lower()
        == ctx.bot_github_username.lower(),
    ),
    _Gate(
        "not_already_merged",
        REASON_ALREADY_MERGED,
        lambda ctx, pr: not bool(pr.get("merged")),
    ),
    # Requires at least one HUMAN approval, not just any approval.
    # ``human_approvals_count`` is computed in ``github_api.get_pr_state``
    # by filtering out reviewers that look like GitHub Apps (``type``
    # == "Bot", ``login`` ends with "[bot]", or matches the
    # L3_APPROVAL_BOT_DENYLIST env var). Falls back to the legacy
    # ``approvals_count`` only when the human-specific field is
    # missing from the pr_state dict — keeps ADO callers (which don't
    # yet distinguish bot reviewers) from fail-CLOSED regressing.
    _Gate(
        "has_approval",
        REASON_NO_APPROVAL,
        lambda ctx, pr: int(
            pr.get("human_approvals_count", pr.get("approvals_count") or 0)
            or 0
        ) > 0,
    ),
    _Gate(
        "no_changes_requested",
        REASON_CHANGES_REQUESTED,
        lambda ctx, pr: int(pr.get("changes_requested_count") or 0) == 0,
    ),
    _Gate(
        "ci_passed",
        REASON_CI_NOT_PASSED,
        lambda ctx, pr: bool(pr.get("checks_passed")),
    ),
    _Gate(
        "mergeable",
        REASON_NOT_MERGEABLE,
        lambda ctx, pr: bool(pr.get("mergeable"))
        and (pr.get("mergeable_state") or "").lower() == "clean",
    ),
]


def evaluate_policy_gates(
    ctx: AutoMergeContext, pr_state: dict[str, Any]
) -> tuple[bool, str, dict[str, bool]]:
    """Pure function: apply all gates. Returns (should_merge, reason_code, gate_map).

    pr_state keys required: author, merged, mergeable, mergeable_state,
    approvals_count, changes_requested_count, checks_passed.

    Gates are iterated in the order declared in ``_GATES``. The first
    gate whose predicate returns False short-circuits with its reason
    code. Gates with an ``applies_when`` precondition (currently only
    ``low_risk_ticket_type``) record True and skip the predicate when
    the precondition is False. ``global_enabled`` is recorded but
    never fails the gates — the orchestrator decides whether to
    execute the merge vs. record it as a dry run.
    """
    gates: dict[str, bool] = {"global_enabled": ctx.global_enabled}

    for gate in _GATES:
        if gate.applies_when is not None and not gate.applies_when(ctx, pr_state):
            gates[gate.name] = True
            continue
        ok = gate.predicate(ctx, pr_state)
        gates[gate.name] = ok
        if not ok:
            return False, gate.reason, gates

    return True, REASON_OK, gates
