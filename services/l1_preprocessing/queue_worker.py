"""Queue-based ticket processing worker.

Uses Redis + RQ (Redis Queue) for concurrent ticket processing.
Each ticket gets its own worker job, enabling multiple tickets to
be processed in parallel.

Usage:
    # Start Redis first: redis-server
    # Then start the worker:
    rq worker harness-tickets --path services/l1_preprocessing

    # Or with logging:
    rq worker harness-tickets --path services/l1_preprocessing --verbose
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from claim_store import _clear_trigger_state, _release_ticket
from config import settings
from models import (
    TicketPayload,
)
from pipeline import Pipeline
from tracer import append_trace, generate_trace_id

logger = structlog.get_logger()

QUEUE_NAME = "harness-tickets"


def process_ticket_sync(ticket_data: dict[str, Any], trace_id: str = "") -> dict[str, Any]:
    """Process a ticket synchronously (called by RQ worker).

    RQ workers run in a separate process, so we need to reconstruct
    the ticket and pipeline from the serialized data.
    """
    ticket_id = ticket_data.get("id", "unknown")
    tid = trace_id or generate_trace_id()
    try:
        ticket = TicketPayload(**ticket_data)
        log = logger.bind(ticket_id=ticket.id, source=ticket.source)
        log.info("queue_worker_processing_ticket")
        append_trace(
            ticket.id,
            tid,
            "pipeline",
            "processing_started",
            ticket_type=ticket.ticket_type,
            source=ticket.source,
            dispatch="queue",
        )

        pipeline = Pipeline(settings=settings)

        # RQ workers are synchronous — run the async pipeline in an event loop
        result = asyncio.run(pipeline.process(ticket, trace_id=tid))

        log.info("queue_worker_completed", **result)
        trace_data = {k: v for k, v in result.items() if k != "ticket_id"}
        append_trace(ticket.id, tid, "pipeline", "processing_completed", **trace_data)
        if not result.get("spawn_triggered"):
            _release_ticket(ticket.id)
            _clear_trigger_state(ticket.id)
        return result
    except Exception as exc:
        logger.error(
            "queue_worker_failed",
            ticket_id=ticket_id,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
            exc_info=True,
        )
        append_trace(
            str(ticket_id),
            tid,
            "pipeline",
            "error",
            error_type=type(exc).__name__,
            error_message=str(exc)[:500],
            dispatch="queue",
        )
        _release_ticket(str(ticket_id))
        _clear_trigger_state(str(ticket_id))
        return {"status": "failed", "ticket_id": ticket_id, "error": str(exc)[:500]}


def enqueue_ticket(ticket: TicketPayload, trace_id: str = "") -> str | None:
    """Enqueue a ticket for processing via Redis Queue.

    Returns the job ID if enqueued, None if Redis is not available
    (falls back to in-process background task).
    """
    try:
        from redis import Redis
        from rq import Queue

        redis_url = settings.redis_url
        if not redis_url:
            return None

        redis_conn = Redis.from_url(redis_url)
        q = Queue(QUEUE_NAME, connection=redis_conn)

        job = q.enqueue(
            process_ticket_sync,
            ticket.model_dump(),
            trace_id,
            job_timeout=settings.queue_job_timeout,
            result_ttl=3600,
            description=f"Process ticket {ticket.id}",
        )

        job_id = str(job.id)
        logger.info("ticket_enqueued", ticket_id=ticket.id, job_id=job_id)
        append_trace(
            ticket.id,
            trace_id or generate_trace_id(),
            "queue",
            "ticket_queued",
            job_id=job_id,
            queue_name=QUEUE_NAME,
            job_timeout=settings.queue_job_timeout,
        )
        return job_id

    except ImportError:
        logger.warning("redis_not_installed")
        return None
    except Exception:
        logger.warning("redis_connection_failed", exc_info=True)
        return None
