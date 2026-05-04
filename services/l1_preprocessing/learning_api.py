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

Write auth accepts either the autonomy admin token used by scripts or
the operator dashboard API key used by the SPA. These actions change
harness-owned state and should not be exposed unauthenticated. When
neither configured token is presented, the endpoints fail closed.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError, field_validator

import autonomy_ingest
from autonomy_ingest import _guard_admin_request
from autonomy_store import (
    autonomy_conn,
    get_latest_outcome,
    get_lesson_by_id,
    list_evidence_for_lessons,
    list_lesson_candidates,
    list_lesson_evidence,
    set_lesson_status_reason,
    update_lesson_status,
)
from config import settings
from learning_miner.drafter_consistency_check import (
    ConsistencyChecker,
    ConsistencyVerdict,
)
from learning_miner.drafter_markdown import (
    DrafterResult,
    MarkdownDrafter,
    check_target_path,
)
from learning_miner.outcomes import Verdict
from learning_miner.pr_opener import (
    OpenPRInputs,
    RevertPRInputs,
    open_pr_for_lesson,
    open_revert_pr_for_lesson,
)

logger = structlog.get_logger()

router = APIRouter()

_LESSON_OPERATION_LOCKS: dict[str, asyncio.Lock] = {}
_LESSON_OPERATION_LOCKS_GUARD = threading.Lock()


def _settings() -> Any:
    """Resolve settings through main when available.

    Most L1 split-out routers use this pattern so tests and runtime code that
    patch ``main.settings`` see the same values the endpoint uses.
    """
    try:
        import main  # local import avoids module-load cycles

        return main.settings
    except Exception:
        return settings


def _lesson_operation_lock(lesson_id: str) -> asyncio.Lock:
    """Return the per-lesson async lock for long-running write operations."""
    with _LESSON_OPERATION_LOCKS_GUARD:
        lock = _LESSON_OPERATION_LOCKS.get(lesson_id)
        if lock is None:
            lock = asyncio.Lock()
            _LESSON_OPERATION_LOCKS[lesson_id] = lock
        return lock


def _candidate_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Candidate row + a parsed ``proposed_delta`` alongside the raw JSON."""
    out = dict(row)
    raw = out.get("proposed_delta_json", "")
    try:
        out["proposed_delta"] = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        # Cap raw to keep a corrupted row from bloating the API
        # response. 2KB is enough to see the shape of the garbage
        # without dumping kilobytes of malformed JSON into every
        # list-candidates payload.
        trimmed = raw[:2000] if isinstance(raw, str) else raw
        out["proposed_delta"] = {"_parse_error": True, "raw": trimmed}
    return out


class ApproveIn(BaseModel):
    reason: str = ""


class RejectIn(BaseModel):
    reason: str = ""


class SnoozeIn(BaseModel):
    reason: str = ""
    next_review_at: str  # ISO 8601; required so callers can't "forever snooze"

    @field_validator("next_review_at")
    @classmethod
    def _parseable_iso(cls, v: str) -> str:
        """Reject non-ISO values — the comment says ISO 8601 is required
        but without this validator, any string (``"tomorrow"``, ``""``,
        ``"never"``) sailed through and landed in the DB, defeating
        the "forever snooze" prevention the comment claims.
        """
        if not v:
            raise ValueError("next_review_at is required")
        try:
            datetime.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(
                f"next_review_at must be ISO 8601: {exc}"
            ) from exc
        return v


class RevertIn(BaseModel):
    reason: str = ""


# Verdicts that make a revert request valid. Everything else the
# operator should reject or snooze — reverts are the heavy hammer
# and only exist for confirmed-bad outcomes. Sourced from the
# ``Verdict`` StrEnum so an enum rename can't silently desync.
_REVERTABLE_VERDICTS: frozenset[str] = frozenset(
    {Verdict.REGRESSED.value, Verdict.HUMAN_REEDIT.value}
)


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
        # Batch the evidence lookup — a per-row ``list_lesson_evidence``
        # loop issued one SELECT per candidate, so include_evidence=true
        # on limit=500 was up to 500 SQL round-trips. list_evidence_for_lessons
        # fetches all rows in one WHERE IN query and buckets client-side.
        evidence_by_id: dict[str, list[Any]] = {}
        if include_evidence and rows:
            ids = [str(r["lesson_id"]) for r in rows]
            raw = list_evidence_for_lessons(conn, ids)
            evidence_by_id = {k: list(v) for k, v in raw.items()}
        out: list[dict[str, Any]] = []
        for row in rows:
            record = _candidate_to_dict(row)
            if include_evidence:
                ev = evidence_by_id.get(str(record["lesson_id"]), [])
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


async def _guard_learning_write_request(
    request: Request,
    autonomy_admin_token: str | None,
    operator_api_key: str | None,
) -> bytes:
    """Authorize a Learning mutation from either admin API or operator UI.

    ``X-Autonomy-Admin-Token`` remains the script/internal automation
    contract. The Preact operator dashboard already carries ``X-API-Key``;
    in the current single-operator deployment that key is sufficient for
    Learning triage writes, and avoids exposing the separate admin token to
    browser code.
    """
    active_settings = _settings()
    if (
        active_settings.api_key
        and operator_api_key
        and hmac.compare_digest(operator_api_key, active_settings.api_key)
    ):
        body = await request.body()
        if len(body) > active_settings.autonomy_internal_max_body_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")
        if not autonomy_ingest._bucket.try_consume():
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        return body
    return await _guard_admin_request(request, autonomy_admin_token)


def _parse_body(body: bytes, model: type[BaseModel]) -> Any:
    """Validate a request body against a Pydantic model.

    Narrowed the exception catch: Pydantic's
    ``model_validate_json`` raises ``ValidationError`` (and inside it
    can bubble ``json.JSONDecodeError`` / ``UnicodeDecodeError`` for
    malformed payloads). Catching bare ``Exception`` used to swallow
    arbitrary bugs — an AttributeError from a misconfigured model
    definition, for instance — as a 422 that looks like user error.
    Now those propagate as 500 so they surface in monitoring.
    """
    try:
        return model.model_validate_json(body or b"{}")
    except (ValidationError, ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --- drafter helpers --------------------------------------------------


def _repo_root() -> Path:
    # services/l1_preprocessing/ is two dirs below the repo root.
    return Path(__file__).resolve().parents[2]


async def _load_proposed_lesson(lesson_id: str) -> sqlite3.Row:
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


_DRAFTER_UNIFIED_DIFF_KEY = "unified_diff"
_DRAFTER_ORIGIN_KEY = "drafter_origin"
_DRAFTER_ORIGIN_MARKDOWN = "markdown_drafter"

# Keys that /draft writes into proposed_delta_json, and /draft strips
# back out when reloading. Keep writer and reader pointing at the same
# set so a new drafter-side key can't silently escape the strip.
_DRAFTER_OUTPUT_KEYS: frozenset[str] = frozenset({
    _DRAFTER_UNIFIED_DIFF_KEY, _DRAFTER_ORIGIN_KEY,
})


def _row_proposed_delta(row: sqlite3.Row) -> dict[str, Any]:
    """Parse the stored proposed_delta_json into a dict.

    A candidate with a blank or malformed delta can't be drafted —
    callers turn this into a drafter failure, not a 500.

    Strips drafter-output keys (``unified_diff``, ``drafter_origin``)
    before returning: the ``draft_ready -> proposed`` transition is
    legal in the store, so a lesson bounced back to proposed retains
    the old drafter output on the row. Without stripping, /draft
    would feed the stale diff into the next LLM prompt — confusing
    the model about whether the "starter" already includes a draft.
    """
    raw = row["proposed_delta_json"] or ""
    try:
        obj = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        obj = {}
    if not isinstance(obj, dict):
        return {}
    return {k: v for k, v in obj.items() if k not in _DRAFTER_OUTPUT_KEYS}


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
) -> DrafterResult:
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
) -> ConsistencyVerdict:
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
    merged[_DRAFTER_UNIFIED_DIFF_KEY] = unified_diff
    merged[_DRAFTER_ORIGIN_KEY] = _DRAFTER_ORIGIN_MARKDOWN
    return merged


def _record_drafter_failure(lesson_id: str, error: str) -> None:
    """Surface a drafter failure as ``status_reason`` without transitioning."""
    with autonomy_conn() as conn:
        set_lesson_status_reason(conn, lesson_id, error)


async def _open_pr_for_lesson(
    lesson_id: str, approved_record: dict[str, Any]
) -> dict[str, Any]:
    """Run the PR opener for a freshly-approved lesson.

    On success (real run) transitions the lesson to ``applied`` with
    ``pr_url`` recorded. On failure leaves the lesson at ``approved``
    and writes the error to ``status_reason`` so the dashboard
    surfaces it — operators can then decide to retry (re-call
    /approve via API) or reject.

    The dry-run path does the local clone + commit but never pushes
    or calls ``gh pr create``. The lesson STAYS at ``approved``
    (not ``applied``) — ``applied`` would be a misleading terminal
    state for a no-network rehearsal. The status_reason is stamped
    with ``pr_opener dry-run ok (branch=..., commit=...)`` so the
    dashboard still surfaces the rehearsal result, and the operator
    can flip the real-PR flag and retry /approve without needing a
    separate transition path.
    """
    load = _load_pr_opener_inputs(lesson_id, approved_record)
    if load.inputs is None:
        _record_drafter_failure(lesson_id, load.error)
        return {
            "pr_opener_enabled": True,
            "pr_opener_success": False,
            "error": load.error,
        }

    result = await asyncio.to_thread(open_pr_for_lesson, load.inputs)
    if not result.success:
        _record_drafter_failure(
            lesson_id, f"pr_opener: {result.error}"
        )
        return {
            "pr_opener_enabled": True,
            "pr_opener_success": False,
            "error": result.error,
        }

    if result.dry_run:
        # Dry-run exercises the full local flow but never pushes.
        # Keep the lesson at ``approved`` so the operator can flip
        # the real-PR flag and retry — ``applied`` would be a
        # misleading terminal state for a no-network run.
        _record_drafter_failure(
            lesson_id,
            f"pr_opener dry-run ok (branch={result.branch}, "
            f"commit={result.commit_sha[:8]})",
        )
        return {
            "pr_opener_enabled": True,
            "pr_opener_success": True,
            "pr_opener_dry_run": True,
            "branch": result.branch,
            "commit_sha": result.commit_sha,
        }

    with autonomy_conn() as conn:
        try:
            # merged_commit_sha is intentionally NOT set here.
            # ``result.commit_sha`` is the lesson-branch HEAD before
            # push — not the sha that lands on main after merge.
            # outcomes.py's ``_poll_merge_state`` populates the real
            # merge commit from ``gh pr view --json mergeCommit``
            # once the PR merges.
            update_lesson_status(
                conn,
                lesson_id,
                "applied",
                reason="pr opened",
                pr_url=result.pr_url,
            )
        except ValueError as exc:
            return {
                "pr_opener_enabled": True,
                "pr_opener_success": False,
                "error": str(exc),
            }
    return {
        "pr_opener_enabled": True,
        "pr_opener_success": True,
        "pr_opener_dry_run": False,
        "pr_url": result.pr_url,
        "branch": result.branch,
        "commit_sha": result.commit_sha,
    }


@dataclass(frozen=True)
class _PROpenerInputLoad:
    """Result of ``_load_pr_opener_inputs``.

    Exactly one of ``inputs`` / ``error`` is populated. Using a
    dataclass instead of a union-return keeps the caller's
    dispatch type-safe and avoids the ``isinstance(x, str)`` smell.
    """

    inputs: OpenPRInputs | None = None
    error: str = ""


def _load_pr_opener_inputs(
    lesson_id: str, approved_record: dict[str, Any]
) -> _PROpenerInputLoad:
    """Pull the fields the PR opener needs off the stored candidate."""
    delta = approved_record.get("proposed_delta") or {}
    # _candidate_to_dict stamps _parse_error when proposed_delta_json
    # is malformed. Surface that explicitly — otherwise the
    # downstream "unified_diff missing" message would incorrectly
    # blame /draft when the real issue is DB corruption.
    if isinstance(delta, dict) and delta.get("_parse_error"):
        return _PROpenerInputLoad(
            error=(
                "pr_opener: proposed_delta_json is malformed in the "
                "store — cannot build PR inputs"
            )
        )
    unified_diff = str(delta.get("unified_diff") or "")
    if not unified_diff.strip():
        return _PROpenerInputLoad(
            error="pr_opener: proposed_delta.unified_diff missing — run /draft first"
        )
    rationale = str(delta.get("rationale_md") or "")

    trace_ids: list[str] = []
    seen: set[str] = set()
    with autonomy_conn() as conn:
        for row in list_lesson_evidence(conn, lesson_id):
            tid = row["trace_id"] or ""
            if tid and tid not in seen:
                seen.add(tid)
                trace_ids.append(tid)

    return _PROpenerInputLoad(
        inputs=OpenPRInputs(
            lesson_id=lesson_id,
            unified_diff=unified_diff,
            scope_key=str(approved_record.get("scope_key") or ""),
            detector_name=str(approved_record.get("detector_name") or ""),
            rationale_md=rationale,
            evidence_trace_ids=trace_ids,
            harness_repo_url=settings.learning_harness_repo_url,
            base_branch=settings.learning_harness_base_branch,
            dry_run=settings.learning_pr_opener_dry_run,
            reviewers=_configured_reviewers(),
        )
    )


def _configured_reviewers() -> tuple[str, ...]:
    """Parse the ``LEARNING_PR_OPENER_REVIEWERS`` comma-separated env.

    Kept as a small helper so both approve (PR) and revert (PR) flow
    pull from the same source of truth — if an operator updates the
    env, both paths pick it up without a redeploy.

    Dedupes while preserving insertion order: a misconfigured env
    like ``"a,b,a"`` would otherwise produce ``--reviewer a
    --reviewer b --reviewer a``, which some ``gh`` versions reject
    with "already requested review from @a" (fails the PR creation).
    """
    raw = (settings.learning_pr_opener_reviewers or "").strip()
    if not raw:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for h in raw.split(","):
        # Strip ``@`` defensively — the config comment says "no @",
        # but an operator who follows the conventional @mention
        # syntax shouldn't have their env silently break ``gh``.
        handle = h.strip().lstrip("@")
        if handle and handle not in seen:
            seen.add(handle)
            out.append(handle)
    return tuple(out)


@router.post("/api/learning/candidates/{lesson_id}/approve")
async def post_approve(
    lesson_id: str,
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Approve a ``draft_ready`` lesson and open its PR.

    ``proposed`` candidates must run through ``/draft`` first. When
    the PR opener is enabled, a successful approve walks
    ``draft_ready -> approved -> applied`` (the last step is the
    PR-opened marker). When the PR opener is disabled, the lesson
    stays at ``approved`` and the operator can open the PR manually.

    Re-entrable: if a previous call transitioned the lesson to
    ``approved`` but the PR opener failed, calling /approve again
    skips the transition and retries the PR opener. This is the
    sanctioned recovery path — no separate /retry-pr endpoint needed.
    """
    body = await _guard_learning_write_request(
        request, x_autonomy_admin_token, x_api_key
    )
    payload = _parse_body(body, ApproveIn)

    async with _lesson_operation_lock(lesson_id):
        with autonomy_conn() as conn:
            existing = get_lesson_by_id(conn, lesson_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="lesson not found")

        if str(existing["status"]) == "approved":
            # Already approved (prior attempt failed at PR opener stage).
            # Surface the existing record and re-run the opener.
            approved = _candidate_to_dict(existing)
        else:
            approved = _transition(lesson_id, "approved", reason=payload.reason)

        active_settings = _settings()
        if not active_settings.learning_pr_opener_enabled:
            approved["pr_opener_enabled"] = False
            return approved
        pr_outcome = await _open_pr_for_lesson(lesson_id, approved)
        approved.update(pr_outcome)
        return approved


@router.post("/api/learning/candidates/{lesson_id}/draft")
async def post_draft(
    lesson_id: str,
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Run the Markdown drafter + consistency check on a proposed lesson.

    ``proposed -> draft_ready`` on success. Any failure (drafter error
    or consistency contradiction) leaves the lesson at ``proposed``
    with ``status_reason`` set so the dashboard surfaces it.
    """
    await _guard_learning_write_request(
        request, x_autonomy_admin_token, x_api_key
    )
    async with _lesson_operation_lock(lesson_id):
        return await _draft_locked(lesson_id)


async def _draft_locked(lesson_id: str) -> dict[str, Any]:
    """Draft a proposed lesson while the per-lesson operation lock is held."""
    row = await _load_proposed_lesson(lesson_id)
    proposed_delta = _row_proposed_delta(row)
    evidence_snippets = _load_evidence_snippets(lesson_id)

    # Validate target_path against the drafter allowlist BEFORE reading
    # the file off disk. Without this guard an absolute path in
    # proposed_delta (e.g. /etc/passwd) slips past _repo_root() because
    # `Path("/a") / "/etc/passwd"` evaluates to `Path("/etc/passwd")` —
    # pathlib discards the left operand for absolute RHS. The drafter's
    # own precheck would eventually reject, but only after the read has
    # already loaded arbitrary file contents into a Claude prompt.
    target_path = str(proposed_delta.get("target_path") or "")
    path_err = check_target_path(target_path)
    if path_err is not None:
        _record_drafter_failure(lesson_id, path_err)
        return {
            "lesson_id": lesson_id,
            "status": "proposed",
            "drafter_success": False,
            "error": path_err,
        }

    # Read the target file once up front so the drafter and consistency
    # check share a single view — no TOCTOU gap between two reads.
    target_abs = _repo_root() / target_path
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
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await _guard_learning_write_request(
        request, x_autonomy_admin_token, x_api_key
    )
    payload = _parse_body(body, RejectIn)
    return _transition(lesson_id, "rejected", reason=payload.reason)


@router.post("/api/learning/candidates/{lesson_id}/snooze")
async def post_snooze(
    lesson_id: str,
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await _guard_learning_write_request(
        request, x_autonomy_admin_token, x_api_key
    )
    payload = _parse_body(body, SnoozeIn)
    return _transition(
        lesson_id,
        "snoozed",
        reason=payload.reason,
        next_review_at=payload.next_review_at,
    )


@router.post("/api/learning/candidates/{lesson_id}/revert")
async def post_revert(
    lesson_id: str,
    request: Request,
    x_autonomy_admin_token: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Open a revert PR for an applied lesson with a bad outcome.

    Gated: the lesson must be at ``status='applied'`` with a non-empty
    ``merged_commit_sha`` AND the latest recorded outcome verdict
    must be ``regressed`` or ``human_reedit``. We transition to
    ``reverted`` only after the revert PR is successfully pushed and
    opened on GitHub — on failure the lesson stays at ``applied`` and
    the error lands on ``status_reason`` so the operator can retry.

    Dry-run (``LEARNING_PR_OPENER_DRY_RUN=true``) keeps the lesson at
    ``applied`` and stamps ``status_reason`` with the local branch +
    commit summary. The operator flips the real-PR flag and re-hits
    /revert to actually push — ``reverted`` is terminal, so staying
    at ``applied`` preserves the retry path. Mirrors the approve
    flow's dry-run semantics.
    """
    body = await _guard_learning_write_request(
        request, x_autonomy_admin_token, x_api_key
    )
    payload = _parse_body(body, RevertIn)

    with autonomy_conn() as conn:
        row = get_lesson_by_id(conn, lesson_id)
        if row is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        if row["status"] != "applied":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"lesson not in 'applied' state "
                    f"(currently {row['status']!r})"
                ),
            )
        merged_commit_sha = str(row["merged_commit_sha"] or "")
        if not merged_commit_sha:
            raise HTTPException(
                status_code=409,
                detail=(
                    "lesson has no merged_commit_sha — outcomes poll "
                    "may not have run yet"
                ),
            )
        outcome = get_latest_outcome(conn, lesson_id)
        verdict = str(outcome["verdict"] or "") if outcome is not None else ""
        if verdict not in _REVERTABLE_VERDICTS:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"latest outcome verdict {verdict!r} is not "
                    "revertable (requires regressed or human_reedit)"
                ),
            )

    inputs = RevertPRInputs(
        lesson_id=lesson_id,
        merged_commit_sha=merged_commit_sha,
        verdict=verdict,
        reason_md=payload.reason,
        harness_repo_url=settings.learning_harness_repo_url,
        base_branch=settings.learning_harness_base_branch,
        dry_run=settings.learning_pr_opener_dry_run,
        reviewers=_configured_reviewers(),
    )

    result = await asyncio.to_thread(open_revert_pr_for_lesson, inputs)
    if not result.success:
        _record_drafter_failure(
            lesson_id, f"revert_pr_opener: {result.error}"
        )
        return {
            "revert_success": False,
            "error": result.error,
        }

    if result.dry_run:
        # Dry-run: leave lesson at ``applied``; the operator can flip
        # real-PR mode and retry. Matches approve's dry-run behavior.
        with autonomy_conn() as conn:
            set_lesson_status_reason(
                conn,
                lesson_id,
                (
                    f"revert_pr_opener dry-run ok "
                    f"(branch={result.branch}, commit={result.commit_sha[:8]})"
                ),
            )
        return {
            "revert_success": True,
            "revert_dry_run": True,
            "branch": result.branch,
            "commit_sha": result.commit_sha,
        }

    with autonomy_conn() as conn:
        try:
            update_lesson_status(
                conn,
                lesson_id,
                "reverted",
                reason=payload.reason or f"reverted ({verdict})",
                pr_url=result.pr_url,
            )
        except ValueError as exc:
            return {
                "revert_success": False,
                "error": str(exc),
            }
    return {
        "revert_success": True,
        "revert_dry_run": False,
        "pr_url": result.pr_url,
        "branch": result.branch,
        "commit_sha": result.commit_sha,
    }
