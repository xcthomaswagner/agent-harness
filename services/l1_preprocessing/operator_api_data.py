"""JSON endpoints for the operator dashboard under ``/api/operator``.

Split from ``operator_api.py`` (which handles the SPA shell + static
assets) to keep concerns clean: this module is data, that module is
HTML + static serving.

Each view of the dashboard gets one or two endpoints here. Shapes are
stable — the SPA's TypeScript types in services/operator_ui/src/api/
types.ts track them 1:1. Breaking-change discipline: if a field renames
or disappears, bump the route path (``/v2/...``) rather than silently
mutating the contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from shared.model_policy import (
    DEFAULT_POLICY_PATH as _MODEL_POLICY_PATH,
)
from shared.model_policy import (
    MODEL_OPTIONS as _MODEL_OPTIONS,
)
from shared.model_policy import (
    REASONING_OPTIONS as _REASONING_OPTIONS,
)
from shared.model_policy import (
    default_policy as _shared_default_model_policy,
)

from auth import _require_dashboard_auth
from autonomy_metrics import (
    compute_daily_trend,
    compute_profile_metrics,
    compute_ticket_type_breakdown,
)
from autonomy_store import ensure_schema, open_connection
from autonomy_store.auto_merge import list_recent_auto_merge_decisions
from autonomy_store.dashboard_state import (
    clear_dashboard_suppression,
    list_active_suppressions,
    suppress_dashboard_target,
)
from autonomy_store.defects import list_confirmed_escaped_defects
from autonomy_store.issues import (
    list_pr_commits,
    list_review_issues_by_pr_run,
)
from autonomy_store.lessons import list_lesson_candidates
from autonomy_store.pr_runs import (
    ACTIVE_PR_RUN_STATES,
    list_pr_runs,
    mark_stale_pr_runs,
    set_pr_run_lifecycle_state,
    set_pr_runs_lifecycle_state_for_ticket,
)
from autonomy_store.schema import resolve_db_path
from client_profile import list_profiles, load_profile
from live_stream import (
    _find_session_streams,
    _worktree_root_for_ticket,
    collect_finished_activity,
    summarize_stream_teammates,
    summarize_ticket_activity,
)
from repo_workflow import (
    RepoWorkflowError,
    generate_repo_workflow,
    profile_options,
    save_repo_workflow,
)
from tracer import (
    append_trace,
    compute_phase_durations,
    find_run_start_idx,
    generate_trace_id,
    read_trace,
)
from tracer import (
    list_traces as _list_traces,
)

router = APIRouter(
    prefix="/api/operator",
    tags=["operator"],
    dependencies=[Depends(_require_dashboard_auth)],
)

_SERVICE_STARTED_AT = datetime.now(UTC)
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_OPERATOR_BUNDLE_PATH = Path(__file__).resolve().parent / "operator_static" / "build.json"


def _git_value(*args: str) -> str:
    try:
        return subprocess.check_output(
            ("git", *args),
            cwd=_SERVICE_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


_GIT_SHA = _git_value("rev-parse", "--short", "HEAD")
_GIT_BRANCH = _git_value("branch", "--show-current")
_ACTIVE_PROFILE_PR_STATES = ("open", "reviewed", "needs_changes", "awaiting_merge")


def _db_path_from_settings() -> Path:
    """Resolve ``settings.autonomy_db_path`` to a Path.

    Empty settings value falls back to ``<repo>/data/autonomy.db`` via
    ``resolve_db_path`` — same helper every other module in the service
    uses. Resolved at call time so tests' monkey-patched settings take
    effect per-request.
    """
    import main

    return resolve_db_path(main.settings.autonomy_db_path)


class ModelRoleSelection(BaseModel):
    role: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    reasoning: str = Field(min_length=1, max_length=32)


class ModelPolicyUpdate(BaseModel):
    roles: list[ModelRoleSelection]


class RepoWorkflowDraftRequest(BaseModel):
    client_profile: str = Field(default="", max_length=128)
    repo_path: str = Field(default="", max_length=1024)


class RepoWorkflowSaveRequest(BaseModel):
    client_profile: str = Field(default="", max_length=128)
    repo_path: str = Field(default="", max_length=1024)
    content: str = Field(min_length=1, max_length=200_000)


class DashboardStateUpdate(BaseModel):
    state: str = Field(min_length=1, max_length=32)
    reason: str = Field(default="", max_length=500)
    exclude_metrics: bool = True


class DashboardCleanupRequest(BaseModel):
    stale_after_hours: int = Field(default=168, ge=1, le=24 * 90)
    dry_run: bool = False


class _TraceLifecycleDecision(BaseModel):
    state: str
    event: str
    event_at: str
    pr_run_id: int


def _default_model_policy() -> dict[str, Any]:
    return _shared_default_model_policy()


def _read_model_policy_file() -> dict[str, Any]:
    if not _MODEL_POLICY_PATH.is_file():
        return _default_model_policy()
    try:
        raw = json.loads(_MODEL_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _default_model_policy()
    if not isinstance(raw, dict):
        return _default_model_policy()
    defaults = _default_model_policy()
    role_defaults = {r["role"]: r for r in defaults["roles"]}
    configured = raw.get("roles")
    if not isinstance(configured, list):
        configured = []
    configured_by_role: dict[str, dict[str, str]] = {}
    valid_roles = set(role_defaults)
    for row in configured:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "")
        if role not in valid_roles:
            continue
        model = str(row.get("model") or role_defaults[role]["model"])
        reasoning = str(row.get("reasoning") or role_defaults[role]["reasoning"])
        configured_by_role[role] = {
            "role": role,
            "label": role_defaults[role]["label"],
            "model": model if model in _MODEL_OPTIONS else role_defaults[role]["model"],
            "reasoning": (
                reasoning if reasoning in _REASONING_OPTIONS else role_defaults[role]["reasoning"]
            ),
        }
    roles = [configured_by_role.get(r["role"], dict(r)) for r in defaults["roles"]]
    return {
        **defaults,
        "source": "local",
        "updated_at": str(raw.get("updated_at") or ""),
        "roles": roles,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    """Persist text via same-directory replace so readers never see partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            with suppress(FileNotFoundError):
                Path(tmp_name).unlink()


def _write_model_policy_file(update: ModelPolicyUpdate) -> dict[str, Any]:
    defaults = _default_model_policy()
    default_by_role = {r["role"]: r for r in defaults["roles"]}
    next_by_role = {r["role"]: dict(r) for r in defaults["roles"]}
    for row in update.roles:
        if row.role not in default_by_role:
            raise HTTPException(
                status_code=400,
                detail=f"unknown model policy role: {row.role}",
            )
        if row.model not in _MODEL_OPTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported model for {row.role}: {row.model}",
            )
        if row.reasoning not in _REASONING_OPTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported reasoning for {row.role}: {row.reasoning}",
            )
        next_by_role[row.role] = {
            "role": row.role,
            "label": default_by_role[row.role]["label"],
            "model": row.model,
            "reasoning": row.reasoning,
        }
    payload = {
        "version": 1,
        "updated_at": datetime.now(UTC).isoformat(),
        "roles": [next_by_role[r["role"]] for r in defaults["roles"]],
    }
    _atomic_write_text(
        _MODEL_POLICY_PATH,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return _read_model_policy_file()


def _operator_bundle_info() -> dict[str, str]:
    try:
        raw = json.loads(_OPERATOR_BUNDLE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"rev": "", "built_at": ""}
    return {
        "rev": str(raw.get("rev") or ""),
        "built_at": str(raw.get("built_at") or ""),
    }


@router.get("/system")
def get_system() -> dict[str, Any]:
    """Runtime metadata for spotting stale local service processes."""
    now = datetime.now(UTC)
    return {
        "service": "l1_preprocessing",
        "version": "0.1.0",
        "pid": os.getpid(),
        "started_at": _SERVICE_STARTED_AT.isoformat(),
        "uptime_seconds": int((now - _SERVICE_STARTED_AT).total_seconds()),
        "git_sha": _GIT_SHA,
        "git_branch": _GIT_BRANCH,
        "code_path": str(Path(__file__).resolve()),
        "db_path": str(_db_path_from_settings()),
        "operator_bundle": _operator_bundle_info(),
    }


@router.get("/model-policy")
def get_model_policy() -> dict[str, Any]:
    """Return the operator-local model selection policy.

    The dashboard owns this file for now; the runtime model resolver can
    consume the same JSON later without adding multi-user configuration.
    """
    return _read_model_policy_file()


@router.put("/model-policy")
def put_model_policy(update: ModelPolicyUpdate) -> dict[str, Any]:
    """Persist operator-selected model/reasoning choices."""
    return _write_model_policy_file(update)


@router.get("/repo-workflow/options")
def get_repo_workflow_options() -> dict[str, Any]:
    """Client-profile repo choices for generating WORKFLOW.md overlays."""
    return {"profiles": profile_options(list_profiles(), load_profile)}


def _repo_workflow_target(
    client_profile: str,
    repo_path: str,
) -> tuple[str, str, str]:
    profile_name = client_profile.strip()
    profile = None
    if profile_name:
        profile = load_profile(profile_name)
        if profile is None:
            raise HTTPException(status_code=404, detail="client profile not found")
    path = repo_path.strip()
    if not path and profile is not None:
        path = profile.client_repo_path
    if not path:
        raise HTTPException(status_code=400, detail="repo_path is required")
    platform_profile = (
        str(getattr(profile, "platform_profile", "") or "") if profile is not None else ""
    )
    return profile_name, path, platform_profile


@router.post("/repo-workflow/draft")
def post_repo_workflow_draft(request: RepoWorkflowDraftRequest) -> dict[str, Any]:
    """Scan a repo and return an editable WORKFLOW.md draft with evidence."""
    profile_name, path, platform_profile = _repo_workflow_target(
        request.client_profile,
        request.repo_path,
    )
    try:
        return generate_repo_workflow(
            path,
            client_profile=profile_name,
            platform_profile=platform_profile,
        )
    except RepoWorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/repo-workflow")
def put_repo_workflow(request: RepoWorkflowSaveRequest) -> dict[str, Any]:
    """Persist edited WORKFLOW.md content to the selected repo root."""
    _profile_name, path, _platform_profile = _repo_workflow_target(
        request.client_profile,
        request.repo_path,
    )
    try:
        return save_repo_workflow(path, request.content)
    except RepoWorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# Decisions that signal "auto-merge was eligible" (i.e., count in the
# denominator of the adoption rate). ``skipped`` / ``not_eligible``
# / unset values are excluded — they mean the run never qualified
# for auto-merge in the first place.
_AUTO_MERGE_ELIGIBLE_DECISIONS: frozenset[str] = frozenset(
    {"merged", "auto_merged", "merge", "blocked", "hold"}
)
_AUTO_MERGE_SUCCEEDED_DECISIONS: frozenset[str] = frozenset({"merged", "auto_merged", "merge"})


def _compute_auto_merge_rate(conn: Any, profile: str, window_days: int) -> float:
    """Auto-merge adoption rate = succeeded / eligible decisions.

    Reads ``manual_overrides`` where ``override_type =
    'auto_merge_decision'``. The payload's ``decision`` field is
    written by ``record_auto_merge_decision``. Only decisions in
    ``_AUTO_MERGE_ELIGIBLE_DECISIONS`` (merged/auto_merged/merge/
    blocked/hold) count toward the denominator; anything else
    (including future decision values we haven't seen yet) is
    treated as non-eligible and excluded. Keeping this as an
    allow-list rather than a block-list prevents the rate from
    silently absorbing new decision types into the denominator
    and understating adoption.

    Returns 0.0 when there are no eligible decisions in the window.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    rows = list_recent_auto_merge_decisions(
        conn,
        limit=500,
        since_iso=cutoff,
        client_profile=profile,
    )
    if not rows:
        return 0.0
    eligible = 0
    merged = 0
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (ValueError, TypeError):
            continue
        decision = str(payload.get("decision", "")).lower()
        if decision not in _AUTO_MERGE_ELIGIBLE_DECISIONS:
            continue
        eligible += 1
        if decision in _AUTO_MERGE_SUCCEEDED_DECISIONS:
            merged += 1
    if eligible == 0:
        return 0.0
    return round(merged / eligible, 3)


def _count_pr_runs_in_window(conn: Any, profile: str, hours: int) -> tuple[int, int]:
    """Return (in_flight_count, completed_in_window_count).

    in_flight = active, fresh pr_runs that are not merged/closed/suppressed.
    completed = pr_runs merged in the last ``hours``.
    """
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    active_cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    placeholders = ",".join("?" * len(_ACTIVE_PROFILE_PR_STATES))
    in_flight = int(
        conn.execute(
            "SELECT COUNT(*) FROM pr_runs "
            f"WHERE client_profile = ? AND state IN ({placeholders}) "
            "AND merged = 0 AND closed_at = '' AND escalated = 0 "
            "AND suppressed_at = '' AND excluded_from_metrics = 0 "
            "AND COALESCE(NULLIF(last_observed_at, ''), opened_at) >= ?",
            (profile, *_ACTIVE_PROFILE_PR_STATES, active_cutoff),
        ).fetchone()[0]
    )
    completed = int(
        conn.execute(
            "SELECT COUNT(*) FROM pr_runs "
            "WHERE client_profile = ? AND merged = 1 AND merged_at >= ? "
            "AND excluded_from_metrics = 0 "
            "AND state NOT IN ('suppressed', 'misfire')",
            (profile, since),
        ).fetchone()[0]
    )
    return in_flight, completed


def _active_pr_ticket_ids_by_profile(conn: Any) -> dict[str, set[str]]:
    """Return active PR-run ticket IDs, keyed by client profile."""
    active_cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    placeholders = ",".join("?" * len(_ACTIVE_PROFILE_PR_STATES))
    rows = conn.execute(
        "SELECT client_profile, ticket_id FROM pr_runs "
        f"WHERE state IN ({placeholders}) "
        "AND ticket_id != '' AND client_profile != '' "
        "AND merged = 0 AND closed_at = '' AND escalated = 0 "
        "AND suppressed_at = '' AND excluded_from_metrics = 0 "
        "AND COALESCE(NULLIF(last_observed_at, ''), opened_at) >= ?",
        (*_ACTIVE_PROFILE_PR_STATES, active_cutoff),
    ).fetchall()
    out: dict[str, set[str]] = {}
    for row in rows:
        profile = str(row["client_profile"] or "")
        ticket_id = str(row["ticket_id"] or "")
        if profile and ticket_id:
            out.setdefault(profile, set()).add(ticket_id)
    return out


def _source_control_repo(profile: Any) -> str:
    sc = profile.source_control
    org = str(sc.get("org", "") or "").strip().removeprefix("https://github.com/")
    repo = str(sc.get("repo", "") or "").strip()
    if org and repo and "/" not in repo:
        return f"{org.rstrip('/')}/{repo}".lower()
    github_repo = str(sc.get("github_repo", "") or "").strip()
    return github_repo.lower()


def _profile_from_trace(
    trace: dict[str, Any],
    profiles_by_name: dict[str, Any],
) -> str:
    """Best-effort map from live trace to client profile.

    New traces carry ``client_profile`` directly. Older active traces can
    still be mapped by ticket prefix, live worktree source-control metadata,
    or a unique platform profile.
    """
    direct = str(trace.get("client_profile") or trace.get("client_profile_name") or "")
    if direct in profiles_by_name:
        return direct

    entries = trace.get("_raw_entries")
    if isinstance(entries, list):
        for entry in reversed(entries):
            entry_profile = str(
                entry.get("client_profile") or entry.get("client_profile_name") or ""
            )
            if entry_profile in profiles_by_name:
                return entry_profile

    ticket_id = str(trace.get("ticket_id") or "")
    ticket_prefix = ticket_id.split("-", 1)[0].upper() if "-" in ticket_id else ""
    if ticket_prefix:
        matches = [
            name
            for name, profile in profiles_by_name.items()
            if profile.project_key.upper() == ticket_prefix
        ]
        if len(matches) == 1:
            return matches[0]

    if ticket_id:
        with suppress(OSError, ValueError):
            worktree = _worktree_root_for_ticket(ticket_id)
            sc_path = (
                worktree / ".harness" / "source-control.json" if worktree is not None else None
            )
            if sc_path is not None and sc_path.exists():
                source_control = json.loads(sc_path.read_text())
                repo = str(source_control.get("repo") or "")
                org = str(source_control.get("org") or "")
                full_name = (
                    f"{org.rstrip('/')}/{repo}".lower()
                    if org and repo and "/" not in repo
                    else repo.lower()
                )
                matches = [
                    name
                    for name, profile in profiles_by_name.items()
                    if full_name and _source_control_repo(profile) == full_name
                ]
                if len(matches) == 1:
                    return matches[0]

    platform = str(trace.get("platform_profile") or "")
    if not platform and isinstance(entries, list):
        for entry in reversed(entries):
            platform = str(entry.get("platform_profile") or "")
            if platform:
                break
    if platform:
        matches = [
            name
            for name, profile in profiles_by_name.items()
            if profile.platform_profile == platform
        ]
        if len(matches) == 1:
            return matches[0]
    return ""


def _active_trace_counts_by_profile(
    conn: Any,
    profiles_by_name: dict[str, Any],
) -> dict[str, int]:
    """Count active pre-PR traces for Home profile cards."""
    suppressions = list_active_suppressions(conn, target_type="trace")
    pr_states = _latest_pr_state_by_ticket(conn)
    active_pr_tickets = _active_pr_ticket_ids_by_profile(conn)
    counts: dict[str, int] = {name: 0 for name in profiles_by_name}
    for trace in _list_traces(offset=0, limit=0):
        ticket_id = str(trace.get("ticket_id") or "")
        shaped = _shape_trace_row(
            trace,
            suppression=suppressions.get(ticket_id),
            pr_state=pr_states.get(ticket_id),
        )
        if shaped["status"] != "in-flight":
            continue
        profile = _profile_from_trace(trace, profiles_by_name)
        if not profile:
            continue
        if ticket_id in active_pr_tickets.get(profile, set()):
            continue
        counts[profile] = counts.get(profile, 0) + 1
    return counts


@router.get("/profiles")
def get_profiles() -> dict[str, Any]:
    """List all client profiles with recent autonomy metrics.

    Reads ``runtime/client-profiles/*.yaml`` for the authoritative list
    (the profiles that actually exist — not the 4 mock profiles from
    the design). For each profile, joins with ``compute_profile_metrics``
    for FPA / escape / catch and ``_compute_auto_merge_rate`` for
    auto-merge adoption. Counts in-flight and completed-24h PR runs so
    the Home profile cards have live activity numbers.

    Contract (per profile):

      {
        "id": str,
        "name": str,
        "sample": str,                          # "Salesforce · Apex"
        "in_flight": int,
        "completed_24h": int,
        "fpa": float (0..1) | None,
        "escape": float (0..1) | None,
        "catch": float (0..1) | None,
        "auto_merge": float (0..1),
      }
    """
    profile_names = list_profiles()
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        profiles_by_name: dict[str, Any] = {}
        for name in profile_names:
            profile = load_profile(name)
            if profile is None:
                continue
            profiles_by_name[name] = profile

        active_trace_counts = _active_trace_counts_by_profile(conn, profiles_by_name)
        out: list[dict[str, Any]] = []
        for name, profile in profiles_by_name.items():
            # ClientProfile exposes ``name`` and ``platform_profile`` —
            # use the profile name as the display label and the platform
            # string as the sample tag beside it.
            display = profile.name or name
            sample = profile.platform_profile or ""
            metrics = compute_profile_metrics(conn, name, window_days=30)
            in_flight, completed_24h = _count_pr_runs_in_window(conn, name, hours=24)
            in_flight += active_trace_counts.get(name, 0)
            auto_merge = _compute_auto_merge_rate(conn, name, window_days=30)
            out.append(
                {
                    "id": name,
                    "name": display,
                    "sample": sample,
                    "in_flight": in_flight,
                    "completed_24h": completed_24h,
                    "fpa": metrics.get("first_pass_acceptance_rate"),
                    "escape": metrics.get("defect_escape_rate"),
                    "catch": metrics.get("self_review_catch_rate"),
                    "auto_merge": auto_merge,
                }
            )
        # Sort by in-flight DESC, then name ASC — busiest profiles first.
        out.sort(key=lambda p: (-p["in_flight"], p["name"]))
        return {"profiles": out}
    finally:
        conn.close()


# --- Traces ---------------------------------------------------------------

# The tracer exposes a free-form status vocabulary ("Processing", "PR
# Created", "CI Fix", "Enriched", etc.). The operator dashboard wants a
# much coarser in-flight / stuck / queued / done partition so filter
# chips work. This table is the single source of truth; update it here
# when tracer adds a new status.
_STATUS_TO_BUCKET: dict[str, str] = {
    # Terminal successes + terminal failures both fall in "done" so the
    # board clears them out of the operator's immediate attention.
    "Complete": "done",
    "Merged": "done",
    "Closed": "done",
    "Failed": "done",
    "Timed Out": "done",
    "Cleaned Up": "done",
    "Suppressed": "hidden",
    "Misfire": "hidden",
    # Webhook deliberately skipped (no ai-implement tag) or manually routed
    # outside the pipeline — nothing will happen, clear the board.
    "Skipped": "done",
    "Submitted": "done",
    # Agent work finished — PR exists or pipeline reached a human-review
    # gate. No further pipeline action expected; move off the active board.
    "PR Created": "done",
    "Review Done": "done",
    "QA Done": "done",
    # Stuck — the pipeline is alive but can't progress without help.
    "CI Fix": "stuck",
    "Agent Done (no PR)": "stuck",
    "Escalated": "stuck",
    "Stale": "stuck",
    # Queued — before anything started.
    "Received": "queued",
    "Enriched": "queued",
    # Active pipeline work.
    "Processing": "in-flight",
    "Dispatched": "in-flight",
    "Planned": "in-flight",
    "Implementing": "in-flight",
    "Reviewing": "in-flight",
    "QA Running": "in-flight",
}

# Tickets whose derived status is still "in-flight" after this long with
# no new trace activity are reclassified as "stuck". Agents running for
# more than 2 hours without writing a new event almost certainly died.
_STALE_INFLIGHT_HOURS = 2
_MAX_INFLIGHT_RUN_HOURS = 24

_PR_HIDDEN_STATES = frozenset({"suppressed", "misfire"})
_PR_DONE_STATES = frozenset({"merged", "closed"})
_PR_STALE_STATES = frozenset({"stale", "escalated"})

_TRACE_PROGRESS_EVENTS = frozenset(
    {
        "Pipeline started",
        "processing_started",
        "processing_completed",
        "l2_dispatched",
        "agent_finished",
        "Pipeline complete",
    }
)


def _normalize_trace_status(status: str) -> str:
    """Map tracer status → operator-bucket (in-flight/stuck/queued/done)."""
    return _STATUS_TO_BUCKET.get(status, "in-flight")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _last_progress_at(t: dict[str, Any]) -> datetime | None:
    """Return last forward-progress timestamp, ignoring duplicate chatter."""
    entries = t.get("_raw_entries")
    run_start_idx = int(t.get("_run_start_idx") or 0)
    if not isinstance(entries, list):
        return _parse_iso(str(t.get("completed_at") or ""))
    for entry in reversed(entries[run_start_idx:]):
        event = str(entry.get("event") or "")
        phase = str(entry.get("phase") or "")
        if (
            entry.get("source") == "agent"
            or event in _TRACE_PROGRESS_EVENTS
            or phase in ("pipeline", "l2_dispatch")
        ):
            return _parse_iso(str(entry.get("timestamp") or ""))
    return _parse_iso(str(t.get("completed_at") or ""))


def _apply_trace_staleness(bucket: str, t: dict[str, Any]) -> str:
    """Reclassify silent active/queued trace rows as stuck."""
    if bucket not in ("in-flight", "queued"):
        return bucket

    now = datetime.now(UTC)
    last_progress = _last_progress_at(t)
    if last_progress is not None:
        age_hours = (now - last_progress).total_seconds() / 3600
        if age_hours > _STALE_INFLIGHT_HOURS:
            return "stuck"

    run_started = _parse_iso(str(t.get("run_started_at") or t.get("started_at") or ""))
    if run_started is not None:
        run_age_hours = (now - run_started).total_seconds() / 3600
        if run_age_hours > _MAX_INFLIGHT_RUN_HOURS:
            return "stuck"

    return bucket


def _shape_trace_row(
    t: dict[str, Any],
    *,
    suppression: Any | None = None,
    pr_state: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Reshape a tracer.list_traces row to the operator dashboard contract."""
    raw_status = str(t.get("status", ""))
    bucket = _normalize_trace_status(raw_status)
    state_reason = ""

    if suppression is not None and bucket != "hidden":
        raw_status = "Suppressed"
        bucket = "hidden"
        state_reason = str(suppression["reason"] or "")

    if pr_state:
        lifecycle_state = pr_state.get("state", "")
        if lifecycle_state in _PR_DONE_STATES:
            raw_status = "Merged" if lifecycle_state == "merged" else "Closed"
            bucket = "done"
            state_reason = pr_state.get("state_reason", "") or state_reason
        elif lifecycle_state in _PR_HIDDEN_STATES:
            raw_status = "Misfire" if lifecycle_state == "misfire" else "Suppressed"
            bucket = "hidden"
            state_reason = pr_state.get("state_reason", "") or state_reason
        elif lifecycle_state in _PR_STALE_STATES and bucket == "in-flight":
            raw_status = "Stale"
            bucket = "stuck"
            state_reason = pr_state.get("state_reason", "") or state_reason

    # Staleness override: a ticket still reporting "in-flight" or "queued"
    # whose last meaningful progress is old, or whose current run exceeded
    # the max runtime, is almost certainly dead. Late duplicate review/webhook
    # events must not keep ancient work active.
    bucket = _apply_trace_staleness(bucket, t)

    return {
        "id": t["ticket_id"],
        "title": t.get("ticket_title") or "",
        "status": bucket,
        "raw_status": raw_status,
        "hidden": bucket == "hidden",
        "lifecycle_state": pr_state.get("state", "") if pr_state else "",
        "state_reason": state_reason,
        "run_id": t.get("run_id") or t.get("trace_id", ""),
        "phase": t.get("current_phase", ""),
        "elapsed": t.get("duration", ""),
        "started_at": t.get("run_started_at") or t.get("started_at", ""),
        "pr_url": t.get("pr_url") or None,
        "pipeline_mode": t.get("pipeline_mode", ""),
        "review_verdict": t.get("review_verdict", ""),
        "qa_result": t.get("qa_result", ""),
    }


def _latest_pr_state_by_ticket(conn: Any) -> dict[str, dict[str, str]]:
    """Return newest pr_run state per ticket for trace-list shaping."""
    rows = conn.execute(
        "SELECT ticket_id, state, state_reason, updated_at, last_observed_at, id "
        "FROM pr_runs WHERE ticket_id != '' "
        "ORDER BY COALESCE(NULLIF(last_observed_at, ''), updated_at) DESC, id DESC"
    ).fetchall()
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        ticket_id = str(row["ticket_id"] or "")
        if ticket_id and ticket_id not in out:
            out[ticket_id] = {
                "state": str(row["state"] or ""),
                "state_reason": str(row["state_reason"] or ""),
            }
    return out


@router.get("/traces")
def get_traces(
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """Recent pipeline traces, newest first.

    Query params:
      * ``status`` — optional bucket filter (``in-flight`` | ``stuck`` |
        ``queued`` | ``done``). Unknown values yield zero rows.
      * ``limit`` — default 100, cap 500.
      * ``offset`` — for pagination.

    The source is tracer.list_traces (scans every JSONL under data/logs/).
    At scale this becomes expensive; mtime-sorted + limit provides
    adequate short-term pagination. A cache layer lands when list size
    exceeds ~2,500 runs (current scale: low hundreds).
    """
    capped_limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    raw = _list_traces(offset=0, limit=0)
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        suppressions = list_active_suppressions(conn, target_type="trace")
        pr_states = _latest_pr_state_by_ticket(conn)
    finally:
        conn.close()
    shaped = [
        _shape_trace_row(
            t,
            suppression=suppressions.get(str(t.get("ticket_id") or "")),
            pr_state=pr_states.get(str(t.get("ticket_id") or "")),
        )
        for t in raw
    ]
    if not include_hidden:
        shaped = [t for t in shaped if not t["hidden"]]
    status_counts = {
        "all": len(shaped),
        "in-flight": 0,
        "stuck": 0,
        "queued": 0,
        "done": 0,
        "hidden": 0,
    }
    for trace in shaped:
        trace_status = str(trace.get("status") or "")
        if trace_status in status_counts:
            status_counts[trace_status] += 1
    if status is not None:
        shaped = [t for t in shaped if t["status"] == status]
    page = shaped[offset : offset + capped_limit]
    return {
        "traces": page,
        "count": len(shaped),
        "status_counts": status_counts,
        "offset": offset,
        "limit": capped_limit,
        "include_hidden": include_hidden,
    }


# --- Autonomy ------------------------------------------------------------


@router.get("/autonomy/{profile}")
def get_autonomy(profile: str, window_days: int = 30) -> dict[str, Any]:
    """Per-profile autonomy report.

    Bundles together every metric the Autonomy view renders:
      * headline numbers (FPA / escape / catch / auto-merge)
      * daily trends for each of the four metrics
      * by-ticket-type breakdown
      * recent escaped defects (30d)

    One endpoint per view, one fetch per page load. The trend arrays
    reuse compute_daily_trend; auto-merge was added to that function in
    the same commit as this endpoint.
    """
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        metrics = compute_profile_metrics(conn, profile, window_days=window_days)
        auto_merge = _compute_auto_merge_rate(conn, profile, window_days=window_days)
        by_type = compute_ticket_type_breakdown(conn, profile, window_days=window_days)
        trend_fpa = compute_daily_trend(conn, profile, window_days, "fpa")
        trend_escape = compute_daily_trend(conn, profile, window_days, "defect_escape")
        trend_catch = compute_daily_trend(conn, profile, window_days, "catch_rate")
        trend_auto = compute_daily_trend(conn, profile, window_days, "auto_merge")

        # Recent escaped defects — list_confirmed_escaped_defects needs a
        # pr_run_ids list; pull all merged pr_runs in window and feed
        # them. Caller filters to the top-N most recent for display.
        pr_rows = list_pr_runs(conn, client_profile=profile)
        merged_ids = [int(r["id"]) for r in pr_rows if int(r["merged"])]
        escaped_rows = list_confirmed_escaped_defects(conn, merged_ids, window_days=30)
        escaped: list[dict[str, Any]] = []
        for r in escaped_rows[:20]:
            escaped.append(
                {
                    "id": f"D-{r['id']}",
                    "ticket_id": r["ticket_id"] or "",
                    "pr_number": int(r["pr_number"]) if r["pr_number"] else None,
                    "severity": str(r["severity"] or "").lower() or "minor",
                    "where": str(r["target_id"] or "")[:120],
                    "reported_at": r["reported_at"] or "",
                    "note": str(r["note"] or "")[:300],
                }
            )

        return {
            "profile": profile,
            "window_days": window_days,
            "metrics": {
                "fpa": metrics.get("first_pass_acceptance_rate"),
                "escape": metrics.get("defect_escape_rate"),
                "catch": metrics.get("self_review_catch_rate"),
                "auto_merge": auto_merge,
                "sample_size": metrics.get("sample_size", 0),
                "merged_count": metrics.get("merged_count", 0),
                "recommended_mode": metrics.get("recommended_mode", ""),
                "data_quality_status": metrics.get("data_quality_status", ""),
            },
            "trends": {
                "fpa": _shape_trend(trend_fpa),
                "escape": _shape_trend(trend_escape),
                "catch": _shape_trend(trend_catch),
                "auto_merge": _shape_trend(trend_auto),
            },
            "by_type": [
                {
                    "ticket_type": row["ticket_type"],
                    "volume": row["sample_size"],
                    "fpa": row["first_pass_acceptance_rate"],
                    "catch": row["self_review_catch_rate"],
                    "escape": row["defect_escape_rate"],
                    "merged": row["merged_count"],
                }
                for row in by_type
            ],
            "escaped": escaped,
        }
    finally:
        conn.close()


def _shape_trend(
    series: list[tuple[str, float | None, int]],
) -> list[dict[str, Any]]:
    """Turn compute_daily_trend tuples into JSON-friendly dicts."""
    return [{"date": d, "value": v, "sample": n} for (d, v, n) in series]


# --- Trace detail --------------------------------------------------------

# Fixed phase sequence the design renders as a 5-dot row. Runtime phase
# events map into this fixed set; unmapped phases fall into the closest
# bucket but are still returned in the raw timeline below.
_CANONICAL_PHASES: tuple[tuple[str, str], ...] = (
    ("planning", "Planning"),
    ("scaffolding", "Scaffolding"),
    ("implementing", "Implementing"),
    ("reviewing", "Reviewing"),
    ("merging", "Merging"),
)

_PHASE_ALIASES: dict[str, str] = {
    "analyst": "planning",
    "plan": "planning",
    "planner": "planning",
    "challenger": "planning",
    "risk_challenge": "planning",
    "pipeline": "scaffolding",
    "worktree": "scaffolding",
    "spawn": "scaffolding",
    "develop": "implementing",
    "developer": "implementing",
    "implement": "implementing",
    "implementation": "implementing",
    "security_scan": "reviewing",
    "code_review": "reviewing",
    "review": "reviewing",
    "reviewer": "reviewing",
    "judge": "reviewing",
    "qa": "reviewing",
    "qa_validation": "reviewing",
    "simplify": "reviewing",
    "reflection": "reviewing",
    "pr_review_spawned": "reviewing",
    "l3_pr_review": "reviewing",
    "l3_changes_requested": "reviewing",
    "l3_review": "reviewing",
    "l3_approval": "reviewing",
    "merge": "merging",
    "merge_coordinator": "merging",
    "pr": "merging",
    "pr_created": "merging",
    "complete": "merging",
}


def _canonical_phase(phase: str) -> str | None:
    """Map an agent phase label to one of the 5 canonical buckets, or
    None when the phase is outside the rendered pipeline (webhook,
    ticket_read, operator, artifact, completion).
    """
    p = phase.lower().replace("-", "_")
    if not p or p in ("webhook", "ticket_read", "operator", "artifact", "completion"):
        return None
    alias = _PHASE_ALIASES.get(p)
    if alias:
        return alias
    for canon, _label in _CANONICAL_PHASES:
        if canon in p:
            return canon
    return None


_L1_PRELUDE_EVENTS = {
    "manual_ticket_submitted",
    "webhook_received",
    "processing_started",
    "analyst_started",
    "analyst_completed",
    "l2_dispatched",
    "processing_completed",
}


def _timeline_start_idx(entries: list[dict[str, Any]], run_start_idx: int) -> int:
    """Include the same-run L1 prelude when rendering phase dots.

    Manual tickets often produce analyst/pipeline events before the
    agent-written run start. The detail timeline should show that
    planning/scaffolding work instead of resetting those buckets to
    pending as soon as the team lead starts writing its own events.
    """
    if run_start_idx <= 0 or run_start_idx >= len(entries):
        return run_start_idx
    run_trace = str(entries[run_start_idx].get("trace_id") or "")
    idx = run_start_idx
    while idx > 0:
        candidate = entries[idx - 1]
        if run_trace and str(candidate.get("trace_id") or "") not in {"", run_trace}:
            break
        event = str(candidate.get("event") or "")
        phase = _canonical_phase(str(candidate.get("phase") or ""))
        if event in _L1_PRELUDE_EVENTS or phase in {"planning", "scaffolding"}:
            idx -= 1
            continue
        break
    return idx


def _shape_trace_detail(
    ticket_id: str,
    entries: list[dict[str, Any]],
    *,
    suppression: Any | None = None,
    pr_state: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Reshape a full trace entry list into the operator detail payload.

    Returns:
      {
        id, title, status, raw_status, started_at, elapsed, pr_url,
        phases: [{ key, name, state, duration_seconds, event_count }],
        events: [{ t, ev, phase, msg }],   (latest 200 entries)
      }
    """
    if not entries:
        raise HTTPException(status_code=404, detail=f"trace not found: {ticket_id}")

    run_start_idx = find_run_start_idx(entries)
    run_entries = entries[run_start_idx:]
    timeline_entries = entries[_timeline_start_idx(entries, run_start_idx) :]
    metadata = _extract_trace_metadata_local(entries)
    raw_status = metadata["status"]
    bucket = _normalize_trace_status(raw_status)
    state_reason = ""
    if suppression is not None and bucket != "hidden":
        raw_status = "Suppressed"
        bucket = "hidden"
        state_reason = str(suppression["reason"] or "")
    if pr_state:
        lifecycle_state = pr_state.get("state", "")
        if lifecycle_state in _PR_DONE_STATES:
            raw_status = "Merged" if lifecycle_state == "merged" else "Closed"
            bucket = "done"
            state_reason = pr_state.get("state_reason", "") or state_reason
        elif lifecycle_state in _PR_HIDDEN_STATES:
            raw_status = "Misfire" if lifecycle_state == "misfire" else "Suppressed"
            bucket = "hidden"
            state_reason = pr_state.get("state_reason", "") or state_reason
        elif lifecycle_state in _PR_STALE_STATES and bucket == "in-flight":
            raw_status = "Stale"
            bucket = "stuck"
            state_reason = pr_state.get("state_reason", "") or state_reason

    bucket = _apply_trace_staleness(
        bucket,
        {
            "_raw_entries": entries,
            "_run_start_idx": run_start_idx,
            "run_started_at": metadata["started_at"],
            "started_at": metadata["started_at"],
            "completed_at": metadata["started_at"],
        },
    )

    # Phase durations + event counts, mapped into canonical buckets.
    durations = compute_phase_durations(entries, run_start_idx=run_start_idx)
    per_bucket_duration: dict[str, float] = {k: 0.0 for k, _ in _CANONICAL_PHASES}
    per_bucket_events: dict[str, int] = {k: 0 for k, _ in _CANONICAL_PHASES}
    for d in durations:
        canon = _canonical_phase(str(d.get("phase", "")))
        if canon is None:
            continue
        per_bucket_duration[canon] += float(d.get("duration_seconds") or 0.0)
        per_bucket_events[canon] += 1
    for e in timeline_entries:
        canon = _canonical_phase(str(e.get("phase", "")))
        if canon:
            per_bucket_events[canon] += 1

    # Derive per-phase state:
    #   done    — duration>0 AND a later phase has activity
    #   active  — currently-emitting phase (last agent-written phase)
    #   fail    — any error event in that phase
    #   pending — no activity yet
    current = _derive_current_phase_local(run_entries)
    current_canon = _canonical_phase(current) if current else None
    fail_phases: set[str] = set()
    for e in run_entries:
        if "error" in str(e.get("event", "")).lower() or e.get("level") == "error":
            canon = _canonical_phase(str(e.get("phase", "")))
            if canon:
                fail_phases.add(canon)

    # Ordered done detection: a phase is "done" when some later canonical
    # phase has ≥1 event AND the phase itself has ≥1 event.
    phase_order = [k for k, _ in _CANONICAL_PHASES]
    phases_out: list[dict[str, Any]] = []
    for i, (key, label) in enumerate(_CANONICAL_PHASES):
        if key in fail_phases:
            state = "fail"
        elif key == current_canon and bucket not in {"done", "hidden"}:
            state = "active"
        elif per_bucket_events[key] > 0:
            later_active = any(
                per_bucket_events[phase_order[j]] > 0 for j in range(i + 1, len(phase_order))
            )
            state = "done" if later_active or bucket in {"done", "hidden"} else "active"
        else:
            state = "pending"
        phases_out.append(
            {
                "key": key,
                "name": label,
                "state": state,
                "duration_seconds": round(per_bucket_duration[key], 1),
                "event_count": per_bucket_events[key],
            }
        )

    # Raw event stream — last 200 items, agent-filtered, shaped for the
    # design's 3-column log.
    event_rows: list[dict[str, Any]] = []
    for e in run_entries[-200:]:
        phase = str(e.get("phase", ""))
        canon = _canonical_phase(phase)
        event_rows.append(
            {
                "t": str(e.get("timestamp", "")),
                "ev": str(e.get("event", "")),
                "phase": canon or phase,
                "msg": _event_message(e),
            }
        )

    return {
        "id": ticket_id,
        "title": metadata["ticket_title"],
        "status": bucket,
        "raw_status": raw_status,
        "hidden": bucket == "hidden",
        "lifecycle_state": pr_state.get("state", "") if pr_state else "",
        "state_reason": state_reason,
        "run_id": run_entries[0].get("trace_id", "") if run_entries else "",
        "pipeline_mode": metadata["pipeline_mode"],
        "started_at": metadata["started_at"],
        "elapsed": metadata["elapsed"],
        "pr_url": metadata["pr_url"] or None,
        "review_verdict": metadata["review_verdict"],
        "qa_result": metadata["qa_result"],
        "phases": phases_out,
        "events": event_rows,
    }


def _extract_trace_metadata_local(
    entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Pull a compact summary off an entries list.

    Uses the same helpers list_traces uses so the detail + summary views
    agree on status vocabulary.
    """
    from tracer import (
        _compute_run_duration,
        _extract_trace_metadata,
        derive_trace_status,
    )

    meta = _extract_trace_metadata(entries)
    events = [e.get("event", "") for e in entries]
    status = derive_trace_status(entries, events, meta.get("pr_url", ""))
    run_start_idx = find_run_start_idx(entries)
    run_entries = entries[run_start_idx:]
    started_at = (
        run_entries[0].get("timestamp", "") if run_entries else entries[0].get("timestamp", "")
    )
    elapsed = _compute_run_duration(run_entries) if run_entries else ""
    return {
        "ticket_title": meta.get("ticket_title", "") or "",
        "pr_url": meta.get("pr_url", "") or "",
        "review_verdict": meta.get("review_verdict", "") or "",
        "qa_result": meta.get("qa_result", "") or "",
        "pipeline_mode": meta.get("pipeline_mode", "") or "",
        "status": status,
        "started_at": started_at,
        "elapsed": elapsed,
    }


def _derive_current_phase_local(run_entries: list[dict[str, Any]]) -> str:
    """Duplicate of tracer._derive_current_phase walking only run_entries.

    Keep in sync with tracer._derive_current_phase if that function grows
    new "non-agent phase to skip" cases (currently only ``ticket_read``).
    The fork exists because this variant takes an already-sliced
    ``run_entries`` list rather than recomputing the run start.
    """
    for e in reversed(run_entries):
        if e.get("source") == "agent":
            phase = str(e.get("phase", ""))
            if phase and phase != "ticket_read":
                return phase
    return ""


def _event_message(entry: dict[str, Any]) -> str:
    """Pull a human-friendly one-line message off a trace entry.

    Falls back through several field names the harness has used over
    time: ``message``, ``msg``, ``summary``, ``detail``.
    """
    for key in ("message", "msg", "summary", "detail"):
        val = entry.get(key)
        if val:
            return str(val)[:300]
    return ""


@router.get("/traces/{ticket_id}")
def get_trace_detail(ticket_id: str) -> dict[str, Any]:
    """Full trace timeline for one ticket.

    Reads the per-ticket JSONL via ``tracer.read_trace`` and reshapes
    into the design's 5-phase timeline + event stream.
    """
    entries = read_trace(ticket_id)
    if not entries:
        raise HTTPException(
            status_code=404,
            detail=f"trace not found: {ticket_id}",
        )
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        suppression = list_active_suppressions(conn, target_type="trace").get(ticket_id)
        pr_state = _latest_pr_state_by_ticket(conn).get(ticket_id)
    finally:
        conn.close()
    return _shape_trace_detail(
        ticket_id,
        entries,
        suppression=suppression,
        pr_state=pr_state,
    )


@router.post("/traces/{ticket_id}/state")
def post_trace_state(ticket_id: str, update: DashboardStateUpdate) -> dict[str, Any]:
    """Operator action: mark a trace suppressed, misfire, stale, or active."""
    state = update.state.lower()
    if state not in ("suppressed", "misfire", "stale", "open"):
        raise HTTPException(
            status_code=400,
            detail="state must be one of: suppressed, misfire, stale, open",
        )

    entries = read_trace(ticket_id)
    trace_id = str(entries[-1].get("trace_id") or "") if entries else generate_trace_id()
    event_by_state = {
        "suppressed": "trace_suppressed",
        "misfire": "trace_marked_misfire",
        "stale": "trace_marked_stale",
        "open": "trace_unsuppressed",
    }

    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        if state in ("suppressed", "misfire"):
            suppression_id = suppress_dashboard_target(
                conn,
                target_type="trace",
                target_id=ticket_id,
                reason=update.reason,
                payload={
                    "state": state,
                    "exclude_metrics": update.exclude_metrics,
                },
            )
        else:
            suppression_id = clear_dashboard_suppression(
                conn,
                target_type="trace",
                target_id=ticket_id,
                reason=update.reason,
            )

        affected_pr_runs = set_pr_runs_lifecycle_state_for_ticket(
            conn,
            ticket_id,
            state=state,
            reason=update.reason,
            exclude_metrics=(
                update.exclude_metrics if state in ("suppressed", "misfire") else False
            ),
            only_active=state not in ("open",),
            source_states=(("suppressed", "misfire", "stale") if state == "open" else None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()

    append_trace(
        ticket_id,
        trace_id,
        "operator",
        event_by_state[state],
        source="operator",
        state=state,
        reason=update.reason,
        exclude_metrics=update.exclude_metrics,
    )

    return {
        "status": "accepted",
        "ticket_id": ticket_id,
        "state": state,
        "suppression_id": suppression_id,
        "affected_pr_runs": affected_pr_runs,
    }


@router.post("/pr-runs/{pr_run_id}/state")
def post_pr_run_state(pr_run_id: int, update: DashboardStateUpdate) -> dict[str, Any]:
    """Operator action: set lifecycle state for one pr_run."""
    state = update.state.lower()
    try:
        conn = open_connection(_db_path_from_settings())
        try:
            ensure_schema(conn)
            ok = set_pr_run_lifecycle_state(
                conn,
                pr_run_id,
                state=state,
                reason=update.reason,
                exclude_metrics=update.exclude_metrics,
            )
        finally:
            conn.close()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"pr_run {pr_run_id} not found")
    return {"status": "accepted", "pr_run_id": pr_run_id, "state": state}


def _entry_pr_matches(entry: dict[str, Any], pr_number: int) -> bool:
    raw = entry.get("pr_number")
    if raw in (None, ""):
        return True
    try:
        return int(str(raw)) == pr_number
    except (TypeError, ValueError):
        return False


def _latest_trace_lifecycle_decision(
    *,
    pr_run_id: int,
    ticket_id: str,
    pr_number: int,
    current_state: str,
) -> _TraceLifecycleDecision | None:
    entries = read_trace(ticket_id)
    candidates: list[tuple[int, str, str]] = []
    for index, entry in enumerate(entries):
        if not _entry_pr_matches(entry, pr_number):
            continue
        event = str(entry.get("event") or "")
        event_at = str(entry.get("timestamp") or "")
        if event == "pr_merged":
            candidates.append((index, "merged", event_at))
        elif event == "pr_closed":
            candidates.append((index, "closed", event_at))
        elif current_state != "stale" and event == "review_approved":
            candidates.append((index, "reviewed", event_at))
        elif current_state != "stale" and event in (
            "review_changes_requested",
            "changes_requested_spawned",
        ):
            candidates.append((index, "needs_changes", event_at))
    if not candidates:
        return None

    terminal = [c for c in candidates if c[1] in ("merged", "closed")]
    index, state, event_at = max(terminal or candidates, key=lambda c: c[0])
    event = str(entries[index].get("event") or "")
    return _TraceLifecycleDecision(
        state=state,
        event=event,
        event_at=event_at,
        pr_run_id=pr_run_id,
    )


def _reconcile_pr_run_lifecycle_from_traces(
    conn: Any,
    *,
    dry_run: bool,
) -> tuple[int, set[int]]:
    rows = conn.execute(
        "SELECT id, ticket_id, pr_number, state FROM pr_runs "
        "WHERE ticket_id != '' "
        "AND state NOT IN ('merged', 'closed', 'suppressed', 'misfire')"
    ).fetchall()
    count = 0
    terminal_ids: set[int] = set()
    for row in rows:
        current_state = str(row["state"] or "open")
        decision = _latest_trace_lifecycle_decision(
            pr_run_id=int(row["id"]),
            ticket_id=str(row["ticket_id"]),
            pr_number=int(row["pr_number"]),
            current_state=current_state,
        )
        if decision is None or decision.state == current_state:
            continue
        if decision.state in ("merged", "closed"):
            terminal_ids.add(decision.pr_run_id)
        count += 1
        if dry_run:
            continue
        set_pr_run_lifecycle_state(
            conn,
            decision.pr_run_id,
            state=decision.state,
            reason=f"reconciled from trace event {decision.event}",
            created_by="operator_dashboard",
            exclude_metrics=False,
            terminal_at=decision.event_at if decision.state in ("merged", "closed") else None,
        )
    return count, terminal_ids


def _count_stale_pr_runs_after_trace_reconcile(
    conn: Any,
    *,
    older_than_iso: str,
    exclude_ids: set[int],
) -> int:
    placeholders = ",".join("?" * len(ACTIVE_PR_RUN_STATES))
    params: list[object] = [
        *sorted(ACTIVE_PR_RUN_STATES),
        older_than_iso,
        older_than_iso,
    ]
    exclude_clause = ""
    if exclude_ids:
        exclude_placeholders = ",".join("?" * len(exclude_ids))
        exclude_clause = f"AND id NOT IN ({exclude_placeholders}) "
        params.extend(sorted(exclude_ids))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pr_runs "
        f"WHERE state IN ({placeholders}) "
        "AND merged = 0 AND closed_at = '' AND suppressed_at = '' "
        "AND COALESCE(NULLIF(last_observed_at, ''), opened_at) < ? "
        "AND opened_at < ? "
        f"{exclude_clause}",
        params,
    ).fetchone()
    return int(row["n"] if row else 0)


@router.post("/dashboard/reconcile-stale")
def post_dashboard_reconcile_stale(
    request: DashboardCleanupRequest,
) -> dict[str, Any]:
    """Backfill trace lifecycle, then mark old active pr_runs stale."""
    cutoff = (datetime.now(UTC) - timedelta(hours=request.stale_after_hours)).isoformat()
    reason = f"no lifecycle observation since {cutoff}"
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        lifecycle_count, terminal_ids = _reconcile_pr_run_lifecycle_from_traces(
            conn,
            dry_run=request.dry_run,
        )
        if request.dry_run:
            count = _count_stale_pr_runs_after_trace_reconcile(
                conn,
                older_than_iso=cutoff,
                exclude_ids=terminal_ids,
            )
        else:
            count = mark_stale_pr_runs(
                conn,
                older_than_iso=cutoff,
                reason=reason,
                created_by="operator_dashboard",
                dry_run=False,
            )
    finally:
        conn.close()
    return {
        "status": "dry_run" if request.dry_run else "accepted",
        "stale_after_hours": request.stale_after_hours,
        "lifecycle_reconciled": lifecycle_count,
        "matched": count,
    }


# --- PR drilldown --------------------------------------------------------


def _severity_order(severity: str) -> int:
    """Sort severities in descending importance for the design's
    "most serious first" list."""
    s = severity.lower()
    return {"critical": 0, "blocker": 1, "major": 2, "minor": 3, "info": 4}.get(s, 5)


# --- Agent roster (per-ticket) ------------------------------------------


def _last_event_time(path: Path) -> datetime | None:
    """Tail the last JSONL line and pull its timestamp.

    Efficient enough for the scale we're at (single ticket's file, ~KB
    to low-MB). Returns None on any parse error — callers treat that
    as "no recent activity".
    """
    try:
        with path.open("rb") as fh:
            # Seek near the end and read the last ~4 KB; good enough for
            # a JSONL file where one line is under 2 KB.
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        last = json.loads(lines[-1])
    except (ValueError, TypeError):
        return None
    ts = last.get("timestamp") or last.get("started_at") or last.get("t")
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _roster_state(last: datetime | None) -> str:
    """Derive a human-friendly state from last-event-age.

    <= 60s since last event → running
    <= 5 min                → idle
    > 5 min or None         → stale
    """
    if last is None:
        return "stale"
    age = (datetime.now(UTC) - last).total_seconds()
    if age <= 60:
        return "running"
    if age <= 5 * 60:
        return "idle"
    return "stale"


def _parse_roster_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _trace_archive_for_ticket(ticket_id: str) -> Path | None:
    for entry in reversed(read_trace(ticket_id)):
        raw_path = entry.get("worktree_path") or entry.get("worktree")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        worktree_path = Path(raw_path).expanduser().resolve()
        for parent in worktree_path.parents:
            if parent.name == "worktrees":
                candidate = parent.parent / "trace-archive" / ticket_id
                if candidate.is_dir():
                    return candidate
                break
    return None


def _client_readiness_path(ticket_id: str) -> tuple[Path | None, str]:
    worktree = _worktree_root_for_ticket(ticket_id)
    if worktree is not None:
        path = worktree / ".harness" / "client-readiness.json"
        if path.is_file():
            return path, "worktree"
    archive = _trace_archive_for_ticket(ticket_id)
    if archive is not None:
        path = archive / "client-readiness.json"
        if path.is_file():
            return path, "archive"
    return None, ""


@router.get("/tickets/{ticket_id}/readiness")
def get_ticket_readiness(ticket_id: str) -> dict[str, Any]:
    """Client readiness notes written by spawn_team for a ticket run."""
    path, source = _client_readiness_path(ticket_id)
    if path is None:
        return {
            "ticket_id": ticket_id,
            "available": False,
            "source": "",
            "generated_by": "",
            "client_profile": "",
            "is_next": False,
            "warning_count": 0,
            "warnings": [],
        }
    data = _read_json_file(path) or {}
    warnings = data.get("warnings")
    warnings = warnings if isinstance(warnings, list) else []
    return {
        "ticket_id": ticket_id,
        "available": True,
        "source": source,
        "generated_by": str(data.get("generated_by") or ""),
        "client_profile": str(data.get("client_profile") or ""),
        "is_next": bool(data.get("is_next")),
        "warning_count": int(data.get("warning_count") or len(warnings)),
        "warnings": warnings,
    }


@router.get("/tickets/{ticket_id}/agents")
def get_ticket_agents(ticket_id: str) -> dict[str, Any]:
    """Agent roster for one ticket.

    Walks the ticket's worktree via live_stream._find_session_streams and
    classifies each teammate by the freshness of their session-stream.jsonl
    tail. Returns:

      {
        "agents": [
          { "teammate", "state" (running|idle|stale), "last_at" (iso|None) },
          ...
        ]
      }

    When the ticket has no active worktree (pipeline not spawned, or
    already cleaned up), returns an empty list — the view can render
    "no agents spawned yet".
    """
    worktree = _worktree_root_for_ticket(ticket_id)
    if worktree is None:
        return {"agents": []}
    streams = _find_session_streams(worktree)
    agents: list[dict[str, Any]] = []
    for teammate, path in streams:
        for summary in summarize_stream_teammates(teammate, path):
            last = _parse_roster_time(summary.get("last_at"))
            if last is not None:
                summary["last_at"] = last.isoformat()
            # Fall back to the older raw-tail timestamp path for physical
            # streams that have timestamps but no displayable rows. Virtual
            # subagents without rows should stay timestamp-less.
            if last is None and summary.get("teammate") == teammate:
                last = _last_event_time(path)
                summary["last_at"] = last.isoformat() if last else None
            summary["state"] = _roster_state(last)
            agents.append(summary)
    return {"agents": agents}


@router.get("/tickets/{ticket_id}/activity-summary")
def get_ticket_activity_summary(ticket_id: str) -> dict[str, Any]:
    """De-duplicated activity summary for completed/stale ticket review."""
    worktree = _worktree_root_for_ticket(ticket_id)
    if worktree is None:
        return {
            "ticket_id": ticket_id,
            "summary": "No live activity stream is available for this ticket.",
            "raw_event_count": 0,
            "deduped_event_count": 0,
            "teammates": [],
            "highlights": [],
            "warnings": [],
        }
    streams = _find_session_streams(worktree)
    finished_events = collect_finished_activity(worktree)
    if not streams and not finished_events:
        return {
            "ticket_id": ticket_id,
            "summary": "No session-stream files are available for this ticket.",
            "raw_event_count": 0,
            "deduped_event_count": 0,
            "teammates": [],
            "highlights": [],
            "warnings": [],
        }
    return summarize_ticket_activity(
        ticket_id,
        streams,
        finished_events=finished_events,
    )


@router.get("/pr/{pr_run_id}")
def get_pr_detail(pr_run_id: int) -> dict[str, Any]:
    """Full PR drilldown.

    Returns:
      {
        pr_run_id, ticket_id, pr_number, repo, pr_url, head_sha, branch,
        author, client_profile, opened_at, merged, merged_at,
        commits: [{ sha, message, author_name, authored_at }],
        checks: [],                                  # ← placeholder; see below
        issues: [{ id, source, severity, category, summary, where,
                   line_start, matched_lesson_id, matched_confidence }],
        matches: [{ lesson_id, confidence, applied }],
        auto_merge: {
          decision, reason, confidence, payload  — most recent
        } | null,
      }

    CI check list is not persisted today (plan-review gap #7). We
    return [] so the frontend renders a "CI checks not ingested"
    banner rather than fake rows.
    """
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        pr_row = conn.execute("SELECT * FROM pr_runs WHERE id = ?", (pr_run_id,)).fetchone()
        if pr_row is None:
            raise HTTPException(status_code=404, detail=f"pr_run {pr_run_id} not found")

        commits = list_pr_commits(conn, pr_run_id)
        issues = list_review_issues_by_pr_run(conn, pr_run_id)
        # Issue-to-issue matches inside this PR (ai_review ↔ human_review
        # pairings), used to render the "reviewer caught it" column.
        issue_match_rows = conn.execute(
            "SELECT im.* FROM issue_matches im "
            "JOIN review_issues ri ON ri.id = im.human_issue_id "
            "WHERE ri.pr_run_id = ?",
            (pr_run_id,),
        ).fetchall()
        # Lesson matches come from lesson_evidence rows keyed by pr_run_id —
        # each row ties a lesson_candidate to this PR with a source_ref
        # that describes the hit.
        lesson_match_rows = conn.execute(
            "SELECT le.lesson_id, le.source_ref, le.snippet, "
            "       lc.status as lesson_status "
            "FROM lesson_evidence le "
            "LEFT JOIN lesson_candidates lc "
            "       ON lc.lesson_id = le.lesson_id "
            "WHERE le.pr_run_id = ?",
            (pr_run_id,),
        ).fetchall()

        # Most recent auto-merge decision for this PR (latest row).
        am_rows = conn.execute(
            "SELECT * FROM manual_overrides "
            "WHERE override_type = 'auto_merge_decision' "
            "AND target_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (f"{pr_row['repo_full_name']}#{int(pr_row['pr_number'])}",),
        ).fetchall()
        auto_merge: dict[str, Any] | None
        if am_rows:
            try:
                payload = json.loads(am_rows[0]["payload_json"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            auto_merge = {
                "decision": str(payload.get("decision") or ""),
                "reason": str(payload.get("reason") or ""),
                "confidence": payload.get("confidence"),
                "created_at": am_rows[0]["created_at"],
                "gates": payload.get("gates") or {},
            }
        else:
            auto_merge = None

        match_by_issue: dict[int, dict[str, Any]] = {}
        for m in issue_match_rows:
            match_by_issue[int(m["human_issue_id"])] = {
                "ai_issue_id": int(m["ai_issue_id"]),
                "confidence": float(m["confidence"]),
                "matched_by": str(m["matched_by"] or ""),
            }

        issues_shaped = [
            {
                "id": int(issue["id"]),
                "source": str(issue["source"] or ""),
                "severity": str(issue["severity"] or "minor").lower(),
                "category": str(issue["category"] or ""),
                "summary": str(issue["summary"] or "")[:300],
                "where": str(issue["file_path"] or ""),
                "line_start": issue["line_start"],
                "matched": match_by_issue.get(int(issue["id"])),
            }
            for issue in issues
        ]
        issues_shaped.sort(key=lambda i: (_severity_order(i["severity"]), -int(i["id"])))

        # Collapse multiple lesson_evidence rows for the same lesson into one
        # "lesson match" row — the design shows one per lesson, not per hit.
        seen_lessons: dict[str, dict[str, Any]] = {}
        for m in lesson_match_rows:
            lid = str(m["lesson_id"])
            if lid in seen_lessons:
                continue
            seen_lessons[lid] = {
                "lesson_id": lid,
                "status": str(m["lesson_status"] or ""),
                "applied": str(m["lesson_status"] or "") == "applied",
                "source_ref": str(m["source_ref"] or ""),
                "snippet": str(m["snippet"] or "")[:200],
            }
        matches_shaped = list(seen_lessons.values())

        return {
            "pr_run_id": pr_run_id,
            "ticket_id": pr_row["ticket_id"] or "",
            "pr_number": int(pr_row["pr_number"]),
            "repo_full_name": pr_row["repo_full_name"] or "",
            "pr_url": pr_row["pr_url"] or "",
            "head_sha": pr_row["head_sha"] or "",
            "client_profile": pr_row["client_profile"] or "",
            "opened_at": pr_row["opened_at"] or "",
            "merged": bool(int(pr_row["merged"] or 0)),
            "merged_at": pr_row["merged_at"] or "",
            "closed_at": pr_row["closed_at"] or "",
            "lifecycle_state": pr_row["state"] or "",
            "state_reason": pr_row["state_reason"] or "",
            "excluded_from_metrics": bool(int(pr_row["excluded_from_metrics"] or 0)),
            "first_pass_accepted": bool(int(pr_row["first_pass_accepted"] or 0)),
            "commits": [
                {
                    "sha": c["commit_sha"],
                    "message": str(c["commit_message"] or "")[:200],
                    "author": c["commit_author"] or "",
                    "authored_at": c["authored_at"] or "",
                }
                for c in commits
            ],
            "issues": issues_shaped,
            "matches": matches_shaped,
            "auto_merge": auto_merge,
            "ci_checks_available": False,
        }
    finally:
        conn.close()


@router.get("/lessons/counts")
def get_lesson_counts() -> dict[str, Any]:
    """Counts of lesson candidates by state for the Home lesson-strip.

    Harness states are proposed / draft_ready / approved / applied /
    snoozed / rejected / reverted / stale. The design's strip shows 6
    cells mapped to the most operator-relevant transitions — we return
    the full 8 so the frontend can collapse display categories without
    a schema change here (draft_ready folds into "draft", reverted +
    stale are lifecycle states that rarely need surfacing).
    """
    conn = open_connection(_db_path_from_settings())
    try:
        ensure_schema(conn)
        all_rows = list_lesson_candidates(conn, limit=10_000)
        counts = {
            "proposed": 0,
            "draft_ready": 0,
            "approved": 0,
            "applied": 0,
            "snoozed": 0,
            "rejected": 0,
            "reverted": 0,
            "stale": 0,
        }
        for r in all_rows:
            status = str(r["status"] or "").lower()
            if status in counts:
                counts[status] += 1
        return {"counts": counts}
    finally:
        conn.close()


@router.delete(
    "/tickets/{ticket_id}/trigger-label",
    dependencies=[Depends(_require_dashboard_auth)],
)
async def remove_trigger_label(ticket_id: str) -> dict[str, Any]:
    """Remove configured trigger labels from a ticket in ADO.

    Looks up the client profile for this ticket to find the configured
    trigger labels, then calls AdoAdapter.remove_label. Also writes a comment
    so the ADO work item history shows who removed it and why.

    Returns {"removed": true, "labels": ["<label>"]} on success.
    Raises 404 if no profile is found for the ticket's project prefix.
    Raises 502 if the ADO call fails.
    """
    from claim_store import _clear_trigger_state
    from main import _get_ado_adapter
    from tracer import append_trace, generate_trace_id

    prefix = ticket_id.split("-")[0] if "-" in ticket_id else ticket_id
    profile = load_profile(prefix)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"No profile for project prefix '{prefix}'")

    labels = [label for label in dict.fromkeys([profile.ai_label, profile.quick_label]) if label]
    adapter = _get_ado_adapter()

    try:
        for label in labels:
            await adapter.remove_label(ticket_id, label)
        label_text = ", ".join(f"`{label}`" for label in labels)
        await adapter.write_comment(
            ticket_id,
            f"🤖 **Agentic Harness** — trigger label(s) {label_text} "
            f"removed via operator dashboard. "
            f"Re-add the label to trigger a new run.",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ADO write-back failed: {exc}") from exc

    _clear_trigger_state(ticket_id)
    append_trace(
        ticket_id,
        generate_trace_id(),
        "operator",
        "trigger_label_removed",
        source="operator",
        labels=labels,
    )

    return {
        "removed": True,
        "label": labels[0] if labels else "",
        "labels": labels,
        "ticket_id": ticket_id,
    }
