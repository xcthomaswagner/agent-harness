"""Tests for the consistency-check drafter (drafter_consistency_check)."""

from __future__ import annotations

from unittest.mock import MagicMock

import anthropic
import pytest

from learning_miner.drafter_consistency_check import (
    ConsistencyChecker,
    _parse_verdict_json,
)
from tests.conftest import make_anthropic_response


def _mock_response(text: str):
    return make_anthropic_response(text, tokens_in=50, tokens_out=30)


@pytest.fixture
def mock_client(mock_anthropic_client):
    return mock_anthropic_client


@pytest.fixture
def checker(mock_client: MagicMock) -> ConsistencyChecker:
    return ConsistencyChecker(api_key="test", client=mock_client, enabled=True)


class TestParseVerdictJson:
    def test_raw_json(self) -> None:
        obj = _parse_verdict_json(
            '{"contradicts": true, "contradicts_with": "rule A"}'
        )
        assert obj == {"contradicts": True, "contradicts_with": "rule A"}

    def test_fenced_json(self) -> None:
        obj = _parse_verdict_json('```json\n{"contradicts": false}\n```')
        assert obj == {"contradicts": False}

    def test_prose_wrapper(self) -> None:
        obj = _parse_verdict_json(
            'Here is my verdict:\n\n{"contradicts": false}\n\n'
            "Let me know if you have questions."
        )
        assert obj == {"contradicts": False}

    def test_malformed_returns_none(self) -> None:
        assert _parse_verdict_json("not json") is None

    def test_empty_returns_none(self) -> None:
        assert _parse_verdict_json("") is None

    def test_multiple_objects_returns_first(self) -> None:
        """Regression: greedy outermost ``{`` / ``}`` matching produced
        an invalid slice spanning both objects and returned None. Now
        we scan for the first balanced object instead.
        """
        obj = _parse_verdict_json(
            '{"contradicts": false} {"stray": "second object"}'
        )
        assert obj == {"contradicts": False}

    def test_braces_in_string_values(self) -> None:
        """Braces inside quoted JSON strings must not confuse the depth
        tracker — otherwise a reasoning field mentioning ``{braces}``
        would prematurely close the object.
        """
        obj = _parse_verdict_json(
            '{"reasoning": "saw a {nested} brace", "contradicts": true}'
        )
        assert obj is not None
        assert obj["contradicts"] is True
        assert "{nested}" in obj["reasoning"]


class TestCheckerDisabled:
    async def test_disabled_returns_noop_verdict(
        self, mock_client: MagicMock
    ) -> None:
        checker = ConsistencyChecker(
            api_key="test", client=mock_client, enabled=False
        )
        v = await checker.check(current_content="x", unified_diff="y")
        assert v.contradicts is False
        assert v.reasoning == "disabled"
        mock_client.messages.create.assert_not_called()


class TestCheckerHappyPath:
    async def test_no_contradiction(
        self, checker: ConsistencyChecker, mock_client: MagicMock
    ) -> None:
        mock_client.messages.create.return_value = _mock_response(
            '{"contradicts": false, "reasoning": "no conflict"}'
        )
        v = await checker.check(
            current_content="existing", unified_diff="+new rule"
        )
        assert v.contradicts is False
        assert v.reasoning == "no conflict"
        assert v.tokens_in == 50
        assert v.tokens_out == 30

    async def test_contradiction_blocks(
        self, checker: ConsistencyChecker, mock_client: MagicMock
    ) -> None:
        mock_client.messages.create.return_value = _mock_response(
            '{"contradicts": true, "contradicts_with": "Use X tool", '
            '"reasoning": "new rule mandates Y"}'
        )
        v = await checker.check(
            current_content="existing", unified_diff="+new rule"
        )
        assert v.contradicts is True
        assert v.contradicts_with == "Use X tool"


class TestCheckerEmptyDiff:
    async def test_empty_diff_short_circuits(
        self, checker: ConsistencyChecker, mock_client: MagicMock
    ) -> None:
        v = await checker.check(current_content="existing", unified_diff="")
        assert v.contradicts is False
        mock_client.messages.create.assert_not_called()


class TestCheckerErrors:
    async def test_non_retryable_error_fails_open(
        self, checker: ConsistencyChecker, mock_client: MagicMock
    ) -> None:
        mock_client.messages.create.side_effect = anthropic.BadRequestError(
            message="bad",
            response=MagicMock(status_code=400),
            body=None,
        )
        v = await checker.check(
            current_content="existing", unified_diff="+new"
        )
        assert v.contradicts is False
        assert "BadRequestError" in v.error

    async def test_server_error_retries_then_fails_open(
        self,
        checker: ConsistencyChecker,
        mock_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio

        async def _no_sleep(*_: object, **__: object) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        mock_client.messages.create.side_effect = anthropic.APIStatusError(
            message="upstream",
            response=MagicMock(status_code=503),
            body=None,
        )
        v = await checker.check(
            current_content="existing", unified_diff="+new"
        )
        # Consistency check fails OPEN — we'd rather let the operator
        # judge than block every draft when Anthropic has a blip.
        assert v.contradicts is False
        assert mock_client.messages.create.call_count == 3

    async def test_malformed_json_response_fails_open(
        self, checker: ConsistencyChecker, mock_client: MagicMock
    ) -> None:
        mock_client.messages.create.return_value = _mock_response(
            "I think this is fine, no contradiction here."
        )
        v = await checker.check(
            current_content="existing", unified_diff="+new"
        )
        assert v.contradicts is False
        assert "parse failed" in v.reasoning


