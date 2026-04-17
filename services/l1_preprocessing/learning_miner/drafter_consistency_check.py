"""Drafter consistency-check — second Claude call that gates promotion.

Catches the over-specification failure mode the absolute-directive
filter can't — a narrowly-scoped rule that silently conflicts with
a rule already in the target file. Returns ``contradicts=True`` only
when the new rule would make the agent unable to follow an existing
one; stylistic overlap is not flagged.

Killswitch: ``LEARNING_CONSISTENCY_CHECK_ENABLED=false`` short-circuits
every call to ``contradicts=False``. All error paths also fail open.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import anthropic
import structlog

from learning_miner._anthropic_retry import (
    RetryFailure,
    call_with_retry,
)

logger = structlog.get_logger()

MODEL = "claude-opus-4-20250514"


@dataclass(frozen=True)
class ConsistencyVerdict:
    """Second-Claude-call result. ``contradicts=True`` blocks promotion."""

    contradicts: bool
    contradicts_with: str = ""
    reasoning: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""


class ConsistencyChecker:
    """Runs the consistency-check Claude call."""

    def __init__(
        self,
        *,
        api_key: str,
        client: anthropic.AsyncAnthropic | None = None,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key)

    async def check(
        self, *, current_content: str, unified_diff: str
    ) -> ConsistencyVerdict:
        """Run the consistency check; return ``ConsistencyVerdict``.

        When ``enabled=False`` returns a no-op verdict with
        ``contradicts=False`` so callers can treat the killswitch and
        the check-is-on-and-clean paths identically.
        """
        if not self._enabled:
            return ConsistencyVerdict(contradicts=False, reasoning="disabled")
        if not unified_diff.strip():
            return ConsistencyVerdict(
                contradicts=False,
                reasoning="empty diff — nothing to check",
            )

        system_prompt = _build_system_prompt()
        user_prompt = _build_user_prompt(current_content, unified_diff)

        result = await self._call_with_retry(system_prompt, user_prompt)
        if isinstance(result, ConsistencyVerdict):
            return result
        text, tokens_in, tokens_out = result

        parsed = _parse_verdict_json(text)
        if parsed is None:
            # Malformed model output shouldn't hard-block — fail open
            # and let the operator judge.
            logger.warning(
                "learning_consistency_check_parse_failed",
                snippet=text[:300],
            )
            return ConsistencyVerdict(
                contradicts=False,
                reasoning="verdict JSON parse failed — failing open",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        return ConsistencyVerdict(
            contradicts=bool(parsed.get("contradicts", False)),
            contradicts_with=str(parsed.get("contradicts_with", "") or ""),
            reasoning=str(parsed.get("reasoning", "") or ""),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    async def _call_with_retry(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[str, int, int] | ConsistencyVerdict:
        outcome = await call_with_retry(
            self._client,
            model=MODEL,
            max_tokens=512,
            system=system_prompt,
            user=user_prompt,
            log_event="learning_consistency_check_retrying",
        )
        if isinstance(outcome, RetryFailure):
            reasoning = (
                "upstream error — failing open"
                if outcome.retryable
                else "non-retryable API error — failing open"
            )
            return ConsistencyVerdict(
                contradicts=False,
                error=outcome.error,
                reasoning=reasoning,
            )
        return outcome


def _build_system_prompt() -> str:
    return (
        "You are reviewing a proposed addition to a Markdown skill file "
        "for an AI-coding harness. You will receive the CURRENT file "
        "content and the PROPOSED unified diff.\n\n"
        'Return a JSON object: {"contradicts": bool, "contradicts_with": '
        'str, "reasoning": str}.\n\n'
        "Only flag ``contradicts=true`` when the new rule would make the "
        "agent unable to follow an existing rule, or vice versa. "
        "Stylistic overlap or related-but-compatible rules are fine — "
        "do not flag those. When uncertain, prefer "
        'contradicts=false.\n\n'
        "Put the specific existing rule text in ``contradicts_with`` so "
        "the operator can see which rule is conflicting."
    )


def _build_user_prompt(current_content: str, unified_diff: str) -> str:
    return (
        "Current content of the target file:\n\n"
        "<target_file>\n"
        f"{current_content}\n"
        "</target_file>\n\n"
        "Proposed unified diff:\n\n"
        "<diff>\n"
        f"{unified_diff}\n"
        "</diff>\n\n"
        "Return ONLY the JSON object."
    )


def _parse_verdict_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of the model response."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    candidate = fenced.group(1).strip() if fenced else text
    # Grab the outermost ``{...}`` to tolerate leading/trailing prose
    # even without a fence.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end < 0 or end < start:
        return None
    try:
        obj = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
