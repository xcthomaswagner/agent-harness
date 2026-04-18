"""Cross-adapter equivalence: Jira and ADO produce structurally identical TicketPayloads.

The harness pipeline is source-agnostic past the adapter layer — every
downstream consumer (analyst, queue_worker, cross_ticket_coordinator,
autonomy_ingest) reads a ``TicketPayload`` and must not care whether
it came from Jira or ADO. This test asserts the two adapters produce
payloads of the SAME SHAPE (same field names, same field types) so a
future adapter change on one side can't silently drift the shape of
the other.

What we DON'T assert: field VALUES. Only shapes, plus a handful of
content-level invariants that both adapters must honor (e.g., the
``source`` enum differs, but ``labels`` is always a list of strings).

If this test fails, the pipeline contract is broken — either:
  * one adapter forgot to populate a shared field (leaving it as
    type None when the other side makes it a list), or
  * a new model field was added without teaching both adapters
    about it, or
  * a Pydantic type was tightened on one side but not the other.
"""

from __future__ import annotations

import json
from pathlib import Path

from adapters.ado_adapter import AdoAdapter
from adapters.jira_adapter import JiraAdapter
from config import Settings
from models import (
    Attachment,
    CallbackConfig,
    LinkedItem,
    TicketPayload,
    TicketSource,
    TicketType,
)

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


# Fields on ``TicketPayload`` that BOTH adapters must populate with
# the same type. ``raw_payload`` is excluded because both populate
# it (but with source-specific schemas — the whole point of having
# one). ``source`` and ``id`` are excluded because they legitimately
# differ on content AND we assert ``source`` separately.
_SAME_SHAPE_FIELDS = (
    "ticket_type",
    "title",
    "description",
    "acceptance_criteria",
    "attachments",
    "linked_items",
    "labels",
    "priority",
    "assignee",
    "callback",
)


def _load_jira_fixture() -> dict[str, object]:
    data: dict[str, object] = json.loads(
        (FIXTURES / "jira_webhook_story.json").read_text()
    )
    return data


def _load_ado_fixture() -> dict[str, object]:
    data: dict[str, object] = json.loads(
        (FIXTURES / "ado_webhook_story.json").read_text()
    )
    return data


def _make_settings_for_jira() -> Settings:
    return Settings(
        jira_base_url="https://acme.atlassian.net",
        jira_api_token="token",
        jira_user_email="bot@acme.com",
        jira_ac_field_id="customfield_10429",
    )


def _make_settings_for_ado() -> Settings:
    return Settings(
        ado_org_url="https://dev.azure.com/acme",
        ado_pat="pat",
    )


def test_jira_and_ado_adapters_produce_equivalent_ticket_payload_shape() -> None:
    """Jira and ADO webhooks for the SAME logical story normalize to the same TicketPayload shape.

    The two fixtures describe the same story:
      * Jira: ACME-42, labels [ai-implement, sprint-7], one attachment
      * ADO: AcmeProject-42, tags [ai-implement, sprint-7], one attachment

    They SHOULD diverge on ``source``, ``id`` (different prefix
    conventions), ``raw_payload`` (different wire format), and
    ``callback`` auth fields (PAT vs API token). Every other field
    must be the same shape.
    """
    jira_payload = _load_jira_fixture()
    ado_payload = _load_ado_fixture()

    jira_ticket = JiraAdapter(_make_settings_for_jira()).normalize(jira_payload)
    ado_ticket = AdoAdapter(_make_settings_for_ado()).normalize(ado_payload)

    # Sanity: both produced TicketPayload instances.
    assert isinstance(jira_ticket, TicketPayload)
    assert isinstance(ado_ticket, TicketPayload)

    # Source differs (intentionally).
    assert jira_ticket.source == TicketSource.JIRA
    assert ado_ticket.source == TicketSource.ADO

    # Shape check: every ``_SAME_SHAPE_FIELDS`` entry has the same type
    # on both tickets. We use ``type()`` (strict) rather than
    # ``isinstance`` so e.g. a drift from list -> set would surface
    # even if Pydantic coerced the value.
    for field in _SAME_SHAPE_FIELDS:
        assert hasattr(jira_ticket, field), f"jira_ticket missing {field}"
        assert hasattr(ado_ticket, field), f"ado_ticket missing {field}"
        j_val = getattr(jira_ticket, field)
        a_val = getattr(ado_ticket, field)
        assert type(j_val) is type(a_val), (
            f"field {field!r} shape diverged: "
            f"jira={type(j_val).__name__}({j_val!r}) vs "
            f"ado={type(a_val).__name__}({a_val!r})"
        )

    # Content-level invariants both must honor:
    # - ticket_type is a TicketType enum value (both fixtures describe
    #   a "Story"/"User Story" which maps to STORY).
    assert jira_ticket.ticket_type == TicketType.STORY
    assert ado_ticket.ticket_type == TicketType.STORY

    # - labels is always a list of strings, and both adapters surface
    #   the trigger tag.
    assert all(isinstance(label, str) for label in jira_ticket.labels)
    assert all(isinstance(label, str) for label in ado_ticket.labels)
    assert "ai-implement" in jira_ticket.labels
    assert "ai-implement" in ado_ticket.labels

    # - acceptance_criteria is always list[str]; both fixtures include
    #   bulleted ACs.
    assert all(isinstance(ac, str) for ac in jira_ticket.acceptance_criteria)
    assert all(isinstance(ac, str) for ac in ado_ticket.acceptance_criteria)
    assert len(jira_ticket.acceptance_criteria) >= 1
    assert len(ado_ticket.acceptance_criteria) >= 1

    # - attachments list contains Attachment instances with the same
    #   structural fields populated (filename/url/content_type are
    #   always str; local_path starts empty until download).
    assert len(jira_ticket.attachments) >= 1
    assert len(ado_ticket.attachments) >= 1
    for att in (*jira_ticket.attachments, *ado_ticket.attachments):
        assert isinstance(att, Attachment)
        assert isinstance(att.filename, str)
        assert isinstance(att.url, str)
        assert isinstance(att.content_type, str)
        assert att.local_path == ""  # Both adapters defer download

    # - linked_items list contains LinkedItem instances when present.
    #   Both fixtures have a link; each LinkedItem.source reflects the
    #   owning adapter.
    for li in jira_ticket.linked_items:
        assert isinstance(li, LinkedItem)
        assert li.source == TicketSource.JIRA
    for li in ado_ticket.linked_items:
        assert isinstance(li, LinkedItem)
        assert li.source == TicketSource.ADO

    # - callback is a CallbackConfig or None. When present, source
    #   matches the adapter's source.
    assert jira_ticket.callback is None or isinstance(
        jira_ticket.callback, CallbackConfig
    )
    assert ado_ticket.callback is None or isinstance(
        ado_ticket.callback, CallbackConfig
    )
    if jira_ticket.callback is not None:
        assert jira_ticket.callback.source == TicketSource.JIRA
    if ado_ticket.callback is not None:
        assert ado_ticket.callback.source == TicketSource.ADO

    # - raw_payload is always a dict, populated with the source-specific
    #   webhook body. Different content, same shape.
    assert isinstance(jira_ticket.raw_payload, dict)
    assert isinstance(ado_ticket.raw_payload, dict)
    assert jira_ticket.raw_payload  # non-empty
    assert ado_ticket.raw_payload  # non-empty
