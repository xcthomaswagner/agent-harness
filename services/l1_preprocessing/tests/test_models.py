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
        assert restored.generated_acceptance_criteria == ["Auth module uses new token format"]
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
