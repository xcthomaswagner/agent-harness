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

from config import settings
from models import (
    TicketPayload,
)
from pipeline import Pipeline

logger = structlog.get_logger()

QUEUE_NAME = "harness-tickets"


def process_ticket_sync(ticket_data: dict[str, Any]) -> dict[str, Any]:
    """Process a ticket synchronously (called by RQ worker).

    RQ workers run in a separate process, so we need to reconstruct
    the ticket and pipeline from the serialized data.
    """
    ticket_id = ticket_data.get("id", "unknown")
    try:
        ticket = TicketPayload(**ticket_data)
        log = logger.bind(ticket_id=ticket.id, source=ticket.source)
        log.info("queue_worker_processing_ticket")

        pipeline = Pipeline(settings=settings)

        # RQ workers are synchronous — run the async pipeline in an event loop
        result = asyncio.run(pipeline.process(ticket))

        log.info("queue_worker_completed", **result)
        return result
    except Exception as exc:
        logger.error(
            "queue_worker_failed",
            ticket_id=ticket_id,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
            exc_info=True,
        )
        return {"status": "failed", "ticket_id": ticket_id, "error": str(exc)[:500]}


def enqueue_ticket(ticket: TicketPayload) -> str | None:
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
            job_timeout="10m",
            result_ttl=3600,
            description=f"Process ticket {ticket.id}",
        )

        job_id = str(job.id)
        logger.info("ticket_enqueued", ticket_id=ticket.id, job_id=job_id)
        return job_id

    except ImportError:
        logger.warning("redis_not_installed")
        return None
    except Exception:
        logger.warning("redis_connection_failed", exc_info=True)
        return None
