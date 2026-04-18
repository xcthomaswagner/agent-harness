"""Trace bundle / artifact / discuss / re-redact endpoints.

Extracted from ``main.py`` as part of the Phase 4 structural refactor.
These endpoints share a narrow concern: serving or maintaining the
consolidated trace store. Grouping them here keeps ``main.py`` a thin
composition layer.

Mounted on ``router`` below; included by ``main.py``. Existing auth
dependencies (``_require_api_key`` for admin + discuss, open access for
the other two) are preserved exactly.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import secrets
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

import tracer
from auth import _require_api_key
from diagnostic import run_diagnostic_checklist
from investigate_command import (
    DISCUSS_BASE_URL as _DISCUSS_BASE_URL,
)
from investigate_command import (
    build_investigate_command as _build_investigate_command,
)
from redaction import redact
from tracer import (
    ARTIFACT_CODE_REVIEW,
    ARTIFACT_EFFECTIVE_CLAUDE_MD,
    ARTIFACT_QA_MATRIX,
    ARTIFACT_SESSION_LOG,
    ARTIFACT_SESSION_STREAM,
    ARTIFACT_TOOL_INDEX,
    atomic_write_text,
    find_artifact,
    latest_artifacts,
    read_trace,
    redact_entry_in_place,
)

logger = structlog.get_logger()


def _settings() -> Any:
    """Resolve settings through main — see webhooks._settings rationale."""
    import main  # local import dodges module-load circular import
    return main.settings


router = APIRouter()


# --- Trace bundle + individual artifact endpoints ---
#
# These power the post-mortem investigation workflow. `/bundle` packages the
# full trace context into a single gzipped tar for a developer to run a local
# `claude -p` investigation against. `/artifact/<type>` serves one file at a
# time — used by the dashboard Raw Downloads panel.
#
# Every file written into the bundle is passed through ``redact()`` before
# being tarred. The trace store itself is already redacted at consolidation
# time by ``consolidate_worktree_logs``, so most files get a belt-and-
# suspenders second pass (a no-op thanks to the redactor's idempotency
# guarantee). The one exception is ``session-stream.jsonl`` — stored by
# reference, redacted for the first time here. The readme.txt reports the
# total redaction count from the bundle pass.


def _validate_ticket_id(ticket_id: str) -> str:
    """Guard the ticket_id path parameter against traversal / injection.

    Must match ``[A-Za-z0-9_-]+`` — no dots, no slashes, no null bytes.
    Returns the ticket_id on success or raises HTTPException(400).
    """
    if not ticket_id or not re.match(r"^[A-Za-z0-9_-]+$", ticket_id):
        raise HTTPException(status_code=400, detail="Invalid ticket_id")
    return ticket_id


def _extract_ticket_payload(
    ticket_id: str, entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive a ticket.json payload for the bundle.

    Preference order:

    1. `<client_repo.parent>/worktrees/<branch>/.harness/ticket.json` if we can
       figure out the branch from trace entries and the file still exists.
    2. A minimal synthetic payload with the ticket_id and any metadata we can
       glean from the first webhook_received / processing_started entry.
    """
    branch = ""
    for e in entries:
        b = e.get("branch", "")
        if isinstance(b, str) and b:
            branch = b
            break

    branch_ok = re.fullmatch(r"[A-Za-z0-9_./][A-Za-z0-9_./ +-]*", branch)
    settings = _settings()
    if branch and settings.default_client_repo and branch_ok:
        candidate = (
            Path(settings.default_client_repo).parent
            / "worktrees" / branch / ".harness" / "ticket.json"
        )
        try:
            if candidate.exists():
                return json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    # Fallback: synthesize from the first webhook / pipeline / analyst entry
    # so the bundle always contains *some* ticket.json the investigator can
    # anchor on. Field names here must match `models.TicketPayload`
    # (checked against models.py, not guessed): `id`, `title`, `ticket_type`,
    # `source`, `description`, `priority`, `acceptance_criteria`. The
    # post-mortem-analyst skill and downstream consumers read `title` and
    # `description` directly — using `ticket_title` would silently break them.
    synthetic: dict[str, Any] = {
        "id": ticket_id,
        # ticket_id kept as a non-TicketPayload hint so old bundle consumers
        # that keyed off it still work; do not rely on it going forward.
        "ticket_id": ticket_id,
    }
    payload_fields = (
        "title",
        "ticket_type",
        "source",
        "description",
        "priority",
        "acceptance_criteria",
        "labels",
    )
    for e in entries:
        event = e.get("event", "")
        if (
            "webhook_received" in event
            or "processing_started" in event
            or "analyst_completed" in event
        ):
            for key in payload_fields:
                val = e.get(key)
                if val:
                    synthetic[key] = val
            # Also scavenge a nested "ticket" / "enriched_ticket" object if
            # the entry wrapped the payload — the analyst_completed event
            # typically serializes the full EnrichedTicket under one of
            # these keys.
            for wrapper_key in ("ticket", "enriched_ticket", "payload"):
                wrapped = e.get(wrapper_key)
                if isinstance(wrapped, dict):
                    for key in payload_fields:
                        if key in wrapped and key not in synthetic:
                            synthetic[key] = wrapped[key]
            if any(k in synthetic for k in payload_fields):
                break
    return synthetic


_BUNDLE_README = """\
Trace Bundle for ticket {ticket_id}
Generated: {timestamp}

This archive contains the full trace context for an agent run — use it as a
self-contained directory to run `claude -p` against when post-morteming a
failed or surprising run.

Files:
  pipeline.jsonl         Full trace JSONL from the L1 trace store (all layers)
  session-stream.jsonl   Raw Claude Code stream (if captured)
  session.log            Narrative session log (preview, up to 5000 chars)
  effective-CLAUDE.md    CLAUDE.md the agent was operating under
  qa-matrix.md           QA validator report (if present)
  code-review.md         Code reviewer report (if present)
  tool-index.json        Declarative tool-call index from the stream (if present)
  diagnostic.json        Six-item diagnostic checklist (always present)
  ticket.json            Normalized ticket payload (models.TicketPayload shape)

{redaction_block}

How to investigate:

  mkdir -p /tmp/trace-{ticket_id}
  curl -sSf http://localhost:8000/traces/{ticket_id}/bundle | tar xz -C /tmp/trace-{ticket_id}
  cd /tmp/trace-{ticket_id}
  claude -p "Read all the files in this directory. Start by reading diagnostic.json (if it exists) and tool-index.json, then tell me what the first deviation point was. Cite specific line numbers for every claim."
"""  # noqa: E501 — investigation prompt is a single shell literal for easy copy-paste


_REDACTION_BLOCK_REDACTED = """\
*** REDACTED ({count} patterns) ***

This bundle has been passed through the L1 secret redactor. {count} token
patterns matching known-credential shapes (API keys, JWTs, PEM private keys,
bearer headers, etc.) were replaced with [REDACTED] placeholders. The
redactor is idempotent, so re-running it over this bundle is safe. If a
pattern update lands after this bundle was built, POST /admin/re-redact on
the L1 service will rescan the underlying trace store and re-export a clean
copy.

See services/l1_preprocessing/redaction.py for the full pattern list and the
entropy-fallback heuristic that catches novel high-entropy tokens."""


_REDACTION_BLOCK_CLEAN = """\
*** REDACTED (0 patterns) ***

The L1 secret redactor ran over this bundle and flagged no token patterns.
Every file was still scanned — a zero count means the trace genuinely
contained no recognized credential shapes, not that redaction was skipped.
If you think a secret leaked through, check the entropy-fallback threshold
in services/l1_preprocessing/redaction.py and consider adding an explicit
pattern."""


def _build_bundle(ticket_id: str, entries: list[dict[str, Any]]) -> bytes:
    """Build the in-memory gzipped tar bundle for a trace.

    Kept small and simple: for almost all traces the JSONL trace store is a
    few dozen KB, the session stream is the only thing that can be large, and
    even then we're copying a file off disk into a tar — well within what
    fits in memory without worrying about streaming.

    Every file written into the tar is first passed through ``redact()``.
    For artifact contents pulled from the trace store, this is a belt-and-
    suspenders pass — the store is already redacted by
    ``consolidate_worktree_logs`` — and the redactor's idempotency guarantee
    means the second pass is a no-op on already-clean content. The
    session-stream file is a special case: it's stored by reference rather
    than inline in the trace store, so the first real redaction pass on that
    data happens here.
    """
    buf = io.BytesIO()
    added: list[str] = []
    total_redacted = 0

    def _redact_bytes(payload: bytes) -> tuple[bytes, int]:
        """Decode, redact, re-encode.

        Uses ``errors="surrogateescape"`` on both ends so non-UTF-8 bytes
        round-trip losslessly through ``redact()``. Strict UTF-8 decoding
        would silently bypass redaction on any file containing stray
        bytes — common in ``session-stream.jsonl`` where tool output can
        include terminal escape sequences, latin-1 from legacy systems,
        or binary dumps from tools like ``grep``/``curl``. Since the
        bundle path is the ONLY redaction pass that ever runs over the
        session stream (consolidation deliberately stores it by
        reference), a silent bypass would leak credentials that happened
        to share a file with any non-UTF-8 byte.
        """
        text = payload.decode("utf-8", errors="surrogateescape")
        redacted_text, n = redact(text)
        return redacted_text.encode("utf-8", errors="surrogateescape"), n

    # Build the artifact index once — six lookups below would otherwise
    # each scan the full entries list in reverse. See tracer.latest_artifacts.
    artifacts = latest_artifacts(entries)

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:

        def _add_bytes(name: str, payload: bytes) -> None:
            nonlocal total_redacted
            redacted_payload, n = _redact_bytes(payload)
            total_redacted += n
            info = tarfile.TarInfo(name=name)
            info.size = len(redacted_payload)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(redacted_payload))
            added.append(name)

        # pipeline.jsonl — full trace store state serialized one entry per line
        pipeline_lines = [json.dumps(entry) for entry in entries]
        _add_bytes("pipeline.jsonl", ("\n".join(pipeline_lines) + "\n").encode())

        # session-stream.jsonl — copied byte-for-byte from disk if we have it.
        # This is the ONLY place the stream gets redacted: consolidation
        # leaves the on-disk file alone so it remains available as a raw
        # forensic escape hatch for dev-local access.
        stream_entry = artifacts.get(ARTIFACT_SESSION_STREAM)
        if stream_entry:
            stream_path_str = str(stream_entry.get("artifact_path", ""))
            if stream_path_str:
                stream_path = Path(stream_path_str)
                try:
                    if stream_path.exists():
                        _add_bytes("session-stream.jsonl", stream_path.read_bytes())
                except OSError:
                    pass

        # session.log — 5000-char preview is the best we have in the trace store
        log_entry = artifacts.get(ARTIFACT_SESSION_LOG)
        if log_entry and log_entry.get("content"):
            _add_bytes("session.log", str(log_entry["content"]).encode())

        # effective-CLAUDE.md — instructions the agent was actually running under
        claude_md_entry = artifacts.get(ARTIFACT_EFFECTIVE_CLAUDE_MD)
        if claude_md_entry and claude_md_entry.get("content"):
            _add_bytes("effective-CLAUDE.md", str(claude_md_entry["content"]).encode())

        # qa-matrix.md
        qa_entry = artifacts.get(ARTIFACT_QA_MATRIX)
        if qa_entry and qa_entry.get("content"):
            _add_bytes("qa-matrix.md", str(qa_entry["content"]).encode())

        # code-review.md
        review_entry = artifacts.get(ARTIFACT_CODE_REVIEW)
        if review_entry and review_entry.get("content"):
            _add_bytes("code-review.md", str(review_entry["content"]).encode())

        # diagnostic.json — computed inline at bundle time. Commit 3's
        # `run_diagnostic_checklist` is a pure analyzer (no persistence), so
        # it's safe to call from here without touching the trace store. This
        # keeps commit 3 reusable for both the dashboard render AND the
        # bundle export.
        try:
            checks = run_diagnostic_checklist(entries)
            _add_bytes("diagnostic.json", json.dumps(checks, indent=2).encode())
        except Exception:
            logger.exception("diagnostic_bundle_failed", ticket_id=ticket_id)
            # Don't fail the bundle if diagnostic computation crashes —
            # continue without it so the investigator still gets the other
            # artifacts.

        # tool-index.json — declarative tool-call summary
        tool_index_entry = artifacts.get(ARTIFACT_TOOL_INDEX)
        if tool_index_entry and "index" in tool_index_entry:
            _add_bytes(
                "tool-index.json",
                json.dumps(tool_index_entry["index"], indent=2).encode(),
            )

        # ticket.json
        ticket_payload = _extract_ticket_payload(ticket_id, entries)
        _add_bytes("ticket.json", json.dumps(ticket_payload, indent=2).encode())

        # readme.txt (always last so the investigator sees it alongside files).
        # NOT redacted — it's purely generated content, and it reports the
        # redaction count from the preceding files. Sent via raw tar addfile
        # to bypass _add_bytes which would double-count the redaction pass.
        redaction_block = (
            _REDACTION_BLOCK_REDACTED.format(count=total_redacted)
            if total_redacted > 0
            else _REDACTION_BLOCK_CLEAN
        )
        readme = _BUNDLE_README.format(
            ticket_id=ticket_id,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            redaction_block=redaction_block,
        )
        readme_bytes = readme.encode()
        info = tarfile.TarInfo(name="readme.txt")
        info.size = len(readme_bytes)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(readme_bytes))
        added.append("readme.txt")

    logger.info(
        "trace_bundle_built",
        ticket_id=ticket_id,
        files=added,
        redaction_count=total_redacted,
    )
    return buf.getvalue()


# Route-ordering note: this endpoint used to live in main.py after
# ``trace_router`` was included. It works regardless of declaration order
# because `/traces/{id}` and `/traces/{id}/bundle` differ in segment count
# and FastAPI/Starlette matches by segment count before falling back to
# dynamic path parameters. Do NOT add a catch-all like ``/{rest:path}`` to
# trace_router or this route will be silently shadowed.
@router.get("/traces/{ticket_id}/bundle")
async def trace_bundle(ticket_id: str) -> Response:
    """Return a gzipped tar of the full trace context for ``ticket_id``.

    See ``_BUNDLE_README`` above for the file listing and the redaction
    warning. The bundle is built entirely in-memory — fine for today because
    the largest payload is session-stream.jsonl and traces big enough to
    cause memory pressure are rare.
    """
    _validate_ticket_id(ticket_id)
    entries = read_trace(ticket_id)
    if not entries:
        raise HTTPException(status_code=404, detail=f"No trace for ticket '{ticket_id}'")

    payload = _build_bundle(ticket_id, entries)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    filename = f"trace-{ticket_id}-{timestamp}.tar.gz"
    return Response(
        content=payload,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Discuss-with-Claude (Tier 1 post-mortem investigation handoff) ---
#
# The dashboard's "Investigate this trace locally" disclosure renders a copy-
# paste shell snippet that downloads the bundle and launches a local
# ``claude -p`` session. This endpoint returns the same snippet as JSON, plus
# a short-lived opaque session token and the bundle URL, so a future dashboard
# button (or a CLI wrapper) can hand the developer everything needed to start
# an investigation in one call.
#
# Base URL is hard-coded to ``http://localhost:8000`` to match the dashboard's
# existing investigate_cmd template — the whole feature is Tier 1, single-dev,
# local-only. Don't be clever about deriving it from the request host; that
# breaks when ngrok forwards public traffic to loopback.

_DISCUSS_SESSION_TTL = timedelta(hours=1)
_DISCUSS_AUDIT_FILENAME = "discuss-audit.jsonl"


def _discuss_audit_path() -> Path:
    """Return the on-disk path for ``discuss-audit.jsonl``.

    Kept as a sibling directory next to ``LOGS_DIR`` (``<LOGS_DIR>.parent/
    audit/``) rather than inside it. If the audit file lived in
    ``LOGS_DIR`` it would be indistinguishable from a per-ticket trace
    file: ``_validate_ticket_id`` accepts ``discuss-audit``, so
    ``GET /traces/discuss-audit/bundle``, ``list_traces``, and the
    dashboard would all treat the audit log as a phantom ticket and
    expose its contents (cross-ticket ``session_token`` / ``source_ip`` /
    ``user_agent`` values) to unauthenticated readers. Returning a
    function instead of a module-level constant so tests that monkey-patch
    ``tracer.LOGS_DIR`` see the updated sibling path without extra plumbing.
    """
    return tracer.LOGS_DIR.parent / "audit" / _DISCUSS_AUDIT_FILENAME

# Serializes writes to discuss-audit.jsonl across concurrent discuss
# requests. Two callers interleaving their ``open("a") / write`` pair on
# the same file would otherwise race — the append-mode open ensures each
# ``write()`` lands at EOF, but a write without a trailing newline from
# one caller could still be stitched onto another caller's JSON object.
# The lock also keeps the blocking I/O explicitly off the event loop when
# combined with ``asyncio.to_thread``.
_discuss_audit_lock = asyncio.Lock()


def _write_discuss_audit_line(entry: dict[str, Any]) -> None:
    """Blocking helper: append one JSON line to discuss-audit.jsonl.

    Called from inside ``asyncio.to_thread`` under ``_discuss_audit_lock``.
    Kept tiny so the thread stays busy for the minimum time.
    """
    audit_path = _discuss_audit_path()
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


async def _append_discuss_audit(
    ticket_id: str,
    session_token: str,
    source_ip: str,
    user_agent: str,
) -> None:
    """Append one JSON line to ``<LOGS_DIR>.parent/audit/discuss-audit.jsonl``.

    This is a plain append-only file, NOT a per-ticket trace store entry —
    it serves as the cross-ticket "who investigated what and when" record.
    Stored in a sibling ``audit/`` directory (not inside ``LOGS_DIR``) so
    it cannot be read as a phantom ticket through the trace-bundle /
    list-traces endpoints. Created on first use. One line per endpoint
    invocation. Never rotated or truncated from here — operator can
    rotate manually if it grows.

    Async so callers don't block the event loop on disk I/O: the actual
    write runs in a worker thread, serialized by ``_discuss_audit_lock``
    so concurrent discuss requests can't interleave partial JSON lines.
    """
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "ticket_id": ticket_id,
        "session_token": session_token,
        "source_ip": source_ip,
        "user_agent": user_agent,
    }
    async with _discuss_audit_lock:
        await asyncio.to_thread(_write_discuss_audit_line, entry)


class DiscussResponse(BaseModel):
    """Response body for ``POST /traces/{ticket_id}/discuss``."""

    ticket_id: str
    session_token: str
    bundle_url: str
    investigate_command: str
    expires_at: str


@router.post(
    "/traces/{ticket_id}/discuss",
    response_model=DiscussResponse,
    dependencies=[Depends(_require_api_key)],
)
async def discuss_trace(ticket_id: str, request: Request) -> DiscussResponse:
    """Mint a short-lived investigation handoff for a trace.

    Returns a JSON body containing:

    * ``session_token`` — an opaque random string. **Write-only**: the
      token appears in this response and in the audit log, and is NOT
      validated on any subsequent request. A future commit may add
      validation (e.g. a registry or signed token) but for Tier 1 the
      token is purely a correlation ID in the audit log.
    * ``bundle_url`` — the URL a developer or UI can hit to download the
      trace bundle (pre-existing ``/traces/<id>/bundle`` endpoint).
    * ``investigate_command`` — the copy-paste shell snippet that the
      dashboard investigate disclosure already renders, returned as a
      string so a future UI button can launch it programmatically.
    * ``expires_at`` — ISO-8601 UTC timestamp one hour from now. Advisory
      only (nothing enforces it — see write-only note above).

    Every invocation writes one line to
    ``<LOGS_DIR>.parent/audit/discuss-audit.jsonl`` with timestamp, ticket_id, token,
    source IP, and user-agent. This is append-only and intentionally
    separate from the per-ticket trace store — it's the cross-trace
    "who investigated what" record, useful even on a solo harness for
    remembering what you looked at across sessions.

    **Out of scope for Tier 1:** token validation, a token registry,
    rate limiting, token revocation or blacklisting. The endpoint is
    protected by the existing ``X-API-Key`` gate and that's the whole
    security model.
    """
    _validate_ticket_id(ticket_id)
    entries = read_trace(ticket_id)
    if not entries:
        raise HTTPException(status_code=404, detail=f"No trace for ticket '{ticket_id}'")

    session_token = secrets.token_urlsafe(24)
    expires_at = datetime.now(UTC) + _DISCUSS_SESSION_TTL

    source_ip = request.client.host if request.client else ""
    user_agent = request.headers.get("user-agent", "")
    await _append_discuss_audit(
        ticket_id=ticket_id,
        session_token=session_token,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    logger.info(
        "discuss_session_minted",
        ticket_id=ticket_id,
        source_ip=source_ip,
    )

    return DiscussResponse(
        ticket_id=ticket_id,
        session_token=session_token,
        bundle_url=f"{_DISCUSS_BASE_URL}/traces/{ticket_id}/bundle",
        investigate_command=_build_investigate_command(ticket_id),
        expires_at=expires_at.isoformat(),
    )


# Map the public artifact name (what shows up in the URL) to the internal
# artifact event name and the media type we serve. Kept narrow on purpose —
# only the three artifacts the dashboard "Raw Downloads" panel needs today.
_ARTIFACT_DOWNLOAD_MAP: dict[str, tuple[str, str]] = {
    "session_log": (ARTIFACT_SESSION_LOG, "text/plain; charset=utf-8"),
    "session_stream": (ARTIFACT_SESSION_STREAM, "application/json"),
    "effective_claude_md": (ARTIFACT_EFFECTIVE_CLAUDE_MD, "text/markdown; charset=utf-8"),
}


@router.get("/traces/{ticket_id}/artifact/{artifact_type}")
async def trace_artifact(ticket_id: str, artifact_type: str) -> Response:
    """Return a single raw artifact file from a trace.

    session_log and effective_claude_md are served from the trace store's
    inlined ``content`` field (already redacted at consolidation time).
    session_stream is served from disk via the ``artifact_path`` stored on
    the reference entry — 404 if the file was cleaned up (e.g. worktree
    already purged and not archived).

    FORENSIC ESCAPE HATCH: unlike the ``/bundle`` endpoint, this endpoint
    serves ``session_stream`` bytes directly from disk without running them
    through ``redact()``. That's deliberate — the raw stream is preserved on
    the operator's local filesystem as a last-resort debugging source. If
    you're routing the response off-box, use ``/bundle`` instead so the
    stream gets redacted on the way out.
    """
    _validate_ticket_id(ticket_id)
    mapping = _ARTIFACT_DOWNLOAD_MAP.get(artifact_type)
    if mapping is None:
        raise HTTPException(status_code=404, detail=f"Unknown artifact type: {artifact_type}")

    event_name, media_type = mapping
    entries = read_trace(ticket_id)
    if not entries:
        raise HTTPException(status_code=404, detail=f"No trace for ticket '{ticket_id}'")

    entry = find_artifact(entries, event_name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_type}' not in trace")

    if artifact_type == "session_stream":
        stream_path_str = str(entry.get("artifact_path", ""))
        if not stream_path_str:
            raise HTTPException(status_code=404, detail="session_stream has no artifact_path")
        stream_path = Path(stream_path_str)
        if not stream_path.exists():
            raise HTTPException(status_code=404, detail="session_stream file missing on disk")
        try:
            body = stream_path.read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"read failed: {exc}") from exc
        # Force a download rather than letting the browser try to render a
        # multi-MB JSONL blob as a syntax-highlighted JSON document in a new
        # tab. The dashboard Raw Downloads panel expects a file save.
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Content-Disposition": 'attachment; filename="session-stream.jsonl"',
            },
        )

    content = entry.get("content")
    if content is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_type}' has no content")
    return PlainTextResponse(content=str(content), media_type=media_type)


# --- Admin: re-redact all existing trace entries ---
#
# When a redaction pattern update lands, existing traces don't automatically
# benefit — the redactor ran against them at consolidation time with the old
# pattern set. This endpoint rescans every trace file in the trace store and
# re-runs ``redact()`` over every known-risky field on every entry.
#
# Because the redactor is idempotent, running this when patterns haven't
# changed is a no-op — ``additional_patterns_found`` should be 0. A non-zero
# count on an unchanged pattern set indicates an idempotency bug.
@router.post(
    "/admin/re-redact",
    status_code=200,
    dependencies=[Depends(_require_api_key)],
)
async def admin_re_redact() -> dict[str, int]:
    """Re-run the redactor over every trace entry in the store.

    Cleans the same field set that ``consolidate_worktree_logs`` redacts on
    import, so a pattern update can catch secrets that were already written
    into non-``content`` fields on earlier runs:

    * Top-level string fields in ``_REDACT_IMPORTED_FIELDS``: ``content``,
      ``data``, ``error``, ``message``, ``output``, ``stderr``, ``stdout``,
      ``debug_payload``, ``tool_result``, ``details``, ``evidence``.
    * ``tool_index`` entries: the ``index.first_tool_error.message`` field
      (which captures up to 500 chars of raw tool-error content and can
      easily hold a live credential from a shell command).
    * Corrupt/unparseable lines: run through ``redact()`` as a raw string
      and written back redacted, so a partial-write during a crash that
      left a token on disk gets cleaned up on the next admin pass.

    Fields NOT covered: nested dicts/lists inside risky fields, and any
    top-level field name not in ``_REDACT_IMPORTED_FIELDS``. Nested leaks
    are rare in practice; if that assumption stops holding, extend the
    walk here and in ``tracer.consolidate_worktree_logs``.

    Returns a summary of the scan:

    * ``traces_processed`` — number of trace files walked.
    * ``entries_redacted`` — number of entries whose content changed.
    * ``additional_patterns_found`` — total *new* redaction hits on this pass.
      Expected to be 0 if patterns haven't changed since the original
      consolidation. Non-zero indicates either a pattern update or an
      idempotency violation in the redactor.

    Concurrency: this endpoint rewrites trace files in place without file
    locking. Run during quiet periods — a concurrent ``append_trace`` that
    interleaves with the rewrite can race and lose the appended entry.
    """
    logs_dir = tracer.LOGS_DIR
    if not logs_dir.exists():
        return {
            "traces_processed": 0,
            "entries_redacted": 0,
            "additional_patterns_found": 0,
        }

    traces_processed = 0
    entries_redacted = 0
    additional_patterns = 0

    for trace_file in sorted(logs_dir.glob("*.jsonl")):
        traces_processed += 1
        try:
            raw = trace_file.read_text()
        except OSError:
            logger.exception("re_redact_read_failed", trace_file=str(trace_file))
            continue

        out_lines: list[str] = []
        file_changed = False  # only rewrite the file if at least one line differed
        for line in raw.splitlines():
            if not line.strip():
                out_lines.append(line)
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # Corrupt/partial-write line (e.g. crash mid-append). A
                # partial write may contain a live token, so run the raw
                # string through redact() before writing it back rather
                # than passing it through verbatim.
                redacted_line, n = redact(line)
                if n:
                    additional_patterns += n
                    entries_redacted += 1
                    file_changed = True
                out_lines.append(redacted_line)
                continue

            # One helper call covers every known-risky string pocket in
            # the entry (top-level _REDACT_IMPORTED_FIELDS + the nested
            # index.first_tool_error.message pocket). Single source of
            # truth shared with tracer.consolidate_worktree_logs, so new
            # risky fields only need to be added in one place.
            n = redact_entry_in_place(entry)
            if n:
                additional_patterns += n
                entries_redacted += 1
                file_changed = True
            out_lines.append(json.dumps(entry))

        # Skip the write entirely when nothing changed. This closes a real
        # data-loss window: if a live pipeline was appending to this file
        # between our read_text() and write_text(), an unconditional rewrite
        # silently clobbered the appended entries. A no-op rewrite has no
        # reason to exist and no reason to take that risk.
        if not file_changed:
            continue

        new_text = "\n".join(out_lines)
        if raw.endswith("\n") and not new_text.endswith("\n"):
            new_text += "\n"
        # Atomic replace via the shared tracer helper — a crash mid-write
        # leaves the original file intact instead of producing a
        # truncated one.
        try:
            atomic_write_text(trace_file, new_text)
        except OSError:
            logger.exception("re_redact_write_failed", trace_file=str(trace_file))

    if additional_patterns:
        logger.warning(
            "re_redact_found_new_patterns",
            additional_patterns_found=additional_patterns,
            hint=(
                "Either patterns were updated since last consolidation, or "
                "the redactor failed its idempotency guarantee. Investigate."
            ),
        )
    logger.info(
        "re_redact_complete",
        traces_processed=traces_processed,
        entries_redacted=entries_redacted,
        additional_patterns_found=additional_patterns,
    )
    return {
        "traces_processed": traces_processed,
        "entries_redacted": entries_redacted,
        "additional_patterns_found": additional_patterns,
    }
