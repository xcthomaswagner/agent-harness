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
from client_profile import ClientProfile, load_profile
from config import Settings
from conflict_detector import ConflictDetector
from figma_extractor import FigmaExtractor, detect_figma_links
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
        conflict_detector: ConflictDetector | None = None,
        figma_extractor: FigmaExtractor | None = None,
    ) -> None:
        self._settings = settings
        self._analyst = analyst or TicketAnalyst(settings=settings)
        self._jira_adapter = jira_adapter or JiraAdapter(settings=settings)
        self._conflict_detector = conflict_detector or ConflictDetector()
        self._figma_extractor = figma_extractor or FigmaExtractor(
            api_token=settings.figma_api_token
        )

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

    def _load_client_profile(self, ticket: TicketPayload) -> ClientProfile | None:
        """Load the client profile for this ticket's project."""
        profile_name = self._settings.default_client_profile
        if not profile_name:
            return None
        profile = load_profile(profile_name)
        if profile:
            log = logger.bind(ticket_id=ticket.id)
            log.info("client_profile_loaded", profile=profile_name)
        return profile

    async def _handle_enriched(
        self, enriched: EnrichedTicket, log: Any
    ) -> dict[str, Any]:
        """Handle an enriched ticket — write back to Jira and trigger L2."""
        adapter = self._get_adapter(enriched)
        profile = self._load_client_profile(enriched)

        # Check for conflicts with in-progress tickets
        conflicts = self._conflict_detector.check_conflicts(
            enriched.id, [f.name for f in (enriched.test_scenarios or [])]
        )
        if conflicts and enriched.callback:
            warning = self._conflict_detector.format_warning(enriched.id, conflicts)
            await adapter.write_comment(enriched.id, warning)
            log.warning("conflict_warning_posted", conflicts=len(conflicts))

        # Extract Figma design spec if Figma link found in ticket
        if not enriched.figma_design_spec:
            all_text = f"{enriched.description} {' '.join(enriched.acceptance_criteria)}"
            figma_links = detect_figma_links(all_text)
            if figma_links:
                spec = await self._figma_extractor.extract(figma_links[0]["url"])
                if spec:
                    enriched.figma_design_spec = spec
                    log.info("figma_design_extracted", components=len(spec.components))

        # Auto-detect platform profile from client profile
        if not enriched.platform_profile and profile and profile.platform_profile:
            enriched.platform_profile = profile.platform_profile
            log.info("platform_profile_set", profile=profile.platform_profile)

        # Determine done status from client profile
        done_status = profile.done_status if profile else "In Progress"

        # Transition to "In Progress"
        if enriched.callback:
            try:
                target_status = "In Progress" if done_status == "Done" else done_status
                await adapter.transition_status(enriched.id, target_status)
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

        # Register as active ticket for conflict detection
        affected_files = []
        if enriched.size_assessment:
            affected_files = [enriched.title]  # Placeholder — real file list comes from plan
        self._conflict_detector.register(
            enriched.id, enriched.title, affected_files, f"ai/{enriched.id}"
        )

        # Use client profile repo path if available
        client_repo = (
            (profile.client_repo_path if profile else "")
            or self._settings.default_client_repo
        )

        # Trigger L2 (spawn Agent Team)
        spawn_result = await self._trigger_l2(
            enriched, ticket_path, log,
            pipeline_mode=pipeline_mode, client_repo_override=client_repo,
        )

        return {
            "status": "enriched",
            "ticket_id": enriched.id,
            "generated_ac_count": len(enriched.generated_acceptance_criteria),
            "test_scenario_count": len(enriched.test_scenarios),
            "spawn_triggered": spawn_result,
            "conflicts": len(conflicts) if conflicts else 0,
            "figma_extracted": enriched.figma_design_spec is not None,
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
        client_repo_override: str = "",
    ) -> bool:
        """Trigger L2 by calling the spawn script or Composio.

        Tries Composio (`ao spawn`) first if configured. Falls back to the
        custom spawn script.

        Args:
            pipeline_mode: "multi" (default) for full review/QA pipeline,
                          "quick" for single-agent fast mode.

        Returns True if spawn was triggered, False if skipped.
        """
        client_repo = client_repo_override or self._settings.default_client_repo
        if not client_repo:
            log.warning(
                "l2_spawn_skipped",
                reason="No default_client_repo configured in settings",
            )
            return False

        branch_name = f"ai/{enriched.id}"

        # Try Composio first
        if self._settings.use_composio:
            return self._spawn_via_composio(
                enriched, ticket_path, branch_name, pipeline_mode, log
            )

        # Fall back to custom spawn script
        return self._spawn_via_script(
            enriched, ticket_path, branch_name, pipeline_mode, client_repo, log
        )

    def _spawn_via_composio(
        self,
        enriched: EnrichedTicket,
        ticket_path: Path,
        branch_name: str,
        pipeline_mode: str,
        log: Any,
    ) -> bool:
        """Spawn via Composio Agent Orchestrator (`ao spawn`).

        Composio handles worktree creation, session management, and the dashboard.
        We still need to inject runtime files via the postCreate hook in
        agent-orchestrator.yaml.
        """
        cmd = [
            "ao", "spawn", enriched.id,
            "--project", self._settings.composio_project or "default",
        ]

        log.info(
            "l2_composio_spawn",
            branch=branch_name,
            pipeline_mode=pipeline_mode,
        )

        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except FileNotFoundError:
            log.warning("composio_not_found_falling_back_to_script")
            return self._spawn_via_script(
                enriched, ticket_path, branch_name, pipeline_mode,
                self._settings.default_client_repo, log,
            )

    def _spawn_via_script(
        self,
        enriched: EnrichedTicket,
        ticket_path: Path,
        branch_name: str,
        pipeline_mode: str,
        client_repo: str,
        log: Any,
    ) -> bool:
        """Spawn via custom spawn-team.sh script."""
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
            "l2_script_spawn",
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
