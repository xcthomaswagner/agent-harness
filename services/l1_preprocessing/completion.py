"""Agent completion / retest / live-trace endpoints.

Extracted from ``main.py`` as part of the Phase 4 structural refactor.
These endpoints are the spawn script's and running agent's control plane:
``/api/agent-complete`` (agent finished, update Jira/ADO), ``/api/retest``
(rerun a phase on an existing branch), ``/api/agent-trace`` (live event
stream from the file watcher).

Mounted on ``router`` below; included by ``main.py``.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.env_sanitize import sanitized_env

from adapters.ado_adapter import AdoAdapter
from adapters.jira_adapter import JiraAdapter
from auth import _require_api_key
from claim_store import _clear_trigger_state, _release_ticket
from tracer import append_trace, consolidate_worktree_logs, generate_trace_id


def _settings() -> Any:
    """Resolve settings through main — see webhooks._settings for the
    reason. Keep identical to preserve ``patch("main.settings")`` behavior.
    """
    import main  # local import dodges module-load circular import
    return main.settings


logger = structlog.get_logger()

router = APIRouter()

_TICKET_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+-[0-9]+$")
# Branch names: alphanumeric + slashes/underscores/dots/hyphens, but the
# ``..`` sequence is forbidden to prevent path traversal into sibling
# directories of the worktrees parent. The containment check below uses
# Path.is_relative_to as the real guardrail; this regex is belt-and-
# braces so a bad branch fails fast at input validation.
_BRANCH_PATTERN = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9/_.-]*$")

# Phase 7: reserved git ref names that the character-class pattern
# above doesn't catch. ``HEAD`` and friends pass the regex but can
# resolve to surprising paths inside ``.git/``; ``*.lock`` is git's
# in-progress ref lock convention and writing through such a name
# breaks concurrent ref updates; leading/trailing ``/`` and the
# literal ``.git`` open other git-internal paths.
_RESERVED_BRANCH_NAMES = frozenset({
    "HEAD", "ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD"
})


def _is_safe_branch(name: str) -> bool:
    """Return True when the name is safe to use as a worktree branch.

    Combines the character-class ``_BRANCH_PATTERN`` with rejection
    of git-reserved ref names (``HEAD``/``ORIG_HEAD``/etc.), the
    ``.lock`` suffix (git's in-progress ref-lock convention), and
    leading/trailing slash. Belt-and-braces: callers that also
    resolve the resulting path against a containment root catch
    the remaining traversal vectors.
    """
    if not name or not _BRANCH_PATTERN.match(name):
        return False
    if name in _RESERVED_BRANCH_NAMES:
        return False
    if name == ".git":
        return False
    if name.startswith("/") or name.endswith("/"):
        return False
    if name.endswith(".lock"):
        return False
    return True


_VALID_PHASES = {"qa", "e2e", "review"}


def _validate_ticket_id(ticket_id: str) -> str:
    """Guard the ticket_id path parameter against traversal / injection.

    Must match ``[A-Za-z0-9_-]+`` — no dots, no slashes, no null bytes.
    Returns the ticket_id on success or raises HTTPException(400).
    """
    if not ticket_id or not re.match(r"^[A-Za-z0-9_-]+$", ticket_id):
        raise HTTPException(status_code=400, detail="Invalid ticket_id")
    return ticket_id


def _resolve_worktree_dir(client_repo: str, branch: str) -> Path:
    """Validate ``branch`` and return the resolved worktree directory.

    Shared by ``/api/retest`` and ``/api/agent-complete`` — both
    construct ``<client_repo>/../worktrees/<branch>`` from a
    request-supplied branch name and must defend against ``..`` and
    sibling-prefix traversal (e.g. ``worktrees-evil``). Raises
    ``HTTPException(400)`` on invalid regex, empty branch, or a path
    that resolves outside the worktrees parent directory.

    Callers should still verify ``result.exists()`` themselves since
    the semantics of "worktree doesn't exist yet" vs "branch is
    invalid" differ across the two endpoints.
    """
    if not _is_safe_branch(branch):
        raise HTTPException(
            status_code=400,
            detail="Invalid branch name (alphanumeric, slashes, dots, hyphens only)",
        )

    worktrees_parent = (Path(client_repo).parent / "worktrees").resolve()
    worktree_resolved = (worktrees_parent / branch).resolve()
    # Path containment guard — use Path.relative_to which checks path
    # components, not string prefixes. A sibling directory like
    # ``worktrees-evil`` would pass a naive startswith check because
    # its resolved path literally starts with ``/.../worktrees``.
    try:
        worktree_resolved.relative_to(worktrees_parent)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Branch resolves outside worktree directory",
        ) from None
    return worktree_resolved


class RetestPayload(BaseModel):
    """Payload for re-running specific pipeline phases on an existing branch."""

    ticket_id: str
    phase: str = "qa"  # "qa", "e2e", "review"
    branch: str = ""  # defaults to ai/<ticket-id>


@router.post("/api/retest", status_code=202, dependencies=[Depends(_require_api_key)])
async def retest(payload: RetestPayload, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Re-run a specific phase on an existing branch.

    Usage:
        curl -X POST localhost:8000/api/retest -H 'Content-Type: application/json' \
            -d '{"ticket_id": "SCRUM-8", "phase": "e2e"}'

    Phases:
        - qa: full QA validation (unit + integration + e2e)
        - e2e: E2E browser tests only
        - review: code review only
    """
    # Input validation — ticket_id used in filesystem paths
    if not _TICKET_ID_PATTERN.match(payload.ticket_id):
        raise HTTPException(
            status_code=400, detail="Invalid ticket_id format (expected: PROJ-123)"
        )
    if payload.phase not in _VALID_PHASES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid phase '{payload.phase}'. Must be one of: {_VALID_PHASES}",
        )

    branch = payload.branch or f"ai/{payload.ticket_id}"
    settings = _settings()
    client_repo = settings.default_client_repo
    if not client_repo:
        return {"status": "error", "detail": "No default_client_repo configured"}

    worktree_resolved = _resolve_worktree_dir(client_repo, branch)
    if not worktree_resolved.exists():
        return {
            "status": "error",
            "detail": f"Worktree not found for branch '{branch}'. Run the ticket first.",
        }
    worktree_dir = str(worktree_resolved)

    log = logger.bind(ticket_id=payload.ticket_id, phase=payload.phase)
    log.info("retest_requested", branch=branch)

    phase_prompts = {
        "qa": (
            f"You are a QA validator. The code is already implemented on branch {branch}. "
            f"Read the enriched ticket at .harness/ticket.json. "
            f"Run the full test suite. If playwright.config.ts exists, also run E2E tests "
            f"by starting the dev server and using Playwright MCP. "
            f"Write your QA matrix to .harness/logs/qa-matrix.md. "
            f"If any tests were previously skipped, try to run them now and explain "
            f"any failures with exact error messages and remediation steps."
        ),
        "e2e": (
            f"You are a QA validator focused on E2E tests only. "
            f"The code is already implemented on branch {branch}. "
            f"Kill any process on port 3000 first: lsof -ti:3000 | xargs kill 2>/dev/null. "
            f"Start the dev server. Run E2E tests using Playwright MCP: "
            f"navigate pages, interact with UI, take screenshots, validate acceptance criteria. "
            f"Write results to .harness/logs/qa-e2e-retest.md. "
            f"If tests fail, include the exact error, what you tried, and how to fix."
        ),
        "review": (
            f"You are a code reviewer. The code is already on branch {branch}. "
            f"Run git diff main...HEAD and review for correctness, security, style, "
            f"and test coverage. Write your review to .harness/logs/code-review-retest.md."
        ),
    }

    prompt = phase_prompts.get(payload.phase, phase_prompts["qa"])

    env = sanitized_env()
    cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]

    def run_retest() -> None:
        try:
            log_file = Path(worktree_dir) / ".harness" / "logs" / f"retest-{payload.phase}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("w") as f:
                subprocess.run(cmd, cwd=worktree_dir, env=env, stdout=f, stderr=subprocess.STDOUT)
            log.info("retest_complete", log_file=str(log_file))
        except Exception:
            log.exception("retest_failed")

    background_tasks.add_task(run_retest)
    return {"status": "accepted", "ticket_id": payload.ticket_id, "phase": payload.phase}


class FailedUnit(BaseModel):
    """A blocked/failed implementation unit."""

    unit_id: str = ""
    description: str = ""
    failure_reason: str = ""


class CompletionPayload(BaseModel):
    """Payload sent by the spawn script when an agent finishes."""

    ticket_id: str
    source: str = "jira"
    trace_id: str = ""  # From trace-config.json — correlates with live-reported entries
    status: str  # "complete", "partial", "escalated"
    pr_url: str = ""
    branch: str = ""
    repo_full_name: str = ""
    head_sha: str = ""
    failed_units: list[FailedUnit] = []


def _derive_head_sha(worktree_path: str) -> str:
    """Run 'git rev-parse HEAD' in the worktree. Returns '' on any failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


def _derive_repo_full_name(worktree_path: str) -> str:
    """Parse 'git config --get remote.origin.url' into 'owner/repo'.

    Returns '' on any failure.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
            if m:
                return m.group(1)
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


@router.post("/api/agent-trace", status_code=200)
async def agent_trace(request: Request) -> dict[str, str]:
    """Accept live trace events from running agents.

    Called by the file-watcher thread in spawn_team.py as the agent
    writes to pipeline.jsonl. Entries appear in the dashboard in real-time.
    No auth required — internal network only (same host as spawn_team.py).

    ``ticket_id`` is validated against the trace-store id pattern BEFORE
    being passed to ``append_trace``: without validation, a path-like
    value (``../../tmp/pwn``) would escape ``LOGS_DIR`` and have
    attacker-controlled JSON appended to an arbitrary ``.jsonl`` file
    since ``append_trace`` builds ``LOGS_DIR / f"{ticket_id}.jsonl"``.
    The endpoint is intentionally open to the local file-watcher, so
    input sanitisation is the sole guardrail.
    """
    body = await request.json()
    ticket_id = str(body.pop("ticket_id", ""))
    trace_id = str(body.pop("trace_id", ""))
    phase = str(body.pop("phase", ""))
    event = str(body.pop("event", ""))
    body.pop("timestamp", None)  # append_trace generates its own timestamp
    if not ticket_id or not event:
        return {"status": "ok"}
    # Reject path-like ticket_ids (``..``, slashes, absolute paths).
    # Raises HTTPException(400) before any filesystem access.
    _validate_ticket_id(ticket_id)
    append_trace(ticket_id, trace_id, phase, event, source="agent", **body)
    return {"status": "ok"}


@router.post("/api/agent-complete", status_code=200, dependencies=[Depends(_require_api_key)])
async def agent_complete(payload: CompletionPayload) -> dict[str, str]:
    """Called by the spawn script when the agent finishes.

    Updates the Jira/ADO ticket with the PR link and transitions to Done.

    Validates both ``ticket_id`` and ``branch`` because both flow into
    filesystem paths: ``ticket_id`` becomes ``LOGS_DIR/{ticket_id}.jsonl``
    via ``append_trace`` and ``branch`` becomes the worktree path that
    ``consolidate_worktree_logs`` reads from. Without validation, a
    caller with a leaked API key could plant ``.jsonl`` files outside
    ``LOGS_DIR`` or point consolidation at an arbitrary git repo. The
    ``/api/retest`` endpoint validates both the same way; keeping the
    two endpoints symmetrical closes the defense-in-depth gap.
    """
    # Lazy imports to avoid module-load circular import: main imports this
    # router at top, we need main's adapter factory + background-task helper.
    from main import _get_ado_adapter, _get_jira_adapter, _spawn_background_task

    if not _TICKET_ID_PATTERN.match(payload.ticket_id):
        raise HTTPException(
            status_code=400,
            detail="Invalid ticket_id format (expected: PROJ-123)",
        )
    # Validate branch via the shared helper. Fall back to ``ai/<ticket_id>``
    # if the caller omitted a branch (matches the retest convention and
    # the spawn_team.py default).
    branch = payload.branch or f"ai/{payload.ticket_id}"
    settings = _settings()
    client_repo = settings.default_client_repo
    worktree_resolved: Path | None = None
    if client_repo:
        worktree_resolved = _resolve_worktree_dir(client_repo, branch)

    log = logger.bind(ticket_id=payload.ticket_id, status=payload.status)
    log.info("agent_completion_received", pr_url=payload.pr_url)

    # Delay releasing the ticket — ADO fires webhooks when we post comments
    # and transition status below. Keep the ticket claimed to absorb those
    # self-triggered webhooks before allowing reprocessing. Edge-detection
    # memory is cleared on the same schedule so the lifecycles align and a
    # future re-add of the trigger tag produces a fresh edge as expected.
    # Window is tunable via settings.agent_complete_release_delay_sec.
    async def _delayed_release(ticket_id: str) -> None:
        # Re-resolve inside the task so we see fresh patched settings.
        await asyncio.sleep(_settings().agent_complete_release_delay_sec)
        _release_ticket(ticket_id)
        _clear_trigger_state(ticket_id)

    _spawn_background_task(_delayed_release(payload.ticket_id))

    # Trace: record completion — reuse the trace_id from the spawn chain
    # so live-reported entries and completion entries share the same trace_id
    trace_id = payload.trace_id or generate_trace_id()
    append_trace(payload.ticket_id, trace_id, "completion", "agent_finished",
                 status=payload.status, pr_url=payload.pr_url, branch=branch)

    # Consolidate worktree artifacts into the persistent trace
    worktree_path = (
        str(worktree_resolved)
        if worktree_resolved is not None
        else f"{settings.default_client_repo}/../worktrees/{branch}"
    )
    try:
        repo = payload.repo_full_name or _derive_repo_full_name(worktree_path)
        sha = payload.head_sha or _derive_head_sha(worktree_path)
        consolidate_worktree_logs(
            payload.ticket_id,
            trace_id,
            worktree_path,
            repo_full_name=repo,
            head_sha=sha,
        )
    except Exception:
        log.exception("worktree_consolidation_failed", worktree=worktree_path)
        # Continue — don't block Jira updates because consolidation failed

    # Route to the correct adapter based on ticket source
    if payload.source == "ado":
        adapter: JiraAdapter | AdoAdapter = _get_ado_adapter()
    else:
        adapter = _get_jira_adapter()

    try:
        if payload.pr_url:
            comment = (
                f"*AI Pipeline — Complete*\n\n"
                f"PR: {payload.pr_url}\n"
                f"Branch: {payload.branch}\n"
                f"Status: {payload.status}"
            )
            await adapter.write_comment(payload.ticket_id, comment)

        # Link ADO work item to PR (if source is ADO and PR was created)
        if payload.pr_url and isinstance(adapter, AdoAdapter):
            try:
                await adapter.link_work_item_to_pr(payload.ticket_id, payload.pr_url)
                log.info("ado_work_item_linked_to_pr")
            except Exception:
                log.warning("ado_work_item_pr_link_failed")

        # Upload final screenshot if it exists in the worktree
        # Note: ADO adapter doesn't have upload_attachment yet — skip for ADO
        screenshot_path = Path(worktree_path) / ".harness" / "screenshots" / "final.png"
        if screenshot_path.exists() and isinstance(adapter, JiraAdapter):
            await adapter.upload_attachment(
                payload.ticket_id,
                str(screenshot_path),
                filename=f"{payload.ticket_id}-implementation.png",
            )
            log.info("screenshot_uploaded", path=str(screenshot_path))

        if payload.status not in ("complete", "partial", "escalated"):
            log.warning("unknown_completion_status", status=payload.status)
        elif payload.status == "complete":
            await adapter.transition_status(payload.ticket_id, "Done")
            log.info("ticket_transitioned_to_done")
        elif payload.status in ("partial", "escalated"):
            label = "needs-human" if payload.status == "escalated" else "partial-implementation"
            await adapter.add_label(payload.ticket_id, label)

            for unit in payload.failed_units:
                sub_comment = (
                    f"*AI Pipeline — Failed Unit: {unit.unit_id}*\n\n"
                    f"*Description:* {unit.description}\n"
                    f"*Failure:* {unit.failure_reason}\n\n"
                    f"This unit needs manual implementation or investigation."
                )
                await adapter.write_comment(payload.ticket_id, sub_comment)
                log.info("failed_unit_reported", unit_id=unit.unit_id)

    except Exception:
        log.exception("completion_update_failed")

    return {"status": "ok", "ticket_id": payload.ticket_id}
