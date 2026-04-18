"""Tests for the payload-delta-aware ADO edge-trigger dedupe.

Covers two paths:

1. Payload-based: resource.fields['System.Tags'] delta available → use it.
2. Memory fallback: delta absent → compare against the per-process last-seen
   state (legacy behavior preserved for backward compatibility).
"""

from __future__ import annotations

import pytest

from adapters.ado_adapter import extract_tag_transition
from main import _check_trigger_edge, _last_trigger_state


@pytest.fixture(autouse=True)
def _clear_memory_between_tests() -> None:
    """Isolate tests by clearing the in-process edge memory before each."""
    _last_trigger_state.clear()


# ---------------- extract_tag_transition ---------------------------------


def _payload_with_delta(old: str, new: str, tags_str: str | None = None) -> dict:
    """Build a minimal payload that looks like ADO workitem.updated.

    ``resource.fields['System.Tags']`` carries the delta; ``resource.revision.fields``
    carries the full post-state.
    """
    return {
        "resource": {
            "fields": {"System.Tags": {"oldValue": old, "newValue": new}},
            "revision": {
                "fields": {"System.Tags": tags_str if tags_str is not None else new}
            },
        },
    }


def _payload_no_delta(tags_str: str) -> dict:
    """Payload without a tag delta — simulates workitem.created or a
    non-tag update."""
    return {
        "resource": {
            "fields": {},
            "revision": {"fields": {"System.Tags": tags_str}},
        },
    }


def test_payload_delta_absent_to_present_fires_edge() -> None:
    p = _payload_with_delta(old="other", new="other; ai-implement")
    before, now = extract_tag_transition(p, ["ai-implement", "ai-quick"])
    assert before is False
    assert now is True
    assert _check_trigger_edge("T-1", now, was_present_before=before) is True


def test_payload_delta_already_present_does_not_fire() -> None:
    p = _payload_with_delta(old="ai-implement; foo", new="ai-implement; foo; bar")
    before, now = extract_tag_transition(p, ["ai-implement", "ai-quick"])
    assert before is True
    assert now is True
    # Tag was already present before — not a new edge.
    assert _check_trigger_edge("T-2", now, was_present_before=before) is False


def test_payload_delta_present_to_absent_does_not_fire() -> None:
    """Tag removed in this webhook — not a trigger."""
    p = _payload_with_delta(old="ai-implement; foo", new="foo")
    before, now = extract_tag_transition(p, ["ai-implement"])
    assert before is True
    assert now is False
    assert _check_trigger_edge("T-3", now, was_present_before=before) is False


def test_payload_delta_missing_falls_back_to_memory_first_seen() -> None:
    """No delta (e.g. workitem.created) and never-seen ticket + tag present
    → fires once."""
    p = _payload_no_delta(tags_str="ai-implement; other")
    before, now = extract_tag_transition(p, ["ai-implement"])
    assert before is None  # delta absent
    assert now is True
    assert _check_trigger_edge("T-4", now, was_present_before=before) is True
    # A second webhook with same memory state → not a new edge.
    assert _check_trigger_edge("T-4", now, was_present_before=None) is False


def test_payload_delta_missing_with_continued_presence_does_not_fire() -> None:
    """No delta but we've seen the tag before in memory → skip."""
    # Prime memory: a prior webhook already saw the tag present.
    _check_trigger_edge("T-5", True, was_present_before=None)
    p = _payload_no_delta(tags_str="ai-implement")
    before, now = extract_tag_transition(p, ["ai-implement"])
    assert before is None
    assert _check_trigger_edge("T-5", now, was_present_before=before) is False


def test_tag_match_is_case_insensitive_and_exact() -> None:
    """AI-Implement should match ai-implement; ai-implement-later should NOT."""
    p = _payload_with_delta(old="foo", new="foo; AI-Implement")
    before, now = extract_tag_transition(p, ["ai-implement"])
    assert before is False
    assert now is True
    # Substring-only match is a known hazard — explicit regression guard.
    p2 = _payload_with_delta(old="", new="ai-implement-later")
    before2, now2 = extract_tag_transition(p2, ["ai-implement"])
    assert before2 is False
    assert now2 is False  # exact match only


def test_quick_label_counts_as_trigger() -> None:
    """Both ai_label and quick_label are triggers; either transition fires."""
    p = _payload_with_delta(old="", new="ai-quick")
    before, now = extract_tag_transition(p, ["ai-implement", "ai-quick"])
    assert before is False
    assert now is True


def test_payload_delta_with_empty_old_value() -> None:
    """ADO sometimes sends only newValue for a freshly-added tag."""
    p = {
        "resource": {
            "fields": {"System.Tags": {"newValue": "ai-implement"}},
            "revision": {"fields": {"System.Tags": "ai-implement"}},
        },
    }
    before, now = extract_tag_transition(p, ["ai-implement"])
    assert before is False
    assert now is True


def test_payload_non_dict_delta_falls_back_gracefully() -> None:
    """Malformed delta shouldn't crash — return (None, is_present_now)."""
    p = {
        "resource": {
            "fields": {"System.Tags": "unexpected-string"},
            "revision": {"fields": {"System.Tags": "ai-implement"}},
        },
    }
    before, now = extract_tag_transition(p, ["ai-implement"])
    assert before is None
    assert now is True


def test_memory_updated_even_when_payload_delta_used() -> None:
    """Using the payload path still updates memory so a later no-delta
    webhook has a fresh baseline to compare against."""
    p1 = _payload_with_delta(old="", new="ai-implement")
    before, now = extract_tag_transition(p1, ["ai-implement"])
    _check_trigger_edge("T-9", now, was_present_before=before)
    # Memory now shows the tag as present.
    assert _last_trigger_state.get("T-9") is True
    # A later webhook without delta with the tag still present → skip.
    assert _check_trigger_edge("T-9", True, was_present_before=None) is False
