"""L1 Pipeline — orchestrates ticket analysis and output routing.

This module connects the ticket analyst to the downstream actions:
- Enriched tickets → trigger L2 (spawn Agent Team)
- Info requests → write comment to Jira/ADO, update status
- Decomposition plans → flag for manual PM splitting
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import structlog

from adapters.ado_adapter import AdoAdapter
from adapters.jira_adapter import JiraAdapter
from analyst import TicketAnalyst
from client_profile import ClientProfile, find_profile_by_project_key, load_profile
from config import Settings
from figma_extractor import (
    FigmaExtractor,
    detect_figma_links,
    rendered_frames_to_attachments,
)
from models import (
    DecompositionPlan,
    EnrichedTicket,
    InfoRequest,
    TicketPayload,
    TicketSource,
    classify_analyst_output,
)
from tracer import append_trace, generate_trace_id

logger = structlog.get_logger()

HARNESS_ROOT = Path(__file__).resolve().parents[2]
SPAWN_SCRIPT = HARNESS_ROOT / "scripts" / "spawn_team.py"


class Pipeline:
    """Orchestrates the L1 pre-processing pipeline."""

    def __init__(
        self,
        settings: Settings,
        analyst: TicketAnalyst | None = None,
        jira_adapter: JiraAdapter | None = None,
        ado_adapter: AdoAdapter | None = None,
        figma_extractor: FigmaExtractor | None = None,
    ) -> None:
        self._settings = settings
        self._analyst = analyst or TicketAnalyst(settings=settings)
        self._jira_adapter = jira_adapter or JiraAdapter(settings=settings)
        self._ado_adapter = ado_adapter or AdoAdapter(settings=settings)
        self._figma_extractor = figma_extractor or FigmaExtractor(
            api_token=settings.figma_api_token
        )
        self._temp_dirs: list[str] = []  # Track for cleanup after spawn

    def _get_adapter(
        self, ticket: EnrichedTicket | InfoRequest | DecompositionPlan
    ) -> JiraAdapter | AdoAdapter:
        """Return the appropriate adapter for write-back operations.

        Routes to JiraAdapter or AdoAdapter based on ticket.source.
        """
        if ticket.source == TicketSource.ADO:
            return self._ado_adapter
        return self._jira_adapter

    async def process(
        self, ticket: TicketPayload, trace_id: str = ""
    ) -> dict[str, Any]:
        """Run a ticket through the full L1 pipeline.

        Args:
            trace_id: If provided, reuse the trace ID from the webhook handler.
                      Otherwise generate a new one.

        Returns a status dict with the outcome for logging/API response.
        """
        log = logger.bind(ticket_id=ticket.id, source=ticket.source)
        log.info("pipeline_started")

        tid = trace_id or generate_trace_id()

        # Step 0: Download image attachments so analyst can see them
        ticket = await self._download_image_attachments(ticket, log)

        # Step 1: Run analyst
        output = await self._analyst.analyze(ticket)
        output_type = classify_analyst_output(output)
        log.info("analyst_completed", output_type=output_type)
        # Token counts stashed by analyst — may be absent if analyst is mocked
        tokens_in = getattr(self._analyst, "_last_tokens_in", 0)
        tokens_out = getattr(self._analyst, "_last_tokens_out", 0)
        append_trace(ticket.id, tid, "analyst", "analyst_completed",
                     output_type=output_type,
                     tokens_in=tokens_in if isinstance(tokens_in, int) else 0,
                     tokens_out=tokens_out if isinstance(tokens_out, int) else 0)

        # Step 2: Route based on output type
        if output_type == "enriched":
            if not isinstance(output, EnrichedTicket):
                raise TypeError(f"Expected EnrichedTicket, got {type(output).__name__}")
            return await self._handle_enriched(output, log, tid)

        if output_type == "info_request":
            if not isinstance(output, InfoRequest):
                raise TypeError(f"Expected InfoRequest, got {type(output).__name__}")
            return await self._handle_info_request(output, log)

        if not isinstance(output, DecompositionPlan):
            raise TypeError(f"Expected DecompositionPlan, got {type(output).__name__}")
        return await self._handle_decomposition(output, log)

    def _load_client_profile(self, ticket: TicketPayload) -> ClientProfile | None:
        """Load the client profile for this ticket's project.

        Routing order:
        1. Match by project key extracted from ticket ID (e.g., SCRUM-16 → SCRUM)
        2. Fall back to DEFAULT_CLIENT_PROFILE from settings
        """
        log = logger.bind(ticket_id=ticket.id)

        # Extract project key from ticket ID (e.g., "ROC-1" → "ROC")
        project_key = ticket.id.rsplit("-", 1)[0] if "-" in ticket.id else ""
        if project_key:
            profile = find_profile_by_project_key(project_key)
            if profile:
                log.info("client_profile_routed", profile=profile.name, project_key=project_key)
                return profile

        # Fall back to default
        profile_name = self._settings.default_client_profile
        if not profile_name:
            return None
        profile = load_profile(profile_name)
        if profile:
            log.info("client_profile_loaded", profile=profile_name)
        return profile

    async def _download_image_attachments(
        self, ticket: TicketPayload, log: Any
    ) -> TicketPayload:
        """Download image attachments from the ticket source so the analyst can see them."""
        image_attachments = [a for a in ticket.attachments if a.is_design_image]
        if not image_attachments:
            return ticket

        log.info("downloading_image_attachments", count=len(image_attachments))

        dest_dir = tempfile.mkdtemp(prefix=f"attachments-{ticket.id}-")
        self._temp_dirs.append(dest_dir)
        # Route attachment download by source — ADO adapter lacks this method
        # so we skip for ADO tickets (attachments stay as URL-only references)
        if ticket.source == TicketSource.ADO:
            log.info("attachment_download_skipped_ado")
            downloaded = 0
        else:
            updated = await self._jira_adapter.download_image_attachments(
                ticket.attachments, dest_dir
            )
            ticket.attachments = updated
            downloaded = sum(1 for a in updated if a.local_path)
        log.info(
            "image_attachments_downloaded",
            downloaded=downloaded,
            total=len(image_attachments),
        )
        return ticket

    async def _extract_figma_if_needed(
        self, enriched: EnrichedTicket, log: Any
    ) -> None:
        """Extract Figma design spec if a Figma link is found in the ticket."""
        if enriched.figma_design_spec:
            return

        all_text = f"{enriched.description} {' '.join(enriched.acceptance_criteria)}"
        figma_links = detect_figma_links(all_text)
        if not figma_links:
            return

        figma_img_dir = tempfile.mkdtemp(prefix=f"figma-{enriched.id}-")
        self._temp_dirs.append(figma_img_dir)
        spec = await self._figma_extractor.extract(
            figma_links[0]["url"], image_dest_dir=figma_img_dir,
        )
        if not spec:
            return

        enriched.figma_design_spec = spec
        log.info("figma_design_extracted",
                 components=len(spec.components), rendered=len(spec.rendered_frames))

        frame_atts = rendered_frames_to_attachments(spec)
        if frame_atts:
            enriched.attachments.extend(frame_atts)
            log.info("figma_frames_added_as_attachments", count=len(frame_atts))

    def _resolve_platform_profile(
        self, enriched: EnrichedTicket, profile: ClientProfile | None, log: Any
    ) -> None:
        """Auto-detect platform profile from client config or repo files."""
        if enriched.platform_profile:
            return
        if profile and profile.platform_profile:
            enriched.platform_profile = profile.platform_profile
            log.info("platform_profile_from_config", profile=profile.platform_profile)
        else:
            detected = self._detect_platform_from_repo()
            if detected:
                enriched.platform_profile = detected
                log.info("platform_profile_auto_detected", profile=detected)

    async def _handle_enriched(
        self, enriched: EnrichedTicket, log: Any, trace_id: str = ""
    ) -> dict[str, Any]:
        """Handle an enriched ticket — write back to Jira and trigger L2."""
        tid = trace_id or generate_trace_id()
        adapter = self._get_adapter(enriched)
        profile = self._load_client_profile(enriched)

        await self._extract_figma_if_needed(enriched, log)
        self._resolve_platform_profile(enriched, profile, log)

        done_status = profile.done_status if profile else "Done"
        in_progress_status = profile.in_progress_status if profile else "In Progress"

        if enriched.callback:
            try:
                target_status = in_progress_status if done_status == "Done" else done_status
                await adapter.transition_status(enriched.id, target_status)
                log.info("ticket_transitioned", target=target_status)
            except Exception as exc:
                log.warning("status_transition_failed", target=target_status)
                append_trace(
                    enriched.id, tid, "pipeline", "error",
                    error_type="TransitionFailed",
                    error_message=str(exc)[:500],
                )

        if enriched.callback and (enriched.generated_acceptance_criteria or enriched.edge_cases):
            parts = []
            if enriched.generated_acceptance_criteria:
                ac_text = "\n".join(f"- {ac}" for ac in enriched.generated_acceptance_criteria)
                parts.append(f"*AI Analyst — Generated Acceptance Criteria:*\n\n{ac_text}")
            if enriched.edge_cases:
                ec_text = "\n".join(f"- {ec}" for ec in enriched.edge_cases)
                parts.append(f"*Edge Cases:*\n{ec_text}")
            comment = "\n\n".join(parts)
            try:
                await adapter.write_comment(enriched.id, comment)
                log.info("enrichment_written_to_ticket_source")
            except Exception as exc:
                log.warning("enrichment_comment_failed", error=str(exc)[:200])

        ticket_path = self._write_ticket_json(enriched)
        quick_label = profile.quick_label if profile else "ai-quick"
        pipeline_mode = "quick" if quick_label in enriched.labels else "multi"

        client_repo = (
            (profile.client_repo_path if profile else "")
            or self._settings.default_client_repo
        )

        spawn_result = await self._trigger_l2(
            enriched, ticket_path, log,
            pipeline_mode=pipeline_mode, client_repo_override=client_repo,
            trace_id=tid,
            client_profile_name=profile.name if profile else "",
        )
        self._cleanup_temp_dirs(log)

        append_trace(enriched.id, tid, "pipeline", "l2_dispatched",
                     pipeline_mode=pipeline_mode, spawn_triggered=spawn_result,
                     figma_extracted=enriched.figma_design_spec is not None,
                     platform_profile=enriched.platform_profile or "none")

        return {
            "status": "enriched",
            "ticket_id": enriched.id,
            "generated_ac_count": len(enriched.generated_acceptance_criteria),
            "test_scenario_count": len(enriched.test_scenarios),
            "spawn_triggered": spawn_result,
            "figma_extracted": enriched.figma_design_spec is not None,
        }

    async def _handle_info_request(
        self, info_req: InfoRequest, log: Any
    ) -> dict[str, Any]:
        """Handle an info request — post questions to ticket source, change status."""
        questions_text = "\n".join(f"- {q}" for q in info_req.questions)
        comment = (
            f"*AI Analyst — Information Needed:*\n\n{questions_text}"
            f"\n\n*Context:* {info_req.context}"
        )

        if info_req.callback:
            adapter = self._get_adapter(info_req)
            try:
                await adapter.write_comment(info_req.ticket_id, comment)
                await adapter.transition_status(
                    info_req.ticket_id, "Needs Clarification"
                )
                log.info("info_request_posted")
            except Exception as exc:
                log.warning("info_request_write_failed", error=str(exc)[:200])

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
            adapter = self._get_adapter(decomp)
            try:
                await adapter.write_comment(decomp.ticket_id, comment)
                await adapter.add_label(decomp.ticket_id, "needs-splitting")
                log.info("decomposition_flagged")
            except Exception as exc:
                log.warning("decomposition_write_failed", error=str(exc)[:200])

        return {
            "status": "decomposition",
            "ticket_id": decomp.ticket_id,
            "sub_ticket_count": len(decomp.sub_tickets),
        }

    def _detect_platform_from_repo(self) -> str:
        """Auto-detect platform from repo files."""
        repo_path = Path(self._settings.default_client_repo)
        if not repo_path.exists():
            return ""

        # Sitecore
        if (repo_path / "sitecore.json").exists():
            return "sitecore"
        pkg_json = repo_path / "package.json"
        if pkg_json.exists():
            try:
                import json

                pkg = json.loads(pkg_json.read_text())
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if any(k.startswith("@sitecore-jss") for k in deps):
                    return "sitecore"
            except Exception:
                pass

        # Salesforce
        if (repo_path / "sfdx-project.json").exists():
            return "salesforce"
        if (repo_path / "force-app").is_dir():
            return "salesforce"

        return ""

    def _cleanup_temp_dirs(self, log: Any) -> None:
        """Remove temporary directories created during processing."""
        import shutil

        for d in self._temp_dirs:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                log.warning("temp_dir_cleanup_failed", path=d)
        self._temp_dirs.clear()

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
        trace_id: str = "",
        client_profile_name: str = "",
    ) -> bool:
        """Trigger L2 by calling the spawn script.

        Args:
            pipeline_mode: "multi" (default) for full review/QA pipeline,
                          "quick" for single-agent fast mode.
            trace_id: Trace ID for error reporting.
            client_profile_name: Client profile name (e.g., "xcsf30") for
                                source control context in agent sessions.

        Returns True if spawn was triggered, False if skipped.
        """
        import asyncio

        client_repo = client_repo_override or self._settings.default_client_repo
        if not client_repo:
            log.warning(
                "l2_spawn_skipped",
                reason="No default_client_repo configured in settings",
            )
            return False

        branch_name = f"ai/{enriched.id}"
        cmd = [
            sys.executable, str(SPAWN_SCRIPT),
            "--client-repo", client_repo,
            "--ticket-json", str(ticket_path),
            "--branch-name", branch_name,
        ]

        if enriched.platform_profile:
            cmd.extend(["--platform-profile", enriched.platform_profile])

        if client_profile_name:
            cmd.extend(["--client-profile", client_profile_name])

        if trace_id:
            cmd.extend(["--trace-id", trace_id])

        if pipeline_mode == "quick":
            cmd.extend(["--mode", "quick"])

        log.info(
            "l2_spawn_triggered",
            branch=branch_name,
            client_repo=client_repo,
            pipeline_mode=pipeline_mode,
        )

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        # Check for immediate spawn failure (exits within 2s = setup error)
        await asyncio.sleep(2)
        exit_code = proc.poll()
        if exit_code is not None and exit_code != 0:
            stderr_out = ""
            if proc.stderr:
                stderr_out = (proc.stderr.read() or b"").decode(errors="replace")[:1000]
            if proc.stderr:
                proc.stderr.close()
            log.error("l2_spawn_failed", exit_code=exit_code, stderr=stderr_out)
            tid = trace_id or generate_trace_id()
            append_trace(
                enriched.id, tid, "spawn", "error",
                error_type="SpawnFailed",
                error_message=f"spawn_team.py exited {exit_code}",
                error_context={"stderr": stderr_out, "exit_code": exit_code},
            )
            return False

        # Process still running — detach stderr pipe and let it run
        if proc.stderr:
            proc.stderr.close()
        return True
