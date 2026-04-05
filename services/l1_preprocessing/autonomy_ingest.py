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
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError

from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    get_pr_run_by_unique,
    list_client_profiles,
    list_pr_runs,
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
    return pr_run_id


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


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
    # Fail-closed auth
    if not settings.l1_internal_api_token:
        raise HTTPException(status_code=503, detail="Internal API not configured")
    if not x_internal_api_token or x_internal_api_token != settings.l1_internal_api_token:
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    # Body size guard BEFORE parsing
    body = await request.body()
    if len(body) > settings.autonomy_internal_max_body_bytes:
        raise HTTPException(status_code=413, detail="Payload too large")

    # Rate limit
    if not _bucket.try_consume():
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

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


def _aggregate_profile(
    conn: sqlite3.Connection, profile: str, since_iso: str
) -> dict[str, Any]:
    rows = list_pr_runs(conn, client_profile=profile, since_iso=since_iso)
    sample_size = len(rows)
    merged_count = sum(1 for r in rows if int(r["merged"]) == 1)
    fpa_count = sum(1 for r in rows if int(r["first_pass_accepted"]) == 1)
    fpa_rate = (fpa_count / sample_size) if sample_size > 0 else 0.0
    return {
        "client_profile": profile,
        "sample_size": sample_size,
        "merged_count": merged_count,
        "first_pass_acceptance_rate": fpa_rate,
        "defect_escape_rate": 0.0,
        "self_review_catch_rate": None,
        "recommended_mode": "conservative",
        "data_quality": {
            "status": "phase1_partial",
            "notes": [
                "defect_escape_not_yet_computed",
                "self_review_catch_not_yet_computed",
            ],
        },
    }


@router.get("/api/autonomy")
async def get_autonomy(
    client_profile: str = "",
    window_days: int = 30,
) -> dict[str, Any]:
    """Return per-profile autonomy aggregates over a rolling window."""
    if window_days <= 0:
        raise HTTPException(status_code=400, detail="window_days must be positive")

    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    conn = _open_conn()
    try:
        if client_profile:
            return _aggregate_profile(conn, client_profile, cutoff)

        profiles = list_client_profiles(conn)
        per_profile = [_aggregate_profile(conn, p, cutoff) for p in profiles]
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
