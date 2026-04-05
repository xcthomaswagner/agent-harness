"""Jira bug webhook → NormalizedBug for defect ingestion."""

from __future__ import annotations

import re
from typing import Any, Literal

import structlog
from pydantic import BaseModel

from adapters.jira_adapter import JiraAdapter

logger = structlog.get_logger()

_PRIORITY_MAP = {
    "highest": "critical",
    "blocker": "critical",
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "lowest": "low",
    "trivial": "low",
}

_LINK_TYPE_DEFAULT = ("is caused by", "relates to", "is blocked by")
_TICKET_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")


class NormalizedBug(BaseModel):
    bug_key: str
    issuetype: str
    created_at: str
    severity: str
    labels: list[str]
    summary: str
    description: str
    candidate_parent_keys: list[str]
    qa_confirmed: bool
    category: Literal["escaped", "feature_request", "pre_existing", "infra"]


def map_priority_to_severity(priority_name: str) -> str:
    return _PRIORITY_MAP.get((priority_name or "").strip().lower(), "")


def derive_category(
    labels: list[str], description: str, issuetype: str
) -> Literal["escaped", "feature_request", "pre_existing", "infra"]:
    lower_labels = {label.lower() for label in labels}
    if "pre-existing" in lower_labels or "pre_existing" in lower_labels:
        return "pre_existing"
    if "infra" in lower_labels or "infrastructure" in lower_labels:
        return "infra"
    if "feature-request" in lower_labels or "feature_request" in lower_labels:
        return "feature_request"
    desc_lower = (description or "").lower()
    if "[pre-existing]" in desc_lower or "[pre_existing]" in desc_lower:
        return "pre_existing"
    if "[infra]" in desc_lower:
        return "infra"
    if issuetype.lower() in ("story", "new feature", "improvement"):
        return "feature_request"
    return "escaped"


def extract_candidate_parents(fields: dict[str, Any], settings: Any) -> list[str]:
    """Extract implemented-ticket candidate keys in priority order.

    1. Custom field (settings.jira_implemented_ticket_field_id)
    2. fields.parent.key (if not Epic)
    3. Linked issues via fields.issuelinks[] (prefer spec link types in order)
    4. Labels matching PROJ-N
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(key: str) -> None:
        if key and key not in seen:
            seen.add(key)
            candidates.append(key)

    # 1. Custom field
    field_id = getattr(settings, "jira_implemented_ticket_field_id", "") or ""
    if field_id and fields.get(field_id):
        val = fields[field_id]
        if isinstance(val, str):
            _add(val)
        elif isinstance(val, dict) and val.get("key"):
            _add(str(val["key"]))

    # 2. Parent (skip Epics — parent may be epic which isn't the implementation)
    parent = fields.get("parent") or {}
    if isinstance(parent, dict):
        parent_type = (
            ((parent.get("fields") or {}).get("issuetype") or {}).get("name", "") or ""
        ).lower()
        if parent.get("key") and parent_type != "epic":
            _add(str(parent["key"]))

    # 3. Issue links
    link_types_csv = (
        getattr(settings, "jira_bug_link_types", "") or ",".join(_LINK_TYPE_DEFAULT)
    )
    link_types = [t.strip().lower() for t in link_types_csv.split(",") if t.strip()]
    issuelinks = fields.get("issuelinks") or []
    # Process each configured type in order to preserve priority
    for t in link_types:
        for link in issuelinks:
            if not isinstance(link, dict):
                continue
            link_type = link.get("type") or {}
            link_type_name = str(link_type.get("name") or "").lower()
            link_inward = str(link_type.get("inward") or "").lower()
            link_outward = str(link_type.get("outward") or "").lower()
            if t not in (link_type_name, link_inward, link_outward):
                continue
            inward = link.get("inwardIssue") or {}
            outward = link.get("outwardIssue") or {}
            if isinstance(inward, dict) and inward.get("key"):
                _add(str(inward["key"]))
            if isinstance(outward, dict) and outward.get("key"):
                _add(str(outward["key"]))

    # 4. Label regex fallback
    for label in fields.get("labels") or []:
        label_str = str(label)
        if _TICKET_RE.fullmatch(label_str):
            _add(label_str)

    return candidates


def normalize_jira_bug(payload: dict[str, Any], settings: Any) -> NormalizedBug:
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}

    bug_key = str(issue.get("key") or "")
    issuetype = str((fields.get("issuetype") or {}).get("name") or "")
    created = str(fields.get("created") or "")
    priority = str((fields.get("priority") or {}).get("name") or "")
    labels = [str(label) for label in (fields.get("labels") or [])]
    summary = str(fields.get("summary") or "")[:2000]

    # Description — may be string or ADF
    raw_desc = fields.get("description")
    description = ""
    if isinstance(raw_desc, str):
        description = raw_desc
    elif isinstance(raw_desc, dict):
        # ADF — use JiraAdapter's static text extractor
        description = JiraAdapter._extract_text(raw_desc) or ""
    description = description[:4000]

    # QA confirmed flag
    qa_field_id = getattr(settings, "jira_qa_confirmed_field_id", "") or ""
    qa_confirmed = True
    if qa_field_id:
        val = fields.get(qa_field_id)
        if isinstance(val, dict):
            qa_confirmed = bool(val.get("value"))
        elif val is not None:
            qa_confirmed = bool(val)
    if "needs-triage" in {label.lower() for label in labels}:
        qa_confirmed = False

    return NormalizedBug(
        bug_key=bug_key,
        issuetype=issuetype,
        created_at=created,
        severity=map_priority_to_severity(priority),
        labels=labels,
        summary=summary,
        description=description,
        candidate_parent_keys=extract_candidate_parents(fields, settings),
        qa_confirmed=qa_confirmed,
        category=derive_category(labels, description, issuetype),
    )
