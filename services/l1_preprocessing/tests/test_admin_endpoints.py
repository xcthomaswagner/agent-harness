"""Tests for admin control-plane endpoints.

Covers ``POST /admin/re-redact`` — re-runs the secret redactor over every
trace entry in the store. Needed when pattern updates land so existing
traces benefit from the new coverage without a full re-consolidation.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from tracer import ARTIFACT_CODE_REVIEW, ARTIFACT_SESSION_LOG, append_trace

_SECRET = "sk-ant-api03-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    return logs


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_post_admin_re_redact_requires_auth(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """When ``settings.api_key`` is configured, the endpoint must 401 without
    the header. The local-dev default (no key configured) is exercised by
    the other tests in this module — this test specifically verifies the
    gate flips on when a key is set."""
    from main import settings as _settings
    with patch.object(_settings, "api_key", "secret-key-123"), \
         patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.post("/admin/re-redact")
    assert resp.status_code == 401


async def test_post_admin_re_redact_processes_all_traces(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Seed two traces (one with a secret, one without), hit the endpoint,
    verify both traces are walked and the secret gets redacted on disk.
    """
    with patch("tracer.LOGS_DIR", trace_dir):
        # Trace 1: contains a secret in a content field. Bypasses
        # consolidation's redaction because we go direct to append_trace.
        append_trace(
            "RR-1", "t0", "artifact", ARTIFACT_SESSION_LOG,
            content=f"[bootstrap] key={_SECRET}\n",
        )
        # Trace 2: clean content, nothing to redact.
        append_trace(
            "RR-2", "t0", "artifact", ARTIFACT_CODE_REVIEW,
            content="# Review\nLGTM\n",
        )

        resp = await client.post("/admin/re-redact")

    assert resp.status_code == 200
    body = resp.json()
    assert body["traces_processed"] == 2
    assert body["entries_redacted"] == 1, (
        "only the trace with a seeded secret should flip an entry"
    )
    assert body["additional_patterns_found"] >= 1, (
        "the redactor must find the seeded secret on this pass"
    )

    # Verify on-disk state: the secret is gone from RR-1's trace file.
    rr1_text = (trace_dir / "RR-1.jsonl").read_text()
    assert _SECRET not in rr1_text
    assert "sk-ant-[REDACTED]" in rr1_text
    # RR-2 is untouched apart from a re-serialization pass — still readable.
    rr2_text = (trace_dir / "RR-2.jsonl").read_text()
    assert "LGTM" in rr2_text


async def test_post_admin_re_redact_idempotent_when_patterns_unchanged(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """A second run over an already-redacted store must return
    ``additional_patterns_found: 0``. This is the canary for the redactor's
    idempotency guarantee — if it breaks, this test catches it."""
    with patch("tracer.LOGS_DIR", trace_dir):
        append_trace(
            "RR-3", "t0", "artifact", ARTIFACT_SESSION_LOG,
            content=f"token={_SECRET}\n",
        )
        first = await client.post("/admin/re-redact")
        assert first.status_code == 200
        assert first.json()["additional_patterns_found"] >= 1

        second = await client.post("/admin/re-redact")

    assert second.status_code == 200
    body = second.json()
    assert body["traces_processed"] == 1
    assert body["entries_redacted"] == 0
    assert body["additional_patterns_found"] == 0, (
        "redactor is supposed to be idempotent — second pass must be a no-op"
    )


async def test_post_admin_re_redact_covers_non_content_fields(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Fix 3 regression: the endpoint must clean the same field set that
    consolidation redacts on import — not just ``content``. Existing traces
    from before the consolidation fix will still have raw secrets in
    ``debug_payload`` / ``error`` / etc. and this endpoint must catch them.
    """
    with patch("tracer.LOGS_DIR", trace_dir):
        # Seed via append_trace directly so we bypass consolidation's
        # redaction path — simulates a legacy trace written before the fix.
        append_trace(
            "RR-4", "t0", "pipeline", "tool_failed",
            debug_payload=f"token={_SECRET}",
            error=f"auth failed with key {_SECRET}",
            stderr=f"leak on stderr {_SECRET}",
        )

        resp = await client.post("/admin/re-redact")

    assert resp.status_code == 200
    text = (trace_dir / "RR-4.jsonl").read_text()
    assert _SECRET not in text, (
        "re-redact endpoint must clean debug_payload/error/stderr, "
        "not just content"
    )
    assert text.count("[REDACTED]") >= 3


async def test_post_admin_re_redact_covers_tool_index_first_tool_error(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Fix 3 regression: the endpoint must also clean the nested
    ``index.first_tool_error.message`` field on tool_index entries.
    """
    from tracer import ARTIFACT_TOOL_INDEX

    with patch("tracer.LOGS_DIR", trace_dir):
        append_trace(
            "RR-5", "t0", "artifact", ARTIFACT_TOOL_INDEX,
            index={
                "tool_counts": {"Bash": 1},
                "tool_errors": {"Bash": 1},
                "mcp_servers_used": [],
                "mcp_servers_available": [],
                "mcp_servers_unused": [],
                "first_tool_error": {
                    "tool": "Bash",
                    "line": 7,
                    "message": f"sf: access token {_SECRET} has expired",
                },
                "assistant_turns": 1,
                "tool_call_count": 1,
            },
        )

        resp = await client.post("/admin/re-redact")

    assert resp.status_code == 200
    text = (trace_dir / "RR-5.jsonl").read_text()
    assert _SECRET not in text, (
        "re-redact endpoint must clean index.first_tool_error.message"
    )
    assert "[REDACTED]" in text


async def test_post_admin_re_redact_scrubs_corrupt_line(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Fix 4 regression: a corrupt/unparseable line with a live secret
    must be run through ``redact()`` before being written back. Previously
    the endpoint passed corrupt lines through verbatim, leaving the
    credential on disk.
    """
    trace_file = trace_dir / "RR-6.jsonl"
    # Simulate a partial crash-write: line is not valid JSON and contains
    # a live Anthropic key.
    trace_file.write_text(f"{{partial write trailing {_SECRET}\n")

    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.post("/admin/re-redact")

    assert resp.status_code == 200
    text = trace_file.read_text()
    assert _SECRET not in text, (
        "corrupt line must be redacted, not passed through verbatim"
    )
    assert "[REDACTED]" in text
