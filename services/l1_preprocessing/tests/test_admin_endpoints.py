"""Tests for admin control-plane endpoints.

Covers ``POST /admin/re-redact`` — re-runs the secret redactor over every
trace entry in the store. Needed when pattern updates land so existing
traces benefit from the new coverage without a full re-consolidation.

Also covers ``POST /traces/<id>/discuss`` — mints a short-lived
investigation handoff (session token + bundle URL + copy-paste shell
snippet) and writes a line to ``discuss-audit.jsonl``.
"""

from __future__ import annotations

import json
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


# --- POST /traces/<id>/discuss -------------------------------------------
#
# Tier 1 post-mortem investigation handoff. Mints an opaque session token,
# returns the bundle URL and copy-paste shell snippet, and writes one line
# to discuss-audit.jsonl. Token is write-only by design — see endpoint
# docstring for the security model.


def _seed_minimal_trace(ticket_id: str) -> None:
    """Seed the smallest trace that makes ``read_trace`` return non-empty."""
    append_trace(ticket_id, "t0", "webhook", "jira_webhook_received",
                 ticket_type="story", source="jira", title=f"{ticket_id} title")


async def test_discuss_requires_api_key(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """With ``settings.api_key`` configured, discuss must 401 without the
    header. Mirrors the re-redact auth test — same dependency, same gate."""
    from main import settings as _settings
    with patch.object(_settings, "api_key", "secret-key-123"), \
         patch("tracer.LOGS_DIR", trace_dir):
        _seed_minimal_trace("DISC-AUTH")
        resp = await client.post("/traces/DISC-AUTH/discuss")
    assert resp.status_code == 401


async def test_discuss_returns_bundle_url_and_token(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Happy path: valid ticket returns all five fields with sensible shape."""
    with patch("tracer.LOGS_DIR", trace_dir):
        _seed_minimal_trace("DISC-1")
        resp = await client.post("/traces/DISC-1/discuss")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "ticket_id", "session_token", "bundle_url",
        "investigate_command", "expires_at",
    }
    assert body["ticket_id"] == "DISC-1"
    assert body["session_token"]  # non-empty opaque string
    assert len(body["session_token"]) >= 24
    assert body["bundle_url"].endswith("/traces/DISC-1/bundle")
    # investigate_command should mention both the download and the claude launch
    assert "curl" in body["investigate_command"]
    assert "DISC-1" in body["investigate_command"]
    assert "claude -p" in body["investigate_command"]
    # ISO-8601 timestamp, parseable
    from datetime import datetime
    datetime.fromisoformat(body["expires_at"])  # raises if malformed


async def test_discuss_token_is_random(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Two calls against the same ticket must mint distinct tokens —
    otherwise the audit log can't distinguish separate investigations."""
    with patch("tracer.LOGS_DIR", trace_dir):
        _seed_minimal_trace("DISC-2")
        r1 = await client.post("/traces/DISC-2/discuss")
        r2 = await client.post("/traces/DISC-2/discuss")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["session_token"] != r2.json()["session_token"]


async def test_discuss_writes_audit_log_line(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """A successful call appends exactly one line to discuss-audit.jsonl
    containing every required field."""
    with patch("tracer.LOGS_DIR", trace_dir):
        _seed_minimal_trace("DISC-3")
        resp = await client.post("/traces/DISC-3/discuss")

    assert resp.status_code == 200
    audit_file = trace_dir / "discuss-audit.jsonl"
    assert audit_file.exists()
    lines = audit_file.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["ticket_id"] == "DISC-3"
    assert entry["session_token"] == resp.json()["session_token"]
    assert "timestamp" in entry
    assert "source_ip" in entry
    assert "user_agent" in entry


async def test_discuss_audit_log_appends_not_overwrites(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Two calls must produce two distinct lines in the audit file — the
    audit log is append-only by contract."""
    with patch("tracer.LOGS_DIR", trace_dir):
        _seed_minimal_trace("DISC-4")
        await client.post("/traces/DISC-4/discuss")
        await client.post("/traces/DISC-4/discuss")

    audit_file = trace_dir / "discuss-audit.jsonl"
    lines = audit_file.read_text().splitlines()
    assert len(lines) == 2
    # Distinct tokens across the two lines — another sanity check that
    # each call mints a fresh token.
    t1 = json.loads(lines[0])["session_token"]
    t2 = json.loads(lines[1])["session_token"]
    assert t1 != t2


async def test_discuss_returns_404_for_unknown_trace(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """No trace for this ticket_id → 404, matching the bundle endpoint."""
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.post("/traces/NOPE-999/discuss")
    assert resp.status_code == 404
    # And the audit log must not be created for rejected requests — the
    # audit line represents an actual minted session, not an attempted one.
    assert not (trace_dir / "discuss-audit.jsonl").exists()


async def test_discuss_audit_captures_source_ip_and_user_agent(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Spoofed ``user-agent`` header must land verbatim in the audit line.
    ``source_ip`` comes from ``request.client.host`` — ASGITransport fills
    this with the test client's host string (non-empty, non-None)."""
    with patch("tracer.LOGS_DIR", trace_dir):
        _seed_minimal_trace("DISC-5")
        resp = await client.post(
            "/traces/DISC-5/discuss",
            headers={"User-Agent": "post-mortem-cli/1.2.3"},
        )

    assert resp.status_code == 200
    entry = json.loads(
        (trace_dir / "discuss-audit.jsonl").read_text().splitlines()[0]
    )
    assert entry["user_agent"] == "post-mortem-cli/1.2.3"
    # ASGITransport reports a client host — just assert it's a string and
    # was captured (any non-None value from request.client.host is fine).
    assert isinstance(entry["source_ip"], str)
