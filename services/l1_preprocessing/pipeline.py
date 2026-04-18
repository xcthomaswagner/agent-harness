"""L1 Pipeline — orchestrates ticket analysis and output routing.

This module connects the ticket analyst to the downstream actions:
- Enriched tickets → trigger L2 (spawn Agent Team)
- Info requests → write comment to Jira/ADO, update status
- Decomposition plans → flag for manual PM splitting
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog

from adapters.ado_adapter import AdoAdapter
from adapters.base import TicketWriteBackAdapter
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
from redaction import redact
from tracer import BILLING_API, append_trace, generate_trace_id

logger = structlog.get_logger()

HARNESS_ROOT = Path(__file__).resolve().parents[2]
SPAWN_SCRIPT = HARNESS_ROOT / "scripts" / "spawn_team.py"

# Strong references to spawn reaper tasks. Without this the asyncio task
# handle returned by ``create_task`` would be the only reference to the
# coroutine and GC could collect it mid-flight — the docs are explicit
# about this. Tasks remove themselves from the set when they finish.
_SPAWN_REAPER_TASKS: set[Any] = set()

# Per-repo locks for _ensure_client_repo. Two webhooks landing simultaneously
# for the same missing path would both try to clone otherwise — git fails
# the second attempt with "destination exists" and one ticket never spawns.
# Keyed by the resolved local_path string. Lock objects live for process
# lifetime which is fine: a handful of distinct client repos at most.
_REPO_LOCKS: dict[str, threading.Lock] = {}
_REPO_LOCKS_LOCK = threading.Lock()


def _get_repo_lock(local_path: str) -> threading.Lock:
    """Return the per-path threading.Lock used by _ensure_client_repo.

    Lazy-init under the registry lock so two callers never get different
    Lock instances for the same path.
    """
    with _REPO_LOCKS_LOCK:
        return _REPO_LOCKS.setdefault(local_path, threading.Lock())


def _normalize_remote(url: str) -> str:
    """Strip credentials and trailing slash from a git remote URL for
    comparison. ``https://user:pat@host/x/y`` → ``host/x/y``.

    spawn_team rewrites ``origin`` with a PAT-embedded URL on each spawn,
    so a literal string compare against the profile's plain org URL will
    always fail. Compare host+path only.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    path = parts.path.rstrip("/").removesuffix(".git")
    return f"{host}{path}"


def _cleanup_partial_clone(repo: Path) -> None:
    """Remove a directory left behind by a failed/timed-out git clone.

    Without this, the next call's "path exists and is a git repo" branch
    would see the partial .git dir and silently return True with broken
    state underneath.
    """
    with contextlib.suppress(OSError):
        shutil.rmtree(str(repo))


def _build_clone_url(source_control: dict[str, Any]) -> str:
    """Construct an auth URL for git clone from a profile's source_control.

    Constructed in memory; caller must never log the result. ADO uses
    ADO_PAT with a dummy username. GitHub uses GITHUB_TOKEN. Returns
    empty string when creds are missing.
    """
    sc_type = str(source_control.get("type", "")).lower()
    if sc_type == "azure-repos":
        pat = os.environ.get("ADO_PAT", "")
        org = str(source_control.get("org", "")).rstrip("/")
        project = str(source_control.get("ado_project", "")) or str(
            source_control.get("repo", "")
        )
        repo = str(source_control.get("repo", ""))
        if not (pat and org and project and repo):
            return ""
        # ADO accepts any non-empty username; the PAT is what matters.
        # Preserve the org URL's path prefix so modern dev.azure.com/<org>
        # URLs work alongside the legacy *.visualstudio.com form.
        org_parts = urlsplit(org)
        host = org_parts.hostname or ""
        org_path = org_parts.path.rstrip("/")
        return f"https://any:{pat}@{host}{org_path}/{project}/_git/{repo}"
    if sc_type in ("github", "git"):
        token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
        gh_repo = str(source_control.get("repo", ""))
        if not (token and gh_repo):
            return ""
        return f"https://x-access-token:{token}@github.com/{gh_repo}.git"
    return ""


def _ensure_client_repo(
    local_path: str,
    source_control: dict[str, Any],
    log: Any,
) -> bool:
    """Ensure the client repo exists at ``local_path`` as a valid git checkout.

    Returns True when the repo is ready for spawn; False on any failure
    condition (caller skips spawn with a structured log line, never with a
    phantom 'spawn_failed' status).

    Behavior:
    - path missing → clone from source_control config
    - path exists, not a git repo → skip (do not clobber arbitrary dirs)
    - path exists, git repo, wrong remote → skip (could be intentional)
    - path exists, git repo, correct remote → git fetch (NOT reset --hard;
      worktrees share refs and a reset can corrupt in-flight operations)

    Called under a per-repo lock to prevent concurrent clones racing into
    the same destination.
    """
    lock = _get_repo_lock(local_path)
    with lock:
        repo = Path(local_path)

        if not repo.exists():
            clone_url = _build_clone_url(source_control)
            if not clone_url:
                log.warning(
                    "client_repo_clone_skipped",
                    reason="missing source_control config or auth token",
                    local_path=local_path,
                )
                return False
            try:
                repo.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.error(
                    "client_repo_parent_mkdir_failed",
                    local_path=local_path,
                    error=str(exc)[:200],
                )
                return False

            log.info("client_repo_cloning", local_path=local_path)
            # Intentionally do NOT log clone_url — it contains the PAT.
            try:
                result = subprocess.run(
                    ["git", "clone", clone_url, str(repo)],
                    capture_output=True, text=True, timeout=600,
                )
            except subprocess.TimeoutExpired:
                _cleanup_partial_clone(repo)
                log.error("client_repo_clone_timeout", local_path=local_path)
                return False
            if result.returncode != 0:
                _cleanup_partial_clone(repo)
                # Redact any embedded credentials from stderr before logging;
                # `redact()` handles the `https://user:pat@host/...` shape
                # and any other secret patterns git may echo.
                stderr, _ = redact(result.stderr[:500])
                log.error(
                    "client_repo_clone_failed",
                    local_path=local_path,
                    stderr=stderr,
                )
                return False
            log.info("client_repo_cloned", local_path=local_path)
            return True

        # Exists — verify it's a git repo.
        if not (repo / ".git").exists():
            log.warning(
                "client_repo_not_git",
                reason="path exists but has no .git",
                local_path=local_path,
            )
            return False

        # Exists and is a git repo — verify remote if we can compute the
        # expected one. If source_control is empty we have no way to verify
        # the remote or fetch updates; trust the local repo and return.
        # This also keeps tests that set up a fake .git dir without
        # source_control config from needing to mock subprocess.run.
        clone_url = _build_clone_url(source_control)
        if not clone_url:
            return True

        try:
            remote_result = subprocess.run(
                ["git", "-C", str(repo), "remote", "get-url", "origin"],
                capture_output=True, text=True, check=False,
            )
        except OSError as exc:
            log.warning(
                "client_repo_remote_check_failed",
                local_path=local_path,
                error=str(exc)[:200],
            )
            return False
        actual_remote = remote_result.stdout.strip()
        if actual_remote:
            norm_actual = _normalize_remote(actual_remote)
            norm_expected = _normalize_remote(clone_url)
            if norm_actual != norm_expected:
                log.warning(
                    "client_repo_remote_mismatch",
                    local_path=local_path,
                    expected=norm_expected,
                    actual=norm_actual,
                )
                return False

        # Best-effort fetch. We deliberately do NOT `reset --hard`: the base
        # repo's refs are shared with any live worktrees, and a reset during
        # an in-flight agent push can corrupt refs.
        try:
            fetch_result = subprocess.run(
                ["git", "-C", str(repo), "fetch", "--quiet", "--no-tags"],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("client_repo_fetch_timeout", local_path=local_path)
            # Fetch timeout is non-fatal — spawn can still succeed against
            # the existing local state.
        else:
            if fetch_result.returncode != 0:
                # Non-fatal — spawn can still succeed against local state,
                # but operators need visibility (expired PAT, network down).
                log.warning(
                    "client_repo_fetch_failed",
                    local_path=local_path,
                    returncode=fetch_result.returncode,
                    stderr=fetch_result.stderr[:200],
                )
        return True


def _has_sitecore_jss_dep(repo_path: Path) -> bool:
    """Return True if ``package.json`` declares any ``@sitecore-jss/*``
    dependency. Used by ``Pipeline._detect_platform_from_repo`` as a
    fallback when the explicit ``sitecore.json`` marker is absent.
    """
    pkg_json = repo_path / "package.json"
    if not pkg_json.exists():
        return False
    import json

    pkg = json.loads(pkg_json.read_text())
    deps = {
        **pkg.get("dependencies", {}),
        **pkg.get("devDependencies", {}),
    }
    return any(k.startswith("@sitecore-jss") for k in deps)


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
        # Temp-dir tracking lives on each ``process()`` call, NOT on the
        # Pipeline instance — this service runs as a module-level
        # singleton and two concurrent webhook tasks would otherwise
        # share the list. Ticket A's cleanup would rmtree ticket B's
        # still-in-use attachment/figma dirs, breaking the spawned L2
        # session silently. See the ``_process_temp_dirs`` list built
        # in ``process()`` and passed down to helpers.

    def _get_adapter(
        self, ticket: EnrichedTicket | InfoRequest | DecompositionPlan
    ) -> TicketWriteBackAdapter:
        """Return the appropriate adapter for write-back operations.

        Routes to JiraAdapter or AdoAdapter based on ticket.source. The
        return type is the structural ``TicketWriteBackAdapter``
        protocol (see ``adapters/base.py``) — callers should only
        depend on ``write_comment`` / ``transition_status`` /
        ``add_label``. Adding a new ticket source is a matter of
        implementing those three methods.
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

        # Per-call temp-dir tracking — see the Pipeline constructor
        # comment. Scoping the list here prevents concurrent webhook
        # tasks from trampling each other's in-flight directories.
        temp_dirs: list[str] = []

        # Step 0: Download image attachments so analyst can see them
        ticket = await self._download_image_attachments(ticket, log, temp_dirs)

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
                     tokens_out=tokens_out if isinstance(tokens_out, int) else 0,
                     billing=BILLING_API)

        # Step 2: Route based on output type
        try:
            if output_type == "enriched":
                if not isinstance(output, EnrichedTicket):
                    raise TypeError(f"Expected EnrichedTicket, got {type(output).__name__}")
                return await self._handle_enriched(output, log, tid, temp_dirs)

            if output_type == "info_request":
                if not isinstance(output, InfoRequest):
                    raise TypeError(f"Expected InfoRequest, got {type(output).__name__}")
                return await self._handle_info_request(output, log)

            if not isinstance(output, DecompositionPlan):
                raise TypeError(f"Expected DecompositionPlan, got {type(output).__name__}")
            return await self._handle_decomposition(output, log)
        finally:
            # Ensure temp dirs are cleaned on ALL paths, not just enriched.
            # _handle_enriched also calls cleanup internally, but the
            # idempotent shutil.rmtree + clear() makes the double-call safe.
            self._cleanup_temp_dirs(log, temp_dirs)

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
        self, ticket: TicketPayload, log: Any, temp_dirs: list[str]
    ) -> TicketPayload:
        """Download image attachments from the ticket source so the analyst can see them."""
        image_attachments = [a for a in ticket.attachments if a.is_design_image]
        if not image_attachments:
            return ticket

        log.info("downloading_image_attachments", count=len(image_attachments))

        dest_dir = tempfile.mkdtemp(prefix=f"attachments-{ticket.id}-")
        temp_dirs.append(dest_dir)
        if ticket.source == TicketSource.ADO:
            updated = await self._ado_adapter.download_image_attachments(
                ticket.attachments, dest_dir
            )
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
        self, enriched: EnrichedTicket, log: Any, temp_dirs: list[str]
    ) -> None:
        """Extract Figma design spec if a Figma link is found in the ticket."""
        if enriched.figma_design_spec:
            return

        all_text = f"{enriched.description} {' '.join(enriched.acceptance_criteria)}"
        figma_links = detect_figma_links(all_text)
        if not figma_links:
            return

        figma_img_dir = tempfile.mkdtemp(prefix=f"figma-{enriched.id}-")
        temp_dirs.append(figma_img_dir)
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
        self,
        enriched: EnrichedTicket,
        log: Any,
        trace_id: str = "",
        temp_dirs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Handle an enriched ticket — write back to Jira and trigger L2."""
        if temp_dirs is None:
            temp_dirs = []
        tid = trace_id or generate_trace_id()
        adapter = self._get_adapter(enriched)
        profile = self._load_client_profile(enriched)

        await self._extract_figma_if_needed(enriched, log, temp_dirs)
        self._resolve_platform_profile(enriched, profile, log)

        in_progress_status = profile.in_progress_status if profile else "In Progress"

        if enriched.callback:
            # Always transition to the profile's in_progress_status when
            # L1 starts work — the profile's ``done_status`` is reserved
            # for "pipeline complete / PR created" and must never be set
            # at ingest time (the schema comment is explicit about this).
            # An older version had ``target_status = in_progress_status
            # if done_status == "Done" else done_status`` which silently
            # marked tickets Done at ingest for any profile whose
            # done_status was customized away from the literal "Done".
            target_status = in_progress_status
            try:
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

        try:
            spawn_result = await self._trigger_l2(
                enriched, ticket_path, log,
                pipeline_mode=pipeline_mode, client_repo_override=client_repo,
                trace_id=tid,
                client_profile_name=profile.name if profile else "",
                source_control=profile.source_control if profile else {},
            )
        finally:
            ticket_path.unlink(missing_ok=True)

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

    async def _safe_adapter_writeback(
        self,
        adapter: TicketWriteBackAdapter,
        ticket_id: str,
        log: Any,
        *,
        op_name: str,
        comment: str | None = None,
        transition: str | None = None,
        label: str | None = None,
    ) -> bool:
        """Post a comment / transition status / add a label on a ticket, swallowing errors.

        Shared between ``_handle_info_request`` and ``_handle_decomposition``
        (which used to copy-paste the same try/except with the same
        ``error=str(exc)[:200]`` truncation). Each optional step is
        performed in the order ``comment → transition → label`` when
        provided. On success logs ``<op_name>_written``; on any
        exception logs ``<op_name>_failed`` with the truncated error
        and returns False so callers can branch if needed.
        """
        try:
            if comment is not None:
                await adapter.write_comment(ticket_id, comment)
            if transition is not None:
                await adapter.transition_status(ticket_id, transition)
            if label is not None:
                await adapter.add_label(ticket_id, label)
            log.info(f"{op_name}_written")
            return True
        except Exception as exc:
            log.warning(f"{op_name}_failed", error=str(exc)[:200])
            return False

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
            await self._safe_adapter_writeback(
                self._get_adapter(info_req),
                info_req.ticket_id,
                log,
                op_name="info_request",
                comment=comment,
                transition="Needs Clarification",
            )

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
            await self._safe_adapter_writeback(
                self._get_adapter(decomp),
                decomp.ticket_id,
                log,
                op_name="decomposition",
                comment=comment,
                label="needs-splitting",
            )

        return {
            "status": "decomposition",
            "ticket_id": decomp.ticket_id,
            "sub_ticket_count": len(decomp.sub_tickets),
        }

    def _detect_platform_from_repo(self) -> str:
        """Auto-detect platform from repo files.

        Checks are run in order and the first match wins. Each check
        is a ``(platform, predicate)`` tuple; predicates are tiny
        callables on the repo root Path so adding a new platform is
        a one-line addition instead of another ``if`` branch with its
        own exception-handling shape.
        """
        repo_path = Path(self._settings.default_client_repo)
        if not repo_path.exists():
            return ""

        checks: list[tuple[str, Callable[[Path], bool]]] = [
            # Sitecore — marker file + JSS dependency scan as a fallback.
            ("sitecore", lambda p: (p / "sitecore.json").exists()),
            ("sitecore", _has_sitecore_jss_dep),
            # Salesforce — DX project file OR the canonical force-app dir.
            ("salesforce", lambda p: (p / "sfdx-project.json").exists()),
            ("salesforce", lambda p: (p / "force-app").is_dir()),
        ]
        for platform, predicate in checks:
            try:
                if predicate(repo_path):
                    return platform
            except Exception:
                # Any predicate that blows up (malformed package.json,
                # permission error) is treated as "no match" — same
                # behavior as the previous ``try/except Exception:
                # pass`` around the package.json branch.
                continue

        return ""

    @staticmethod
    def _cleanup_temp_dirs(log: Any, temp_dirs: list[str]) -> None:
        """Remove temporary directories created during processing."""
        import shutil

        for d in temp_dirs:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                log.warning("temp_dir_cleanup_failed", path=d)
        temp_dirs.clear()

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
        source_control: dict[str, Any] | None = None,
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
        client_repo = client_repo_override or self._settings.default_client_repo
        if not client_repo:
            log.warning(
                "l2_spawn_skipped",
                reason="No default_client_repo configured in settings",
            )
            return False

        # Verify (and auto-provision) the client repo before spawning.
        # Runs off the event loop: git clone can take minutes for large
        # Salesforce repos and blocking uvicorn here wedges all webhooks.
        repo_ready = await asyncio.to_thread(
            _ensure_client_repo, client_repo, source_control or {}, log,
        )
        if not repo_ready:
            log.warning(
                "l2_spawn_skipped",
                reason="client_repo_unavailable",
                client_repo=client_repo,
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

        # Redirect stderr to a temp file (not a PIPE): the child keeps
        # running for the full L2 pipeline lifetime, often several minutes,
        # and if we hand it a kernel pipe with a ~64KB buffer every stderr
        # write past that point blocks. Worse, closing the Python-side read
        # end while the child is alive raises SIGPIPE in the child on its
        # next stderr write (Popen resets SIGPIPE to SIG_DFL on POSIX), so
        # previous ``stderr=PIPE`` + ``stderr.close()`` risked killing the
        # spawned team after the 2-second health check. A temp file avoids
        # both problems: writes never block, and we can still read stderr
        # for the early-failure diagnostic path.
        # Deliberately NOT a context manager: the file handle is handed
        # to Popen and outlives this function. A ``with`` block would
        # close it before the child has written anything.
        stderr_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w+b",
            prefix=f"spawn-{enriched.id}-",
            suffix=".err",
            delete=False,
        )
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                start_new_session=True,
            )
        except Exception:
            stderr_file.close()
            with contextlib.suppress(OSError):
                Path(stderr_file.name).unlink()
            raise

        # Check for immediate spawn failure (exits within 2s = setup error)
        await asyncio.sleep(2)
        exit_code = proc.poll()
        if exit_code is not None and exit_code != 0:
            # Early failure: read stderr from the temp file, log, record.
            stderr_out = ""
            try:
                stderr_file.flush()
                stderr_file.seek(0)
                stderr_out = stderr_file.read().decode(errors="replace")[:1000]
            except OSError:
                pass
            stderr_file.close()
            with contextlib.suppress(OSError):
                Path(stderr_file.name).unlink()
            log.error("l2_spawn_failed", exit_code=exit_code, stderr=stderr_out)
            tid = trace_id or generate_trace_id()
            append_trace(
                enriched.id, tid, "spawn", "error",
                error_type="SpawnFailed",
                error_message=f"spawn_team.py exited {exit_code}",
                error_context={"stderr": stderr_out, "exit_code": exit_code},
            )
            return False

        # Process still running — detach and schedule a reaper. The reaper
        # waits for proc in a worker thread so it doesn't block the event
        # loop, then removes the stderr temp file. Without this the child
        # would remain a zombie until the L1 service exits.
        stderr_path = Path(stderr_file.name)
        stderr_file.close()  # Parent doesn't need its own handle anymore.

        async def _reap_spawn() -> None:
            try:
                await asyncio.to_thread(proc.wait)
            finally:
                with contextlib.suppress(OSError):
                    stderr_path.unlink()

        reaper = asyncio.create_task(_reap_spawn())
        _SPAWN_REAPER_TASKS.add(reaper)
        reaper.add_done_callback(_SPAWN_REAPER_TASKS.discard)
        return True
