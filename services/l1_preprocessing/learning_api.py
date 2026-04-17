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
from pathlib import Path
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
    set_lesson_status_reason,
    update_lesson_status,
)
from config import settings
from learning_miner.drafter_consistency_check import ConsistencyChecker
from learning_miner.drafter_markdown import MarkdownDrafter

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


# --- drafter helpers --------------------------------------------------


def _repo_root() -> Path:
    # services/l1_preprocessing/ is two dirs below the repo root.
    return Path(__file__).resolve().parents[2]


async def _load_proposed_lesson(lesson_id: str) -> Any:
    """Fetch a lesson row; reject unless it is currently at ``proposed``.

    /draft is idempotent from the operator's perspective only in the
    sense that "try again if it failed" — not in the sense of "drafts
    twice in a row land two drafts." Once a lesson is at draft_ready
    the operator should /reject then re-emit via the nightly rescan
    rather than re-drafting in place.
    """
    with autonomy_conn() as conn:
        row = get_lesson_by_id(conn, lesson_id)
    if row is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    if str(row["status"]) != "proposed":
        raise HTTPException(
            status_code=409,
            detail=(
                f"/draft requires status='proposed', got "
                f"{row['status']!r}"
            ),
        )
    return row


def _row_proposed_delta(row: Any) -> dict[str, Any]:
    """Parse the stored proposed_delta_json into a dict.

    A candidate with a blank or malformed delta can't be drafted —
    callers turn this into a drafter failure, not a 500.
    """
    raw = row["proposed_delta_json"] or ""
    try:
        obj = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        obj = {}
    return obj if isinstance(obj, dict) else {}


def _load_evidence_snippets(lesson_id: str) -> list[str]:
    with autonomy_conn() as conn:
        rows = list_lesson_evidence(conn, lesson_id)
    out: list[str] = []
    for r in rows:
        snippet = r["snippet"] or ""
        if snippet:
            out.append(snippet)
    return out


async def _run_drafter(
    proposed_delta: dict[str, Any],
    evidence_snippets: list[str],
    current_content: str,
) -> Any:
    """Dispatch to the Markdown drafter. Isolates SDK import cost here."""
    drafter = MarkdownDrafter(
        api_key=settings.anthropic_api_key,
        repo_root=_repo_root(),
    )
    return await drafter.draft(
        proposed_delta=proposed_delta,
        evidence_snippets=evidence_snippets,
        current_content=current_content,
    )


async def _run_consistency_check(
    current_content: str, unified_diff: str
) -> Any:
    checker = ConsistencyChecker(
        api_key=settings.anthropic_api_key,
        enabled=settings.learning_consistency_check_enabled,
    )
    return await checker.check(
        current_content=current_content,
        unified_diff=unified_diff,
    )


def _merge_diff_into_delta(
    proposed_delta: dict[str, Any], unified_diff: str
) -> dict[str, Any]:
    """Stamp the Claude-drafted diff + a drafter_origin marker onto
    the detector's delta so the dashboard can distinguish a mechanical
    starter from a drafter-promoted one.
    """
    merged = dict(proposed_delta)
    merged["unified_diff"] = unified_diff
    merged["drafter_origin"] = "markdown_drafter"
    return merged


def _record_drafter_failure(lesson_id: str, error: str) -> None:
    """Surface a drafter failure as ``status_reason`` without transitioning."""
    with autonomy_conn() as conn:
        set_lesson_status_reason(conn, lesson_id, error)


@router.post("/api/learning/candidates/{lesson_id}/approve")
async def post_approve(
    lesson_id: str,
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Approve a ``draft_ready`` lesson.

    ``proposed`` candidates must run through ``/draft`` first so a
    Claude-drafted diff is written before approval. The transition
    table in ``autonomy_store`` enforces this; the /approve handler
    just surfaces the resulting 409.
    """
    body = await _guard_admin_request(request, x_autonomy_admin_token)
    payload = _parse_body(body, ApproveIn)
    return _transition(lesson_id, "approved", reason=payload.reason)


@router.post("/api/learning/candidates/{lesson_id}/draft")
async def post_draft(
    lesson_id: str,
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Run the Markdown drafter + consistency check on a proposed lesson.

    ``proposed -> draft_ready`` on success. Any failure (drafter error
    or consistency contradiction) leaves the lesson at ``proposed``
    with ``status_reason`` set so the dashboard surfaces it.
    """
    await _guard_admin_request(request, x_autonomy_admin_token)
    row = await _load_proposed_lesson(lesson_id)
    proposed_delta = _row_proposed_delta(row)
    evidence_snippets = _load_evidence_snippets(lesson_id)

    # Read the target file once up front so the drafter and consistency
    # check share a single view — no TOCTOU gap between two reads.
    target_abs = _repo_root() / str(proposed_delta.get("target_path"))
    try:
        current_content = target_abs.read_text()
    except OSError as exc:
        err = f"target file not readable: {exc}"
        _record_drafter_failure(lesson_id, err)
        return {
            "lesson_id": lesson_id,
            "status": "proposed",
            "drafter_success": False,
            "error": err,
        }

    drafter_result = await _run_drafter(
        proposed_delta, evidence_snippets, current_content
    )
    if not drafter_result.success:
        _record_drafter_failure(lesson_id, drafter_result.error)
        return {
            "lesson_id": lesson_id,
            "status": "proposed",
            "drafter_success": False,
            "error": drafter_result.error,
        }

    verdict = await _run_consistency_check(
        current_content, drafter_result.unified_diff
    )
    if verdict.contradicts:
        reason = (
            f"consistency check blocked: {verdict.reasoning} "
            f"(conflicts with: {verdict.contradicts_with})"
        )
        _record_drafter_failure(lesson_id, reason)
        return {
            "lesson_id": lesson_id,
            "status": "proposed",
            "drafter_success": True,
            "consistency_contradicts": True,
            "contradicts_with": verdict.contradicts_with,
            "reasoning": verdict.reasoning,
        }

    updated_delta = _merge_diff_into_delta(
        proposed_delta, drafter_result.unified_diff
    )
    with autonomy_conn() as conn:
        try:
            updated = update_lesson_status(
                conn,
                lesson_id,
                "draft_ready",
                reason="drafter produced unified diff",
                proposed_delta_json=json.dumps(
                    updated_delta, sort_keys=True
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "learning_candidate_drafted",
        lesson_id=lesson_id,
        tokens_in=(
            drafter_result.tokens_in + verdict.tokens_in
        ),
        tokens_out=(
            drafter_result.tokens_out + verdict.tokens_out
        ),
    )
    return {
        "lesson_id": lesson_id,
        "status": "draft_ready",
        "drafter_success": True,
        "consistency_contradicts": False,
        "candidate": _candidate_to_dict(updated),
    }


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
