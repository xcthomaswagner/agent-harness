"""Jira adapter — normalizes webhook payloads and provides write-back operations."""

from __future__ import annotations

import base64
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

# Jira issue type name -> our TicketType
_JIRA_TYPE_MAP: dict[str, TicketType] = {
    "story": TicketType.STORY,
    "user story": TicketType.STORY,
    "bug": TicketType.BUG,
    "task": TicketType.TASK,
    "sub-task": TicketType.TASK,
    "subtask": TicketType.TASK,
}


class JiraAdapter:
    """Normalizes Jira webhook payloads and writes back to Jira REST API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.jira_base_url,
            headers=self._auth_headers(settings),
            timeout=30.0,
        )

    @staticmethod
    def _auth_headers(settings: Settings) -> dict[str, str]:
        """Build Basic auth headers for Jira REST API."""
        credentials = f"{settings.jira_user_email}:{settings.jira_api_token}"
        token = base64.b64encode(credentials.encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def normalize(self, webhook_payload: dict[str, Any]) -> TicketPayload:
        """Convert a Jira webhook payload into a normalized TicketPayload.

        Jira automation webhooks send the issue data under the "issue" key.
        The exact structure depends on the webhook configuration.
        """
        issue = webhook_payload.get("issue", webhook_payload)
        fields = issue.get("fields", {})
        key = issue.get("key", "")

        # Issue type mapping
        raw_type = (fields.get("issuetype", {}).get("name", "") or "").lower()
        ticket_type = _JIRA_TYPE_MAP.get(raw_type, TicketType.TASK)

        # Acceptance criteria from custom field
        ac_field = self._settings.jira_ac_field_id
        raw_ac_value = fields.get(ac_field, "") or ""
        raw_ac = self._extract_text(raw_ac_value)
        acceptance_criteria = self._parse_acceptance_criteria(raw_ac)

        # Attachments
        attachments = [
            Attachment(
                filename=att.get("filename", ""),
                url=att.get("content", ""),
                content_type=att.get("mimeType", ""),
            )
            for att in fields.get("attachment", [])
        ]

        # Linked issues
        linked_items = [
            LinkedItem(
                id=link.get("outwardIssue", link.get("inwardIssue", {})).get("key", ""),
                source=TicketSource.JIRA,
                relationship=link.get("type", {}).get("name", ""),
                title=link.get("outwardIssue", link.get("inwardIssue", {}))
                .get("fields", {})
                .get("summary", ""),
            )
            for link in fields.get("issuelinks", [])
            if link.get("outwardIssue") or link.get("inwardIssue")
        ]

        # Labels
        labels = fields.get("labels", []) or []

        # Priority
        priority = (fields.get("priority", {}) or {}).get("name", "")

        # Assignee
        assignee = (fields.get("assignee", {}) or {}).get("emailAddress", "")

        # Callback config for write-back
        callback = CallbackConfig(
            base_url=self._settings.jira_base_url,
            ticket_id=key,
            source=TicketSource.JIRA,
            auth_token=self._settings.jira_api_token,
        )

        return TicketPayload(
            source=TicketSource.JIRA,
            id=key,
            ticket_type=ticket_type,
            title=fields.get("summary", ""),
            description=self._extract_text(fields.get("description", "")),
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
    def _extract_text(value: object) -> str:
        """Extract plain text from a field value.

        Handles both plain strings and Atlassian Document Format (ADF) dicts.
        ADF is used by Jira REST API v3 for rich text fields.
        """
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return str(value) if value else ""
        if value.get("type") != "doc":
            return str(value)
        return JiraAdapter._adf_to_text(value)

    @staticmethod
    def _adf_to_text(node: dict[str, Any]) -> str:
        """Recursively convert an ADF document node to plain text."""
        node_type = node.get("type", "")
        text_parts: list[str] = []

        # Text node — the leaf
        if node_type == "text":
            return str(node.get("text", ""))

        # Hard break
        if node_type == "hardBreak":
            return "\n"

        # Process children
        for child in node.get("content", []):
            text_parts.append(JiraAdapter._adf_to_text(child))

        joined = "".join(text_parts)

        # Add formatting based on node type
        if node_type == "paragraph":
            return joined + "\n"
        if node_type == "listItem":
            return "- " + joined
        if node_type in ("bulletList", "orderedList"):
            return joined + "\n"
        if node_type == "heading":
            level = int(node.get("attrs", {}).get("level", 1))
            return "#" * level + " " + joined + "\n"

        return joined

    @staticmethod
    def _parse_acceptance_criteria(raw: str) -> list[str]:
        """Parse acceptance criteria from a text field.

        Supports:
        - Line-separated items
        - Bullet-prefixed items (-, *, •)
        - Numbered items (1., 2., etc.)
        """
        if not raw.strip():
            return []
        lines = raw.strip().splitlines()
        criteria: list[str] = []
        for line in lines:
            cleaned = line.strip()
            # Strip common bullet/number prefixes
            for prefix in ("-", "*", "•"):
                if cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix) :].strip()
                    break
            else:
                # Check for numbered prefix like "1.", "2.", "10.", etc.
                dot_pos = cleaned.find(".")
                if dot_pos > 0 and cleaned[:dot_pos].isdigit():
                    cleaned = cleaned[dot_pos + 1 :].strip()
            if cleaned:
                criteria.append(cleaned)
        return criteria

    # --- Write-back operations ---

    async def write_comment(self, ticket_id: str, comment: str) -> None:
        """Post a comment on a Jira ticket."""
        url = f"/rest/api/3/issue/{ticket_id}/comment"
        body = {"body": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": comment}]}
        ]}}
        response = await self._client.post(url, json=body)
        response.raise_for_status()
        logger.info("jira_comment_posted", ticket_id=ticket_id)

    async def update_fields(self, ticket_id: str, fields: dict[str, Any]) -> None:
        """Update fields on a Jira ticket."""
        url = f"/rest/api/3/issue/{ticket_id}"
        response = await self._client.put(url, json={"fields": fields})
        response.raise_for_status()
        logger.info("jira_fields_updated", ticket_id=ticket_id, fields=list(fields.keys()))

    async def transition_status(self, ticket_id: str, target_status: str) -> None:
        """Transition a Jira ticket to a target status.

        First fetches available transitions, finds the matching one, then executes.
        """
        url = f"/rest/api/3/issue/{ticket_id}/transitions"
        response = await self._client.get(url)
        response.raise_for_status()

        transitions = response.json().get("transitions", [])
        target_transition = None
        for t in transitions:
            if t.get("name", "").lower() == target_status.lower():
                target_transition = t
                break

        if not target_transition:
            available = [t.get("name") for t in transitions]
            logger.warning(
                "jira_transition_not_found",
                ticket_id=ticket_id,
                target=target_status,
                available=available,
            )
            return

        response = await self._client.post(
            url, json={"transition": {"id": target_transition["id"]}}
        )
        response.raise_for_status()
        logger.info("jira_status_transitioned", ticket_id=ticket_id, target=target_status)

    async def add_label(self, ticket_id: str, label: str) -> None:
        """Add a label to a Jira ticket."""
        url = f"/rest/api/3/issue/{ticket_id}"
        body = {"update": {"labels": [{"add": label}]}}
        response = await self._client.put(url, json=body)
        response.raise_for_status()
        logger.info("jira_label_added", ticket_id=ticket_id, label=label)
