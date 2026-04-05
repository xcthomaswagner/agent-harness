"""Autonomy event ingestion + aggregate read API.

Exposes an internal write endpoint for recording PR lifecycle events
(pr_opened, review_approved, etc.) and a read endpoint that computes
per-client-profile aggregates over a rolling window.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError

from autonomy_matching import match_human_issues_for_pr_run
from autonomy_metrics import compute_profile_metrics
from autonomy_store import (
    PrRunUpsert,
    drain_pending_ai_issues,
    ensure_schema,
    get_pr_run_by_unique,
    insert_review_issue,
    list_client_profiles,
    open_connection,
    resolve_db_path,
    upsert_pr_run,
)
from client_profile import find_profile_by_project_key
from config import settings

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------

class TokenBucket:
    """Thread-safe token bucket for request rate limiting."""

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self._capacity = float(capacity)
        self._refill_per_sec = float(refill_per_sec)
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(
            self._capacity, self._tokens + elapsed * self._refill_per_sec
        )
        self._last_refill = now

    def try_consume(self, cost: float = 1.0) -> bool:
        """Return True and deduct cost if tokens available; False otherwise."""
        with self._lock:
            self._refill()
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False


_bucket = TokenBucket(
    capacity=settings.autonomy_internal_rate_bucket_capacity,
    refill_per_sec=settings.autonomy_internal_rate_refill_per_sec,
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

EventType = Literal[
    "pr_opened",
    "pr_synchronized",
    "review_approved",
    "review_changes_requested",
    "review_comment",
    "pr_merged",
]


HumanIssueEventType = Literal[
    "review_comment",
    "review_changes_requested",
    "review_approved",
]


class HumanIssueIn(BaseModel):
    """A human-raised review issue — line-anchored comment or review body."""

    repo_full_name: str
    pr_number: int
    head_sha: str
    ticket_id: str
    client_profile: str = ""
    # Comment metadata
    external_id: str
    event_type: HumanIssueEventType
    file_path: str = ""
    line_start: int = 0
    line_end: int = 0
    summary: str
    details: str = ""
    reviewer_login: str = ""
    event_at: str
    comment_url: str = ""


class AutonomyEventIn(BaseModel):
    """Incoming autonomy event posted by L3 (or backfill tooling)."""

    event_type: EventType
    repo_full_name: str
    pr_number: int
    pr_url: str = ""
    head_ref: str = ""
    head_sha: str
    base_sha: str = ""
    ticket_id: str
    ticket_type: str = ""
    client_profile: str = ""
    event_at: str
    reviewer_login: str = ""
    review_id: str = ""
    comment_id: str = ""
    review_body: str = ""
    comment_url: str = ""
    merged_at: str = ""
    pipeline_mode: str = ""


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------

def resolve_client_profile(ticket_id: str, supplied: str) -> tuple[str, bool]:
    """Return (profile_name, degraded_flag).

    If `supplied` is non-empty, trust it and return it with degraded=False.
    Otherwise extract the project key prefix from `ticket_id` (before the
    first '-') and look up a client profile with a matching project_key.
    If resolution fails, return ("", True).
    """
    if supplied:
        return (supplied, False)
    if "-" not in ticket_id:
        return ("", True)
    project_key = ticket_id.split("-", 1)[0]
    if not project_key:
        return ("", True)
    profile = find_profile_by_project_key(project_key)
    if profile is None:
        return ("", True)
    return (profile.name, False)


# ---------------------------------------------------------------------------
# Event → pr_runs mapping
# ---------------------------------------------------------------------------

def apply_event(
    conn: sqlite3.Connection, event: AutonomyEventIn, client_profile: str
) -> int:
    """Apply an autonomy event to pr_runs; return the pr_run id.

    State transitions:
      * pr_opened: upsert with opened_at, client_profile, ticket_type
      * pr_synchronized: no-op upsert (ensures row exists, updates updated_at)
      * review_approved: set approved_at. If no prior row, set
        first_pass_accepted=1. If a prior row has first_pass_accepted=0
        already (meaning a changes_requested or other downgrade landed),
        leave it at 0.
      * review_changes_requested: set first_pass_accepted=0
      * review_comment: no-op upsert (Phase 2 will track follow-ups)
      * pr_merged: set merged=1 and merged_at
    """
    # Look up any existing row so we can make first-pass decisions.
    existing = get_pr_run_by_unique(
        conn, event.repo_full_name, event.pr_number, event.head_sha
    )

    # Base upsert carries identity + any fields we always want to refresh.
    upsert = PrRunUpsert(
        ticket_id=event.ticket_id,
        pr_number=event.pr_number,
        repo_full_name=event.repo_full_name,
        pr_url=event.pr_url,
        ticket_type=event.ticket_type,
        pipeline_mode=event.pipeline_mode,
        head_sha=event.head_sha,
        base_sha=event.base_sha,
        client_profile=client_profile,
    )

    et = event.event_type
    if et == "pr_opened":
        upsert.opened_at = event.event_at
    elif et == "pr_synchronized":
        # no-op on state; row existence ensured by upsert
        pass
    elif et == "review_approved":
        upsert.approved_at = event.event_at
        if existing is None:
            upsert.first_pass_accepted = 1
        else:
            # If we've previously downgraded to 0, keep it there.
            prior_fpa = int(existing["first_pass_accepted"])
            # Detect whether the 0 was actively set (row has been updated
            # since creation) versus the default on an insert that had no
            # approval signal. If updated_at == created_at AND fpa==0, then
            # no prior event landed that explicitly downgraded — treat as
            # first approval and flip to 1.
            was_updated = existing["updated_at"] != existing["created_at"]
            if prior_fpa == 0 and was_updated:
                # Already downgraded by prior event — leave it.
                pass
            else:
                upsert.first_pass_accepted = 1
    elif et == "review_changes_requested":
        upsert.first_pass_accepted = 0
    elif et == "review_comment":
        pass
    elif et == "pr_merged":
        upsert.merged = 1
        upsert.merged_at = event.merged_at or event.event_at

    pr_run_id = upsert_pr_run(conn, upsert)

    # Drain any pending AI issues that arrived via sidecar before the PR was
    # opened/synchronized. Idempotent — drain_pending_ai_issues checks for
    # existing (pr_run_id, source, external_id) rows. Wrapped so matching
    # failures never break webhook ingestion (L3 would retry otherwise).
    if et in ("pr_opened", "pr_synchronized"):
        try:
            drained = drain_pending_ai_issues(
                conn,
                repo_full_name=event.repo_full_name,
                head_sha=event.head_sha,
                ticket_id=event.ticket_id,
                pr_run_id=pr_run_id,
            )
            logger.info(
                "autonomy_pending_drained",
                pr_run_id=pr_run_id,
                count=drained,
                event_type=et,
            )
            if drained > 0:
                match_summary = match_human_issues_for_pr_run(conn, pr_run_id)
                logger.info(
                    "autonomy_match_retry_after_drain",
                    pr_run_id=pr_run_id,
                    **match_summary,
                )
        except Exception:
            logger.exception(
                "autonomy_drain_or_match_failed",
                pr_run_id=pr_run_id,
                event_type=et,
            )

    return pr_run_id


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


async def _guard_internal_request(
    request: Request, token_header: str | None
) -> bytes:
    """Shared auth/size/rate-limit guard for internal POST endpoints.

    Returns the request body bytes on success. Raises HTTPException for
    auth (503/401), size (413), or rate-limit (429) failures.
    """
    if not settings.l1_internal_api_token:
        raise HTTPException(status_code=503, detail="Internal API not configured")
    if not token_header or token_header != settings.l1_internal_api_token:
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    body = await request.body()
    if len(body) > settings.autonomy_internal_max_body_bytes:
        raise HTTPException(status_code=413, detail="Payload too large")
    if not _bucket.try_consume():
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    return body


def _upsert_human_issue(
    conn: sqlite3.Connection,
    *,
    pr_run_id: int,
    external_id: str,
    summary: str,
    details: str,
    file_path: str,
    line_start: int,
    line_end: int,
    is_code_change_request: int,
    source_ref: str,
) -> tuple[int, str]:
    """Insert-or-update a human_review review_issues row keyed on
    (pr_run_id, source='human_review', external_id).
    Returns (issue_id, action) where action is 'inserted' or 'updated'.
    """
    existing = conn.execute(
        "SELECT id FROM review_issues WHERE pr_run_id = ? "
        "AND source = 'human_review' AND external_id = ?",
        (pr_run_id, external_id),
    ).fetchone()
    if existing is not None:
        with conn:
            conn.execute(
                "UPDATE review_issues SET summary = ?, details = ?, "
                "file_path = ?, line_start = ?, line_end = ?, "
                "is_code_change_request = ?, source_ref = ? WHERE id = ?",
                (
                    summary,
                    details,
                    file_path,
                    line_start,
                    line_end,
                    int(is_code_change_request),
                    source_ref,
                    existing["id"],
                ),
            )
        return int(existing["id"]), "updated"

    new_id = insert_review_issue(
        conn,
        pr_run_id=pr_run_id,
        source="human_review",
        external_id=external_id,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        summary=summary,
        details=details,
        status="open",
        source_ref=source_ref,
        is_valid=1,
        is_code_change_request=int(is_code_change_request),
    )
    return new_id, "inserted"


def _open_conn() -> sqlite3.Connection:
    db_path = resolve_db_path(settings.autonomy_db_path)
    conn = open_connection(db_path)
    ensure_schema(conn)
    return conn


@router.post("/api/internal/autonomy/events")
async def post_autonomy_event(
    request: Request,
    x_internal_api_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Ingest a single autonomy event from L3 (or backfill tooling).

    Auth: X-Internal-Api-Token must match settings.l1_internal_api_token.
    Fail-closed: if that setting is empty, returns 503.
    """
    body = await _guard_internal_request(request, x_internal_api_token)

    # Parse body
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")

    try:
        event = AutonomyEventIn(**data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Resolve client profile
    profile_name, degraded = resolve_client_profile(event.ticket_id, event.client_profile)
    if degraded:
        logger.warning(
            "autonomy_profile_resolution_degraded",
            ticket_id=event.ticket_id,
        )

    conn = _open_conn()
    try:
        pr_run_id = apply_event(conn, event, profile_name)
    finally:
        conn.close()

    logger.info(
        "autonomy_event_ingested",
        event_type=event.event_type,
        pr_run_id=pr_run_id,
        client_profile=profile_name,
        ticket_id=event.ticket_id,
    )

    return {
        "status": "accepted",
        "pr_run_id": pr_run_id,
        "client_profile": profile_name,
    }


@router.post("/api/internal/autonomy/human-issues")
async def post_autonomy_human_issue(
    request: Request,
    x_internal_api_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Ingest a human review issue (line comment or review body).

    Creates pr_run if missing, inserts or updates a review_issues row with
    source='human_review', then runs the matcher.
    """
    body = await _guard_internal_request(request, x_internal_api_token)

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")

    try:
        human = HumanIssueIn(**data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    profile_name, degraded = resolve_client_profile(
        human.ticket_id, human.client_profile
    )
    if degraded:
        logger.warning(
            "autonomy_profile_resolution_degraded",
            ticket_id=human.ticket_id,
        )

    is_code_change_request = (
        1 if human.event_type == "review_changes_requested" else 0
    )

    conn = _open_conn()
    try:
        # Ensure pr_run exists (stub if missing).
        existing = get_pr_run_by_unique(
            conn, human.repo_full_name, human.pr_number, human.head_sha
        )
        if existing is None:
            pr_run_id = upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id=human.ticket_id,
                    pr_number=human.pr_number,
                    repo_full_name=human.repo_full_name,
                    head_sha=human.head_sha,
                    client_profile=profile_name,
                    opened_at=human.event_at,
                ),
            )
        else:
            pr_run_id = int(existing["id"])

        issue_id, action = _upsert_human_issue(
            conn,
            pr_run_id=pr_run_id,
            external_id=human.external_id,
            summary=human.summary,
            details=human.details,
            file_path=human.file_path,
            line_start=human.line_start,
            line_end=human.line_end,
            is_code_change_request=is_code_change_request,
            source_ref=human.comment_url,
        )

        match_summary = match_human_issues_for_pr_run(conn, pr_run_id)
    finally:
        conn.close()

    logger.info(
        "autonomy_human_issue_ingested",
        pr_run_id=pr_run_id,
        human_issue_id=issue_id,
        action=action,
        event_type=human.event_type,
        match_summary=match_summary,
    )

    return {
        "status": "accepted",
        "human_issue_id": issue_id,
        "pr_run_id": pr_run_id,
        "action": action,
        "client_profile": profile_name,
        "match_summary": match_summary,
    }


def _aggregate_profile(
    conn: sqlite3.Connection, profile: str, window_days: int
) -> dict[str, Any]:
    """Build the JSON-API shape for one profile from the shared metrics.

    recent_rows is omitted from the API response (it's a UI-only affordance
    of the HTML dashboard).
    """
    metrics = compute_profile_metrics(
        conn, profile, window_days, include_recent_rows=0
    )
    metrics.pop("recent_rows", None)
    # Nest data_quality for backward compatibility with prior response shape.
    metrics["data_quality"] = {
        "status": metrics.pop("data_quality_status"),
        "notes": metrics.pop("data_quality_notes"),
    }
    return metrics


@router.get("/api/autonomy")
async def get_autonomy(
    client_profile: str = "",
    window_days: int = 30,
) -> dict[str, Any]:
    """Return per-profile autonomy aggregates over a rolling window."""
    if window_days <= 0:
        raise HTTPException(status_code=400, detail="window_days must be positive")

    conn = _open_conn()
    try:
        if client_profile:
            return _aggregate_profile(conn, client_profile, window_days)

        profiles = list_client_profiles(conn)
        per_profile = [_aggregate_profile(conn, p, window_days) for p in profiles]
        total_sample = sum(p["sample_size"] for p in per_profile)
        return {
            "profiles": per_profile,
            "global_summary": {
                "profile_count": len(per_profile),
                "total_sample_size": total_sample,
                "window_days": window_days,
            },
        }
    finally:
        conn.close()
