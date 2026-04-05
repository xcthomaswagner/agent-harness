"""Tests for the L3 → L1 event forwarding backlog."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import backlog as backlog_mod


@pytest.fixture
def _tmp_backlog() -> Path:
    """Return the currently-monkeypatched BACKLOG_PATH (set by conftest)."""
    return backlog_mod.BACKLOG_PATH


async def _always_ok(payload: dict[str, Any]) -> bool:
    return True


async def _always_fail(payload: dict[str, Any]) -> bool:
    return False


async def test_append_creates_file_with_entry(_tmp_backlog: Path) -> None:
    await backlog_mod.append_backlog("autonomy_event", {"foo": "bar"})
    assert _tmp_backlog.exists()
    lines = _tmp_backlog.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["endpoint"] == "autonomy_event"
    assert entry["payload"] == {"foo": "bar"}
    assert entry["attempts"] == 1
    assert "ts" in entry


async def test_append_increments_attempts_across_drains(_tmp_backlog: Path) -> None:
    await backlog_mod.append_backlog("autonomy_event", {"x": 1})

    forwarders: dict[str, backlog_mod.ForwarderFn] = {"autonomy_event": _always_fail}
    result = await backlog_mod.drain_backlog(forwarders)
    assert result["drained"] == 0
    assert result["remaining"] == 1

    lines = _tmp_backlog.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["attempts"] == 2

    # Drain again — attempts goes to 3
    result = await backlog_mod.drain_backlog(forwarders)
    assert result["remaining"] == 1
    entry = json.loads(_tmp_backlog.read_text().splitlines()[0])
    assert entry["attempts"] == 3


async def test_drain_success_empties_file(_tmp_backlog: Path) -> None:
    for i in range(3):
        await backlog_mod.append_backlog("autonomy_event", {"i": i})
    assert _tmp_backlog.exists()

    forwarders: dict[str, backlog_mod.ForwarderFn] = {"autonomy_event": _always_ok}
    result = await backlog_mod.drain_backlog(forwarders)
    assert result["drained"] == 3
    assert result["remaining"] == 0
    assert not _tmp_backlog.exists()


async def test_drain_partial_failure_keeps_failed(_tmp_backlog: Path) -> None:
    await backlog_mod.append_backlog("autonomy_event", {"ok": True})
    await backlog_mod.append_backlog("human_issue", {"fail": True})

    async def _mixed(payload: dict[str, Any]) -> bool:
        return bool(payload.get("ok"))

    forwarders: dict[str, backlog_mod.ForwarderFn] = {
        "autonomy_event": _mixed,
        "human_issue": _mixed,
    }
    result = await backlog_mod.drain_backlog(forwarders)
    assert result["drained"] == 1
    assert result["remaining"] == 1

    lines = _tmp_backlog.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["endpoint"] == "human_issue"
    assert entry["attempts"] == 2


async def test_drain_drops_after_max_attempts(_tmp_backlog: Path) -> None:
    await backlog_mod.append_backlog(
        "autonomy_event", {"x": 1}, attempts=backlog_mod.MAX_DRAIN_ATTEMPTS
    )

    forwarders: dict[str, backlog_mod.ForwarderFn] = {"autonomy_event": _always_fail}
    result = await backlog_mod.drain_backlog(forwarders)
    assert result["dropped"] == 1
    assert result["drained"] == 0
    assert result["remaining"] == 0
    assert not _tmp_backlog.exists()


async def test_corrupt_line_skipped_and_logged(_tmp_backlog: Path) -> None:
    _tmp_backlog.parent.mkdir(parents=True, exist_ok=True)
    _tmp_backlog.write_text("not valid json\n")

    forwarders: dict[str, backlog_mod.ForwarderFn] = {"autonomy_event": _always_ok}
    result = await backlog_mod.drain_backlog(forwarders)
    assert result["corrupt"] == 1
    assert result["drained"] == 0


async def test_unknown_endpoint_skipped(_tmp_backlog: Path) -> None:
    await backlog_mod.append_backlog("bogus", {"x": 1})

    forwarders: dict[str, backlog_mod.ForwarderFn] = {"autonomy_event": _always_ok}
    result = await backlog_mod.drain_backlog(forwarders)
    assert result["corrupt"] == 1
    assert result["drained"] == 0


async def test_concurrent_appends_no_interleave(_tmp_backlog: Path) -> None:
    await asyncio.gather(
        *[backlog_mod.append_backlog("autonomy_event", {"i": i}) for i in range(20)]
    )
    lines = _tmp_backlog.read_text().splitlines()
    assert len(lines) == 20
    for line in lines:
        entry = json.loads(line)
        assert entry["endpoint"] == "autonomy_event"
        assert "i" in entry["payload"]


async def test_backlog_status_reports_correct_counts(_tmp_backlog: Path) -> None:
    status = backlog_mod.backlog_status()
    assert status["entries"] == 0
    assert status["bytes"] == 0

    await backlog_mod.append_backlog("autonomy_event", {"i": 1})
    await backlog_mod.append_backlog("autonomy_event", {"i": 2})

    status = backlog_mod.backlog_status()
    assert status["entries"] == 2
    assert status["bytes"] > 0
    assert status["oldest_ts"] != ""
    assert status["newest_ts"] != ""


async def test_size_cap_trims_oldest(
    _tmp_backlog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-fill with many lines
    _tmp_backlog.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"ts": "t", "endpoint": "autonomy_event", "payload": {"i": i}, "attempts": 1})
        for i in range(100)
    ]
    _tmp_backlog.write_text("\n".join(lines) + "\n")

    # Set size cap below current file size
    current_size = _tmp_backlog.stat().st_size
    monkeypatch.setattr(backlog_mod, "MAX_BACKLOG_BYTES", current_size // 2)

    # Append triggers _enforce_size_cap
    await backlog_mod.append_backlog("autonomy_event", {"i": 999})

    remaining_lines = _tmp_backlog.read_text().splitlines()
    # Should have trimmed ~20% of the old 100 lines, plus added 1
    assert len(remaining_lines) < 100
    # Oldest-kept payload should NOT be i=0
    first_kept = json.loads(remaining_lines[0])
    assert first_kept["payload"]["i"] != 0
    # Last should be the new appended one
    last = json.loads(remaining_lines[-1])
    assert last["payload"]["i"] == 999
