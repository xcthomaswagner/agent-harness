"""L1 Pipeline — orchestrates ticket analysis and output routing.

This module connects the ticket analyst to the downstream actions:
- Enriched tickets → trigger L2 (spawn Agent Team)
- Info requests → write comment to Jira/ADO, update status
- Decomposition plans → flag for manual PM splitting
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import structlog

from adapters.jira_adapter import JiraAdapter
from analyst import TicketAnalyst
from config import Settings
from models import (
    DecompositionPlan,
    EnrichedTicket,
    InfoRequest,
    TicketPayload,
    classify_analyst_output,
)

logger = structlog.get_logger()

HARNESS_ROOT = Path(__file__).resolve().parents[2]
SPAWN_SCRIPT = HARNESS_ROOT / "scripts" / "spawn-team.sh"


class Pipeline:
    """Orchestrates the L1 pre-processing pipeline."""

    def __init__(
        self,
        settings: Settings,
        analyst: TicketAnalyst | None = None,
        jira_adapter: JiraAdapter | None = None,
    ) -> None:
        self._settings = settings
        self._analyst = analyst or TicketAnalyst(settings=settings)
        self._jira_adapter = jira_adapter or JiraAdapter(settings=settings)

    def _get_adapter(
        self, ticket: EnrichedTicket | InfoRequest | DecompositionPlan
    ) -> JiraAdapter:
        """Return the appropriate adapter for write-back operations.

        Currently only Jira is supported. ADO adapter will be added when
        the pipeline routes ADO tickets through here.
        """
        return self._jira_adapter

    async def process(self, ticket: TicketPayload) -> dict[str, Any]:
        """Run a ticket through the full L1 pipeline.

        Returns a status dict with the outcome for logging/API response.
        """
        log = logger.bind(ticket_id=ticket.id, source=ticket.source)
        log.info("pipeline_started")

        # Step 1: Run analyst
        output = await self._analyst.analyze(ticket)
        output_type = classify_analyst_output(output)
        log.info("analyst_completed", output_type=output_type)

        # Step 2: Route based on output type
        if output_type == "enriched":
            if not isinstance(output, EnrichedTicket):
                raise TypeError(f"Expected EnrichedTicket, got {type(output).__name__}")
            return await self._handle_enriched(output, log)

        if output_type == "info_request":
            if not isinstance(output, InfoRequest):
                raise TypeError(f"Expected InfoRequest, got {type(output).__name__}")
            return await self._handle_info_request(output, log)

        if not isinstance(output, DecompositionPlan):
            raise TypeError(f"Expected DecompositionPlan, got {type(output).__name__}")
        return await self._handle_decomposition(output, log)

    async def _handle_enriched(
        self, enriched: EnrichedTicket, log: Any
    ) -> dict[str, Any]:
        """Handle an enriched ticket — write back to Jira and trigger L2."""
        adapter = self._get_adapter(enriched)

        # Transition to "In Progress"
        if enriched.callback:
            try:
                await adapter.transition_status(enriched.id, "In Progress")
                log.info("ticket_transitioned_to_in_progress")
            except Exception:
                log.warning("status_transition_failed", target="In Progress")

        # Write generated AC back to Jira
        if enriched.callback and enriched.generated_acceptance_criteria:
            ac_text = "\n".join(
                f"- {ac}" for ac in enriched.generated_acceptance_criteria
            )
            comment = (
                f"*AI Analyst — Generated Acceptance Criteria:*\n\n{ac_text}"
                f"\n\n*Edge Cases:*\n"
                + "\n".join(f"- {ec}" for ec in enriched.edge_cases)
            )
            await adapter.write_comment(enriched.id, comment)
            log.info("enrichment_written_to_jira")

        # Write enriched ticket to temp file for spawn script
        ticket_path = self._write_ticket_json(enriched)

        # Determine pipeline mode from labels
        pipeline_mode = "multi"
        if "ai-quick" in enriched.labels:
            pipeline_mode = "quick"

        # Trigger L2 (spawn Agent Team)
        spawn_result = await self._trigger_l2(
            enriched, ticket_path, log, pipeline_mode=pipeline_mode
        )

        return {
            "status": "enriched",
            "ticket_id": enriched.id,
            "generated_ac_count": len(enriched.generated_acceptance_criteria),
            "test_scenario_count": len(enriched.test_scenarios),
            "spawn_triggered": spawn_result,
        }

    async def _handle_info_request(
        self, info_req: InfoRequest, log: Any
    ) -> dict[str, Any]:
        """Handle an info request — post questions to Jira, change status."""
        questions_text = "\n".join(f"- {q}" for q in info_req.questions)
        comment = (
            f"*AI Analyst — Information Needed:*\n\n{questions_text}"
            f"\n\n*Context:* {info_req.context}"
        )

        if info_req.callback:
            await self._jira_adapter.write_comment(info_req.ticket_id, comment)
            await self._jira_adapter.transition_status(
                info_req.ticket_id, "Needs Clarification"
            )
            log.info("info_request_posted_to_jira")

        return {
            "status": "info_request",
            "ticket_id": info_req.ticket_id,
            "question_count": len(info_req.questions),
        }

    async def _handle_decomposition(
        self, decomp: DecompositionPlan, log: Any
    ) -> dict[str, Any]:
        """Handle a decomposition plan — flag for manual PM splitting."""
        comment = (
            f"*AI Analyst — Ticket Too Large for Single Agent Team:*\n\n"
            f"*Reason:* {decomp.reason}\n\n"
            f"*Suggested Sub-tickets ({len(decomp.sub_tickets)}):*\n"
            + "\n".join(
                f"- **{st.title}** ({st.estimated_size}): {st.description}"
                for st in decomp.sub_tickets
            )
            + f"\n\n*Dependency Order:* {' → '.join(decomp.dependency_order)}"
        )

        if decomp.callback:
            await self._jira_adapter.write_comment(decomp.ticket_id, comment)
            await self._jira_adapter.add_label(decomp.ticket_id, "needs-splitting")
            log.info("decomposition_flagged_for_pm")

        return {
            "status": "decomposition",
            "ticket_id": decomp.ticket_id,
            "sub_ticket_count": len(decomp.sub_tickets),
        }

    @staticmethod
    def _write_ticket_json(enriched: EnrichedTicket) -> Path:
        """Write the enriched ticket to a temp file for the spawn script."""
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix=f"ticket-{enriched.id}-", delete=False
        ) as tmp:
            tmp.write(enriched.model_dump_json(indent=2))
            return Path(tmp.name)

    async def _trigger_l2(
        self,
        enriched: EnrichedTicket,
        ticket_path: Path,
        log: Any,
        pipeline_mode: str = "multi",
    ) -> bool:
        """Trigger L2 by calling the spawn script.

        Args:
            pipeline_mode: "multi" (default) for full review/QA pipeline,
                          "quick" for single-agent fast mode.

        Returns True if spawn was triggered, False if skipped.
        """
        client_repo = self._settings.default_client_repo
        if not client_repo:
            log.warning(
                "l2_spawn_skipped",
                reason="No default_client_repo configured in settings",
            )
            return False

        branch_name = f"ai/{enriched.id}"
        cmd = [
            str(SPAWN_SCRIPT),
            "--client-repo", client_repo,
            "--ticket-json", str(ticket_path),
            "--branch-name", branch_name,
        ]

        if enriched.platform_profile:
            cmd.extend(["--platform-profile", enriched.platform_profile])

        if pipeline_mode == "quick":
            cmd.extend(["--mode", "quick"])

        log.info(
            "l2_spawn_triggered",
            branch=branch_name,
            client_repo=client_repo,
            pipeline_mode=pipeline_mode,
        )

        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        return True
