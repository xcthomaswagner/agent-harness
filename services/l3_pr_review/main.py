"""L3 PR Review Service — GitHub webhook receiver for PR events."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

# Add L1 to path for shared tracer access (single-machine deployment)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "l1_preprocessing"))
from tracer import append_trace, generate_trace_id, read_trace

from ado_api import post_ado_pr_comment
from ado_event_classifier import classify_ado_event
from auto_merge import evaluate_and_maybe_merge, evaluate_and_maybe_merge_ado
from backlog import append_backlog, backlog_status, drain_backlog
from event_classifier import EventType, classify_event
from github_api import get_pr_state
from spawner import SessionSpawner

load_dotenv()

logger = structlog.get_logger()

# Hold references to fire-and-forget startup tasks so they aren't GC'd.
_startup_tasks: set[asyncio.Task[None]] = set()

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
L1_SERVICE_URL = os.getenv("L1_SERVICE_URL", "http://localhost:8000")
L1_INTERNAL_API_TOKEN = os.getenv("L1_INTERNAL_API_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_GITHUB_USERNAME", "github-actions[bot]")
# Hidden marker injected into all agent-posted comments for self-detection.
# This catches bot-loops even when the agent uses the same GitHub user as the human.
BOT_COMMENT_MARKER = "<!-- xcagent -->"

# Dedup: track recently processed GitHub delivery IDs to prevent double-processing
# on webhook retries or race conditions. OrderedDict gives FIFO eviction so recent
# entries are never evicted before old ones.
_processed_deliveries: OrderedDict[str, None] = OrderedDict()
_MAX_DELIVERY_CACHE = 500

app = FastAPI(
    title="Agentic Harness L3 PR Review",
    description="Receives GitHub PR webhooks, classifies events, spawns review/fix sessions.",
    version="0.1.0",
)

_spawner: SessionSpawner | None = None


def _get_spawner() -> SessionSpawner:
    global _spawner
    if _spawner is None:
        _spawner = SessionSpawner(repo_path=os.getenv("CLIENT_REPO_PATH", ""))
    return _spawner


# --- Helpers ---


def _require_internal_api_token(x_internal_api_token: str | None) -> None:
    """Validate the admin API token in constant time.

    Plain ``!=`` leaks timing on the first differing byte, so an
    attacker can byte-by-byte recover the secret. Use
    ``hmac.compare_digest`` and raise the generic 401 only after the
    compare, so missing tokens and wrong tokens take the same path.

    Fails with 503 when the admin API isn't configured (empty env
    var) so we don't accidentally accept an empty-string token.
    """
    expected = os.getenv("L1_INTERNAL_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Admin API not configured")
    provided = x_internal_api_token or ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token")


_AI_BRANCH_PATTERN = re.compile(r"^ai/([A-Za-z0-9]+-\d+)")


_TICKET_TYPE_LABELS: tuple[str, ...] = (
    "bug",
    "chore",
    "config",
    "dependency",
    "docs",
    "story",
)


def _ticket_type_from_labels(
    labels: list[str], *, default: str = "story"
) -> str:
    """Return the first label that matches a known ticket type, or ``default``.

    Consolidates the copy-pasted label scan that appeared in both
    ``_handle_review_approved`` and ``_handle_ci_passed``. The two
    call sites previously used *different* label sets (one omitted
    "story"), so a new ticket type would have required touching both.
    """
    for label in labels:
        if label in _TICKET_TYPE_LABELS:
            return label
    return default


def _pr_head_branch(pr: dict[str, Any]) -> str:
    """Extract the PR head branch (ref) safely from a GitHub PR dict."""
    return (pr.get("head") or {}).get("ref", "") or ""


def _pr_head_sha(pr: dict[str, Any]) -> str:
    """Extract the PR head sha safely from a GitHub PR dict."""
    return (pr.get("head") or {}).get("sha", "") or ""


def _ticket_id_from_payload(payload: dict[str, Any]) -> str:
    """Extract ticket ID from the PR branch name (e.g., ai/SCRUM-16 → SCRUM-16)."""
    pr = payload.get("pull_request", {})
    branch = _pr_head_branch(pr)
    match = _AI_BRANCH_PATTERN.match(branch)
    return match.group(1) if match else ""


@dataclass(frozen=True)
class _PRHandlerCtx:
    """Fields every PR-scoped handler needs to do its work.

    Collapses the 5-line block of ``pr.get(...)``/``_pr_head_branch``/
    ``_ticket_id_from_payload``/``repo_full_name`` that each handler
    used to open-code. Previously this block drifted between
    handlers — ``_handle_review_approved`` read ``repo`` from
    ``pr.base.repo.full_name`` while others used
    ``payload.repository.full_name``. Centralising here collapses
    the fallback chain into one place and makes any new handler
    start from a typed object rather than six ``payload.get`` calls.
    """
    pr: dict[str, Any]
    pr_number: int
    branch: str
    head_sha: str
    repo: str
    ticket_id: str
    labels: list[str]


def _pr_handler_ctx(payload: dict[str, Any]) -> _PRHandlerCtx:
    """Build a ``_PRHandlerCtx`` from a GitHub PR webhook payload."""
    pr = payload.get("pull_request", {}) or {}
    # repo resolution: prefer top-level repository.full_name (always
    # present on PR webhooks), fall back to pr.base.repo.full_name
    # (used by review webhooks where the top-level repo may be
    # missing or stale). This fallback chain was previously
    # duplicated across handlers with subtle drift.
    repo = (payload.get("repository") or {}).get("full_name", "") or (
        ((pr.get("base") or {}).get("repo") or {}).get("full_name", "")
    )
    return _PRHandlerCtx(
        pr=pr,
        pr_number=int(pr.get("number") or 0),
        branch=_pr_head_branch(pr),
        head_sha=_pr_head_sha(pr),
        repo=repo,
        ticket_id=_ticket_id_from_payload(payload) or "",
        labels=[
            (label or {}).get("name", "") for label in (pr.get("labels") or [])
        ],
    )


def _is_bot_actor(
    user: dict[str, Any] | None, body: str = ""
) -> bool:
    """Canonical bot-detection helper used by every webhook handler.

    Returns True when ANY of the following signals are present:

    1. ``user.type == "Bot"`` — GitHub App installations.
    2. ``user.login`` ends in ``[bot]`` — GitHub convention for Apps.
    3. ``user.login`` (case-insensitive) equals the harness's own
       ``BOT_GITHUB_USERNAME`` env var — critical for catching
       self-reviews posted by the harness under a normal-looking
       user account (e.g. ``xcagentrockwell``), which has
       ``type=User`` and no ``[bot]`` suffix.
    4. The message ``body`` contains the hidden
       ``BOT_COMMENT_MARKER`` — second-layer detection for cases
       where the actor is spoofed or misconfigured.

    Previously there were two separate helpers — ``_is_bot_user``
    only covered signals (1) and (2), and ``_is_bot_comment`` added
    (3) and (4). ``_forward_review_body_human_issue`` used the
    weaker ``_is_bot_user``, which meant harness-posted review
    bodies (posted under ``xcagentrockwell``) slipped past the
    filter and were forwarded to L1 as if they were human
    feedback, triggering a self-reinforcing bot loop where the
    harness re-queued work based on its own critiques. Unifying
    both helpers into one canonical path closes that gap and
    makes it impossible to add a new handler that forgets a check.
    """
    if not isinstance(user, dict):
        user = {}
    login = (user.get("login") or "").lower()
    if user.get("type") == "Bot":
        return True
    if login.endswith("[bot]"):
        return True
    if login and BOT_USERNAME and login == BOT_USERNAME.lower():
        return True
    return bool(body and BOT_COMMENT_MARKER and BOT_COMMENT_MARKER in body)


def _is_bot_comment(payload: dict[str, Any], user_path: list[str]) -> bool:
    """Back-compat shim that walks ``user_path`` out of ``payload`` and
    delegates to :func:`_is_bot_actor`.

    Kept so existing call sites don't need to learn the new
    signature in this commit. New code should call ``_is_bot_actor``
    directly.
    """
    obj: Any = payload
    for key in user_path:
        obj = obj.get(key, {})
    user = obj if isinstance(obj, dict) else {}
    body = (
        payload.get("review", {}).get("body", "")
        or payload.get("comment", {}).get("body", "")
    )
    return _is_bot_actor(user, body)


def _lookup_trace_id(ticket_id: str) -> str:
    """Find the L2 run's trace ID for this ticket.

    Looks for the ``agent_finished`` or ``Pipeline complete`` event's trace_id
    (the L2 run's ID) rather than just taking the last entry, which could be
    from any source.
    """
    entries = read_trace(ticket_id)
    for entry in reversed(entries):
        ev = entry.get("event", "")
        if "agent_finished" in ev or "Pipeline complete" in ev:
            return str(entry.get("trace_id", generate_trace_id()))
    if entries:
        return str(entries[-1].get("trace_id", generate_trace_id()))
    # No trace exists — L3 event will start a new trace chain
    logger.warning("trace_id_lookup_miss", ticket_id=ticket_id,
                   hint="No existing trace found; L3 event will have a new trace ID")
    return generate_trace_id()


# --- Autonomy event forwarding (L3 → L1) ---


_AUTONOMY_EVENTS_PATH = "/api/internal/autonomy/events"
_HUMAN_ISSUES_PATH = "/api/internal/autonomy/human-issues"


async def _post_to_l1_with_retry(
    path: str,
    payload: dict[str, Any],
    *,
    log_event: str,
    log_context: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> bool:
    """POST ``payload`` to L1 with retry-once on transient failure.

    Returns True on 2xx, False on non-retryable 4xx or double-fail 5xx.
    Shared path for every L1 forwarder — both the internal autonomy
    pipelines (which use ``X-Internal-Api-Token``, the default) and
    the ``/api/agent-complete`` caller (which passes an explicit
    ``X-API-Key`` header sourced from the shared
    ``L1_INTERNAL_API_TOKEN`` env var — see ``_handle_review_approved``).

    ``log_context`` is merged into every failure log.
    """
    url = f"{L1_SERVICE_URL.rstrip('/')}{path}"
    if headers is None:
        headers = {"X-Internal-Api-Token": L1_INTERNAL_API_TOKEN}

    last_error: str = ""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                return False
            if resp.status_code >= 400:
                logger.error(
                    log_event,
                    status_code=resp.status_code,
                    body=resp.text[:500],
                    **log_context,
                )
                return False
            return True
        except httpx.RequestError as exc:
            last_error = f"RequestError: {exc}"
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return False

    logger.error(log_event, error=last_error, url=url, **log_context)
    return False


_GITHUB_DEFECT_LINK_PATH = "/api/internal/autonomy/github-defect-link"


@dataclass(frozen=True)
class _ForwarderSpec:
    """Static config for one L1 forwarder kind.

    Consolidates the path, log-event base name, and the payload keys
    to harvest for structured logs. Previously every forwarder
    duplicated ~25 lines of "skip if no token / retry / log / backlog"
    scaffolding plus its own `_once` wrapper. With this registry the
    two wrappers collapse to tiny functions that look up the spec.
    """
    path: str
    log_event_base: str  # "l1_autonomy_event_forward" -> "_skipped" / "_failed"
    skipped_reason: str
    context_keys: tuple[str, ...]


_FORWARDERS: dict[str, _ForwarderSpec] = {
    "autonomy_event": _ForwarderSpec(
        path=_AUTONOMY_EVENTS_PATH,
        log_event_base="l1_autonomy_event_forward",
        skipped_reason=(
            "L1_INTERNAL_API_TOKEN unset — autonomy events are being dropped. "
            "Set L1_INTERNAL_API_TOKEN to enable forwarding."
        ),
        context_keys=("event_type", "ticket_id"),
    ),
    "human_issue": _ForwarderSpec(
        path=_HUMAN_ISSUES_PATH,
        log_event_base="l1_human_issue_forward",
        skipped_reason=(
            "L1_INTERNAL_API_TOKEN unset — human review issues are being dropped. "
            "Set L1_INTERNAL_API_TOKEN to enable forwarding."
        ),
        context_keys=("event_type", "ticket_id"),
    ),
    "github_defect": _ForwarderSpec(
        path=_GITHUB_DEFECT_LINK_PATH,
        log_event_base="l1_github_defect_forward",
        skipped_reason=(
            "L1_INTERNAL_API_TOKEN unset — GitHub defect links are being dropped. "
            "Set L1_INTERNAL_API_TOKEN to enable forwarding."
        ),
        context_keys=("issue_number", "pr_number"),
    ),
}


def _forwarder_context(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    spec = _FORWARDERS[kind]
    return {key: payload.get(key) for key in spec.context_keys}


async def _forward_to_l1_once(kind: str, payload: dict[str, Any]) -> bool:
    """POST ``payload`` to the L1 endpoint for ``kind`` with retry-once.

    Assumes ``L1_INTERNAL_API_TOKEN`` is set — callers must short-circuit
    otherwise. Registry-driven replacement for the three
    ``_forward_*_once`` helpers.
    """
    spec = _FORWARDERS[kind]
    return await _post_to_l1_with_retry(
        spec.path,
        payload,
        log_event=f"{spec.log_event_base}_failed",
        log_context=_forwarder_context(kind, payload),
    )


async def _forward_to_l1(kind: str, payload: dict[str, Any]) -> None:
    """Forward ``payload`` to L1 and persist to backlog on final failure.

    Short-circuits (without backlog) when ``L1_INTERNAL_API_TOKEN``
    is unset. Single entry-point for every L1 forwarder — the three
    old outer wrappers collapse to one function.
    """
    spec = _FORWARDERS[kind]
    context = _forwarder_context(kind, payload)
    if not L1_INTERNAL_API_TOKEN:
        logger.warning(
            f"{spec.log_event_base}_skipped",
            reason=spec.skipped_reason,
            **context,
        )
        return
    if not await _forward_to_l1_once(kind, payload):
        logger.error(
            f"{spec.log_event_base}_failed",
            backlogged=True,
            **context,
        )
        await append_backlog(kind, payload)


# Back-compat aliases for existing call sites and tests. Eventually
# every caller should say ``_forward_to_l1("autonomy_event", event)``
# but keeping these as thin shims avoids a sweeping rename in this
# commit.
async def _forward_autonomy_event_once(event: dict[str, Any]) -> bool:
    return await _forward_to_l1_once("autonomy_event", event)


async def _forward_autonomy_event(event: dict[str, Any]) -> None:
    await _forward_to_l1("autonomy_event", event)


async def _forward_human_issue_once(payload: dict[str, Any]) -> bool:
    return await _forward_to_l1_once("human_issue", payload)


async def _forward_human_issue(payload: dict[str, Any]) -> None:
    await _forward_to_l1("human_issue", payload)


async def _forward_github_defect_once(payload: dict[str, Any]) -> bool:
    return await _forward_to_l1_once("github_defect", payload)


async def _forward_github_defect(payload: dict[str, Any]) -> None:
    await _forward_to_l1("github_defect", payload)

# Match (in order): full PR URL, owner/repo#N, bare #N (same-repo).
_PR_REF_URL_PATTERN = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)"
)
_PR_REF_OWNER_REPO_PATTERN = re.compile(
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)"
)
_PR_REF_BARE_PATTERN = re.compile(r"(?<![A-Za-z0-9/#])#(\d+)(?![A-Za-z0-9/])")


def _extract_pr_ref(body: str, same_repo: str) -> tuple[str, int] | None:
    """Return (repo_full_name, pr_number) from first PR reference in body, else None.

    Cross-repo references are REJECTED: if the body points at an
    ``owner/repo`` that doesn't equal ``same_repo``, this returns None.

    The issue body that feeds this function is attacker-controllable
    (anyone who can create a labelled issue — broad permission on
    public repos, any contributor on private). Previously a PR URL
    pointing at an unrelated victim repo in a different org would
    be accepted verbatim, and ``_handle_issue_labeled`` would forward
    a defect-link to L1 with ``pr_repo_full_name`` set to the victim's
    repo — polluting L1's defect audit trail across orgs and
    potentially starving autonomy on the victim repo if L1 gates
    auto-merge on defect counts. Same-repo references via the bare
    ``#N`` pattern are unchanged: they already required ``same_repo``
    to resolve, so there's no attack surface there.

    ``same_repo`` is the repository the webhook originated from —
    the caller must pass it (and it should never be empty in
    practice; if it is, we fall through and return None rather than
    trusting the body alone).
    """
    if not body:
        return None
    m = _PR_REF_URL_PATTERN.search(body)
    if m:
        ref_repo = m.group(1)
        if not same_repo or ref_repo != same_repo:
            logger.warning(
                "pr_ref_cross_repo_rejected",
                ref_repo=ref_repo,
                source_repo=same_repo,
                pattern="url",
            )
            return None
        return ref_repo, int(m.group(2))
    m = _PR_REF_OWNER_REPO_PATTERN.search(body)
    if m:
        ref_repo = m.group(1)
        if not same_repo or ref_repo != same_repo:
            logger.warning(
                "pr_ref_cross_repo_rejected",
                ref_repo=ref_repo,
                source_repo=same_repo,
                pattern="owner_repo",
            )
            return None
        return ref_repo, int(m.group(2))
    m = _PR_REF_BARE_PATTERN.search(body)
    if m and same_repo:
        return same_repo, int(m.group(1))
    return None


def _category_from_labels(labels: list[str]) -> str:
    """Map GitHub issue labels to defect_links.category."""
    lower = {(label_name or "").lower() for label_name in labels}
    if "pre-existing" in lower or "pre_existing" in lower:
        return "pre_existing"
    if "infra" in lower or "infrastructure" in lower:
        return "infra"
    if "feature-request" in lower or "feature_request" in lower or "enhancement" in lower:
        return "feature_request"
    return "escaped"


def _is_bot_user(user: dict[str, Any], body: str = "") -> bool:
    """Back-compat shim delegating to :func:`_is_bot_actor`.

    The old implementation checked only ``type=="Bot"`` and the
    ``[bot]`` login suffix — it missed the harness's own
    ``xcagentrockwell`` account (normal User type, no ``[bot]``
    suffix), so review bodies the harness itself posted slipped
    through every guard that called this helper. New callers should
    pass ``body`` so the ``BOT_COMMENT_MARKER`` fallback can catch
    spoofed cases.
    """
    return _is_bot_actor(user, body)


def _truncate(value: str | None, limit: int = 2000) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit]


def _build_autonomy_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_at: str | None = None,
) -> dict[str, Any] | None:
    """Build a normalized AutonomyEventIn payload from a GitHub webhook payload.

    Returns None if required fields are missing (no ticket_id, no PR number, etc.).
    """
    pr = payload.get("pull_request", {}) or {}
    repo = payload.get("repository", {}) or {}
    repo_full_name = repo.get("full_name", "") or (
        pr.get("base", {}).get("repo", {}).get("full_name", "")
    )
    pr_number = pr.get("number", 0)
    head = pr.get("head", {}) or {}
    base = pr.get("base", {}) or {}
    head_sha = head.get("sha", "")
    ticket_id = _ticket_id_from_payload(payload)

    if not (repo_full_name and pr_number and head_sha and ticket_id):
        logger.debug(
            "autonomy_event_missing_required_fields",
            event_type=event_type,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            ticket_id=ticket_id,
        )
        return None

    event: dict[str, Any] = {
        "event_type": event_type,
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "ticket_id": ticket_id,
        "event_at": event_at or datetime.now(UTC).isoformat(),
        "pr_url": pr.get("html_url", "") or None,
        "head_ref": head.get("ref", "") or None,
        "base_sha": base.get("sha", "") or None,
    }

    if event_type == "pr_merged":
        merged_at = pr.get("merged_at")
        if merged_at:
            event["merged_at"] = merged_at

    review = payload.get("review") or {}
    if review:
        user = review.get("user") or {}
        reviewer_login = user.get("login")
        if reviewer_login:
            event["reviewer_login"] = reviewer_login
        review_id = review.get("id")
        if review_id is not None:
            event["review_id"] = str(review_id)
        body = _truncate(review.get("body"))
        if body is not None:
            event["review_body"] = body
        review_url = review.get("html_url")
        if review_url:
            event["comment_url"] = review_url

    comment = payload.get("comment") or {}
    if comment:
        comment_id = comment.get("id")
        if comment_id is not None:
            event["comment_id"] = str(comment_id)
        comment_url = comment.get("html_url")
        if comment_url and "comment_url" not in event:
            event["comment_url"] = comment_url

    # Strip empty-string optionals for a cleaner payload
    return {k: v for k, v in event.items() if v is not None and v != ""}


async def _forward_review_body_human_issue(
    event_type: str, payload: dict[str, Any]
) -> None:
    """Forward the top-level review body as a human issue, if present and non-bot.

    Passing ``body`` to :func:`_is_bot_user` is critical: the
    harness posts reviews under a normal user account
    (``xcagentrockwell``), so ``type`` is ``"User"`` and the
    ``[bot]`` suffix check misses. The body-marker fallback
    catches those self-reviews; without it, the harness's own
    critiques were forwarded to L1 as human issues and triggered
    a self-reinforcing bot loop.
    """
    review = payload.get("review") or {}
    body = review.get("body") or ""
    if not body.strip():
        return
    user = review.get("user") or {}
    if _is_bot_user(user, body):
        return

    ticket_id = _ticket_id_from_payload(payload)
    if not ticket_id:
        logger.info("review_body_no_ticket_id", event_type=event_type)
        return

    pr = payload.get("pull_request", {}) or {}
    repo = payload.get("repository", {}) or {}
    repo_full_name = repo.get("full_name", "") or (
        pr.get("base", {}).get("repo", {}).get("full_name", "")
    )
    human_issue = {
        "repo_full_name": repo_full_name,
        "pr_number": pr.get("number", 0),
        "head_sha": _pr_head_sha(pr),
        "ticket_id": ticket_id,
        "external_id": str(review.get("id", "")),
        "event_type": event_type,
        "file_path": "",
        "line_start": 0,
        "line_end": 0,
        "summary": _truncate(body, 500) or "",
        "details": _truncate(body, 4000) or "",
        "reviewer_login": user.get("login", ""),
        "event_at": review.get("submitted_at") or datetime.now(UTC).isoformat(),
        "comment_url": review.get("html_url", ""),
    }
    await _forward_human_issue(human_issue)


async def _forward_review_events(
    event_type: str,
    payload: dict[str, Any],
    *,
    include_body: bool = True,
) -> None:
    """Forward both autonomy event and (optionally) review body as a human issue.

    Consolidates the "build autonomy event + forward + forward review
    body" prelude that was copy-pasted into ``_handle_review_comment``,
    ``_handle_review_changes_requested``, and ``_handle_review_approved``.
    ``include_body=False`` is used by issue_comment paths where no
    ``review`` object is present in the payload.
    """
    autonomy_event = _build_autonomy_event(event_type, payload)
    if autonomy_event:
        await _forward_autonomy_event(autonomy_event)
    if include_body:
        await _forward_review_body_human_issue(event_type, payload)


# --- Event handlers ---


async def _handle_pr_opened(payload: dict[str, Any]) -> None:
    """Handle a new or ready-for-review PR — spawn AI review."""
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number", 0)
    if not pr_number or not isinstance(pr_number, int) or pr_number <= 0:
        logger.debug("pr_opened_invalid_pr_number", raw=pr.get("number"))
        return
    pr_diff_url = pr.get("diff_url", "")
    pr_body = pr.get("body", "")

    log = logger.bind(pr_number=pr_number)
    log.info("handling_pr_opened")

    ticket_id = _ticket_id_from_payload(payload)
    if ticket_id:
        append_trace(ticket_id, _lookup_trace_id(ticket_id), "l3_pr_review",
                     "pr_review_spawned", pr_number=pr_number)

    # Forward autonomy event to L1 (pr_opened or pr_synchronized based on action)
    action = payload.get("action", "")
    autonomy_event_type = "pr_synchronized" if action == "synchronize" else "pr_opened"
    autonomy_event = _build_autonomy_event(autonomy_event_type, payload)
    if autonomy_event:
        await _forward_autonomy_event(autonomy_event)

    branch = _pr_head_branch(pr)
    repo = payload.get("repository", {}).get("full_name", "")
    _get_spawner().spawn_pr_review(
        pr_number=pr_number,
        pr_diff=f"Diff available at: {pr_diff_url}",
        ticket_context=pr_body,
        branch=branch,
        repo=repo,
    )


async def _fetch_ci_logs(repo: str, run_id: int) -> str:
    """Fetch CI failure logs from GitHub Actions API."""
    import httpx

    gh_token = os.getenv("GITHUB_TOKEN", "")
    if not gh_token or not run_id:
        return ""

    try:
        async with httpx.AsyncClient() as client:
            # Get failed jobs
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs",
                headers={
                    "Authorization": f"Bearer {gh_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=15.0,
            )
            if resp.status_code != 200:
                return f"Failed to fetch jobs: HTTP {resp.status_code}"

            jobs = resp.json().get("jobs", [])
            failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]

            logs: list[str] = []
            for job in failed_jobs[:3]:  # Limit to 3 failed jobs
                job_name = job.get("name", "unknown")
                steps = job.get("steps", [])
                failed_steps = [
                    s for s in steps if s.get("conclusion") == "failure"
                ]
                for step in failed_steps:
                    logs.append(
                        f"Job: {job_name} | Step: {step.get('name', '?')} "
                        f"| Status: {step.get('conclusion', '?')}"
                    )

            return "\n".join(logs) if logs else "CI failed but no step details available"
    except Exception as exc:
        logger.warning("ci_log_fetch_failed", error=str(exc))
        return f"Failed to fetch CI logs: {exc}"


async def _handle_ci_failed(payload: dict[str, Any]) -> None:
    """Handle CI failure — fetch logs and spawn fix agent."""
    check = payload.get("check_suite", payload.get("check_run", {}))
    pr_numbers = [
        n for pr in check.get("pull_requests", [])
        if (n := pr.get("number")) and isinstance(n, int) and n > 0
    ]
    branch = check.get("head_branch", "")
    conclusion = check.get("conclusion", "")
    repo = (
        check.get("repository", {}).get("full_name", "")
        or payload.get("repository", {}).get("full_name", "")
    )

    if not pr_numbers:
        logger.debug("ci_failure_no_pr", branch=branch)
        return

    log = logger.bind(pr_numbers=pr_numbers, branch=branch)
    log.info("handling_ci_failure")

    # Trace CI failure — extract ticket ID from branch name
    match = _AI_BRANCH_PATTERN.match(branch)
    if match:
        ci_ticket_id = match.group(1)
        append_trace(ci_ticket_id, _lookup_trace_id(ci_ticket_id), "l3_ci_fix",
                     "ci_fix_spawned", branch=branch, pr_numbers=pr_numbers)

    # Fetch actual failure logs from GitHub Actions
    run_id = check.get("id", 0)
    failure_logs = await _fetch_ci_logs(repo, run_id)
    if not failure_logs:
        failure_logs = f"CI {conclusion} on branch {branch}. Check the Actions tab for details."

    log.info("ci_logs_fetched", log_length=len(failure_logs))

    for pr_number in pr_numbers:
        _get_spawner().spawn_ci_fix(
            pr_number=pr_number,
            branch=branch,
            failure_logs=failure_logs,
            repo=repo,
        )


async def _handle_review_comment(payload: dict[str, Any]) -> None:
    """Handle human review comment — spawn response agent."""
    # From pull_request_review event
    review = payload.get("review", {})
    if review:
        if _is_bot_comment(payload, ["review", "user"]):
            logger.debug("ignoring_bot_review_comment")
            return
        pr_number = payload.get("pull_request", {}).get("number", 0)
        comment_body = review.get("body", "")
        comment_author = review.get("user", {}).get("login", "unknown")
    else:
        # From issue_comment event
        if _is_bot_comment(payload, ["comment", "user"]):
            logger.debug("ignoring_bot_issue_comment")
            return
        issue = payload.get("issue", {})
        pr_number = issue.get("number", 0)
        comment = payload.get("comment", {})
        comment_body = comment.get("body", "")
        comment_author = comment.get("user", {}).get("login", "unknown")

    if not comment_body.strip():
        return
    if not pr_number or not isinstance(pr_number, int) or pr_number <= 0:
        logger.debug("review_comment_invalid_pr_number", raw=pr_number)
        return

    log = logger.bind(pr_number=pr_number, author=comment_author)
    log.info("handling_review_comment")

    # Forward autonomy event + review body (body only when a review
    # object is present; issue_comment events skip it).
    await _forward_review_events(
        "review_comment", payload, include_body=bool(review)
    )

    ticket_id = _ticket_id_from_payload(payload)
    if ticket_id:
        append_trace(ticket_id, _lookup_trace_id(ticket_id), "l3_comment",
                     "comment_response_spawned", pr_number=pr_number,
                     author=comment_author)

    pr = payload.get("pull_request", {})
    branch = _pr_head_branch(pr)
    repo = payload.get("repository", {}).get("full_name", "")
    _get_spawner().spawn_comment_response(
        pr_number=pr_number,
        comment_body=comment_body,
        comment_author=comment_author,
        branch=branch,
        repo=repo,
    )


async def _handle_review_changes_requested(payload: dict[str, Any]) -> None:
    """Handle change requests — spawn targeted fix agent."""
    if _is_bot_comment(payload, ["review", "user"]):
        logger.debug("ignoring_bot_changes_requested")
        return
    review = payload.get("review", {})
    ctx = _pr_handler_ctx(payload)
    review_body = review.get("body", "")
    reviewer = review.get("user", {}).get("login", "unknown")

    if not ctx.pr_number or ctx.pr_number <= 0:
        logger.debug("changes_requested_invalid_pr_number", raw=ctx.pr_number)
        return

    log = logger.bind(pr_number=ctx.pr_number, reviewer=reviewer)
    log.info("handling_changes_requested")

    await _forward_review_events("review_changes_requested", payload)

    if ctx.ticket_id:
        append_trace(
            ctx.ticket_id, _lookup_trace_id(ctx.ticket_id),
            "l3_changes_requested", "changes_requested_spawned",
            pr_number=ctx.pr_number, reviewer=reviewer,
        )

    _get_spawner().spawn_comment_response(
        pr_number=ctx.pr_number,
        comment_body=f"Changes requested by @{reviewer}:\n\n{review_body[:3000]}",
        comment_author=reviewer,
        branch=ctx.branch,
        repo=ctx.repo,
    )


async def _handle_review_approved(payload: dict[str, Any]) -> None:
    """Handle PR approval — check if auto-merge is appropriate."""
    if _is_bot_comment(payload, ["review", "user"]):
        logger.debug("ignoring_bot_approval")
        return
    ctx = _pr_handler_ctx(payload)
    reviewer = payload.get("review", {}).get("user", {}).get("login", "unknown")

    log = logger.bind(pr_number=ctx.pr_number, reviewer=reviewer)
    log.info("handling_review_approved")

    await _forward_review_events("review_approved", payload)

    # Approvals on non-AI branches (main, develop, a human PR that
    # happens to be on this repo) must not trigger L1's
    # /api/agent-complete — silently marking an unrelated branch as
    # a "completed ticket".
    if not ctx.ticket_id:
        log.info(
            "pr_approved_skipped_non_ai_branch",
            branch=ctx.branch,
            reason="branch does not match ai/<TICKET>-<N> pattern",
        )
        return

    append_trace(
        ctx.ticket_id, _lookup_trace_id(ctx.ticket_id),
        "l3_approval", "review_approved",
        pr_number=ctx.pr_number, reviewer=reviewer,
    )

    ticket_type = _ticket_type_from_labels(ctx.labels)

    # Notify L1 of the approval for autonomy tracking via the shared
    # retry helper. Phase 1 auth: L1's /api/agent-complete is gated
    # behind ``_require_api_key`` (X-API-Key header). Reuse the
    # L1_INTERNAL_API_TOKEN env var as the shared secret — L1 and L3
    # already share its value for the autonomy event path, and adding
    # a second env var for the same shared secret invites drift.
    await _post_to_l1_with_retry(
        "/api/agent-complete",
        {
            "ticket_id": ctx.ticket_id,
            "status": "complete",
            "pr_url": ctx.pr.get("html_url", ""),
            "branch": ctx.branch,
        },
        log_event="l1_agent_complete_failed",
        log_context={"ticket_id": ctx.ticket_id, "branch": ctx.branch},
        headers={"X-API-Key": os.getenv("L1_INTERNAL_API_TOKEN") or ""},
    )

    log.info(
        "pr_approved",
        ticket_type=ticket_type,
        branch=ctx.branch,
        repo=ctx.repo,
    )

    try:
        await evaluate_and_maybe_merge(
            repo_full_name=ctx.repo,
            pr_number=ctx.pr_number,
            head_sha=ctx.head_sha,
            ticket_id=ctx.ticket_id,
            ticket_type=ticket_type,
            trigger_event="review_approved",
        )
    except Exception:
        log.exception("auto_merge_evaluation_failed")


async def _handle_review_comment_created(payload: dict[str, Any]) -> None:
    """Handle line-anchored PR review comment — forward as a human issue to L1."""
    comment = payload.get("comment") or {}
    if not comment:
        return
    action = payload.get("action", "")
    if action not in ("created", "edited"):
        return
    body = comment.get("body") or ""
    if not body.strip():
        return
    user = comment.get("user") or {}
    # _is_bot_user now also checks BOT_USERNAME and the
    # BOT_COMMENT_MARKER in body, so the explicit marker guard
    # below is redundant — kept as-is to preserve the specific
    # "ignoring_marker_*" log event during the transition.
    if _is_bot_user(user, body):
        logger.debug("ignoring_bot_review_comment_created")
        return

    ticket_id = _ticket_id_from_payload(payload)
    if not ticket_id:
        logger.info("review_comment_no_ticket_id")
        return

    pr = payload.get("pull_request", {}) or {}
    repo = payload.get("repository", {}) or {}
    repo_full_name = repo.get("full_name", "") or (
        pr.get("base", {}).get("repo", {}).get("full_name", "")
    )
    line_start = comment.get("line") or comment.get("original_line") or 0
    line_end = line_start

    human_issue = {
        "repo_full_name": repo_full_name,
        "pr_number": pr.get("number", 0),
        "head_sha": _pr_head_sha(pr),
        "ticket_id": ticket_id,
        "external_id": str(comment.get("id", "")),
        "event_type": "review_comment",
        "file_path": comment.get("path", ""),
        "line_start": int(line_start) if line_start else 0,
        "line_end": int(line_end) if line_end else 0,
        "summary": _truncate(body, 500) or "",
        "details": _truncate(body, 4000) or "",
        "reviewer_login": user.get("login", ""),
        "event_at": comment.get("created_at") or datetime.now(UTC).isoformat(),
        "comment_url": comment.get("html_url", ""),
    }
    await _forward_human_issue(human_issue)


async def _handle_pr_merged(payload: dict[str, Any]) -> None:
    """Handle PR merged — forward autonomy event to L1."""
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number", 0)
    if not pr_number or not isinstance(pr_number, int) or pr_number <= 0:
        logger.debug("pr_merged_invalid_pr_number", raw=pr.get("number"))
        return
    merged_at = pr.get("merged_at")

    log = logger.bind(pr_number=pr_number, merged_at=merged_at)
    log.info("handling_pr_merged")

    autonomy_event = _build_autonomy_event(
        "pr_merged", payload, event_at=merged_at or None
    )
    if autonomy_event:
        await _forward_autonomy_event(autonomy_event)


async def _handle_ci_passed(payload: dict[str, Any]) -> None:
    """Handle CI passing — check if PR is approved and ready for auto-merge."""
    check = payload.get("check_suite", payload.get("check_run", {}))
    pr_entries = check.get("pull_requests", []) or []
    pr_numbers = [
        n for pr in pr_entries
        if (n := pr.get("number")) and isinstance(n, int) and n > 0
    ]
    branch = check.get("head_branch", "")
    head_sha = check.get("head_sha", "") or check.get("head_commit", {}).get("id", "")
    repo_full_name = (
        payload.get("repository", {}).get("full_name", "")
        or check.get("repository", {}).get("full_name", "")
    )

    if not pr_numbers:
        return

    log = logger.bind(pr_numbers=pr_numbers, branch=branch)
    log.info("ci_passed", branch=branch)

    # Derive ticket_id from branch (ai/TICKET-123)
    match = _AI_BRANCH_PATTERN.match(branch or "")
    ticket_id = match.group(1) if match else ""

    # Phase 4: evaluate auto-merge for each PR (check suite may be attached to multiple)
    for pr_number in pr_numbers:
        try:
            # We need ticket_type; fetch labels via PR state
            pr_state = await get_pr_state(repo_full_name, pr_number)
            ticket_type = _ticket_type_from_labels(
                pr_state.get("labels", []) if pr_state else [],
                default="",
            )
            await evaluate_and_maybe_merge(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                ticket_id=ticket_id,
                ticket_type=ticket_type,
                trigger_event="ci_passed",
            )
        except Exception:
            log.exception("auto_merge_evaluation_failed", pr_number=pr_number)


async def _handle_issue_labeled(payload: dict[str, Any]) -> None:
    """Handle GitHub issues.labeled event — forward as defect-link if applicable.

    Checks if any label matches the configured defect labels
    (GITHUB_DEFECT_LABELS env var, default: defect,bug,regression).
    If so, parses the issue body for a PR reference and forwards the
    normalized payload to L1.
    """
    if payload.get("action", "") != "labeled":
        return
    issue = payload.get("issue") or {}
    if not issue:
        return

    defect_labels_env = os.getenv(
        "GITHUB_DEFECT_LABELS", "defect,bug,regression"
    )
    defect_labels = [
        label.strip().lower()
        for label in defect_labels_env.split(",")
        if label.strip()
    ]
    labels = [
        (label_obj or {}).get("name", "")
        for label_obj in (issue.get("labels") or [])
    ]
    lower_labels = {label_name.lower() for label_name in labels}
    if not any(defect_label in lower_labels for defect_label in defect_labels):
        logger.debug(
            "github_issue_labeled_not_defect",
            issue_number=issue.get("number"),
            labels=labels,
        )
        return

    repo_full_name = (payload.get("repository") or {}).get("full_name", "")
    body = issue.get("body") or ""
    pr_ref = _extract_pr_ref(body, repo_full_name)
    if not pr_ref:
        logger.info(
            "github_defect_no_pr_ref",
            issue_number=issue.get("number"),
            repo=repo_full_name,
        )
        return
    pr_repo, pr_number = pr_ref

    forward_payload = {
        "issue_number": int(issue.get("number") or 0),
        "issue_url": issue.get("html_url", "") or "",
        "issue_title": (issue.get("title") or "")[:500],
        "issue_body": body[:2000],
        "labels": labels,
        "reported_at": issue.get("created_at", "") or "",
        "reporter_login": (issue.get("user") or {}).get("login", "") or "",
        "pr_repo_full_name": pr_repo,
        "pr_number": pr_number,
        "category": _category_from_labels(labels),
        "severity": "",
    }
    logger.info(
        "github_defect_forwarding",
        issue_number=forward_payload["issue_number"],
        pr_repo=pr_repo,
        pr_number=pr_number,
        category=forward_payload["category"],
    )
    await _forward_github_defect(forward_payload)


# Route map
_HANDLERS: dict[EventType, Any] = {
    EventType.PR_OPENED: _handle_pr_opened,
    EventType.PR_SYNCHRONIZE: _handle_pr_opened,  # Re-review on new commits
    EventType.PR_READY_FOR_REVIEW: _handle_pr_opened,
    EventType.PR_MERGED: _handle_pr_merged,
    EventType.CI_FAILED: _handle_ci_failed,
    EventType.CI_PASSED: _handle_ci_passed,
    EventType.REVIEW_APPROVED: _handle_review_approved,
    EventType.REVIEW_COMMENT: _handle_review_comment,
    EventType.REVIEW_CHANGES_REQUESTED: _handle_review_changes_requested,
    EventType.REVIEW_COMMENT_CREATED: _handle_review_comment_created,
    EventType.ISSUE_LABELED: _handle_issue_labeled,
}


# --- Endpoints ---


@app.on_event("startup")
async def _drain_backlog_on_startup() -> None:
    async def _drain() -> None:
        forwarders: dict[str, Callable[[dict[str, Any]], Awaitable[bool]]] = {
            "autonomy_event": _forward_autonomy_event_once,
            "human_issue": _forward_human_issue_once,
            "github_defect": _forward_github_defect_once,
        }
        try:
            await drain_backlog(forwarders)
        except Exception:
            logger.exception("l3_backlog_startup_drain_failed")

    # Fire-and-forget so startup isn't blocked on L1 being down.
    # Store reference so asyncio doesn't GC the task mid-flight.
    _startup_tasks.add(asyncio.create_task(_drain()))


@app.post("/admin/backlog/drain")
async def post_drain_backlog(
    x_internal_api_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_internal_api_token(x_internal_api_token)
    forwarders: dict[str, Callable[[dict[str, Any]], Awaitable[bool]]] = {
        "autonomy_event": _forward_autonomy_event_once,
        "human_issue": _forward_human_issue_once,
        "github_defect": _forward_github_defect_once,
    }
    result = await drain_backlog(forwarders)
    return {"status": "ok", **result}


@app.get("/admin/backlog/status")
async def get_backlog_status(
    x_internal_api_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_internal_api_token(x_internal_api_token)
    return backlog_status()


@app.get("/health")
async def health() -> dict[str, Any]:
    warnings: list[str] = []
    if not L1_INTERNAL_API_TOKEN:
        warnings.append("L1_INTERNAL_API_TOKEN not set — event forwarding disabled")
    if not os.getenv("GITHUB_TOKEN") and not os.getenv("AGENT_GH_TOKEN"):
        warnings.append("No GitHub token configured — PR state fetching will fail")
    status = "degraded" if warnings else "ok"
    result: dict[str, Any] = {"status": status}
    if warnings:
        result["warnings"] = warnings
    return result


@app.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None, alias="x-hub-signature-256"),
    x_github_event: str | None = Header(default=None, alias="x-github-event"),
    x_github_delivery: str | None = Header(default=None, alias="x-github-delivery"),
) -> dict[str, str]:
    """Receive GitHub webhooks for PR events.

    Phase 1 fail-closed: when ``GITHUB_WEBHOOK_SECRET`` is unset and
    ``ALLOW_UNSIGNED_WEBHOOKS=true`` is not set either, raise 503.
    Previously we accepted unsigned requests whenever no secret was
    configured ("dev mode"), which meant any misconfigured production
    deploy silently accepted anonymous webhooks.
    """
    body = await request.body()

    # Validate signature — fail-closed by default.
    allow_unsigned = os.getenv("ALLOW_UNSIGNED_WEBHOOKS", "").lower() == "true"
    if not WEBHOOK_SECRET and not allow_unsigned:
        raise HTTPException(
            status_code=503, detail="GITHUB_WEBHOOK_SECRET not configured"
        )
    if WEBHOOK_SECRET:
        if not x_hub_signature_256:
            raise HTTPException(status_code=401, detail="Missing webhook signature")
        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Dedup: skip if this delivery was already processed (webhook retry)
    delivery_id = x_github_delivery or ""
    if delivery_id and delivery_id in _processed_deliveries:
        logger.debug("duplicate_delivery_skipped", delivery_id=delivery_id)
        return {"status": "skipped", "reason": "duplicate delivery"}
    if delivery_id:
        _processed_deliveries[delivery_id] = None
        while len(_processed_deliveries) > _MAX_DELIVERY_CACHE:
            _processed_deliveries.popitem(last=False)  # FIFO: evict oldest

    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc

    headers = {
        "x-github-event": x_github_event or "",
    }

    event_type = classify_event(headers, payload)
    logger.info(
        "github_webhook_received",
        event_type=event_type,
        github_event=x_github_event,
        delivery_id=delivery_id,
    )

    if event_type == EventType.IGNORED:
        return {"status": "ignored", "event_type": event_type}

    handler = _HANDLERS.get(event_type)
    if handler:
        background_tasks.add_task(handler, payload)
        return {"status": "accepted", "event_type": event_type}

    return {"status": "unhandled", "event_type": event_type}


# --- ADO Service Hook webhook ---

ADO_WEBHOOK_TOKEN = os.getenv("ADO_WEBHOOK_TOKEN", "")

_ADO_BRANCH_PATTERN = re.compile(r"^refs/heads/ai/([A-Za-z0-9]+-\d+)")


def _ticket_id_from_ado_payload(payload: dict[str, Any]) -> str:
    """Extract ticket ID from ADO PR source branch (e.g., refs/heads/ai/SCRUM-16 -> SCRUM-16)."""
    resource = payload.get("resource", {})
    source_ref = resource.get("sourceRefName", "")
    match = _ADO_BRANCH_PATTERN.match(source_ref)
    return match.group(1) if match else ""


async def _handle_ado_pr_opened(payload: dict[str, Any]) -> None:
    """Handle a new ADO pull request -- log and prepare for spawner integration."""
    resource = payload.get("resource", {})
    pr_id = resource.get("pullRequestId", 0)
    repo_name = resource.get("repository", {}).get("name", "")
    project = resource.get("repository", {}).get("project", {}).get("name", "")
    source_ref = resource.get("sourceRefName", "")
    title = resource.get("title", "")
    ticket_id = _ticket_id_from_ado_payload(payload)

    log = logger.bind(
        pr_id=pr_id,
        repo=repo_name,
        project=project,
        ticket_id=ticket_id,
        source_control_type="azure-repos",
    )
    log.info(
        "handling_ado_pr_opened",
        title=title,
        source_ref=source_ref,
    )

    if ticket_id:
        append_trace(
            ticket_id,
            _lookup_trace_id(ticket_id),
            "l3_pr_review",
            "ado_pr_review_spawned",
            pr_id=pr_id,
            source_control_type="azure-repos",
        )

    # TODO: Wire into spawner with source_control_type="azure-repos"
    # once SessionSpawner supports ADO PR review sessions.
    log.info("ado_pr_opened_logged", note="spawner integration pending")


def _ado_pr_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract common ADO PR context fields from a webhook payload."""
    resource = payload.get("resource", {})
    repo = resource.get("repository", {})
    return {
        "pr_id": resource.get("pullRequestId", 0),
        "repo_id": repo.get("id", ""),
        "repo_name": repo.get("name", ""),
        "project": repo.get("project", {}).get("name", ""),
        "org_url": _ado_org_url_from_payload(payload),
        "title": resource.get("title", ""),
        "head_sha": (resource.get("lastMergeSourceCommit") or {}).get("commitId", ""),
        "ticket_id": _ticket_id_from_ado_payload(payload),
        "labels": [label.get("name", "") for label in resource.get("labels", [])],
    }


def _ado_org_url_from_payload(payload: dict[str, Any]) -> str:
    """Extract the ADO org URL from the webhook payload's resourceContainers."""
    containers = payload.get("resourceContainers", {})
    collection = containers.get("collection", {})
    base_url = collection.get("baseUrl", "")
    if base_url:
        return base_url.rstrip("/")
    # Fallback: use ADO_ORG_URL env var
    return os.getenv("ADO_ORG_URL", "").rstrip("/")


async def _handle_ado_review_approved(payload: dict[str, Any]) -> None:
    """Handle ADO PR approval — evaluate auto-merge."""
    ctx = _ado_pr_context(payload)
    ticket_id = ctx["ticket_id"]

    log = logger.bind(
        pr_id=ctx["pr_id"],
        project=ctx["project"],
        ticket_id=ticket_id,
        source_control_type="azure-repos",
    )
    log.info("handling_ado_review_approved")

    if not ticket_id:
        log.info(
            "ado_pr_approved_skipped_non_ai_branch",
            reason="branch does not match ai/<TICKET>-<N> pattern",
        )
        return

    append_trace(
        ticket_id, _lookup_trace_id(ticket_id),
        "l3_approval", "ado_review_approved",
        pr_id=ctx["pr_id"],
        source_control_type="azure-repos",
    )

    ticket_type = _ticket_type_from_labels(ctx["labels"])

    try:
        await evaluate_and_maybe_merge_ado(
            org_url=ctx["org_url"],
            project=ctx["project"],
            repo_id=ctx["repo_id"],
            pr_id=ctx["pr_id"],
            head_sha=ctx["head_sha"],
            ticket_id=ticket_id,
            ticket_type=ticket_type,
            trigger_event="review_approved",
        )
    except Exception:
        log.exception("ado_auto_merge_evaluation_failed")


async def _handle_ado_review_changes_requested(payload: dict[str, Any]) -> None:
    """Handle ADO PR rejection — log and post comment."""
    ctx = _ado_pr_context(payload)
    ticket_id = ctx["ticket_id"]

    log = logger.bind(
        pr_id=ctx["pr_id"],
        project=ctx["project"],
        ticket_id=ticket_id,
        source_control_type="azure-repos",
    )
    log.info("handling_ado_review_changes_requested")

    if ticket_id:
        append_trace(
            ticket_id, _lookup_trace_id(ticket_id),
            "l3_changes_requested", "ado_changes_requested",
            pr_id=ctx["pr_id"],
            source_control_type="azure-repos",
        )

    if ctx["org_url"] and ctx["repo_id"] and ctx["pr_id"]:
        ok, msg = await post_ado_pr_comment(
            ctx["org_url"],
            ctx["project"],
            ctx["repo_id"],
            ctx["pr_id"],
            "Changes were requested by a reviewer. The agent will address "
            "feedback once comment response spawning supports Azure Repos.",
        )
        if not ok:
            log.warning("ado_pr_comment_failed", reason=msg)


async def _handle_ado_review_comment(payload: dict[str, Any]) -> None:
    """Handle ADO PR review comment — log trace entry."""
    ctx = _ado_pr_context(payload)
    ticket_id = ctx["ticket_id"]

    log = logger.bind(
        pr_id=ctx["pr_id"],
        project=ctx["project"],
        ticket_id=ticket_id,
        source_control_type="azure-repos",
    )
    log.info("handling_ado_review_comment")

    if ticket_id:
        append_trace(
            ticket_id, _lookup_trace_id(ticket_id),
            "l3_comment", "ado_review_comment",
            pr_id=ctx["pr_id"],
            source_control_type="azure-repos",
        )

    # Spawner does not yet support ADO comment response sessions
    log.info("ado_review_comment_logged", note="spawner integration pending")


async def _handle_ado_build_complete(payload: dict[str, Any]) -> None:
    """Handle ADO build.complete — evaluate auto-merge if CI passed on a PR."""
    resource = payload.get("resource", {})
    result = str(resource.get("result", "")).lower()

    # Try to extract PR association
    trigger_info = resource.get("triggerInfo") or {}
    pr_number_str = trigger_info.get("pr.number") or (
        trigger_info.get("pr", {}).get("number", "")
        if isinstance(trigger_info.get("pr"), dict)
        else ""
    )
    pr_id = int(pr_number_str) if pr_number_str and str(pr_number_str).isdigit() else 0
    reason = resource.get("reason", "")

    # Extract repo and project info from the build resource
    repo_info = resource.get("repository", {})
    repo_id = repo_info.get("id", "")
    project = resource.get("project", {}).get("name", "")
    source_branch = resource.get("sourceBranch", "")

    org_url = _ado_org_url_from_payload(payload)

    log = logger.bind(
        build_result=result,
        pr_id=pr_id,
        reason=reason,
        project=project,
        source_control_type="azure-repos",
    )
    log.info("handling_ado_build_complete")

    if result != "succeeded":
        log.info("ado_build_not_succeeded", result=result)
        return

    if not pr_id and reason == "pullRequest":
        # Build was triggered by a PR but pr.number not in triggerInfo;
        # we can't reliably associate it. Log and skip.
        log.info("ado_build_pr_association_missing")
        return

    if not pr_id:
        log.info("ado_build_not_pr_triggered")
        return

    # Extract ticket_id from source branch
    match = _ADO_BRANCH_PATTERN.match(source_branch or "")
    ticket_id = match.group(1) if match else ""

    # Extract the commit sha the build ran against. ADO surfaces it as
    # ``sourceVersion`` on the build resource, with ``sourceBranchCommit``
    # as a legacy fallback. If a force-push landed AFTER the build
    # started, this sha will no longer match the PR's current head —
    # ``_evaluate_core`` does the comparison and skips with reason
    # ``build_sha_stale``. The prior behavior passed ``head_sha=""``
    # unconditionally, which meant the build was trusted even after
    # a force-push invalidated the CI result.
    build_sha = str(
        resource.get("sourceVersion")
        or resource.get("sourceBranchCommit")
        or ""
    )

    try:
        await evaluate_and_maybe_merge_ado(
            org_url=org_url,
            project=project,
            repo_id=repo_id,
            pr_id=pr_id,
            head_sha=build_sha,  # non-empty: build_sha_stale check runs
            ticket_id=ticket_id,
            ticket_type="",
            trigger_event="ci_passed",
            checks_passed=True,
        )
    except Exception:
        log.exception("ado_auto_merge_from_build_failed")


@app.post("/webhooks/ado-build", status_code=202)
async def ado_build_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_ado_webhook_token: str | None = Header(
        default=None, alias="x-ado-webhook-token"
    ),
) -> dict[str, str]:
    """Receive Azure DevOps Service Hook webhooks for build.complete events.

    Phase 1 fail-closed: when ``ADO_WEBHOOK_TOKEN`` is unset and
    ``ALLOW_UNSIGNED_WEBHOOKS=true`` is not set either, raise 503.
    """
    allow_unsigned = os.getenv("ALLOW_UNSIGNED_WEBHOOKS", "").lower() == "true"
    if not ADO_WEBHOOK_TOKEN and not allow_unsigned:
        raise HTTPException(
            status_code=503, detail="ADO_WEBHOOK_TOKEN not configured"
        )
    if ADO_WEBHOOK_TOKEN and (
        not x_ado_webhook_token
        or not hmac.compare_digest(x_ado_webhook_token, ADO_WEBHOOK_TOKEN)
    ):
        raise HTTPException(status_code=401, detail="Invalid ADO webhook token")

    body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc

    event_type = payload.get("eventType", "")
    if event_type != "build.complete":
        logger.debug("ado_build_webhook_wrong_event", event_type=event_type)
        return {"status": "ignored", "event_type": event_type}

    resource = payload.get("resource", {})
    result = str(resource.get("result", "")).lower()

    logger.info(
        "ado_build_webhook_received",
        event_type=event_type,
        build_result=result,
        build_id=resource.get("id", ""),
    )

    if result == "succeeded":
        background_tasks.add_task(_handle_ado_build_complete, payload)
        return {"status": "accepted", "event_type": "ci_passed"}

    if result in ("failed", "partiallysucceeded"):
        # Log CI failure; no auto-fix for ADO yet
        logger.info("ado_ci_failed", result=result)
        return {"status": "accepted", "event_type": "ci_failed"}

    return {"status": "ignored", "event_type": event_type}


@app.post("/webhooks/ado-pr", status_code=202)
async def ado_pr_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_ado_webhook_token: str | None = Header(
        default=None, alias="x-ado-webhook-token"
    ),
) -> dict[str, str]:
    """Receive Azure DevOps Service Hook webhooks for PR events.

    Phase 1 fail-closed: when ``ADO_WEBHOOK_TOKEN`` is unset and
    ``ALLOW_UNSIGNED_WEBHOOKS=true`` is not set either, raise 503.
    """
    # Validate token if configured (constant-time comparison).
    allow_unsigned = os.getenv("ALLOW_UNSIGNED_WEBHOOKS", "").lower() == "true"
    if not ADO_WEBHOOK_TOKEN and not allow_unsigned:
        raise HTTPException(
            status_code=503, detail="ADO_WEBHOOK_TOKEN not configured"
        )
    if ADO_WEBHOOK_TOKEN and (
        not x_ado_webhook_token
        or not hmac.compare_digest(x_ado_webhook_token, ADO_WEBHOOK_TOKEN)
    ):
        raise HTTPException(status_code=401, detail="Invalid ADO webhook token")

    body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc

    event_type = classify_ado_event(payload)

    resource = payload.get("resource", {})
    pr_id = resource.get("pullRequestId", 0)
    repo_id = resource.get("repository", {}).get("id", "")
    project = resource.get("repository", {}).get("project", {}).get("name", "")

    logger.info(
        "ado_webhook_received",
        event_type=event_type,
        ado_event_type=payload.get("eventType", ""),
        pr_id=pr_id,
        project=project,
        repo_id=repo_id,
    )

    if event_type == EventType.IGNORED:
        return {"status": "ignored", "event_type": event_type}

    if event_type == EventType.PR_OPENED:
        background_tasks.add_task(_handle_ado_pr_opened, payload)
        return {"status": "accepted", "event_type": event_type}

    if event_type == EventType.REVIEW_APPROVED:
        background_tasks.add_task(_handle_ado_review_approved, payload)
        return {"status": "accepted", "event_type": event_type}

    if event_type == EventType.REVIEW_CHANGES_REQUESTED:
        background_tasks.add_task(_handle_ado_review_changes_requested, payload)
        return {"status": "accepted", "event_type": event_type}

    if event_type == EventType.REVIEW_COMMENT:
        background_tasks.add_task(_handle_ado_review_comment, payload)
        return {"status": "accepted", "event_type": event_type}

    # Other ADO event types — log but don't act yet
    logger.info("ado_event_not_yet_handled", event_type=event_type, pr_id=pr_id)
    return {"status": "unhandled", "event_type": event_type}
