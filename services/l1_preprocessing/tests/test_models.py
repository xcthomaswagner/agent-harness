"""Tests for data models — serialization round-trips and validation."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from models import (
    Attachment,
    CallbackConfig,
    DecompositionPlan,
    DesignSpec,
    EnrichedTicket,
    InfoRequest,
    LinkedItem,
    SizeAssessment,
    SizeClassification,
    SubTicketSpec,
    TestScenario,
    TestType,
    TicketPayload,
    TicketSource,
    TicketType,
    classify_analyst_output,
)

# --- TicketPayload ---


class TestTicketPayload:
    def test_minimal_ticket(self) -> None:
        ticket = TicketPayload(
            source=TicketSource.JIRA,
            id="PROJ-123",
            ticket_type=TicketType.STORY,
            title="Add login button",
        )
        assert ticket.source == "jira"
        assert ticket.id == "PROJ-123"
        assert ticket.acceptance_criteria == []
        assert ticket.attachments == []
        assert ticket.callback is None

    def test_full_ticket_round_trip(self) -> None:
        ticket = TicketPayload(
            source=TicketSource.JIRA,
            id="PROJ-456",
            ticket_type=TicketType.BUG,
            title="Fix crash on login",
            description="App crashes when clicking login",
            acceptance_criteria=["Login does not crash", "Error is logged"],
            attachments=[
                Attachment(filename="screenshot.png", url="https://example.com/img.png")
            ],
            linked_items=[
                LinkedItem(
                    id="PROJ-100",
                    source=TicketSource.JIRA,
                    relationship="blocks",
                    title="Release 2.0",
                )
            ],
            labels=["critical", "ai-implement"],
            priority="High",
            assignee="dev@example.com",
            callback=CallbackConfig(
                base_url="https://jira.example.com",
                ticket_id="PROJ-456",
                source=TicketSource.JIRA,
                auth_token="secret",
            ),
            raw_payload={"key": "PROJ-456"},
        )
        # Round-trip via JSON
        json_str = ticket.model_dump_json()
        restored = TicketPayload.model_validate_json(json_str)
        assert restored == ticket
        assert restored.attachments[0].filename == "screenshot.png"
        assert restored.linked_items[0].relationship == "blocks"

    def test_ticket_type_validation(self) -> None:
        with pytest.raises(ValidationError):
            TicketPayload(
                source=TicketSource.JIRA,
                id="X-1",
                ticket_type="epic",  # type: ignore[arg-type]
                title="Invalid",
            )


# --- EnrichedTicket ---


class TestEnrichedTicket:
    def test_enriched_extends_payload(self) -> None:
        enriched = EnrichedTicket(
            source=TicketSource.ADO,
            id="WI-789",
            ticket_type=TicketType.TASK,
            title="Refactor auth module",
            generated_acceptance_criteria=["Auth module uses new token format"],
            test_scenarios=[
                TestScenario(
                    name="Token refresh",
                    test_type=TestType.INTEGRATION,
                    description="Verify token refresh flow works end-to-end",
                    criteria_ref="AC-1",
                )
            ],
            edge_cases=["Token expires during request"],
            size_assessment=SizeAssessment(
                classification=SizeClassification.MEDIUM,
                estimated_units=3,
                recommended_dev_count=2,
                rationale="Three independent modules to update",
            ),
            analyst_notes="Consider backward compatibility",
            enriched_at=datetime(2026, 3, 21, 10, 0, 0, tzinfo=UTC),
        )
        json_str = enriched.model_dump_json()
        restored = EnrichedTicket.model_validate_json(json_str)
        assert [ac.text for ac in restored.generated_acceptance_criteria] == [
            "Auth module uses new token format"
        ]
        assert all(ac.category == "ticket" for ac in restored.generated_acceptance_criteria)
        assert restored.test_scenarios[0].test_type == TestType.INTEGRATION
        assert restored.size_assessment is not None
        assert restored.size_assessment.recommended_dev_count == 2
        assert restored.enriched_at is not None

    def test_enriched_with_figma_spec(self) -> None:
        enriched = EnrichedTicket(
            source=TicketSource.JIRA,
            id="PROJ-200",
            ticket_type=TicketType.STORY,
            title="Build dashboard",
            figma_design_spec=DesignSpec(
                figma_url="https://figma.com/file/abc",
                components=["Card", "Chart", "Sidebar"],
                color_tokens={"primary": "#1B2A4A"},
            ),
            platform_profile="sitecore",
        )
        json_str = enriched.model_dump_json()
        restored = EnrichedTicket.model_validate_json(json_str)
        assert restored.figma_design_spec is not None
        assert len(restored.figma_design_spec.components) == 3
        assert restored.platform_profile == "sitecore"


# --- AcceptanceCriterion migration ---


class TestAcceptanceCriterionMigration:
    def test_legacy_list_str_migrates_to_structured(self) -> None:
        """Legacy ``list[str]`` on disk wraps as ``category=ticket`` ACs."""
        data = {
            "source": "jira",
            "id": "LEG-1",
            "ticket_type": "story",
            "title": "Legacy ticket",
            "generated_acceptance_criteria": ["first ac", "second ac"],
        }
        ticket = EnrichedTicket.model_validate(data)
        assert len(ticket.generated_acceptance_criteria) == 2
        assert ticket.generated_acceptance_criteria[0].id == "AC-001"
        assert ticket.generated_acceptance_criteria[0].category == "ticket"
        assert ticket.generated_acceptance_criteria[0].text == "first ac"
        assert ticket.generated_acceptance_criteria[1].id == "AC-002"
        assert ticket.generated_acceptance_criteria[1].feature_type is None

    def test_structured_ac_roundtrip(self) -> None:
        """New shape round-trips through JSON without loss."""
        enriched = EnrichedTicket(
            source=TicketSource.JIRA,
            id="NEW-1",
            ticket_type=TicketType.STORY,
            title="New-shape ticket",
            generated_acceptance_criteria=[
                {"id": "AC-001", "category": "ticket", "text": "foo"},
                {
                    "id": "AC-002",
                    "category": "implicit",
                    "text": "bar",
                    "feature_type": "form_controls",
                    "verifiable_by": "integration_test",
                },
            ],
            detected_feature_types=["form_controls"],
            classification_reasoning="matched on 'filters'",
        )
        restored = EnrichedTicket.model_validate_json(enriched.model_dump_json())
        assert len(restored.generated_acceptance_criteria) == 2
        assert restored.generated_acceptance_criteria[1].category == "implicit"
        assert restored.generated_acceptance_criteria[1].feature_type == "form_controls"
        assert restored.generated_acceptance_criteria[1].verifiable_by == "integration_test"
        assert restored.detected_feature_types == ["form_controls"]
        assert restored.classification_reasoning == "matched on 'filters'"

    def test_empty_list_stays_empty(self) -> None:
        ticket = EnrichedTicket.model_validate(
            {
                "source": "jira",
                "id": "E-1",
                "ticket_type": "story",
                "title": "empty",
                "generated_acceptance_criteria": [],
            }
        )
        assert ticket.generated_acceptance_criteria == []

    def test_none_normalizes_to_empty(self) -> None:
        ticket = EnrichedTicket.model_validate(
            {
                "source": "jira",
                "id": "N-1",
                "ticket_type": "story",
                "title": "none",
                "generated_acceptance_criteria": None,
            }
        )
        assert ticket.generated_acceptance_criteria == []

    def test_structured_missing_required_field_raises(self) -> None:
        """Dict without ``id`` is invalid structured input."""
        with pytest.raises(ValidationError):
            EnrichedTicket.model_validate(
                {
                    "source": "jira",
                    "id": "BAD-1",
                    "ticket_type": "story",
                    "title": "bad",
                    "generated_acceptance_criteria": [
                        {"category": "ticket", "text": "missing id"}
                    ],
                }
            )

    def test_legacy_empty_strings_are_skipped(self) -> None:
        """Empty-string entries in legacy lists don't produce meaningless ACs."""
        ticket = EnrichedTicket.model_validate(
            {
                "source": "jira",
                "id": "EMPTY-1",
                "ticket_type": "story",
                "title": "empty strings",
                "generated_acceptance_criteria": ["", "  ", "real ac"],
            }
        )
        assert len(ticket.generated_acceptance_criteria) == 1
        assert ticket.generated_acceptance_criteria[0].text == "real ac"
        assert ticket.generated_acceptance_criteria[0].id == "AC-001"

    def test_mixed_legacy_and_structured_rejected(self) -> None:
        """Mixed list (strings + dicts) fails strict validation.

        The ``isinstance(v[0], str)`` check in the migration validator
        only triggers on a fully-legacy list. A mixed list falls
        through to strict validation; dicts lacking ``id``/``category``
        raise. Pinning this behavior so future refactors don't quietly
        relax it.
        """
        with pytest.raises(ValidationError):
            EnrichedTicket.model_validate(
                {
                    "source": "jira",
                    "id": "MIX-1",
                    "ticket_type": "story",
                    "title": "mixed",
                    "generated_acceptance_criteria": [
                        {"id": "AC-001", "category": "ticket", "text": "ok"},
                        "raw string",
                    ],
                }
            )

    def test_analyst_emitted_list_str_silently_migrates_to_ticket_category(
        self,
    ) -> None:
        """Documents the permissive migration behavior for in-process construction.

        If the analyst LLM regresses and emits legacy ``list[str]``, those
        entries are silently reclassified as ``category="ticket"`` — the
        Pydantic ``mode="before"`` validator fires on in-process
        construction too. No fail-fast guard. Regression test so any
        future strict-validation change requires an explicit decision.
        """
        ticket = EnrichedTicket(
            source=TicketSource.JIRA,
            id="DRIFT-1",
            ticket_type=TicketType.STORY,
            title="drift",
            generated_acceptance_criteria=["analyst regression emitted raw string"],
        )
        assert all(
            ac.category == "ticket" for ac in ticket.generated_acceptance_criteria
        )
        assert ticket.generated_acceptance_criteria[0].text.startswith(
            "analyst regression"
        )

    def test_ac_ids_are_sequential_ticket_first_implicit_second(self) -> None:
        """Documents the per-run AC id-ordering contract.

        IDs are positional and NOT stable across re-runs — downstream
        artifacts must not persist joins by ID. This test pins the
        intra-run order so that if the analyst produces both ticket and
        implicit ACs in a single call, ticket ACs come first. No code
        currently enforces order; this test documents the convention.
        """
        ticket = EnrichedTicket(
            source=TicketSource.JIRA,
            id="ORDER-1",
            ticket_type=TicketType.STORY,
            title="order",
            generated_acceptance_criteria=[
                {"id": "AC-001", "category": "ticket", "text": "ticket a"},
                {"id": "AC-002", "category": "ticket", "text": "ticket b"},
                {
                    "id": "AC-003",
                    "category": "implicit",
                    "text": "implicit a",
                    "feature_type": "form_controls",
                },
            ],
        )
        categories = [ac.category for ac in ticket.generated_acceptance_criteria]
        # Ticket-category entries precede implicit-category entries.
        first_implicit = next(
            i for i, c in enumerate(categories) if c == "implicit"
        )
        assert all(c == "ticket" for c in categories[:first_implicit])

    def test_legacy_fixture_file_migrates(self) -> None:
        """Repo-committed fixtures with ``list[str]`` shape migrate cleanly.

        Guards against future tests inventing their own legacy shape
        while the real backwards-compat surface is the tests/fixtures/
        files that the harness ships with.
        """
        import json
        from pathlib import Path

        fixture_root = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
        for name in ("sample-ticket-story.json", "sample-ticket-bug.json"):
            data = json.loads((fixture_root / name).read_text())
            if not data.get("generated_acceptance_criteria"):
                continue
            ticket = EnrichedTicket.model_validate(data)
            assert ticket.generated_acceptance_criteria, (
                f"expected {name} to produce ACs after migration"
            )
            assert all(
                ac.category == "ticket" for ac in ticket.generated_acceptance_criteria
            )


# --- InfoRequest ---


class TestInfoRequest:
    def test_info_request_round_trip(self) -> None:
        req = InfoRequest(
            ticket_id="PROJ-300",
            source=TicketSource.JIRA,
            questions=[
                "What is the expected behavior when the user is offline?",
                "Should the feature work on mobile?",
            ],
            context="Acceptance criteria do not cover offline scenarios",
        )
        json_str = req.model_dump_json()
        restored = InfoRequest.model_validate_json(json_str)
        assert len(restored.questions) == 2
        assert restored.source == TicketSource.JIRA


# --- DecompositionPlan ---


class TestDecompositionPlan:
    def test_decomposition_plan_round_trip(self) -> None:
        plan = DecompositionPlan(
            ticket_id="PROJ-400",
            source=TicketSource.JIRA,
            reason="Ticket has 6 independent units, exceeds 5-unit threshold",
            sub_tickets=[
                SubTicketSpec(
                    title="Implement auth API",
                    description="Build the authentication REST endpoints",
                    ticket_type=TicketType.TASK,
                    acceptance_criteria=["POST /auth/login returns JWT"],
                    estimated_size=SizeClassification.SMALL,
                ),
                SubTicketSpec(
                    title="Implement auth UI",
                    description="Build the login form component",
                    ticket_type=TicketType.TASK,
                    acceptance_criteria=["Login form renders correctly"],
                    estimated_size=SizeClassification.SMALL,
                    depends_on=["Implement auth API"],
                ),
            ],
            dependency_order=["Implement auth API", "Implement auth UI"],
        )
        json_str = plan.model_dump_json()
        restored = DecompositionPlan.model_validate_json(json_str)
        assert len(restored.sub_tickets) == 2
        assert restored.sub_tickets[1].depends_on == ["Implement auth API"]


# --- SizeAssessment validation ---


class TestSizeAssessment:
    def test_estimated_units_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            SizeAssessment(
                classification=SizeClassification.SMALL,
                estimated_units=0,
                recommended_dev_count=1,
            )

    def test_recommended_devs_max_10(self) -> None:
        with pytest.raises(ValidationError):
            SizeAssessment(
                classification=SizeClassification.LARGE,
                estimated_units=5,
                recommended_dev_count=11,
            )


# --- classify_analyst_output ---


class TestClassifyAnalystOutput:
    def test_enriched(self) -> None:
        enriched = EnrichedTicket(
            source=TicketSource.JIRA,
            id="X-1",
            ticket_type=TicketType.STORY,
            title="Test",
        )
        assert classify_analyst_output(enriched) == "enriched"

    def test_info_request(self) -> None:
        req = InfoRequest(
            ticket_id="X-2", source=TicketSource.ADO, questions=["What?"]
        )
        assert classify_analyst_output(req) == "info_request"

    def test_decomposition(self) -> None:
        plan = DecompositionPlan(
            ticket_id="X-3", source=TicketSource.JIRA, reason="Too big"
        )
        assert classify_analyst_output(plan) == "decomposition"


# --- CallbackConfig security ---


class TestCallbackConfig:
    def test_auth_token_hidden_from_repr(self) -> None:
        config = CallbackConfig(
            base_url="https://jira.example.com",
            ticket_id="PROJ-1",
            source=TicketSource.JIRA,
            auth_token="super-secret-token",
        )
        assert "super-secret-token" not in repr(config)

    def test_auth_token_present_in_dump(self) -> None:
        """auth_token must serialize when explicitly dumped (needed for API calls)."""
        config = CallbackConfig(
            base_url="https://jira.example.com",
            ticket_id="PROJ-1",
            source=TicketSource.JIRA,
            auth_token="super-secret-token",
        )
        data = config.model_dump()
        assert data["auth_token"] == "super-secret-token"


# --- InfoRequest validation ---


class TestInfoRequestValidation:
    def test_empty_questions_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InfoRequest(
                ticket_id="X-1",
                source=TicketSource.JIRA,
                questions=[],
            )


# --- SubTicketSpec defaults ---


class TestSubTicketSpec:
    def test_defaults(self) -> None:
        spec = SubTicketSpec(
            title="Do something",
            description="Details here",
            ticket_type=TicketType.TASK,
        )
        assert spec.estimated_size == SizeClassification.SMALL
        assert spec.acceptance_criteria == []
        assert spec.depends_on == []

    def test_round_trip(self) -> None:
        spec = SubTicketSpec(
            title="Build API",
            description="REST endpoints",
            ticket_type=TicketType.TASK,
            acceptance_criteria=["Returns 200"],
            estimated_size=SizeClassification.MEDIUM,
            depends_on=["Setup DB"],
        )
        restored = SubTicketSpec.model_validate_json(spec.model_dump_json())
        assert restored == spec


# --- Enum validation ---


class TestAttachmentProperties:
    def test_is_design_image_png(self) -> None:
        att = Attachment(filename="mock.png", url="https://x", content_type="image/png")
        assert att.is_design_image is True

    def test_is_design_image_jpeg(self) -> None:
        att = Attachment(filename="mock.jpg", url="https://x", content_type="image/jpeg")
        assert att.is_design_image is True

    def test_is_design_image_webp(self) -> None:
        att = Attachment(filename="mock.webp", url="https://x", content_type="image/webp")
        assert att.is_design_image is True

    def test_is_design_image_gif(self) -> None:
        att = Attachment(filename="mock.gif", url="https://x", content_type="image/gif")
        assert att.is_design_image is True

    def test_is_not_design_image_pdf(self) -> None:
        att = Attachment(filename="doc.pdf", url="https://x", content_type="application/pdf")
        assert att.is_design_image is False

    def test_is_not_design_image_empty(self) -> None:
        att = Attachment(filename="file.bin", url="https://x", content_type="")
        assert att.is_design_image is False

    def test_local_path_default_empty(self) -> None:
        att = Attachment(filename="f.png", url="https://x", content_type="image/png")
        assert att.local_path == ""

    def test_local_path_round_trip(self) -> None:
        att = Attachment(
            filename="f.png", url="https://x",
            content_type="image/png", local_path="/tmp/f.png",
        )
        restored = Attachment.model_validate_json(att.model_dump_json())
        assert restored.local_path == "/tmp/f.png"


class TestEnumValidation:
    def test_invalid_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TicketPayload(
                source="github",  # type: ignore[arg-type]
                id="X-1",
                ticket_type=TicketType.STORY,
                title="Invalid source",
            )
