"""Tests for autonomy_jira_bug — Jira bug webhook normalization."""

from __future__ import annotations

from typing import Any

from autonomy_jira_bug import (
    derive_category,
    extract_candidate_parents,
    map_priority_to_severity,
    normalize_jira_bug,
)


class _Settings:
    """Minimal stand-in for the app Settings object."""

    def __init__(
        self,
        jira_implemented_ticket_field_id: str = "",
        jira_bug_link_types: str = "is caused by,relates to,is blocked by",
        jira_qa_confirmed_field_id: str = "",
    ) -> None:
        self.jira_implemented_ticket_field_id = jira_implemented_ticket_field_id
        self.jira_bug_link_types = jira_bug_link_types
        self.jira_qa_confirmed_field_id = jira_qa_confirmed_field_id


# ---------------------------------------------------------------------------
# map_priority_to_severity
# ---------------------------------------------------------------------------

def test_map_priority_highest_to_critical() -> None:
    assert map_priority_to_severity("Highest") == "critical"
    assert map_priority_to_severity("blocker") == "critical"
    assert map_priority_to_severity("Critical") == "critical"


def test_map_priority_standard_buckets() -> None:
    assert map_priority_to_severity("High") == "high"
    assert map_priority_to_severity("Medium") == "medium"
    assert map_priority_to_severity("Low") == "low"
    assert map_priority_to_severity("Lowest") == "low"
    assert map_priority_to_severity("Trivial") == "low"


def test_map_priority_unknown_to_empty() -> None:
    assert map_priority_to_severity("WeirdName") == ""
    assert map_priority_to_severity("") == ""
    assert map_priority_to_severity("   ") == ""


# ---------------------------------------------------------------------------
# derive_category
# ---------------------------------------------------------------------------

def test_derive_category_pre_existing_label() -> None:
    assert derive_category(["pre-existing"], "", "Bug") == "pre_existing"
    assert derive_category(["Pre_Existing"], "", "Bug") == "pre_existing"


def test_derive_category_infra_label() -> None:
    assert derive_category(["infra"], "", "Bug") == "infra"
    assert derive_category(["Infrastructure"], "", "Bug") == "infra"


def test_derive_category_feature_request_label() -> None:
    assert derive_category(["feature-request"], "", "Bug") == "feature_request"


def test_derive_category_feature_request_issuetype() -> None:
    assert derive_category([], "", "Story") == "feature_request"
    assert derive_category([], "", "New Feature") == "feature_request"
    assert derive_category([], "", "Improvement") == "feature_request"


def test_derive_category_defaults_escaped() -> None:
    assert derive_category([], "nothing special", "Bug") == "escaped"
    assert derive_category(["some-other"], "", "Bug") == "escaped"


def test_derive_category_description_marker() -> None:
    assert derive_category([], "[pre-existing] old issue", "Bug") == "pre_existing"
    assert derive_category([], "summary [infra] related", "Bug") == "infra"


def test_derive_category_label_precedence_over_issuetype() -> None:
    # labels should win over story issuetype
    assert derive_category(["pre-existing"], "", "Story") == "pre_existing"


# ---------------------------------------------------------------------------
# extract_candidate_parents
# ---------------------------------------------------------------------------

def test_extract_candidates_custom_field_priority() -> None:
    settings = _Settings(jira_implemented_ticket_field_id="customfield_10050")
    fields = {
        "customfield_10050": "PROJ-100",
        "parent": {
            "key": "PROJ-200",
            "fields": {"issuetype": {"name": "Task"}},
        },
    }
    result = extract_candidate_parents(fields, settings)
    assert result[0] == "PROJ-100"
    assert "PROJ-200" in result


def test_extract_candidates_custom_field_dict_form() -> None:
    settings = _Settings(jira_implemented_ticket_field_id="customfield_10050")
    fields: dict[str, Any] = {
        "customfield_10050": {"key": "PROJ-100"},
    }
    result = extract_candidate_parents(fields, settings)
    assert result == ["PROJ-100"]


def test_extract_candidates_parent_used_when_no_custom() -> None:
    settings = _Settings()
    fields = {
        "parent": {
            "key": "PROJ-50",
            "fields": {"issuetype": {"name": "Task"}},
        },
    }
    result = extract_candidate_parents(fields, settings)
    assert result == ["PROJ-50"]


def test_extract_candidates_skips_epic_parent() -> None:
    settings = _Settings()
    fields = {
        "parent": {
            "key": "EPIC-1",
            "fields": {"issuetype": {"name": "Epic"}},
        },
    }
    result = extract_candidate_parents(fields, settings)
    assert result == []


def test_extract_candidates_issuelinks_ordered_by_type() -> None:
    settings = _Settings()
    # put "relates to" link BEFORE "is caused by" in the list
    fields = {
        "issuelinks": [
            {
                "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                "outwardIssue": {"key": "PROJ-RELATES"},
            },
            {
                "type": {"name": "Problem/Incident", "inward": "is caused by", "outward": "causes"},
                "inwardIssue": {"key": "PROJ-CAUSED"},
            },
            {
                "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                "inwardIssue": {"key": "PROJ-BLOCKED"},
            },
        ],
    }
    result = extract_candidate_parents(fields, settings)
    # priority: "is caused by" first, then "relates to", then "is blocked by"
    assert result == ["PROJ-CAUSED", "PROJ-RELATES", "PROJ-BLOCKED"]


def test_extract_candidates_label_fallback() -> None:
    settings = _Settings()
    fields = {
        "labels": ["urgent", "PROJ-777", "ABC-12", "not-a-ticket"],
    }
    result = extract_candidate_parents(fields, settings)
    assert "PROJ-777" in result
    assert "ABC-12" in result
    assert "urgent" not in result


def test_extract_candidates_dedup() -> None:
    settings = _Settings(jira_implemented_ticket_field_id="customfield_10050")
    fields = {
        "customfield_10050": "PROJ-100",
        "parent": {
            "key": "PROJ-100",
            "fields": {"issuetype": {"name": "Task"}},
        },
        "labels": ["PROJ-100"],
    }
    result = extract_candidate_parents(fields, settings)
    assert result == ["PROJ-100"]


def test_extract_candidates_empty_returns_empty() -> None:
    settings = _Settings()
    assert extract_candidate_parents({}, settings) == []


# ---------------------------------------------------------------------------
# normalize_jira_bug
# ---------------------------------------------------------------------------

def test_normalize_jira_bug_full_payload() -> None:
    settings = _Settings(jira_implemented_ticket_field_id="customfield_10050")
    payload = {
        "issue": {
            "key": "BUG-42",
            "fields": {
                "issuetype": {"name": "Bug"},
                "created": "2026-04-03T10:00:00.000+0000",
                "priority": {"name": "High"},
                "labels": ["customer-reported"],
                "summary": "Checkout fails on mobile",
                "description": "Plain description text",
                "customfield_10050": "PROJ-100",
                "parent": {
                    "key": "PROJ-200",
                    "fields": {"issuetype": {"name": "Task"}},
                },
                "issuelinks": [],
            },
        },
    }
    bug = normalize_jira_bug(payload, settings)
    assert bug.bug_key == "BUG-42"
    assert bug.issuetype == "Bug"
    assert bug.severity == "high"
    assert bug.labels == ["customer-reported"]
    assert bug.summary == "Checkout fails on mobile"
    assert bug.description == "Plain description text"
    assert bug.candidate_parent_keys[0] == "PROJ-100"
    assert bug.qa_confirmed is True
    assert bug.category == "escaped"
    assert bug.created_at.startswith("2026-04-03")


def test_normalize_jira_bug_adf_description() -> None:
    settings = _Settings()
    payload = {
        "issue": {
            "key": "BUG-1",
            "fields": {
                "issuetype": {"name": "Bug"},
                "created": "2026-04-03T10:00:00.000+0000",
                "priority": {"name": "Medium"},
                "summary": "ADF bug",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "Line one."},
                            ],
                        },
                    ],
                },
            },
        },
    }
    bug = normalize_jira_bug(payload, settings)
    assert "Line one." in bug.description


def test_normalize_jira_bug_qa_confirmed_custom_field_dict() -> None:
    settings = _Settings(jira_qa_confirmed_field_id="customfield_20000")
    payload = {
        "issue": {
            "key": "BUG-1",
            "fields": {
                "issuetype": {"name": "Bug"},
                "summary": "x",
                "customfield_20000": {"value": False},
            },
        },
    }
    bug = normalize_jira_bug(payload, settings)
    assert bug.qa_confirmed is False


def test_normalize_jira_bug_needs_triage_label_unconfirms() -> None:
    settings = _Settings()
    payload = {
        "issue": {
            "key": "BUG-1",
            "fields": {
                "issuetype": {"name": "Bug"},
                "summary": "x",
                "labels": ["needs-triage"],
            },
        },
    }
    bug = normalize_jira_bug(payload, settings)
    assert bug.qa_confirmed is False


def test_normalize_jira_bug_truncates_long_fields() -> None:
    settings = _Settings()
    payload = {
        "issue": {
            "key": "BUG-1",
            "fields": {
                "issuetype": {"name": "Bug"},
                "summary": "x" * 3000,
                "description": "y" * 5000,
            },
        },
    }
    bug = normalize_jira_bug(payload, settings)
    assert len(bug.summary) == 2000
    assert len(bug.description) == 4000
