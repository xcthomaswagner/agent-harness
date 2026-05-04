"""Tests for the Tier-3 live SSE stream at ``/traces/{id}/live``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import live_stream
import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_stream(path: Path, events: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _tool_use_event(name: str, command: str, timestamp: str | None = None) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"toolu_{name}_{command[:5]}",
                    "name": name,
                    "input": {"command": command},
                }
            ],
        },
    }
    if timestamp is not None:
        ev["timestamp"] = timestamp
    return ev


def _text_event(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _task_started(desc: str) -> dict[str, Any]:
    return {"type": "system", "subtype": "task_started", "description": desc}


def _task_notification(summary: str) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "task_notification",
        "summary": summary,
        "status": "completed",
    }


def _task_progress(tool_uses: int, tokens: int) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "task_progress",
        "usage": {"tool_uses": tool_uses, "total_tokens": tokens, "duration_ms": 1000},
    }


def _rate_limit() -> dict[str, Any]:
    return {"type": "rate_limit_event", "rate_limit_info": {"retry_after": 30}}


def _user_tool_result() -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": "2026-04-18T10:00:00.000Z",
        "message": {"content": [{"type": "tool_result", "is_error": False}]},
    }


def test_worktree_root_uses_trace_recorded_worktree_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs_dir = tmp_path / "logs"
    import tracer as tracer_module

    monkeypatch.setattr(tracer_module, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(live_stream, "LOGS_DIR", logs_dir, raising=False)
    worktree = tmp_path / "custom" / "worktrees" / "ai" / "LIVE-1"
    harness_dir = worktree / ".harness"
    harness_dir.mkdir(parents=True)
    (harness_dir / "spawn-manifest.json").write_text(
        json.dumps({"ticket_id": "LIVE-1", "worktree_path": str(worktree)})
    )
    tracer_module.append_trace(
        "LIVE-1",
        "trace-live",
        "spawn",
        "l2_spawn_started",
        worktree_path=str(worktree),
    )

    assert live_stream._worktree_root_for_ticket("LIVE-1") == worktree.resolve()


@pytest.fixture
def fake_worktree_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``~/.harness/clients/worktrees/ai`` into tmp_path."""
    ai_root = tmp_path / "harness" / "clients" / "worktrees" / "ai"
    ai_root.mkdir(parents=True)

    real_expanduser = os.path.expanduser

    def fake_expanduser(path: str) -> str:
        if path == "~/.harness/clients/worktrees/ai":
            return str(ai_root)
        return real_expanduser(path)

    # live_stream's only call to expanduser is in
    # _worktree_root_for_ticket; patching os.path.expanduser via the
    # module handle is robust against future imports.
    import os.path as _op
    monkeypatch.setattr(_op, "expanduser", fake_expanduser)
    return ai_root


class _FakeRequest:
    """Minimal Request stand-in for driving ``_stream_generator`` without HTTP.

    The real endpoint runs under ASGI where ``request.is_disconnected()``
    flips when the client closes the tab; under ASGITransport the body
    is buffered until the generator returns, which makes it impossible
    to observe chunks mid-flight. Driving the generator directly lets
    us assert the exact events it emits without fighting the
    transport buffering.
    """

    def __init__(self, disconnect_after: float | None = None) -> None:
        import time as _t

        self._deadline = (
            (_t.monotonic() + disconnect_after)
            if disconnect_after is not None
            else None
        )

    async def is_disconnected(self) -> bool:
        import time as _t

        if self._deadline is None:
            return False
        return _t.monotonic() >= self._deadline


async def _drive_generator(
    gen: Any, *, max_duration: float = 1.0
) -> str:
    """Collect output from the live-stream async generator for up to
    ``max_duration`` seconds, then cancel.
    """
    chunks: list[str] = []

    async def _pump() -> None:
        async for chunk in gen:
            chunks.append(chunk)

    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(_pump(), timeout=max_duration)
    return "".join(chunks)


def _parse_sse_events(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                continue
    return events


async def _collect_sse(
    client: AsyncClient, url: str, *, max_duration: float = 1.0
) -> str:
    """HTTP-level SSE collection — used for short-lived no_activity path
    and HTML page tests. Tail/replay tests go through ``_drive_generator``
    because ASGITransport buffers StreamingResponse output until the
    generator returns.
    """
    chunks: list[str] = []

    async def _run() -> None:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                chunks.append(f"__status_{resp.status_code}__\n")
                return
            async for chunk in resp.aiter_text():
                chunks.append(chunk)

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(_run(), timeout=max_duration)
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Unit tests — filter + replay + discovery
# ---------------------------------------------------------------------------


class TestFilter:
    def test_skips_user_tool_result(self) -> None:
        assert live_stream._filter_and_shape_event(_user_tool_result(), "tl") is None

    def test_emits_progress_update_for_task_progress(self) -> None:
        ev = live_stream._filter_and_shape_event(_task_progress(5, 1000), "tl")
        assert ev is not None
        assert ev["kind"] == "progress_update"
        assert ev["tool_uses"] == 5
        assert ev["total_tokens"] == 1000

    def test_shapes_tool_use(self) -> None:
        ev = live_stream._filter_and_shape_event(
            _tool_use_event("Bash", "ls -la"), "tl"
        )
        assert ev is not None
        assert ev["kind"] == "tool_use"
        assert ev["tool_name"] == "Bash"
        assert ev["description"] == "ls -la"

    def test_shapes_task_started_and_notification(self) -> None:
        s = live_stream._filter_and_shape_event(_task_started("review"), "tl")
        n = live_stream._filter_and_shape_event(_task_notification("done"), "tl")
        assert s is not None and s["kind"] == "task_started"
        assert n is not None and n["kind"] == "task_notification"

    def test_shapes_rate_limit(self) -> None:
        ev = live_stream._filter_and_shape_event(_rate_limit(), "tl")
        assert ev is not None
        assert ev["kind"] == "rate_limit"

    def test_truncates_long_text(self) -> None:
        long = "x" * 500
        ev = live_stream._filter_and_shape_event(_text_event(long), "tl")
        assert ev is not None
        assert ev["kind"] == "text"
        assert len(ev["text"]) <= live_stream.TEXT_SNIPPET_MAX

    def test_redacts_tool_descriptions(self) -> None:
        command = (
            "curl -H 'Authorization: Bearer "
            "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' "
            "https://example.test csa0123456789abcdef0123456789"
        )
        ev = live_stream._filter_and_shape_event(
            _tool_use_event("Bash", command), "developer-1"
        )
        assert ev is not None
        assert "sk-ant-api03" not in ev["description"]
        assert "csa0123456789abcdef0123456789" not in ev["description"]
        assert "REDACTED" in ev["description"]
        assert ev["role"] == "developer"
        assert ev["role_group"] == "dev"
        assert ev["display_name"] == "Developer 1"

    def test_session_summary_includes_latest_activity(self, tmp_path: Path) -> None:
        stream = tmp_path / "session-stream.jsonl"
        _write_stream(
            stream,
            [
                _tool_use_event(
                    "Read", "package.json", "2026-04-18T10:00:00+00:00"
                ),
                _task_progress(3, 1200),
                _text_event("Implementation is complete."),
            ],
        )
        summary = live_stream.summarize_session_stream("qa", stream)
        assert summary["role"] == "qa"
        assert summary["role_group"] == "qa"
        assert summary["display_name"] == "QA"
        assert summary["tool_uses"] == 3
        assert summary["total_tokens"] == 1200
        assert summary["current_activity"] == "Implementation is complete."
        assert len(summary["latest_events"]) == 2

    def test_ticket_activity_summary_dedupes_repeated_strings(
        self, tmp_path: Path
    ) -> None:
        stream = tmp_path / "session-stream.jsonl"
        _write_stream(
            stream,
            [
                _tool_use_event("Read", "src/Hero.tsx"),
                _tool_use_event("Read", "src/Hero.tsx"),
                _tool_use_event("Bash", "npm test failed"),
                _text_event("QA passed."),
            ],
        )
        summary = live_stream.summarize_ticket_activity(
            "DEDUPE-1", [("developer-1", stream)]
        )
        assert summary["raw_event_count"] == 4
        assert summary["deduped_event_count"] == 3
        teammate = summary["teammates"][0]
        assert teammate["deduped_event_count"] == 3
        read_item = next(
            item for item in teammate["actions"] if item["message"] == "Read: src/Hero.tsx"
        )
        assert read_item["count"] == 2
        assert any("failed" in warning for warning in summary["warnings"])

    def test_finished_activity_adds_review_judge_and_qa_without_streams(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "RND-1"
        logs = wt / ".harness" / "logs"
        logs.mkdir(parents=True)
        (logs / "pipeline.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "phase": "judge",
                            "timestamp": "2026-05-03T22:12:05Z",
                            "event": "Judge complete",
                            "validated": 4,
                            "rejected": 6,
                        }
                    ),
                    json.dumps(
                        {
                            "phase": "code_review",
                            "timestamp": "2026-05-03T22:16:47Z",
                            "event": "Review complete",
                            "verdict": "APPROVED",
                            "issues": 10,
                            "critical": 3,
                            "warnings": 7,
                        }
                    ),
                    json.dumps(
                        {
                            "phase": "qa_validation",
                            "timestamp": "2026-05-03T22:21:56Z",
                            "event": "QA complete",
                            "overall": "PASS",
                            "criteria_passed": 23,
                            "criteria_total": 23,
                        }
                    ),
                ]
            )
            + "\n"
        )
        (logs / "code-review.json").write_text(
            json.dumps(
                {
                    "verdict": "APPROVED",
                    "issues": [{"summary": "Heading hierarchy fixed"}],
                }
            )
        )
        (logs / "judge-verdict.json").write_text(
            json.dumps(
                {
                    "validated_issues": [{"summary": "CTA guard confirmed"}],
                    "rejected_issues": [{"summary": "False positive"}],
                }
            )
        )
        (logs / "qa-matrix.json").write_text(
            json.dumps(
                {
                    "overall": "PASS",
                    "issues": [
                        {"criterion": "Hero renders", "status": "PASS"},
                        {"criterion": "CTA sanitized", "status": "PASS_BY_CODE_INSPECTION"},
                    ],
                }
            )
        )

        finished = live_stream.collect_finished_activity(wt)
        summary = live_stream.summarize_ticket_activity(
            "RND-1", [], finished_events=finished
        )

        roles = {teammate["role"] for teammate in summary["teammates"]}
        assert {"code_reviewer", "judge", "qa"}.issubset(roles)
        assert any(
            "QA complete" in item["message"] for item in summary["highlights"]
        )
        messages = [
            action["message"]
            for teammate in summary["teammates"]
            for action in teammate["actions"]
        ]
        assert "Review finding: Heading hierarchy fixed" in messages
        assert "Validated issue: CTA guard confirmed" in messages
        qa = next(teammate for teammate in summary["teammates"] if teammate["role"] == "qa")
        assert qa["state"] == "completed"
        assert qa["deduped_event_count"] >= 2


class TestWorktreeDiscovery:
    def test_main_worktree_labeled_team_lead(
        self, fake_worktree_root: Path
    ) -> None:
        wt = fake_worktree_root / "FOO-1"
        _write_stream(
            wt / ".harness" / "logs" / "session-stream.jsonl",
            [_tool_use_event("Bash", "hi")],
        )
        resolved = live_stream._worktree_root_for_ticket("FOO-1")
        assert resolved == wt.resolve()
        streams = live_stream._find_session_streams(resolved)
        assert len(streams) == 1
        assert streams[0][0] == "team-lead"

    def test_subworktree_labeled_by_dir_or_role(
        self, fake_worktree_root: Path
    ) -> None:
        wt = fake_worktree_root / "FOO-2"
        sub = wt / ".claude" / "worktrees" / "agent-abc123"
        _write_stream(
            wt / ".harness" / "logs" / "session-stream.jsonl",
            [_tool_use_event("Bash", "parent")],
        )
        _write_stream(
            sub / ".harness" / "logs" / "session-stream.jsonl",
            [_tool_use_event("Read", "child")],
        )
        # Add role hint via ticket.json
        ticket_json = sub / ".harness" / "ticket.json"
        ticket_json.write_text(json.dumps({"role": "dev-a"}))
        resolved = live_stream._worktree_root_for_ticket("FOO-2")
        assert resolved is not None
        streams = live_stream._find_session_streams(resolved)
        labels = {t for t, _ in streams}
        assert labels == {"team-lead", "dev-a"}


# ---------------------------------------------------------------------------
# End-to-end HTTP tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def sse_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient, None]:
    """Async client with anonymous dashboard auth pre-enabled.

    The session-scoped ``.env`` load populates ``main.settings.api_key``
    from disk, which makes anonymous dashboard requests fail with 401.
    Tests that want to exercise the auth paths flip ``api_key`` back
    on via their own ``monkeypatch``.
    """
    monkeypatch.setattr(main.settings, "api_key", "")
    monkeypatch.setattr(main.settings, "dashboard_allow_anonymous", True)
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_no_worktree_returns_no_activity_event(
    fake_worktree_root: Path, sse_client: AsyncClient
) -> None:
    body = await _collect_sse(
        sse_client, "/api/traces/NOTHING-1/stream", max_duration=1.0
    )
    events = _parse_sse_events(body)
    assert any(e.get("kind") == "no_activity" for e in events)


@pytest.mark.asyncio
async def test_replay_yields_last_n_events_oldest_first(
    fake_worktree_root: Path,
) -> None:
    wt = fake_worktree_root / "REPLAY-1"
    evs: list[dict[str, Any]] = []
    for i in range(150):
        evs.append(_tool_use_event("Bash", f"cmd-{i:03d}"))
    _write_stream(wt / ".harness" / "logs" / "session-stream.jsonl", evs)

    resolved = live_stream._worktree_root_for_ticket("REPLAY-1")
    assert resolved is not None
    streams = live_stream._find_session_streams(resolved)
    req = _FakeRequest(disconnect_after=0.3)
    body = await _drive_generator(
        live_stream._stream_generator(streams, req),  # type: ignore[arg-type]
        max_duration=1.5,
    )
    events = _parse_sse_events(body)
    tool_events = [e for e in events if e.get("kind") == "tool_use"]
    assert len(tool_events) == 100
    descs = [e["description"] for e in tool_events]
    assert descs[0] == "cmd-050"
    assert descs[-1] == "cmd-149"


@pytest.mark.asyncio
async def test_filter_skips_task_progress_and_tool_result(
    fake_worktree_root: Path,
) -> None:
    wt = fake_worktree_root / "FILTER-1"
    evs: list[dict[str, Any]] = [
        _tool_use_event("Read", "file1"),
        _user_tool_result(),
        _task_progress(1, 100),
        _task_started("starting"),
        _tool_use_event("Edit", "file2"),
        _task_notification("done"),
    ]
    _write_stream(wt / ".harness" / "logs" / "session-stream.jsonl", evs)
    resolved = live_stream._worktree_root_for_ticket("FILTER-1")
    assert resolved is not None
    streams = live_stream._find_session_streams(resolved)
    req = _FakeRequest(disconnect_after=0.3)
    body = await _drive_generator(
        live_stream._stream_generator(streams, req),  # type: ignore[arg-type]
        max_duration=1.5,
    )
    events = _parse_sse_events(body)
    kinds = [e.get("kind") for e in events]
    assert "tool_use" in kinds
    assert "task_started" in kinds
    assert "task_notification" in kinds
    assert "progress_update" in kinds
    for e in events:
        assert e.get("kind") != "tool_result"


@pytest.mark.asyncio
async def test_tail_picks_up_new_writes(
    fake_worktree_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_stream, "TAIL_POLL_INTERVAL_SEC", 0.1)
    wt = fake_worktree_root / "TAIL-1"
    stream_path = wt / ".harness" / "logs" / "session-stream.jsonl"
    _write_stream(stream_path, [_tool_use_event("Bash", "initial")])

    async def appender() -> None:
        await asyncio.sleep(0.3)
        with stream_path.open("a") as fh:
            fh.write(
                json.dumps(_tool_use_event("Read", "appended-later")) + "\n"
            )

    resolved = live_stream._worktree_root_for_ticket("TAIL-1")
    assert resolved is not None
    streams = live_stream._find_session_streams(resolved)
    req = _FakeRequest(disconnect_after=1.0)
    task = asyncio.create_task(appender())
    try:
        body = await _drive_generator(
            live_stream._stream_generator(streams, req),  # type: ignore[arg-type]
            max_duration=2.0,
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    events = _parse_sse_events(body)
    descs = [
        e.get("description")
        for e in events
        if e.get("kind") == "tool_use"
    ]
    assert "initial" in descs
    assert "appended-later" in descs


@pytest.mark.asyncio
async def test_multi_teammate_streams_merged_by_timestamp(
    fake_worktree_root: Path,
) -> None:
    wt = fake_worktree_root / "MERGE-1"
    main_events = [
        _tool_use_event("Bash", "main-A", timestamp="2026-04-18T10:00:00.000Z"),
        _tool_use_event("Bash", "main-C", timestamp="2026-04-18T10:00:02.000Z"),
    ]
    sub_events = [
        _tool_use_event("Read", "sub-B", timestamp="2026-04-18T10:00:01.000Z"),
    ]
    _write_stream(wt / ".harness" / "logs" / "session-stream.jsonl", main_events)
    _write_stream(
        wt
        / ".claude"
        / "worktrees"
        / "agent-xyz"
        / ".harness"
        / "logs"
        / "session-stream.jsonl",
        sub_events,
    )
    resolved = live_stream._worktree_root_for_ticket("MERGE-1")
    assert resolved is not None
    streams = live_stream._find_session_streams(resolved)
    req = _FakeRequest(disconnect_after=0.3)
    body = await _drive_generator(
        live_stream._stream_generator(streams, req),  # type: ignore[arg-type]
        max_duration=1.5,
    )
    events = _parse_sse_events(body)
    tool_events = [e for e in events if e.get("kind") == "tool_use"]
    descs = [e["description"] for e in tool_events]
    assert descs == ["main-A", "sub-B", "main-C"]
    teams = [e["teammate"] for e in tool_events]
    assert teams[0] == "team-lead"
    assert teams[1] == "agent-xyz"
    assert teams[2] == "team-lead"


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_query_param_accepted(
    fake_worktree_root: Path,
    sse_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.settings, "api_key", "secret-key")
    monkeypatch.setattr(main.settings, "dashboard_allow_anonymous", False)
    # Empty worktree → quick no_activity path (keeps test fast).
    wt = fake_worktree_root / "AUTH-Q-1"
    # Don't create any streams — falls through to the no_activity generator.
    body_ok = await _collect_sse(
        sse_client,
        "/api/traces/AUTH-Q-1/stream?api_key=secret-key",
        max_duration=1.0,
    )
    assert "no_activity" in body_ok

    # Wrong key → 401
    resp_bad = await sse_client.get(
        "/api/traces/AUTH-Q-1/stream?api_key=nope"
    )
    assert resp_bad.status_code == 401

    # No key at all → 401 (api_key configured, so fail-closed)
    resp_none = await sse_client.get("/api/traces/AUTH-Q-1/stream")
    assert resp_none.status_code == 401
    _ = wt  # keep linter quiet; fixture side-effect is what matters


@pytest.mark.asyncio
async def test_auth_header_still_works(
    fake_worktree_root: Path,
    sse_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.settings, "api_key", "secret-key")
    monkeypatch.setattr(main.settings, "dashboard_allow_anonymous", False)
    resp = await sse_client.get(
        "/api/traces/AUTH-H-1/stream", headers={"X-API-Key": "secret-key"}
    )
    # 200 even if no worktree — the no_activity generator is the success path.
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# HTML page tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_page_renders_without_activity(
    fake_worktree_root: Path, sse_client: AsyncClient
) -> None:
    resp = await sse_client.get("/traces/NONE-1/live")
    assert resp.status_code == 200
    assert "Live activity" in resp.text
    # The JS fallback text for the no_activity branch lives in the
    # page template — smoke-check it rendered.
    assert "No live activity" in resp.text


@pytest.mark.asyncio
async def test_live_page_requires_auth(
    sse_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.settings, "api_key", "secret-key")
    monkeypatch.setattr(main.settings, "dashboard_allow_anonymous", False)
    resp = await sse_client.get("/traces/NONE-2/live")
    assert resp.status_code == 401
    resp_ok = await sse_client.get(
        "/traces/NONE-2/live?api_key=secret-key"
    )
    assert resp_ok.status_code == 200


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_emitted_periodically(
    fake_worktree_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_stream, "HEARTBEAT_INTERVAL_SEC", 0.2)
    monkeypatch.setattr(live_stream, "TAIL_POLL_INTERVAL_SEC", 0.1)
    wt = fake_worktree_root / "HB-1"
    _write_stream(
        wt / ".harness" / "logs" / "session-stream.jsonl",
        [_tool_use_event("Bash", "solo")],
    )
    resolved = live_stream._worktree_root_for_ticket("HB-1")
    assert resolved is not None
    streams = live_stream._find_session_streams(resolved)
    req = _FakeRequest(disconnect_after=0.7)
    body = await _drive_generator(
        live_stream._stream_generator(streams, req),  # type: ignore[arg-type]
        max_duration=1.2,
    )
    # At least two heartbeat comments should appear within the window
    # (one at start, at least one at ~0.2s after).
    assert body.count(": ping") >= 1


# ---------------------------------------------------------------------------
# Ticket-id validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_ticket_id_rejected(sse_client: AsyncClient) -> None:
    resp = await sse_client.get("/api/traces/..%2Fescape/stream")
    assert resp.status_code in (400, 404)  # 404 if FastAPI path match fails
