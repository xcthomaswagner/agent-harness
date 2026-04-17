"""Shared Anthropic call + retry helper for drafter modules.

Both ``drafter_markdown`` and ``drafter_consistency_check`` need the
same exponential-backoff retry loop for transient upstream errors.
Keeping this in one place prevents policy drift between them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import anthropic
import structlog

logger = structlog.get_logger()


def is_retryable_anthropic_error(exc: anthropic.APIError) -> bool:
    """Transient errors worth retrying: rate limits, connection, 5xx."""
    if isinstance(exc, anthropic.RateLimitError | anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


@dataclass(frozen=True)
class RetryFailure:
    """Result of a retry loop that ran out of attempts or hit a 4xx."""

    error: str
    retryable: bool


async def call_with_retry(
    client: anthropic.AsyncAnthropic,
    *,
    model: str,
    max_tokens: int,
    system: str,
    user: str,
    max_retries: int = 3,
    log_event: str = "learning_drafter_retrying",
) -> tuple[str, int, int] | RetryFailure:
    """Single-prompt Claude call with exponential-backoff retry.

    Returns ``(text, tokens_in, tokens_out)`` on success. Returns a
    ``RetryFailure`` on non-retryable error or exhausted retries — the
    caller decides how to map the failure into their own result type.
    Sleeps 2**attempt seconds between tries (2, 4, 8 at the default).
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(
                getattr(block, "text", "")
                for block in response.content
                if getattr(block, "type", None) == "text"
            )
            return (
                text,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
        except anthropic.APIError as exc:
            if not is_retryable_anthropic_error(exc):
                return RetryFailure(
                    error=f"{type(exc).__name__}: {exc}", retryable=False
                )
            last_exc = exc
            wait = 2**attempt
            logger.warning(
                log_event,
                attempt=attempt,
                wait=wait,
                error_kind=type(exc).__name__,
            )
            if attempt < max_retries:
                await asyncio.sleep(wait)
                continue
    return RetryFailure(
        error=f"failed after retries: {last_exc}", retryable=True
    )
