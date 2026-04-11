"""Tests for the trace bundle + individual artifact endpoints.

Covers ``GET /traces/<id>/bundle`` (gzipped tar of full trace context) and
``GET /traces/<id>/artifact/<type>`` (individual raw artifact download).
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from tracer import (
    ARTIFACT_CODE_REVIEW,
    ARTIFACT_EFFECTIVE_CLAUDE_MD,
    ARTIFACT_QA_MATRIX,
    ARTIFACT_SESSION_LOG,
    ARTIFACT_SESSION_STREAM,
    ARTIFACT_TOOL_INDEX,
    append_trace,
)


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


def _seed_trace(
    trace_dir: Path,
    ticket_id: str,
    *,
    stream_path: Path | None = None,
) -> None:
    """Write a full-featured fake trace with every artifact type."""
    with patch("tracer.LOGS_DIR", trace_dir):
        # Use real models.TicketPayload field names (`title`, not
        # `ticket_title`) so the synthetic ticket.json round-trips cleanly.
        append_trace(ticket_id, "t0", "webhook", "jira_webhook_received",
                     ticket_type="story", source="jira", title="Add widget")
        append_trace(ticket_id, "t0", "pipeline", "processing_started",
                     ticket_type="story", source="jira")
        append_trace(ticket_id, "t0", "artifact", ARTIFACT_CODE_REVIEW,
                     content="# Code Review\n\nLGTM.")
        append_trace(ticket_id, "t0", "artifact", ARTIFACT_QA_MATRIX,
                     content="# QA Matrix\n\nAll pass.")
        append_trace(ticket_id, "t0", "artifact", ARTIFACT_SESSION_LOG,
                     content="session narrative content here")
        append_trace(ticket_id, "t0", "artifact", ARTIFACT_EFFECTIVE_CLAUDE_MD,
                     content="# CLAUDE.md\nInstructions the agent saw.")
        append_trace(ticket_id, "t0", "artifact", ARTIFACT_TOOL_INDEX,
                     index={"tool_calls": [{"tool": "Read", "count": 3}]})
        if stream_path is not None:
            size = stream_path.stat().st_size if stream_path.exists() else 0
            append_trace(ticket_id, "t0", "artifact", ARTIFACT_SESSION_STREAM,
                         artifact_path=str(stream_path),
                         size_bytes=size,
                         line_count=2)


# --- /traces/<id>/bundle ---


async def test_bundle_404_when_trace_missing(
    trace_dir: Path, client: AsyncClient,
) -> None:
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/NOPE-999/bundle")
    assert resp.status_code == 404


async def test_bundle_returns_gzip_with_filename(
    trace_dir: Path, client: AsyncClient,
) -> None:
    _seed_trace(trace_dir, "BUN-1")
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/BUN-1/bundle")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert 'filename="trace-BUN-1-' in disposition
    assert disposition.endswith('.tar.gz"')


async def test_bundle_contents_have_expected_files(
    trace_dir: Path, client: AsyncClient,
) -> None:
    stream = trace_dir / "session-stream.jsonl"
    stream.write_text(
        '{"type": "assistant", "text": "hello"}\n'
        '{"type": "tool_use", "name": "Read"}\n'
    )
    _seed_trace(trace_dir, "BUN-2", stream_path=stream)

    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/BUN-2/bundle")
    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        names = set(tar.getnames())
        # Required files
        assert "pipeline.jsonl" in names
        assert "readme.txt" in names
        assert "ticket.json" in names
        # Seeded artifacts
        assert "code-review.md" in names
        assert "qa-matrix.md" in names
        assert "session.log" in names
        assert "effective-CLAUDE.md" in names
        assert "tool-index.json" in names
        assert "session-stream.jsonl" in names

        # ticket.json contains the ticket_id
        member = tar.extractfile("ticket.json")
        assert member is not None
        payload = json.loads(member.read())
        assert payload.get("id") == "BUN-2" or payload.get("ticket_id") == "BUN-2"

        # pipeline.jsonl has one line per trace entry
        pipeline = tar.extractfile("pipeline.jsonl")
        assert pipeline is not None
        lines = [ln for ln in pipeline.read().decode().splitlines() if ln.strip()]
        assert len(lines) >= 7
        for ln in lines:
            json.loads(ln)  # should all be valid JSON

        # session-stream was copied byte-for-byte
        stream_member = tar.extractfile("session-stream.jsonl")
        assert stream_member is not None
        assert stream_member.read() == stream.read_bytes()

        # tool-index.json serialized the index field
        ti = tar.extractfile("tool-index.json")
        assert ti is not None
        idx = json.loads(ti.read())
        assert "tool_calls" in idx


async def test_bundle_readme_contains_redaction_block(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """A clean trace (no secrets) produces a ``REDACTED (0 patterns)`` block.

    Replaces the old ``NOT REDACTED`` warning — commit 6 wires the redactor
    into the bundle pipeline so every bundle reports its redaction count.
    """
    _seed_trace(trace_dir, "BUN-3")
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/BUN-3/bundle")
    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        readme = tar.extractfile("readme.txt")
        assert readme is not None
        text = readme.read().decode()
    assert "REDACTED (0 patterns)" in text
    assert "NOT REDACTED" not in text, (
        "the old `NOT REDACTED` warning block must be gone now that the "
        "redactor runs on every bundle"
    )
    assert "BUN-3" in text


async def test_bundle_skips_missing_stream_file(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """When artifact_path points to a file that no longer exists, the bundle
    should still succeed — just without session-stream.jsonl."""
    missing = trace_dir / "does-not-exist.jsonl"
    _seed_trace(trace_dir, "BUN-4", stream_path=missing)
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/BUN-4/bundle")
    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "session-stream.jsonl" not in names
    assert "pipeline.jsonl" in names


async def test_bundle_rejects_path_traversal_in_ticket_id(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """A path-like ticket_id should be rejected / not resolve to a valid route."""
    with patch("tracer.LOGS_DIR", trace_dir):
        # Path traversal attempt — FastAPI treats slashes as path separators so
        # this simply doesn't match the route and returns 404.
        resp = await client.get("/traces/..%2F..%2Fetc%2Fpasswd/bundle")
        # Either 400 (explicit rejection) or 404 (no trace) is acceptable —
        # what matters is we never 200 on a malicious path.
        assert resp.status_code in (400, 404)

        # XSS / HTML injection in the path param — rejected by the validator.
        resp = await client.get("/traces/<script>alert(1)<%2Fscript>/bundle")
        assert resp.status_code in (400, 404)


# --- /traces/<id>/artifact/<type> ---


async def test_artifact_session_log_served_as_text(
    trace_dir: Path, client: AsyncClient,
) -> None:
    _seed_trace(trace_dir, "ART-1")
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/ART-1/artifact/session_log")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "session narrative content here" in resp.text


async def test_artifact_effective_claude_md_served_as_markdown(
    trace_dir: Path, client: AsyncClient,
) -> None:
    _seed_trace(trace_dir, "ART-2")
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/ART-2/artifact/effective_claude_md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "CLAUDE.md" in resp.text


async def test_artifact_session_stream_served_from_disk(
    trace_dir: Path, client: AsyncClient,
) -> None:
    stream = trace_dir / "session-stream.jsonl"
    stream.write_text('{"type":"assistant","text":"hi"}\n')
    _seed_trace(trace_dir, "ART-3", stream_path=stream)

    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/ART-3/artifact/session_stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.content == stream.read_bytes()


async def test_artifact_session_stream_404_when_file_missing(
    trace_dir: Path, client: AsyncClient,
) -> None:
    missing = trace_dir / "gone.jsonl"
    _seed_trace(trace_dir, "ART-4", stream_path=missing)
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/ART-4/artifact/session_stream")
    assert resp.status_code == 404


async def test_artifact_unknown_type_returns_404(
    trace_dir: Path, client: AsyncClient,
) -> None:
    _seed_trace(trace_dir, "ART-5")
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/ART-5/artifact/bogus_type")
    assert resp.status_code == 404


async def test_artifact_404_when_trace_missing(
    trace_dir: Path, client: AsyncClient,
) -> None:
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/NOPE/artifact/session_log")
    assert resp.status_code == 404


# --- Regression: diagnostic.json must be computed inline and always present ---


async def test_bundle_diagnostic_json_always_present_and_has_six_checks(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Regression for the reviewer's Critical Finding #1:

    commit 3's ``diagnostic.py`` is a pure analyzer — it never writes a
    ``diagnostic_checklist`` trace artifact. The bundle must compute the
    checklist inline at bundle-generation time so downstream consumers
    (this readme, post-mortem-analyst skill) can rely on diagnostic.json
    existing.
    """
    _seed_trace(trace_dir, "DIAG-1")
    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/DIAG-1/bundle")
    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        names = set(tar.getnames())
        assert "diagnostic.json" in names, (
            "diagnostic.json must always be present — commit 3 is a pure "
            "analyzer, so the bundle must compute it inline"
        )
        diag_member = tar.extractfile("diagnostic.json")
        assert diag_member is not None
        checks = json.loads(diag_member.read())

    # Shape: list of six checks, each with id/status/evidence/label.
    assert isinstance(checks, list)
    assert len(checks) == 6, f"expected 6 checks, got {len(checks)}"
    expected_ids = {
        "platform_detected",
        "skill_invoked",
        "mcp_preferred",
        "first_deviation",
        "scratch_org",
        "review_qa_verdict",
    }
    got_ids = {c.get("id") for c in checks}
    assert got_ids == expected_ids, (
        f"check ids drifted from commit 3's canonical set: "
        f"missing={expected_ids - got_ids}, extra={got_ids - expected_ids}"
    )
    for c in checks:
        assert "status" in c
        assert c["status"] in {"red", "yellow", "green"}
        assert "evidence" in c
        assert "label" in c


# --- Regression: ticket.json fallback uses real TicketPayload field names ---


async def test_bundle_ticket_json_fallback_uses_real_field_names(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """Regression for the reviewer's Critical Finding #2:

    When ``.harness/ticket.json`` does not exist on disk, the synthetic
    payload must use the field names from ``models.TicketPayload`` —
    specifically ``title`` (not ``ticket_title``). The previous build
    used ``ticket_title`` and the post-mortem-analyst skill silently
    failed to find the title.
    """
    ticket_id = "FALLBACK-1"
    # Seed a trace where NO .harness/ticket.json exists on disk. The
    # webhook_received entry embeds the payload fields under the real
    # TicketPayload keys so the synthesizer has something to scavenge.
    with patch("tracer.LOGS_DIR", trace_dir):
        append_trace(
            ticket_id, "t0", "webhook", "jira_webhook_received",
            source="jira",
            title="Add the widget to the header",
            ticket_type="story",
            description="As a user I want a widget so that I can widget.",
            priority="High",
        )
        append_trace(
            ticket_id, "t0", "pipeline", "processing_started",
            ticket_type="story", source="jira",
        )

        # settings.default_client_repo may point at a real path; point it at
        # an isolated tmp location that has no worktrees/.harness subdir so
        # the real-file branch misses and we exercise the fallback path.
        from main import settings as _settings
        with patch.object(
            _settings, "default_client_repo", str(trace_dir / "nowhere"),
        ):
            resp = await client.get(f"/traces/{ticket_id}/bundle")

    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        member = tar.extractfile("ticket.json")
        assert member is not None
        payload = json.loads(member.read())

    # Must use real TicketPayload field names, NOT the old broken ones.
    assert payload.get("id") == ticket_id
    assert payload.get("title") == "Add the widget to the header", (
        "fallback must use `title` (TicketPayload field name), not "
        "`ticket_title`"
    )
    assert payload.get("ticket_type") == "story"
    assert payload.get("source") == "jira"
    assert payload.get("description") == (
        "As a user I want a widget so that I can widget."
    )
    # Negative assertion — the old broken field name must not appear.
    assert "ticket_title" not in payload, (
        "`ticket_title` is not a TicketPayload field — remove it"
    )


# --- Commit 6: redact-on-bundle-export ---


_SECRET = "sk-ant-api03-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


async def test_bundle_redacts_pipeline_jsonl(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """A pipeline event containing a secret must be redacted in the bundle's
    pipeline.jsonl. Covers the case where a secret lives in a non-content
    field the consolidation redactor doesn't touch — the bundle pass catches
    it because pipeline.jsonl is serialized as text and passed through
    redact() before being tarred.
    """
    ticket_id = "REDBUN-1"
    with patch("tracer.LOGS_DIR", trace_dir):
        append_trace(
            ticket_id, "t0", "pipeline", "processing_started",
            ticket_type="story", source="jira",
            # Secret stashed in a non-content field. Consolidation redaction
            # targets `content` fields; the bundle pass is what catches this.
            debug_payload=f"token={_SECRET}",
        )
        resp = await client.get(f"/traces/{ticket_id}/bundle")
    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        pipeline = tar.extractfile("pipeline.jsonl")
        assert pipeline is not None
        body = pipeline.read().decode()

    assert _SECRET not in body
    assert "sk-ant-[REDACTED]" in body


async def test_bundle_redacts_session_stream(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """The on-disk session-stream file stays raw, but the bundle copy must
    be redacted — this is the only place the stream actually gets redacted.
    """
    stream = trace_dir / "session-stream.jsonl"
    stream.write_text(
        json.dumps({"type": "system", "subtype": "init", "mcp_servers": []})
        + "\n"
        + json.dumps({"type": "assistant", "text": f"key={_SECRET}"})
        + "\n",
    )
    _seed_trace(trace_dir, "REDBUN-2", stream_path=stream)

    with patch("tracer.LOGS_DIR", trace_dir):
        resp = await client.get("/traces/REDBUN-2/bundle")
    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        stream_member = tar.extractfile("session-stream.jsonl")
        assert stream_member is not None
        body = stream_member.read().decode()

    assert _SECRET not in body, "bundled stream must be redacted"
    assert "sk-ant-[REDACTED]" in body
    # And the on-disk file is still the raw version — this is the forensic
    # escape hatch the bundle deliberately preserves.
    assert _SECRET in stream.read_text(), (
        "on-disk stream must remain raw for local forensic access"
    )


async def test_bundle_readme_reports_redaction_count(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """When the bundle redacts ≥1 pattern, the readme must say so with a count.

    We seed several secrets across multiple files so the counter increments
    beyond 1 and we can assert the exact number in the readme text.
    """
    ticket_id = "REDBUN-3"
    with patch("tracer.LOGS_DIR", trace_dir):
        append_trace(ticket_id, "t0", "artifact", ARTIFACT_CODE_REVIEW,
                     content=f"# Review\nfound {_SECRET}\n")
        append_trace(ticket_id, "t0", "artifact", ARTIFACT_QA_MATRIX,
                     content=f"# QA\nused {_SECRET}\n")
        resp = await client.get(f"/traces/{ticket_id}/bundle")
    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        readme = tar.extractfile("readme.txt")
        assert readme is not None
        text = readme.read().decode()

    # Both content fields were already redacted at `append_trace` time? No —
    # append_trace does not redact; only consolidate_worktree_logs does. So
    # the trace store entries contain raw secrets, and the bundle pass is
    # what catches them. Count should be ≥ 2 (one per seeded content field).
    # The exact count also includes the pipeline.jsonl serialization pass.
    assert "patterns were redacted" not in text, (
        "the new readme uses the 'REDACTED (N patterns)' shape, not the "
        "'N patterns were redacted' shape — update the assertion if you "
        "change the template"
    )
    # The REDACTED block includes '(N patterns)'. Extract the number.
    import re as _re
    match = _re.search(r"REDACTED \((\d+) patterns\)", text)
    assert match is not None, f"no REDACTED block in readme: {text[:200]}"
    count = int(match.group(1))
    assert count >= 2, (
        f"expected ≥2 patterns redacted across two seeded secrets, got {count}"
    )


async def test_bundle_readme_warning_flips_from_not_redacted_to_redacted(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """When the bundle contains secrets, the readme must use the
    'REDACTED (N patterns)' block, not the zero-count block. This guards
    against the template regressing back to a static warning that doesn't
    reflect what actually happened to the bundle.
    """
    ticket_id = "REDBUN-4"
    with patch("tracer.LOGS_DIR", trace_dir):
        append_trace(ticket_id, "t0", "artifact", ARTIFACT_CODE_REVIEW,
                     content=f"# Review\n{_SECRET}\n")
        resp = await client.get(f"/traces/{ticket_id}/bundle")
    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        readme = tar.extractfile("readme.txt")
        assert readme is not None
        text = readme.read().decode()

    # Positive: the non-zero block mentions "idempotent" and the count.
    assert "idempotent" in text, (
        "the non-zero redaction block must mention the idempotency guarantee"
    )
    # Negative: the zero-count block's distinguishing phrase must be absent.
    assert "flagged no token patterns" not in text, (
        "with seeded secrets the readme must NOT use the clean-bundle block"
    )
    # And the count in parentheses must be > 0.
    import re as _re
    match = _re.search(r"REDACTED \((\d+) patterns\)", text)
    assert match is not None
    assert int(match.group(1)) > 0


async def test_bundle_session_log_already_redacted_idempotent(
    trace_dir: Path, client: AsyncClient,
) -> None:
    """If the trace store's session_log content is already redacted (as
    consolidation leaves it), the bundle's second redaction pass is a no-op:
    the extracted bundled file equals the stored content byte-for-byte.
    """
    ticket_id = "REDBUN-5"
    # Pre-redacted content — simulates what consolidation leaves behind.
    already_clean = "[bootstrap] loading secret sk-ant-[REDACTED]\n[run] done\n"

    with patch("tracer.LOGS_DIR", trace_dir):
        append_trace(ticket_id, "t0", "artifact", ARTIFACT_SESSION_LOG,
                     content=already_clean)
        resp = await client.get(f"/traces/{ticket_id}/bundle")
    assert resp.status_code == 200

    buf = io.BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        member = tar.extractfile("session.log")
        assert member is not None
        body = member.read().decode()

    assert body == already_clean, (
        "redactor is supposed to be idempotent — a second pass on already-"
        "clean content must return it byte-for-byte"
    )
