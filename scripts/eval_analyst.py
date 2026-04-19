#!/usr/bin/env python3
"""Analyst evaluation runner.

Runs the analyst against the golden ticket corpus under ``docs/eval/goldens/``
and scores the output.

Two modes:

- Mocked (default): assembles the system prompt for each golden and asserts
  the feature-type checklist content is present. Does NOT call the LLM.
  Catches prompt-assembly regressions (e.g., deleted IMPLICIT_REQUIREMENTS.md,
  changed filename, broken skill-file loader). Runs free.

- Live (``--live``): calls the real Anthropic analyst and scores the
  ``EnrichedTicket`` output against each golden's expected feature types
  and expected implicit-AC substring set. Requires ``ANTHROPIC_API_KEY``
  in the environment.  Cost per full run: ~$0.25 across 5 goldens.

Example:

    # Cheap CI run — assembly only:
    python scripts/eval_analyst.py

    # Real API run, all goldens:
    python scripts/eval_analyst.py --live

    # Iterate on one golden:
    python scripts/eval_analyst.py --live --golden form_heavy_order_history

Exit code is non-zero when any golden fails.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDENS_DIR = REPO_ROOT / "docs" / "eval" / "goldens"
L1_DIR = REPO_ROOT / "services" / "l1_preprocessing"
# Append (not insert) so L1 modules don't shadow stdlib or
# project-level modules if a future collision appears.
if str(L1_DIR) not in sys.path:
    sys.path.append(str(L1_DIR))


@dataclass
class Golden:
    golden_id: str
    ticket: dict[str, Any]
    expected_feature_types: list[str]
    expected_implicit_acs: list[str]
    expected_min_ticket_acs: int
    expected_max_implicit_acs: int
    notes: str = ""


@dataclass
class ScoreResult:
    golden_id: str
    passed: bool
    reasons: list[str]


def load_goldens(only: str | None = None) -> list[Golden]:
    goldens: list[Golden] = []
    for path in sorted(GOLDENS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        golden = Golden(
            golden_id=data["golden_id"],
            ticket=data["ticket"],
            expected_feature_types=list(data.get("expected_feature_types") or []),
            expected_implicit_acs=list(data.get("expected_implicit_acs") or []),
            expected_min_ticket_acs=int(data.get("expected_min_ticket_acs", 0)),
            expected_max_implicit_acs=int(data.get("expected_max_implicit_acs", 9999)),
            notes=str(data.get("notes", "")),
        )
        if only and golden.golden_id != only:
            continue
        goldens.append(golden)
    if only and not goldens:
        print(f"ERROR: no golden found with id '{only}'", file=sys.stderr)
        sys.exit(2)
    return goldens


def run_mocked(golden: Golden) -> ScoreResult:
    """Assert the analyst system prompt carries the required checklist content.

    Does NOT call the LLM. Catches prompt-assembly regressions only — it
    does not verify the analyst actually produces implicit ACs. For that,
    use ``--live``.
    """
    from analyst import TicketAnalyst
    from config import Settings
    from models import TicketType

    settings = Settings(
        anthropic_api_key="test-key-unused-in-mocked-mode",
        jira_base_url="https://unused.example.com",
        jira_api_token="unused",
        jira_user_email="unused@example.com",
        default_client_repo="",
    )
    # TicketAnalyst ctor initializes the Anthropic client but does not call it.
    analyst = TicketAnalyst(settings=settings)
    try:
        ticket_type = TicketType(golden.ticket.get("ticket_type", "story"))
    except ValueError:
        ticket_type = TicketType.STORY
    prompt = analyst._build_system_prompt(ticket_type)

    reasons: list[str] = []

    if "Implicit Requirements by Feature Type" not in prompt:
        reasons.append("IMPLICIT_REQUIREMENTS.md not present in system prompt")

    for ft in golden.expected_feature_types:
        marker = f"Feature type: {ft}"
        if marker not in prompt:
            reasons.append(
                f"expected feature type '{ft}' missing checklist header in prompt"
            )

    # Skill step 5 (classification) must be visible
    if "Feature-Type Classification" not in prompt:
        reasons.append(
            "SKILL.md Step 5 'Feature-Type Classification' section not in prompt"
        )

    return ScoreResult(
        golden_id=golden.golden_id,
        passed=not reasons,
        reasons=reasons,
    )


def run_live(golden: Golden) -> ScoreResult:
    """Invoke the real analyst and score against the golden's expected set."""
    from analyst import TicketAnalyst
    from config import Settings
    from models import TicketPayload

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ScoreResult(
            golden_id=golden.golden_id,
            passed=False,
            reasons=["ANTHROPIC_API_KEY not set — cannot run live"],
        )

    settings = Settings(
        anthropic_api_key=api_key,
        jira_base_url="https://unused.example.com",
        jira_api_token="unused",
        jira_user_email="unused@example.com",
        default_client_repo="",
    )
    analyst = TicketAnalyst(settings=settings)
    ticket = TicketPayload.model_validate(golden.ticket)

    async def _go() -> Any:
        return await analyst.analyze(ticket)

    enriched = asyncio.run(_go())

    reasons: list[str] = []

    # Enriched?
    from models import EnrichedTicket

    if not isinstance(enriched, EnrichedTicket):
        reasons.append(
            f"analyst returned {type(enriched).__name__}, not EnrichedTicket"
        )
        return ScoreResult(
            golden_id=golden.golden_id, passed=False, reasons=reasons
        )

    # Feature-type match.
    actual_types = set(enriched.detected_feature_types)
    expected_types = set(golden.expected_feature_types)
    if actual_types != expected_types:
        reasons.append(
            f"feature types mismatch: expected {sorted(expected_types)}, "
            f"got {sorted(actual_types)}"
        )

    # Implicit AC substring presence.
    implicit_acs = [
        ac
        for ac in enriched.generated_acceptance_criteria
        if ac.category == "implicit"
    ]
    implicit_texts_lower = [ac.text.lower() for ac in implicit_acs]
    missing = [
        substr
        for substr in golden.expected_implicit_acs
        if not any(substr.lower() in t for t in implicit_texts_lower)
    ]
    if missing:
        reasons.append(f"implicit AC substrings missing: {missing}")

    # Count sanity.
    ticket_ac_count = sum(
        1
        for ac in enriched.generated_acceptance_criteria
        if ac.category == "ticket"
    )
    if ticket_ac_count < golden.expected_min_ticket_acs:
        reasons.append(
            f"ticket-AC count {ticket_ac_count} < min "
            f"{golden.expected_min_ticket_acs}"
        )
    if len(implicit_acs) > golden.expected_max_implicit_acs:
        reasons.append(
            f"implicit-AC count {len(implicit_acs)} > max "
            f"{golden.expected_max_implicit_acs}"
        )

    return ScoreResult(
        golden_id=golden.golden_id,
        passed=not reasons,
        reasons=reasons,
    )


def print_summary(results: list[ScoreResult]) -> None:
    passes = sum(1 for r in results if r.passed)
    total = len(results)
    width = max(len(r.golden_id) for r in results) if results else 10
    print(f"\n{'-' * 60}")
    print(f"{'GOLDEN':<{width}}  RESULT")
    print(f"{'-' * 60}")
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"{r.golden_id:<{width}}  {mark}")
        for reason in r.reasons:
            print(f"{'':<{width}}  · {reason}")
    print(f"{'-' * 60}")
    print(f"{passes}/{total} passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="call the real Anthropic analyst (requires ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--golden",
        help="run a single golden by id (default: run all)",
    )
    args = parser.parse_args()

    goldens = load_goldens(only=args.golden)
    if not goldens:
        print("no goldens found", file=sys.stderr)
        return 2

    results: list[ScoreResult] = []
    for golden in goldens:
        if args.live:
            result = run_live(golden)
        else:
            result = run_mocked(golden)
        results.append(result)

    print_summary(results)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
