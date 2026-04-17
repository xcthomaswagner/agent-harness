"""HTTP API for the self-learning miner — browse + triage candidates.

Phase B endpoints only change DB state. PR opening, diff drafting,
and outcome measurement land in Phases C-E.

Endpoints:
    GET  /api/learning/candidates
    GET  /api/learning/candidates/{lesson_id}
    POST /api/learning/candidates/{lesson_id}/approve
    POST /api/learning/candidates/{lesson_id}/reject
    POST /api/learning/candidates/{lesson_id}/snooze

Approve / reject / snooze drive ``update_lesson_status`` with its
full transition-table validation. Invalid transitions return 409.
Unknown lesson ids return 404.

Admin token auth mirrors the existing autonomy admin endpoints
(``_guard_admin_request``) — these actions change harness-owned
state and should not be exposed unauthenticated. When
``autonomy_admin_token`` is unset, the endpoints return 503 to
make the deployment-is-not-configured case obvious rather than
failing open.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from autonomy_ingest import _guard_admin_request
from autonomy_store import (
    autonomy_conn,
    get_lesson_by_id,
    list_lesson_candidates,
    list_lesson_evidence,
    update_lesson_status,
)

logger = structlog.get_logger()

router = APIRouter()


def _candidate_to_dict(row: Any) -> dict[str, Any]:
    """Candidate row + a parsed ``proposed_delta`` alongside the raw JSON."""
    out = dict(row)
    raw = out.get("proposed_delta_json", "")
    try:
        out["proposed_delta"] = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        out["proposed_delta"] = {"_parse_error": True, "raw": raw}
    return out


class ApproveIn(BaseModel):
    reason: str = ""


class RejectIn(BaseModel):
    reason: str = ""


class SnoozeIn(BaseModel):
    reason: str = ""
    next_review_at: str  # ISO 8601; required so callers can't "forever snooze"


@router.get("/api/learning/candidates")
async def get_learning_candidates(
    status: str | None = Query(default=None),
    client_profile: str | None = Query(default=None),
    detector_name: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    include_evidence: bool = Query(default=False),
) -> dict[str, Any]:
    """List lesson candidates with optional filters.

    ``include_evidence=true`` returns each candidate's evidence rows
    inline — intended for the dashboard's expandable evidence block.
    Capped by the per-lesson trim in ``lesson_evidence`` (20 rows),
    so the worst-case response stays bounded even on ``limit=500``.
    """
    with autonomy_conn() as conn:
        rows = list_lesson_candidates(
            conn,
            status=status,
            client_profile=client_profile,
            detector_name=detector_name,
            limit=limit,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            record = _candidate_to_dict(row)
            if include_evidence:
                ev = list_lesson_evidence(conn, record["lesson_id"])
                record["evidence"] = [dict(e) for e in ev]
            out.append(record)
        return {"candidates": out, "count": len(out)}


@router.get("/api/learning/candidates/{lesson_id}")
async def get_learning_candidate(lesson_id: str) -> dict[str, Any]:
    """Fetch a single candidate with its evidence rows."""
    with autonomy_conn() as conn:
        row = get_lesson_by_id(conn, lesson_id)
        if row is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        record = _candidate_to_dict(row)
        ev = list_lesson_evidence(conn, lesson_id)
        record["evidence"] = [dict(e) for e in ev]
        return record


def _transition(
    lesson_id: str,
    target: Literal["approved", "rejected", "snoozed", "draft_ready"],
    *,
    reason: str,
    next_review_at: str | None = None,
) -> dict[str, Any]:
    """Shared validation + transition for mutation endpoints."""
    with autonomy_conn() as conn:
        if get_lesson_by_id(conn, lesson_id) is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        try:
            updated = update_lesson_status(
                conn,
                lesson_id,
                target,
                reason=reason,
                next_review_at=next_review_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        logger.info(
            "learning_candidate_transition",
            lesson_id=lesson_id,
            target=target,
            reason=reason,
        )
        return _candidate_to_dict(updated)


def _parse_body(body: bytes, model: type[BaseModel]) -> Any:
    try:
        return model.model_validate_json(body or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/learning/candidates/{lesson_id}/approve")
async def post_approve(
    lesson_id: str,
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Approve a lesson. In Phase B this walks ``proposed -> draft_ready ->
    approved`` in two store writes so the operator can exercise the full
    flow before the Phase-C drafter splits the states explicitly."""
    body = await _guard_admin_request(request, x_autonomy_admin_token)
    payload = _parse_body(body, ApproveIn)
    with autonomy_conn() as conn:
        row = get_lesson_by_id(conn, lesson_id)
        if row is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        current = str(row["status"])
        try:
            if current == "proposed":
                update_lesson_status(
                    conn,
                    lesson_id,
                    "draft_ready",
                    reason="auto-promoted pending drafter (Phase B)",
                )
            updated = update_lesson_status(
                conn,
                lesson_id,
                "approved",
                reason=payload.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "learning_candidate_approved",
        lesson_id=lesson_id,
        reason=payload.reason,
    )
    return _candidate_to_dict(updated)


@router.post("/api/learning/candidates/{lesson_id}/reject")
async def post_reject(
    lesson_id: str,
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await _guard_admin_request(request, x_autonomy_admin_token)
    payload = _parse_body(body, RejectIn)
    return _transition(lesson_id, "rejected", reason=payload.reason)


@router.post("/api/learning/candidates/{lesson_id}/snooze")
async def post_snooze(
    lesson_id: str,
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await _guard_admin_request(request, x_autonomy_admin_token)
    payload = _parse_body(body, SnoozeIn)
    return _transition(
        lesson_id,
        "snoozed",
        reason=payload.reason,
        next_review_at=payload.next_review_at,
    )
