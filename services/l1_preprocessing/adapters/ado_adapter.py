"""Azure DevOps adapter — normalizes Service Hook payloads and provides write-back operations."""

from __future__ import annotations

from typing import Any

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

    async def write_comment(self, ticket_id: str, comment: str) -> None:
        """Post a comment on an ADO work item."""
        # ADO uses work item ID (numeric), extract from our composite ID
        wi_id = ticket_id.rsplit("-", 1)[-1] if "-" in ticket_id else ticket_id
        project = ticket_id.rsplit("-", 1)[0] if "-" in ticket_id else ""

        url = f"/{project}/_apis/wit/workItems/{wi_id}/comments?api-version=7.1-preview.4"
        body = {"text": comment}
        response = await self._client.post(
            url, json=body, headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        logger.info("ado_comment_posted", ticket_id=ticket_id)

    async def update_fields(self, ticket_id: str, fields: dict[str, str]) -> None:
        """Update fields on an ADO work item using JSON Patch."""
        wi_id = ticket_id.rsplit("-", 1)[-1] if "-" in ticket_id else ticket_id
        project = ticket_id.rsplit("-", 1)[0] if "-" in ticket_id else ""

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

    async def add_label(self, ticket_id: str, label: str) -> None:
        """Add a tag to an ADO work item."""
        wi_id = ticket_id.rsplit("-", 1)[-1] if "-" in ticket_id else ticket_id
        project = ticket_id.rsplit("-", 1)[0] if "-" in ticket_id else ""

        # First, get current tags
        url = f"/{project}/_apis/wit/workItems/{wi_id}?api-version=7.1&$select=System.Tags"
        response = await self._client.get(url)
        response.raise_for_status()
        current_tags = response.json().get("fields", {}).get("System.Tags", "")

        # Append new tag
        new_tags = f"{current_tags}; {label}" if current_tags else label
        await self.update_fields(ticket_id, {"System.Tags": new_tags})
        logger.info("ado_label_added", ticket_id=ticket_id, label=label)
