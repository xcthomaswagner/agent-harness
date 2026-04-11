"""Azure DevOps adapter — normalizes Service Hook payloads and provides write-back operations."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import structlog

from config import Settings
from models import (
    Attachment,
    CallbackConfig,
    LinkedItem,
    TicketPayload,
    TicketSource,
    TicketType,
)

logger = structlog.get_logger()

# ADO work item type -> our TicketType
_ADO_TYPE_MAP: dict[str, TicketType] = {
    "user story": TicketType.STORY,
    "product backlog item": TicketType.STORY,
    "bug": TicketType.BUG,
    "task": TicketType.TASK,
    "issue": TicketType.BUG,
}


class AdoAdapter:
    """Normalizes Azure DevOps Service Hook payloads and writes back via ADO REST API."""

    # Class-level map: project_key prefix → real ADO project name.
    # Shared across all instances so the webhook handler's registration
    # is visible to the Pipeline's adapter instance.
    _project_key_map: ClassVar[dict[str, str]] = {}

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.ado_org_url,
            headers=self._auth_headers(settings),
            timeout=30.0,
        )

    @staticmethod
    def _auth_headers(settings: Settings) -> dict[str, str]:
        """Build Basic auth headers using Personal Access Token."""
        import base64

        # ADO uses empty username + PAT as password
        credentials = f":{settings.ado_pat}"
        token = base64.b64encode(credentials.encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json-patch+json",
        }

    def normalize(self, webhook_payload: dict[str, Any]) -> TicketPayload:
        """Convert an ADO Service Hook payload into a normalized TicketPayload.

        ADO Service Hooks send work item data under "resource.revision" for
        work item update events, or "resource" for work item created events.
        """
        resource = webhook_payload.get("resource", {})
        # For update events, the work item is in resource.revision
        work_item = resource.get("revision", resource)
        fields = work_item.get("fields", {})
        work_item_id = str(work_item.get("id", resource.get("workItemId", "")))

        # Type mapping
        raw_type = (fields.get("System.WorkItemType", "") or "").lower()
        ticket_type = _ADO_TYPE_MAP.get(raw_type, TicketType.TASK)

        # Extract acceptance criteria (HTML field in ADO)
        raw_ac = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or ""
        acceptance_criteria = self._parse_html_criteria(raw_ac)

        # Description (ADO uses HTML)
        description = fields.get("System.Description", "") or ""

        # Title
        title = fields.get("System.Title", "") or ""

        # Priority
        priority_num = fields.get("Microsoft.VSTS.Common.Priority", "")
        priority = f"P{priority_num}" if priority_num else ""

        # Assignee
        assigned_to = fields.get("System.AssignedTo", {})
        assignee = assigned_to.get("uniqueName", "") if isinstance(assigned_to, dict) else ""

        # Tags → labels
        tags_str = fields.get("System.Tags", "") or ""
        labels = [t.strip() for t in tags_str.split(";") if t.strip()]

        # Relations (linked items)
        linked_items = self._extract_relations(work_item.get("relations", []))

        # Attachments
        attachments = self._extract_attachments(work_item.get("relations", []))

        # Callback config
        org_url = self._settings.ado_org_url
        project = fields.get("System.TeamProject", "")
        callback = CallbackConfig(
            base_url=org_url,
            ticket_id=work_item_id,
            source=TicketSource.ADO,
            auth_token=self._settings.ado_pat,
            ado_project=project,
        ) if org_url else None

        return TicketPayload(
            source=TicketSource.ADO,
            id=f"{project}-{work_item_id}" if project else work_item_id,
            ticket_type=ticket_type,
            title=title,
            description=self._strip_html(description),
            acceptance_criteria=acceptance_criteria,
            attachments=attachments,
            linked_items=linked_items,
            labels=labels,
            priority=priority,
            assignee=assignee,
            callback=callback,
            raw_payload=webhook_payload,
        )

    @staticmethod
    def _strip_html(html: str) -> str:
        """Naive HTML tag stripping for ADO rich text fields."""
        import re

        if not html:
            return ""
        text = re.sub(r"<br\s*/?>", "\n", html)
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")
        text = text.replace("&lt;", "<").replace("&gt;", ">")
        return text.strip()

    @staticmethod
    def _parse_html_criteria(html: str) -> list[str]:
        """Parse acceptance criteria from ADO HTML field.

        ADO acceptance criteria are typically in <li> tags or <br>-separated lines.
        """
        import re

        if not html.strip():
            return []

        # Extract content from <li> tags
        li_items = re.findall(r"<li[^>]*>(.*?)</li>", html, re.DOTALL | re.IGNORECASE)
        if li_items:
            return [
                re.sub(r"<[^>]+>", "", item).strip()
                for item in li_items
                if re.sub(r"<[^>]+>", "", item).strip()
            ]

        # Fall back to <br>-separated or newline-separated
        text = re.sub(r"<br\s*/?>", "\n", html)
        text = re.sub(r"<[^>]+>", "", text)
        lines = text.strip().splitlines()
        criteria: list[str] = []
        for line in lines:
            cleaned = line.strip()
            # Strip bullet prefixes
            for prefix in ("-", "*", "•"):
                if cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix):].strip()
                    break
            if cleaned:
                criteria.append(cleaned)
        return criteria

    @staticmethod
    def _extract_relations(relations: list[dict[str, Any]]) -> list[LinkedItem]:
        """Extract linked work items from ADO relations."""
        items: list[LinkedItem] = []
        for rel in relations or []:
            rel_type = rel.get("attributes", {}).get("name", "")
            url = rel.get("url", "")
            # Only include work item links, not attachment or hyperlink relations
            if "/workItems/" in url:
                # Extract work item ID from URL
                wi_id = url.rsplit("/", 1)[-1]
                items.append(
                    LinkedItem(
                        id=wi_id,
                        source=TicketSource.ADO,
                        relationship=rel_type,
                    )
                )
        return items

    @staticmethod
    def _extract_attachments(relations: list[dict[str, Any]]) -> list[Attachment]:
        """Extract attachments from ADO relations (attachments are relations in ADO)."""
        attachments: list[Attachment] = []
        for rel in relations or []:
            if rel.get("rel") == "AttachedFile":
                attrs = rel.get("attributes", {})
                attachments.append(
                    Attachment(
                        filename=attrs.get("name", ""),
                        url=rel.get("url", ""),
                        content_type=attrs.get("resourceType", ""),
                    )
                )
        return attachments

    # --- Write-back operations ---

    def _parse_ticket_id(self, ticket_id: str) -> tuple[str, str]:
        """Extract (project, work_item_id) from composite ticket ID.

        The project prefix may be a short alias (e.g., "XCSF30") rather than
        the real ADO project name (e.g., "XC-SF-30in30"). If a mapping was
        registered during normalize(), use the real name for API calls.

        Raises ValueError if the ID has no project prefix (no dash).
        """
        if "-" not in ticket_id:
            raise ValueError(
                f"Invalid ADO ticket ID '{ticket_id}': expected 'PROJECT-123' format"
            )
        project_key, wi_id = ticket_id.rsplit("-", 1)
        # Resolve to real ADO project name if mapped
        real_project = self._project_key_map.get(project_key, project_key)
        return real_project, wi_id

    async def write_comment(self, ticket_id: str, comment: str) -> None:
        """Post a comment on an ADO work item."""
        project, wi_id = self._parse_ticket_id(ticket_id)

        url = f"/{project}/_apis/wit/workItems/{wi_id}/comments?api-version=7.1-preview.4"
        body = {"text": comment}
        response = await self._client.post(
            url, json=body, headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        logger.info("ado_comment_posted", ticket_id=ticket_id)

    async def update_fields(self, ticket_id: str, fields: dict[str, str]) -> None:
        """Update fields on an ADO work item using JSON Patch."""
        project, wi_id = self._parse_ticket_id(ticket_id)

        url = f"/{project}/_apis/wit/workItems/{wi_id}?api-version=7.1"
        patch_ops = [
            {"op": "replace", "path": f"/fields/{field}", "value": value}
            for field, value in fields.items()
        ]
        response = await self._client.patch(url, json=patch_ops)
        response.raise_for_status()
        logger.info("ado_fields_updated", ticket_id=ticket_id, fields=list(fields.keys()))

    async def transition_status(self, ticket_id: str, target_status: str) -> None:
        """Transition an ADO work item to a target state."""
        await self.update_fields(ticket_id, {"System.State": target_status})
        logger.info("ado_status_transitioned", ticket_id=ticket_id, target=target_status)

    async def link_work_item_to_pr(
        self, ticket_id: str, pr_url: str, repo_id: str = ""
    ) -> None:
        """Link an ADO work item to a pull request via ArtifactLink relation.

        Args:
            ticket_id: Composite ticket ID (e.g., "PROJECT-123").
            pr_url: Full PR URL or just the PR ID. Used to construct the artifact URI.
            repo_id: Azure Repos repository GUID (needed for artifact URI construction).
        """
        project, wi_id = self._parse_ticket_id(ticket_id)

        # Extract PR ID from URL if needed (e.g., ".../pullrequests/42" → "42")
        pr_id = pr_url.rsplit("/", 1)[-1] if "/" in pr_url else pr_url

        # ADO artifact URI format for pull requests
        artifact_uri = (
            f"vstfs:///Git/PullRequestId/{project}%2F{repo_id}%2F{pr_id}"
            if repo_id
            else f"vstfs:///Git/PullRequestId/{project}%2F{pr_id}"
        )

        url = f"/{project}/_apis/wit/workItems/{wi_id}?api-version=7.1"
        patch_ops = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "ArtifactLink",
                    "url": artifact_uri,
                    "attributes": {"name": "Pull Request"},
                },
            }
        ]
        response = await self._client.patch(url, json=patch_ops)
        response.raise_for_status()
        logger.info(
            "ado_work_item_linked_to_pr",
            ticket_id=ticket_id,
            pr_id=pr_id,
        )

    async def add_label(self, ticket_id: str, label: str) -> None:
        """Add a tag to an ADO work item, idempotently.

        Short-circuits when the tag is already present (case-insensitive
        exact match on a ``;``-separated element). Without this guard,
        calling ``add_label("PROJ-1", "ai_complete")`` twice produced
        ``System.Tags = "ai_complete; ai_complete"``, and over many
        retries (judge → fix cycles, re-runs, manual re-labels) the
        tag list would grow unbounded — polluting the edge-detection
        state machine and inflating ``labels`` in downstream tracing.
        """
        project, wi_id = self._parse_ticket_id(ticket_id)

        # First, get current tags
        url = f"/{project}/_apis/wit/workItems/{wi_id}?api-version=7.1&$select=System.Tags"
        response = await self._client.get(url)
        response.raise_for_status()
        current_tags = response.json().get("fields", {}).get("System.Tags", "")

        # Idempotency guard — case-insensitive exact match on any
        # existing ``;``-separated tag element.
        label_stripped = label.strip()
        existing_tags = {
            t.strip().lower()
            for t in current_tags.split(";")
            if t.strip()
        }
        if label_stripped.lower() in existing_tags:
            logger.info(
                "ado_label_already_present",
                ticket_id=ticket_id,
                label=label,
            )
            return

        # Append new tag
        new_tags = f"{current_tags}; {label_stripped}" if current_tags else label_stripped
        await self.update_fields(ticket_id, {"System.Tags": new_tags})
        logger.info("ado_label_added", ticket_id=ticket_id, label=label)
