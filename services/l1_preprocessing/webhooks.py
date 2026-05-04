"""Webhook endpoints (Jira, Jira-bug, ADO, GitHub proxy) + manual trigger.

Extracted from ``main.py`` as part of the Phase 4 structural refactor.
These endpoints share the ``_validate_and_parse_webhook`` family of
auth helpers and the ``_dispatch_ticket`` fan-out. Keeping them together
lets ``main.py`` become a thin composition layer.

Mounted on ``router`` below; included by ``main.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
)

from adapters.ado_adapter import AdoAdapter, extract_tag_transition
from auth import _require_api_key
from claim_store import (
    COUNTER_ACCEPTED_EDGE,
    COUNTER_SKIPPED_NO_TAG,
    COUNTER_SKIPPED_NOT_EDGE,
    _bump_webhook_counter,
    _check_trigger_edge,
    _clear_trigger_state,
    _jira_delivery_seen,
    _try_claim_ticket,
)
from models import TicketPayload
from tracer import append_trace, generate_trace_id


def _settings() -> Any:
    """Resolve settings through main, so test patches of ``main.settings``
    flow into webhook handlers unchanged.

    The original monolithic ``main.py`` held every endpoint, so
    ``patch("main.settings")`` reached every handler through Python's
    namespace lookup. After splitting endpoints into separate modules,
    each module that did ``from config import settings`` captured its
    own binding of the config singleton and stopped reacting to
    ``patch("main.settings")``. Rather than rewrite every test to
    patch both ``main.settings`` and ``webhooks.settings`` (and
    ``completion.settings`` and ``trace_bundle.settings``), we do one
    import-time lookup per call.
    """
    import main  # local import dodges module-load circular import
    return main.settings

logger = structlog.get_logger()

router = APIRouter()


async def _validate_and_parse_webhook(
    request: Request, signature: str | None,
) -> dict[str, Any]:
    """Validate webhook HMAC signature and parse JSON body.

    Phase 1 fail-closed posture: when no ``webhook_secret`` is
    configured, reject with 503 unless ``allow_unsigned_webhooks``
    is true (the local-dev opt-in). Previously no-secret silently
    opened access.
    """
    body = await request.body()
    settings = _settings()

    if settings.webhook_secret:
        if not signature:
            raise HTTPException(status_code=401, detail="Missing webhook signature")
        expected = hmac.new(
            settings.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        sig_value = signature.removeprefix("sha256=")
        if not hmac.compare_digest(expected, sig_value):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    elif not settings.allow_unsigned_webhooks:
        raise HTTPException(status_code=503, detail="Webhook auth not configured")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Webhook payload must be a JSON object")
    return payload


async def _validate_and_parse_webhook_dual_auth(
    request: Request,
    signature: str | None,
    *,
    bearer_token: str | None,
    bearer_token_secret: str,
    no_secret_behavior: str,
    auth_label: str,
) -> dict[str, Any]:
    """Dual-mode webhook auth: HMAC signature OR shared bearer token.

    Shared by ``jira_bug_webhook`` and ``ado_webhook`` — both need to
    accept either a signed body (for CI/CD pipelines that can compute
    HMAC) or a shared header token (for Jira Automation / ADO Service
    Hooks that cannot). Previously each handler had its own ~28 line
    implementation, and one used raw ``==`` to compare the bearer
    token (timing-attack hazard) while the other used
    ``hmac.compare_digest``. Consolidating here guarantees both paths
    use constant-time comparison.

    ``no_secret_behavior``:
      * ``"fail_closed_503"`` — when neither the HMAC secret nor the
        bearer token is configured, raise 503. Use this for endpoints
        that must never accept unauthenticated requests.
      * ``"open_local_dev"`` — same scenario, accept the request. Use
        this for endpoints whose local-dev workflow relies on running
        the service without any secrets at all.

    ``auth_label`` goes into the 401 detail so the rejected side can
    distinguish which webhook's auth failed.
    """
    body = await request.body()
    settings = _settings()

    auth_ok = False
    # HMAC path — only when both the secret is configured AND a
    # signature header was sent. Missing header isn't an error here;
    # we fall through to the bearer path.
    if settings.webhook_secret and signature:
        expected = hmac.new(
            settings.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        sig_value = signature.removeprefix("sha256=")
        if hmac.compare_digest(expected, sig_value):
            auth_ok = True
    # Bearer token path — constant-time comparison so we don't leak
    # the token prefix via response timing.
    if (
        not auth_ok
        and bearer_token_secret
        and bearer_token
        and hmac.compare_digest(bearer_token, bearer_token_secret)
    ):
        auth_ok = True
    if not auth_ok:
        if not settings.webhook_secret and not bearer_token_secret:
            # Neither secret configured — behavior depends on endpoint.
            if no_secret_behavior == "fail_closed_503":
                raise HTTPException(
                    status_code=503,
                    detail=f"{auth_label} not configured",
                )
            if no_secret_behavior == "open_local_dev":
                # Phase 1: fail closed by default. Local dev must
                # explicitly set ALLOW_UNSIGNED_WEBHOOKS=true to
                # accept unsigned webhooks; production deployments
                # that forget the secret no longer fail open.
                if settings.allow_unsigned_webhooks:
                    auth_ok = True
                else:
                    raise HTTPException(
                        status_code=503,
                        detail=f"{auth_label} not configured",
                    )
            else:
                raise ValueError(
                    f"Invalid no_secret_behavior: {no_secret_behavior!r}"
                )
        else:
            raise HTTPException(
                status_code=401,
                detail=f"Invalid {auth_label} auth",
            )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid JSON body: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422, detail="Webhook payload must be a JSON object"
        )
    return payload


def _enqueue_or_background(
    ticket: TicketPayload, background_tasks: BackgroundTasks,
    trace_id: str = "",
) -> str:
    """Try to enqueue via Redis, fall back to FastAPI BackgroundTasks.

    Returns "queued", "background", or "duplicate".
    """
    if not _try_claim_ticket(ticket.id):
        logger.info("ticket_duplicate_skipped", ticket_id=ticket.id)
        return "duplicate"

    from queue_worker import enqueue_ticket

    job_id = enqueue_ticket(ticket, trace_id=trace_id)
    if job_id:
        logger.info("ticket_queued", ticket_id=ticket.id, job_id=job_id)
        return "queued"

    # Lazy import _process_ticket from main to avoid circular import —
    # main.py imports this router at top, so we can't import the other
    # direction at module-load time.
    from main import _process_ticket

    background_tasks.add_task(_process_ticket, ticket, trace_id)
    return "background"


def _dispatch_ticket(
    ticket: TicketPayload,
    background_tasks: BackgroundTasks,
    *,
    source: str,
    event: str,
    trace_id: str | None = None,
) -> dict[str, str]:
    """Shared tail for every ticket-ingest endpoint.

    Handles the boilerplate that ``jira_webhook`` / ``ado_webhook`` /
    ``manual_process_ticket`` used to copy-paste: mint a trace id,
    emit the breadcrumb log line, append a ``webhook`` trace entry
    so the dashboard lists the run, hand off to
    ``_enqueue_or_background``, and return the ``accepted`` /
    ``skipped`` response dict. Previously ``manual_process_ticket``
    silently omitted the ``append_trace`` breadcrumb, so tickets
    dispatched via the manual endpoint were harder to find in the
    trace list; the helper fixes that drift by construction.
    """
    tid = trace_id or generate_trace_id()
    logger.info(
        event, ticket_id=ticket.id, ticket_type=ticket.ticket_type
    )
    append_trace(
        ticket.id, tid, "webhook", event,
        ticket_type=ticket.ticket_type, source=source,
        ticket_title=ticket.title,
    )
    dispatch = _enqueue_or_background(
        ticket, background_tasks, trace_id=tid
    )
    if dispatch == "duplicate":
        return {
            "status": "skipped",
            "ticket_id": ticket.id,
            "reason": "already processing",
        }
    return {
        "status": "accepted",
        "ticket_id": ticket.id,
        "dispatch": dispatch,
    }


@router.post("/webhooks/jira", status_code=202)
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
    x_atlassian_webhook_identifier: str | None = Header(
        default=None, alias="x-atlassian-webhook-identifier"
    ),
) -> dict[str, str]:
    """Receive a Jira automation webhook and enqueue for processing.

    Phase 5: dedup on ``X-Atlassian-Webhook-Identifier`` so Jira's
    5xx-retry doesn't cause double-processing. Header is optional —
    older payloads or custom senders fall through without dedup.
    """
    from main import _get_jira_adapter

    payload = await _validate_and_parse_webhook(request, x_hub_signature)

    if x_atlassian_webhook_identifier:
        if _jira_delivery_seen(x_atlassian_webhook_identifier):
            logger.info(
                "jira_duplicate_delivery_skipped",
                delivery_id=x_atlassian_webhook_identifier,
            )
            return {"status": "skipped", "reason": "duplicate delivery"}
    else:
        logger.debug("jira_webhook_no_delivery_header")

    ticket = _get_jira_adapter().normalize(payload)
    return _dispatch_ticket(
        ticket,
        background_tasks,
        source="jira",
        event="jira_webhook_received",
    )


@router.post("/webhooks/jira-bug", status_code=202)
async def jira_bug_webhook(
    request: Request,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
    x_jira_bug_token: str | None = Header(default=None, alias="x-jira-bug-token"),
) -> dict[str, Any]:
    """Receive a Jira bug-created webhook and record a defect_link.

    Accepts EITHER HMAC signature (webhook_secret) OR bearer token
    (jira_bug_webhook_token), since Jira Automation cannot compute HMAC.
    Fails closed (503) when neither secret is configured — bug ingest
    must never accept unauthenticated requests.
    """
    from autonomy_ingest import ingest_jira_bug
    from autonomy_jira_bug import normalize_jira_bug
    from autonomy_store import ensure_schema, open_connection, resolve_db_path

    settings = _settings()
    payload = await _validate_and_parse_webhook_dual_auth(
        request,
        x_hub_signature,
        bearer_token=x_jira_bug_token,
        bearer_token_secret=settings.jira_bug_webhook_token or "",
        no_secret_behavior="fail_closed_503",
        auth_label="Bug webhook",
    )
    bug = normalize_jira_bug(payload, settings)

    conn = open_connection(resolve_db_path(settings.autonomy_db_path))
    try:
        ensure_schema(conn)
        result = ingest_jira_bug(conn, bug)
    finally:
        conn.close()

    logger.info("jira_bug_webhook_processed", **result)
    return result


@router.post("/webhooks/ado", status_code=202)
async def ado_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
    x_ado_webhook_token: str | None = Header(default=None, alias="x-ado-webhook-token"),
) -> dict[str, str]:
    """Receive an Azure DevOps Service Hook webhook and enqueue for processing.

    Auth: accepts HMAC signature (x-hub-signature) OR a shared token
    (X-ADO-Webhook-Token). Opens access when neither secret is
    configured so local development can send webhooks without any
    secret management. Rejects otherwise.
    """
    from main import _get_ado_adapter

    settings = _settings()
    payload = await _validate_and_parse_webhook_dual_auth(
        request,
        x_hub_signature,
        bearer_token=x_ado_webhook_token,
        bearer_token_secret=settings.ado_webhook_token or "",
        no_secret_behavior="open_local_dev",
        auth_label="ADO webhook",
    )

    # --- Normalize the ADO payload ---
    ticket = _get_ado_adapter().normalize(payload)

    # --- Profile resolution + ticket ID remapping ---
    resource = payload.get("resource", {})
    work_item = resource.get("revision", resource)
    fields = work_item.get("fields", {})
    ado_project = fields.get("System.TeamProject", "")
    work_item_id = str(work_item.get("id", resource.get("workItemId", "")))

    # ADO work item webhook payloads do not contain repository information —
    # work items are project-scoped and only loosely linked to repos via
    # outbound ArtifactLinks at commit/branch/PR time. The finest routing
    # granularity we get from the webhook alone is System.TeamProject, so
    # client profiles are keyed 1:1 on ado_project_name. If two profiles share
    # the same ado_project_name, the alphabetically-first one wins and the
    # second is unreachable via this path. L3 (PR webhooks) has repo GUID in
    # its payload and uses find_profile_by_ado_repo instead.
    # Resolve via main to honor ``patch("main.find_profile_by_ado_project")``.
    import main  # local import dodges module-load circular import
    profile = main.find_profile_by_ado_project(ado_project) if ado_project else None
    if profile:
        # Remap ticket ID to use the profile's project_key prefix
        ticket.id = f"{profile.project_key}-{work_item_id}"
        # Register mapping so write-back methods resolve the real ADO project name
        AdoAdapter._project_key_map[profile.project_key] = ado_project

    # --- Tag check: skip if neither ai_label nor quick_label is present ---
    # Use the already-tokenized labels from the adapter (exact match, not substring)
    ticket_labels_lower = {lbl.lower() for lbl in ticket.labels}
    ai_label_raw = profile.ai_label if profile else "ai-implement"
    quick_label_raw = profile.quick_label if profile else "ai-quick"
    ai_label = ai_label_raw.lower()
    quick_label = quick_label_raw.lower()
    trigger_labels: list[str] = []
    seen_trigger_labels: set[str] = set()
    for label in (ai_label_raw, quick_label_raw):
        label_key = label.lower()
        if label_key in ticket_labels_lower and label_key not in seen_trigger_labels:
            trigger_labels.append(label)
            seen_trigger_labels.add(label_key)
    tag_present = (
        ai_label in ticket_labels_lower or quick_label in ticket_labels_lower
    )

    # If the tag is absent, clear any remembered state so a future re-add of
    # the tag triggers a fresh edge. Then skip — there's nothing to do.
    if not tag_present:
        _clear_trigger_state(ticket.id)
        _bump_webhook_counter(COUNTER_SKIPPED_NO_TAG)
        reason = f"Neither '{ai_label}' nor '{quick_label}' tag found"
        append_trace(
            ticket.id, generate_trace_id(), "webhook", "ado_webhook_skipped_no_tag",
            ticket_type=ticket.ticket_type, source="ado",
            ticket_title=ticket.title, reason=reason,
        )
        return {"status": "skipped", "ticket_id": ticket.id, "reason": reason}

    # Edge-detect the trigger tag to skip non-dispatching cascade webhooks
    # (PR merge ArtifactLinks, comments, field edits). Prefer the payload's
    # tag delta when present; fall back to in-process memory otherwise.
    # See _check_trigger_edge for semantics.
    was_before, _ = extract_tag_transition(payload, [ai_label, quick_label])
    if not _check_trigger_edge(ticket.id, tag_present, was_present_before=was_before):
        _bump_webhook_counter(COUNTER_SKIPPED_NOT_EDGE)
        signal = "payload_delta" if was_before is not None else "process_memory"
        logger.info(
            "ado_webhook_not_edge",
            ticket_id=ticket.id,
            reason="trigger tag already present; not a new edge",
            signal=signal,
        )
        append_trace(
            ticket.id, generate_trace_id(), "webhook", "ado_webhook_skipped_not_edge",
            ticket_type=ticket.ticket_type, source="ado",
            ticket_title=ticket.title,
            reason="tag already present; not a new edge",
            signal=signal,
        )
        return {
            "status": "skipped",
            "ticket_id": ticket.id,
            "reason": "trigger tag already present on previous webhook (not a new edge)",
        }

    _bump_webhook_counter(COUNTER_ACCEPTED_EDGE)

    # Remove the trigger label and write a pickup comment so ADO reflects
    # that the harness has accepted this ticket and won't re-dispatch it.
    _trigger_labels = tuple(trigger_labels)
    _ado_adapter = _get_ado_adapter()

    async def _ado_pickup_writeback() -> None:
        try:
            for trigger_label in _trigger_labels:
                await _ado_adapter.remove_label(ticket.id, trigger_label)
            if len(_trigger_labels) == 1:
                removal_sentence = (
                    f"The trigger label `{_trigger_labels[0]}` has been removed "
                    "to prevent re-dispatch."
                )
            else:
                label_list = ", ".join(f"`{label}`" for label in _trigger_labels)
                removal_sentence = (
                    f"The trigger labels {label_list} have been removed "
                    "to prevent re-dispatch."
                )
            await _ado_adapter.write_comment(
                ticket.id,
                f"🤖 **Agentic Harness** picked up this ticket. "
                f"Dispatching to agent team now.\n\n"
                f"{removal_sentence} Re-add a trigger label to start a new run.",
            )
        except Exception:
            logger.warning("ado_pickup_writeback_failed", ticket_id=ticket.id, exc_info=True)

    background_tasks.add_task(_ado_pickup_writeback)

    return _dispatch_ticket(
        ticket,
        background_tasks,
        source="ado",
        event="ado_webhook_received",
    )


@router.post("/api/process-ticket", status_code=202, dependencies=[Depends(_require_api_key)])
async def manual_process_ticket(
    ticket: TicketPayload,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Manual trigger — accepts a TicketPayload directly (bypasses webhook).

    Essential for testing the pipeline without Jira/ADO webhooks configured.
    Previously this endpoint skipped the ``append_trace`` webhook
    breadcrumb, so manually-submitted tickets were harder to find in
    the trace dashboard. The shared ``_dispatch_ticket`` helper fixes
    that drift by construction.
    """
    return _dispatch_ticket(
        ticket,
        background_tasks,
        source="manual",
        event="manual_ticket_submitted",
    )


@router.post("/webhooks/github", status_code=202)
async def github_webhook_proxy(request: Request) -> dict[str, str]:
    """Proxy GitHub webhooks to the L3 PR Review Service.

    Since ngrok free tier only supports one tunnel, GitHub webhooks arrive
    at L1 (port 8000) and are forwarded to L3 (port 8001).
    """
    body = await request.body()
    headers = dict(request.headers)

    # Phase 1: on any L3 failure (connect error, 4xx/5xx, HTTP error)
    # raise 503 so GitHub retries the delivery. Previously we returned
    # 202 with ``{"status": "l3_error"}`` in the body, which GitHub
    # treats as successful delivery — so an L3 outage silently dropped
    # every webhook until GitHub was manually re-driven.
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "http://localhost:8001/webhooks/github",
                content=body,
                headers={
                    k: v for k, v in headers.items()
                    if k.lower() in (
                        "content-type", "x-github-event",
                        "x-hub-signature-256", "x-github-delivery",
                        "user-agent",
                    )
                },
                timeout=30.0,
            )
            if response.status_code >= 400:
                logger.warning(
                    "l3_proxy_http_error",
                    status=response.status_code,
                )
                raise HTTPException(
                    status_code=503,
                    detail=f"L3 PR review service error: {response.status_code}",
                )
            result: dict[str, str] = response.json()
            return result
        except httpx.ConnectError as exc:
            logger.warning("l3_service_unavailable")
            raise HTTPException(
                status_code=503,
                detail="L3 PR review service unavailable",
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("l3_proxy_failed", error=str(exc)[:200])
            raise HTTPException(
                status_code=503,
                detail="L3 PR review service error",
            ) from exc
