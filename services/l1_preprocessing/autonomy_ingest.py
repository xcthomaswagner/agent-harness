"""Autonomy event ingestion + aggregate read API.

Exposes an internal write endpoint for recording PR lifecycle events
(pr_opened, review_approved, etc.) and a read endpoint that computes
per-client-profile aggregates over a rolling window.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import threading
import time
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError

from autonomy_attribution import attribute_human_issues_to_commits
from autonomy_jira_bug import NormalizedBug
from autonomy_matching import match_human_issues_for_pr_run
from autonomy_metrics import compute_profile_metrics
from autonomy_store import (
    PrRunUpsert,
    autonomy_conn,
    create_manual_match,
    drain_pending_ai_issues,
    find_latest_merged_pr_run_by_ticket,
    get_auto_merge_toggle,
    get_pr_run_by_unique,
    insert_defect_link,
    insert_manual_override,
    insert_pr_commit,
    insert_review_issue,
    list_client_profiles,
    list_human_issues_for_pr_run,
    list_pr_commits,
    list_recent_auto_merge_decisions,
    promote_match_to_counted,
    record_auto_merge_decision,
    record_defect_sweep_heartbeat,
    set_auto_merge_toggle,
    set_human_issue_code_change_flag,
    upsert_pr_run,
)
from client_profile import find_profile_by_project_key, find_profile_by_repo, load_profile
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
    "pr_closed",
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


class GithubDefectLinkIn(BaseModel):
    """Payload forwarded by L3 when a GitHub issue is labeled as a defect
    and its body references a previously-merged PR.
    """

    issue_number: int
    issue_url: str
    issue_title: str
    issue_body: str = ""
    labels: list[str] = []
    reported_at: str  # ISO 8601 (issue.created_at)
    reporter_login: str = ""
    # PR reference extracted by L3
    pr_repo_full_name: str  # "owner/repo" of the referenced PR
    pr_number: int
    # Classification hint from label
    category: Literal[
        "escaped", "feature_request", "pre_existing", "infra"
    ] = "escaped"
    severity: str = ""


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
    run_id: str = ""


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
      * pr_closed: set closed_at and terminal state without marking merged
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
        last_observed_at=event.event_at,
        run_id=event.run_id,
    )

    et = event.event_type
    # Track whether review_approved should compute first_pass_accepted via
    # the follow-up-commit signal (deferred until after the pr_run upsert).
    approval_pending = False
    prior_fpa_was_downgraded = False
    _changes_req_sentinel = False
    if et == "pr_opened":
        upsert.opened_at = event.event_at
        upsert.state = "open"
    elif et == "pr_synchronized":
        upsert.state = "open"
    elif et == "review_approved":
        upsert.approved_at = event.event_at
        upsert.state = "reviewed"
        if existing is not None:
            # Detect if a prior review_changes_requested explicitly
            # downgraded this PR. Two reliable signals:
            # 1. A review_issues row with is_code_change_request=1
            #    (created by the L3 human-issues endpoint when it
            #    forwards the changes_requested event).
            # 2. The pr_run's first_pass_accepted is 0 AND there
            #    are NO prior review_approved events (approved_at
            #    is empty or matches this event's timestamp).
            #    If approved_at is already set to a *different*
            #    timestamp, a prior approval happened — meaning fpa=0
            #    was set by an intervening changes_requested.
            #
            # Don't use updated_at — pr_synchronized also bumps it.
            changes_req_rows = conn.execute(
                "SELECT 1 FROM review_issues WHERE pr_run_id = ? "
                "AND source = 'human_review' AND is_code_change_request = 1 "
                "LIMIT 1",
                (int(existing["id"]),),
            ).fetchone()
            if changes_req_rows is not None:
                prior_fpa_was_downgraded = True
            elif (
                int(existing["first_pass_accepted"]) == 0
                and existing["approved_at"]
                and existing["approved_at"] != event.event_at
            ):
                # fpa=0 + a prior approval timestamp that isn't this one
                # → a changes_requested intervened between approvals
                prior_fpa_was_downgraded = True
        approval_pending = True
    elif et == "review_changes_requested":
        upsert.state = "needs_changes"
        upsert.state_reason = "review_changes_requested"
        upsert.first_pass_accepted = 0
        # Record a sentinel human_review issue so the approval path can
        # always detect this downgrade, even when L3 doesn't forward a
        # human issue (e.g., changes_requested with an empty review body).
        _changes_req_sentinel = True
    elif et == "review_comment":
        pass
    elif et == "pr_merged":
        terminal_at = event.merged_at or event.event_at
        upsert.state = "merged"
        upsert.merged = 1
        upsert.merged_at = terminal_at
        upsert.terminal_at = terminal_at
    elif et == "pr_closed":
        upsert.state = "closed"
        upsert.state_reason = "closed_without_merge"
        upsert.closed_at = event.event_at
        upsert.terminal_at = event.event_at

    pr_run_id = upsert_pr_run(conn, upsert)

    # Insert a sentinel human_review row for changes_requested so the
    # approval path can always detect the downgrade, even when L3 doesn't
    # forward a human issue (empty review body). Idempotent via external_id.
    if _changes_req_sentinel:
        ext_id = f"changes-req-{event.review_id or event.event_at}"
        existing_sentinel = conn.execute(
            "SELECT 1 FROM review_issues WHERE pr_run_id = ? "
            "AND source = 'human_review' AND external_id = ?",
            (pr_run_id, ext_id),
        ).fetchone()
        if existing_sentinel is None:
            insert_review_issue(
                conn,
                pr_run_id=pr_run_id,
                source="human_review",
                external_id=ext_id,
                summary="changes_requested",
                is_code_change_request=1,
                is_valid=1,
            )

    # Record commit history for follow-up-commit attribution. pr_opened and
    # pr_synchronized both carry the current head_sha + a timestamp; insert
    # is idempotent via UNIQUE (pr_run_id, sha).
    if et in ("pr_opened", "pr_synchronized") and event.head_sha:
        try:
            insert_pr_commit(
                conn,
                pr_run_id=pr_run_id,
                sha=event.head_sha,
                committed_at=event.event_at,
            )
        except Exception:
            logger.exception(
                "autonomy_pr_commit_insert_failed",
                pr_run_id=pr_run_id,
                event_type=et,
            )

    # On review_approved, use the follow-up-commit signal to decide
    # first_pass_accepted and flag human comments that triggered commits.
    if approval_pending:
        try:
            humans = list_human_issues_for_pr_run(conn, pr_run_id)
            commits = list_pr_commits(conn, pr_run_id)
            triggering_ids = attribute_human_issues_to_commits(
                [
                    {"id": int(h["id"]), "created_at": h["created_at"]}
                    for h in humans
                ],
                [
                    {"sha": c["sha"], "committed_at": c["committed_at"]}
                    for c in commits
                ],
                event.event_at,
            )
            for hid in triggering_ids:
                set_human_issue_code_change_flag(conn, hid, 1)
            # Prior changes_requested pins fpa=0; otherwise fpa=1 iff no
            # comment drove a follow-up commit.
            new_fpa = (
                0
                if prior_fpa_was_downgraded or triggering_ids
                else 1
            )
            upsert_pr_run(
                conn,
                PrRunUpsert(
                    ticket_id=event.ticket_id,
                    pr_number=event.pr_number,
                    repo_full_name=event.repo_full_name,
                    head_sha=event.head_sha,
                    first_pass_accepted=new_fpa,
                ),
            )
        except Exception:
            logger.exception(
                "autonomy_approval_attribution_failed",
                pr_run_id=pr_run_id,
            )

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
# Jira bug webhook ingestion
# ---------------------------------------------------------------------------

def _now_iso_utc() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


def ingest_jira_bug(
    conn: sqlite3.Connection, bug: NormalizedBug
) -> dict[str, Any]:
    """Record a Jira bug as a defect_link against the best-matching merged PR.

    Returns a dict with status: 'ignored' | 'deferred' | 'accepted'.
    """
    # Accept common defect issue type names across Jira configurations.
    # Projects may use "Bug", "Defect", custom types like "Production Bug", etc.
    _defect_types = {"bug", "defect", "incident", "production bug", "production defect"}
    if bug.issuetype.lower() not in _defect_types:
        return {
            "status": "ignored",
            "reason": "not_a_defect_type",
            "issuetype": bug.issuetype,
        }
    if not bug.candidate_parent_keys:
        return {
            "status": "ignored",
            "reason": "no_parent_link",
            "bug_key": bug.bug_key,
        }

    # Try candidates, pick the one with the latest merged_at
    best_pr_run: sqlite3.Row | None = None
    best_parent = ""
    for candidate in bug.candidate_parent_keys:
        row = find_latest_merged_pr_run_by_ticket(conn, candidate)
        if row is None:
            continue
        if best_pr_run is None or str(row["merged_at"] or "") > str(
            best_pr_run["merged_at"] or ""
        ):
            best_pr_run = row
            best_parent = candidate

    if best_pr_run is None:
        # Record for later reconciliation
        insert_manual_override(
            conn,
            override_type="unresolved_defect_link",
            target_id=bug.bug_key,
            payload_json=json.dumps(bug.model_dump()),
            created_by="jira_bug_webhook",
        )
        return {
            "status": "deferred",
            "reason": "no_merged_pr_for_candidates",
            "bug_key": bug.bug_key,
            "candidates": bug.candidate_parent_keys,
        }

    defect_id = insert_defect_link(
        conn,
        pr_run_id=int(best_pr_run["id"]),
        defect_key=bug.bug_key,
        source="jira",
        severity=bug.severity,
        reported_at=bug.created_at or _now_iso_utc(),
        confirmed=1 if bug.qa_confirmed else 0,
        notes=bug.summary[:500],
        category=bug.category,
    )
    insert_manual_override(
        conn,
        override_type="defect_link",
        target_id=str(defect_id),
        payload_json=json.dumps(
            {
                "source": "jira_webhook",
                "bug_key": bug.bug_key,
                "parent_ticket_id": best_parent,
                "category": bug.category,
            }
        ),
        created_by="jira_bug_webhook",
    )
    return {
        "status": "accepted",
        "defect_link_id": defect_id,
        "pr_run_id": int(best_pr_run["id"]),
        "parent_ticket_id": best_parent,
        "bug_key": bug.bug_key,
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


async def _guard_internal_request(
    request: Request, token_header: str | None
) -> bytes:
    """Shared auth/size/rate-limit guard for internal POST endpoints.

    Returns the request body bytes on success. Raises HTTPException for
    auth (503/401), size (413), or rate-limit (429) failures. Uses
    ``hmac.compare_digest`` for constant-time token comparison so the
    check does not leak byte-by-byte timing info about the configured
    secret via CPython's short-circuited ``!=`` on strings.
    """
    if not settings.l1_internal_api_token:
        raise HTTPException(status_code=503, detail="Internal API not configured")
    if not token_header or not hmac.compare_digest(
        token_header, settings.l1_internal_api_token
    ):
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

    with autonomy_conn() as conn:
        pr_run_id = apply_event(conn, event, profile_name)

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

    with autonomy_conn() as conn:
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


@router.post("/api/internal/autonomy/github-defect-link")
async def post_github_defect_link(
    request: Request,
    x_internal_api_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Record a defect_link from a GitHub issue labeled as a defect.

    L3 parses the issue body for a PR reference, then forwards the normalized
    payload here. We look up the matching merged pr_run by (repo, pr_number)
    and insert a defect_link with source='github' keyed on
    defect_key="gh-issue:<issue_number>".

    Fail-closed: 503 if L1_INTERNAL_API_TOKEN is unset.
    """
    body = await _guard_internal_request(request, x_internal_api_token)

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")

    try:
        payload = GithubDefectLinkIn(**data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    defect_key = f"gh-issue:{payload.issue_number}"

    with autonomy_conn() as conn:
        # Find the most recently merged pr_run matching (repo, pr_number).
        row = conn.execute(
            "SELECT * FROM pr_runs WHERE repo_full_name = ? AND pr_number = ? "
            "AND merged = 1 ORDER BY datetime(merged_at) DESC, id DESC LIMIT 1",
            (payload.pr_repo_full_name, payload.pr_number),
        ).fetchone()

        if row is None:
            # No merged PR found — record for later reconciliation.
            insert_manual_override(
                conn,
                override_type="unresolved_defect_link",
                target_id=defect_key,
                payload_json=json.dumps(payload.model_dump()),
                created_by="github_defect_webhook",
            )
            logger.info(
                "autonomy_github_defect_deferred",
                defect_key=defect_key,
                pr_repo=payload.pr_repo_full_name,
                pr_number=payload.pr_number,
            )
            return {
                "status": "deferred",
                "reason": "no_merged_pr_found",
                "defect_key": defect_key,
                "pr_repo_full_name": payload.pr_repo_full_name,
                "pr_number": payload.pr_number,
            }

        pr_run_id = int(row["id"])
        notes = (payload.issue_title or "")[:500]
        defect_id = insert_defect_link(
            conn,
            pr_run_id=pr_run_id,
            defect_key=defect_key,
            source="github",
            severity=payload.severity,
            reported_at=payload.reported_at or _now_iso_utc(),
            confirmed=1,
            notes=notes,
            category=payload.category,
        )
        insert_manual_override(
            conn,
            override_type="defect_link",
            target_id=str(defect_id),
            payload_json=json.dumps(
                {
                    "source": "github_webhook",
                    "issue_number": payload.issue_number,
                    "issue_url": payload.issue_url,
                    "labels": payload.labels,
                    "category": payload.category,
                }
            ),
            created_by="github_defect_webhook",
        )

    logger.info(
        "autonomy_github_defect_recorded",
        pr_run_id=pr_run_id,
        defect_link_id=defect_id,
        defect_key=defect_key,
        category=payload.category,
    )

    return {
        "status": "accepted",
        "defect_link_id": defect_id,
        "pr_run_id": pr_run_id,
        "defect_key": defect_key,
    }


async def _guard_admin_request(
    request: Request, token: str | None
) -> bytes:
    """Auth + payload size + rate-limit for admin endpoints.

    Returns the request body bytes on success. Raises HTTPException for
    auth (503/401), size (413), or rate-limit (429) failures. Uses
    ``hmac.compare_digest`` for constant-time token comparison to
    avoid leaking timing info about ``autonomy_admin_token``.
    """
    if not settings.autonomy_admin_token:
        raise HTTPException(status_code=503, detail="Admin API not configured")
    if not token or not hmac.compare_digest(
        token, settings.autonomy_admin_token
    ):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    body = await request.body()
    if len(body) > settings.autonomy_internal_max_body_bytes:
        raise HTTPException(status_code=413, detail="Payload too large")
    if not _bucket.try_consume():
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    return body


async def _guard_admin_read(
    x_autonomy_admin_token: str | None = Header(
        default=None, alias="x-autonomy-admin-token"
    ),
) -> None:
    """FastAPI dependency for read-only admin GET endpoints.

    Validates the admin token with the same constant-time comparison
    as ``_guard_admin_request`` but without reading the body or
    consuming the shared rate-limit bucket — reads don't carry a
    payload and shouldn't compete with writes for the limiter.

    Phase 1 left three GET routes unauthenticated
    (``/api/autonomy/auto-merge-toggle``,
    ``/api/autonomy/auto-merge-decisions``, and ``/api/autonomy``).
    Attaching this guard via ``dependencies=[Depends(_guard_admin_read)]``
    protects them consistently. 503 if the admin token isn't set on
    the service, 401 otherwise — same shape as the write guard so
    clients can reuse their error handling.
    """
    if not settings.autonomy_admin_token:
        raise HTTPException(status_code=503, detail="Admin API not configured")
    if not x_autonomy_admin_token or not hmac.compare_digest(
        x_autonomy_admin_token, settings.autonomy_admin_token
    ):
        raise HTTPException(status_code=401, detail="Invalid admin token")


class ManualDefectIn(BaseModel):
    """Payload for POST /api/autonomy/manual-defect."""

    # Lookup: use pr_run_id directly OR (repo, pr_number, head_sha).
    pr_run_id: int | None = None
    repo_full_name: str = ""
    pr_number: int | None = None
    head_sha: str = ""
    # Defect info
    defect_key: str
    source: Literal["manual", "jira", "github"] = "manual"
    severity: str = ""
    reported_at: str
    confirmed: bool = True
    notes: str = ""
    category: Literal[
        "escaped", "feature_request", "pre_existing", "infra"
    ] = "escaped"


class ManualMatchIn(BaseModel):
    """Payload for POST /api/autonomy/manual-match."""

    mode: Literal["promote", "create"]
    # promote mode
    match_id: int | None = None
    # create mode
    human_issue_id: int | None = None
    ai_issue_id: int | None = None


class DefectSweepHeartbeatIn(BaseModel):
    """Payload for POST /api/autonomy/defect-sweep-heartbeat."""

    client_profile: str
    swept_through: str  # ISO 8601


@router.post("/api/autonomy/manual-defect")
async def post_manual_defect(
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Create or update a defect_links row (admin-only)."""
    body = await _guard_admin_request(request, x_autonomy_admin_token)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=422, detail="Body must be a JSON object"
            )
        payload = ManualDefectIn(**data)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with autonomy_conn() as conn:
        if payload.pr_run_id is not None:
            pr_run_id = payload.pr_run_id
            exists = conn.execute(
                "SELECT 1 FROM pr_runs WHERE id = ?", (pr_run_id,)
            ).fetchone()
            if exists is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"pr_run_id={pr_run_id} not found",
                )
        else:
            if not (
                payload.repo_full_name
                and payload.pr_number is not None
                and payload.head_sha
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Must provide pr_run_id or "
                        "(repo_full_name + pr_number + head_sha)"
                    ),
                )
            row = get_pr_run_by_unique(
                conn,
                payload.repo_full_name,
                payload.pr_number,
                payload.head_sha,
            )
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail="pr_run not found for given (repo, pr_number, head_sha)",
                )
            pr_run_id = int(row["id"])

        defect_id = insert_defect_link(
            conn,
            pr_run_id=pr_run_id,
            defect_key=payload.defect_key,
            source=payload.source,
            severity=payload.severity,
            reported_at=payload.reported_at,
            confirmed=1 if payload.confirmed else 0,
            notes=payload.notes,
            category=payload.category,
        )
        insert_manual_override(
            conn,
            override_type="defect_link",
            target_id=str(defect_id),
            payload_json=json.dumps(payload.model_dump()),
        )

    logger.info(
        "autonomy_manual_defect_recorded",
        pr_run_id=pr_run_id,
        defect_link_id=defect_id,
        defect_key=payload.defect_key,
        category=payload.category,
    )

    return {
        "status": "accepted",
        "defect_link_id": defect_id,
        "pr_run_id": pr_run_id,
    }


@router.post("/api/autonomy/manual-match")
async def post_manual_match(
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Promote a suggested match or create a manual match (admin-only)."""
    body = await _guard_admin_request(request, x_autonomy_admin_token)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=422, detail="Body must be a JSON object"
            )
        payload = ManualMatchIn(**data)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with autonomy_conn() as conn:
        if payload.mode == "promote":
            if payload.match_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="promote mode requires match_id",
                )
            ok = promote_match_to_counted(conn, match_id=payload.match_id)
            if not ok:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"match_id={payload.match_id} not found or not in "
                        "'suggested' state"
                    ),
                )
            logger.info(
                "autonomy_manual_match_promoted",
                match_id=payload.match_id,
            )
            return {
                "status": "accepted",
                "mode": "promote",
                "match_id": payload.match_id,
            }

        # mode == "create"
        if payload.human_issue_id is None or payload.ai_issue_id is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "create mode requires human_issue_id and ai_issue_id"
                ),
            )
        try:
            match_id = create_manual_match(
                conn,
                human_issue_id=payload.human_issue_id,
                ai_issue_id=payload.ai_issue_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        logger.info(
            "autonomy_manual_match_created",
            match_id=match_id,
            human_issue_id=payload.human_issue_id,
            ai_issue_id=payload.ai_issue_id,
        )
        return {
            "status": "accepted",
            "mode": "create",
            "match_id": match_id,
        }


@router.post("/api/autonomy/defect-sweep-heartbeat")
async def post_defect_sweep_heartbeat(
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Record operator's 'I've reviewed defects through T' marker (admin-only)."""
    body = await _guard_admin_request(request, x_autonomy_admin_token)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=422, detail="Body must be a JSON object"
            )
        payload = DefectSweepHeartbeatIn(**data)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with autonomy_conn() as conn:
        override_id = record_defect_sweep_heartbeat(
            conn,
            client_profile=payload.client_profile,
            swept_through_iso=payload.swept_through,
        )

    logger.info(
        "autonomy_defect_sweep_heartbeat_recorded",
        client_profile=payload.client_profile,
        swept_through=payload.swept_through,
        override_id=override_id,
    )

    return {
        "status": "accepted",
        "client_profile": payload.client_profile,
        "swept_through": payload.swept_through,
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


# ---------------------------------------------------------------------------
# Phase 4: auto-merge decisions + kill-switch toggle + repo → profile
# ---------------------------------------------------------------------------

class AutoMergeDecisionIn(BaseModel):
    """Payload for POST /api/internal/autonomy/auto-merge-decisions."""

    repo_full_name: str
    pr_number: int
    head_sha: str
    ticket_id: str = ""
    client_profile: str = ""
    recommended_mode: str = ""
    ticket_type: str = ""
    decision: Literal["merged", "skipped", "dry_run", "failed"]
    reason: str
    gates: dict[str, bool] = {}
    dry_run: bool = False
    evaluated_at: str


class AutoMergeToggleIn(BaseModel):
    """Payload for POST /api/autonomy/auto-merge-toggle."""

    client_profile: str
    enabled: bool
    created_by: str = "admin"


@router.post("/api/internal/autonomy/auto-merge-decisions")
async def post_auto_merge_decision(
    request: Request,
    x_internal_api_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Log an auto-merge decision from L3. Internal-auth gated."""
    body = await _guard_internal_request(request, x_internal_api_token)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=422, detail="Body must be a JSON object"
            )
        payload = AutoMergeDecisionIn(**data)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with autonomy_conn() as conn:
        decision_id = record_auto_merge_decision(
            conn,
            repo_full_name=payload.repo_full_name,
            pr_number=payload.pr_number,
            decision=payload.decision,
            reason=payload.reason,
            payload=payload.model_dump(),
            created_by="l3_auto_merge",
        )

    logger.info(
        "autonomy_auto_merge_decision_recorded",
        repo_full_name=payload.repo_full_name,
        pr_number=payload.pr_number,
        decision=payload.decision,
        reason=payload.reason,
        decision_id=decision_id,
    )
    return {"status": "accepted", "decision_id": decision_id}


@router.post("/api/autonomy/auto-merge-toggle")
async def post_auto_merge_toggle(
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Set the runtime auto-merge kill-switch for a client profile."""
    body = await _guard_admin_request(request, x_autonomy_admin_token)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=422, detail="Body must be a JSON object"
            )
        payload = AutoMergeToggleIn(**data)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with autonomy_conn() as conn:
        row_id = set_auto_merge_toggle(
            conn,
            client_profile=payload.client_profile,
            enabled=payload.enabled,
            created_by=payload.created_by,
        )

    logger.info(
        "autonomy_auto_merge_toggle_set",
        client_profile=payload.client_profile,
        enabled=payload.enabled,
        override_id=row_id,
    )
    return {"status": "ok", "override_id": row_id, "enabled": payload.enabled}


@router.get(
    "/api/autonomy/auto-merge-toggle",
    dependencies=[Depends(_guard_admin_read)],
)
async def get_auto_merge_toggle_effective(
    client_profile: str,
) -> dict[str, Any]:
    """Return the effective auto_merge_enabled for a profile.

    Precedence: runtime toggle (if ever set) > YAML auto_merge_enabled.
    """
    with autonomy_conn() as conn:
        runtime_toggle = get_auto_merge_toggle(conn, client_profile)
    yaml_enabled = False
    profile = load_profile(client_profile)
    if profile is not None:
        yaml_enabled = profile.auto_merge_enabled
    if runtime_toggle is not None:
        return {
            "client_profile": client_profile,
            "enabled": runtime_toggle,
            "source": "runtime_toggle",
            "yaml_default": yaml_enabled,
        }
    return {
        "client_profile": client_profile,
        "enabled": yaml_enabled,
        "source": "yaml",
        "yaml_default": yaml_enabled,
    }


@router.get(
    "/api/autonomy/auto-merge-decisions",
    dependencies=[Depends(_guard_admin_read)],
)
async def get_auto_merge_decisions(
    client_profile: str | None = None,
    repo_full_name: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List recent auto-merge decisions for the dashboard."""
    with autonomy_conn() as conn:
        rows = list_recent_auto_merge_decisions(
            conn,
            limit=min(max(limit, 1), 500),
            repo_full_name=repo_full_name,
            client_profile=client_profile,
        )
    decisions: list[dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        decisions.append(
            {
                "id": r["id"],
                "target_id": r["target_id"],
                "created_at": r["created_at"],
                "created_by": r["created_by"],
                **payload,
            }
        )
    return {"decisions": decisions}


@router.get("/api/internal/autonomy/profile-by-repo")
async def get_profile_by_repo(
    repo_full_name: str,
    x_internal_api_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """L3 helper: resolve a GitHub repo → client_profile + autonomy settings.

    Returns empty strings / defaults if not matched. Auth via the shared
    internal API token.
    """
    if not settings.l1_internal_api_token:
        raise HTTPException(status_code=503, detail="Internal API not configured")
    if (
        not x_internal_api_token
        or not hmac.compare_digest(
            x_internal_api_token, settings.l1_internal_api_token
        )
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    profile = find_profile_by_repo(repo_full_name)
    if profile is None:
        return {
            "client_profile": "",
            "auto_merge_enabled_yaml": False,
            "low_risk_ticket_types": [],
        }
    return {
        "client_profile": profile.name,
        "auto_merge_enabled_yaml": profile.auto_merge_enabled,
        "low_risk_ticket_types": profile.low_risk_ticket_types,
    }


@router.get(
    "/api/autonomy",
    dependencies=[Depends(_guard_admin_read)],
)
async def get_autonomy(
    client_profile: str = "",
    window_days: int = 30,
) -> dict[str, Any]:
    """Return per-profile autonomy aggregates over a rolling window."""
    if window_days <= 0:
        raise HTTPException(status_code=400, detail="window_days must be positive")

    with autonomy_conn() as conn:
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
