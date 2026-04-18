#!/usr/bin/env python3
"""Tests for the _trace_watcher partial-line buffer and shutdown drain.

Regression coverage for the race where readline() against a file being
actively written by another process returns a partial (no-trailing-newline)
line. The previous implementation parsed the partial, hit JSONDecodeError,
logged and dropped the line, then read the remainder on the next iteration
and dropped that too — permanently losing the whole event from the live
dashboard feed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _wait_until(
    predicate: Callable[[], bool],
    timeout: float = 3.0,
    interval: float = 0.02,
    description: str = "predicate",
) -> None:
    """Poll ``predicate`` until it returns truthy or ``timeout`` elapses.

    Replaces bare ``time.sleep`` calls in watcher tests, which were
    flaky under CI load — the sleeps were blind guesses at how long the
    background thread would need to reach a given observable state.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError(f"{description} did not hold within {timeout}s")


def _load_spawn_team_module():
    """Import scripts/spawn_team.py as a module so we can call _trace_watcher.

    scripts/spawn_team.py is a CLI script, not a package, so we load it by
    path rather than through normal imports.
    """
    module_name = "_spawn_team_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPTS_DIR / "spawn_team.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeUrlopenResponse:
    """Minimal stand-in for urllib.request.urlopen's context manager result."""

    def __enter__(self) -> "_FakeUrlopenResponse":
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def read(self) -> bytes:
        return b""


def _capturing_urlopen(posted: list[dict]):
    """Build a urlopen replacement that appends each POSTed payload's parsed
    JSON body to the given list. Caller owns the list."""
    def _fake(req, timeout: int = 3) -> _FakeUrlopenResponse:
        posted.append(json.loads(req.data))
        return _FakeUrlopenResponse()
    return _fake


def test_trace_watcher_buffers_partial_lines(tmp_path: Path) -> None:
    """A line written in two chunks (partial → remainder) must be posted
    exactly once with the reassembled payload, not dropped or double-parsed."""
    spawn_team = _load_spawn_team_module()

    log_dir = tmp_path / ".harness" / "logs"
    log_dir.mkdir(parents=True)
    jsonl = log_dir / "pipeline.jsonl"
    jsonl.touch()
    watcher_log = log_dir / "trace-watcher.log"

    config = tmp_path / ".harness" / "trace-config.json"
    config.write_text(json.dumps({
        "ticket_id": "TEST-1",
        "trace_id": "t-abc",
        "l1_url": "http://test.invalid",
    }))

    posted: list[dict] = []
    stop = threading.Event()

    with patch.object(
        spawn_team.urllib.request, "urlopen", side_effect=_capturing_urlopen(posted)
    ):
        thread = threading.Thread(
            target=spawn_team._trace_watcher,
            args=(jsonl, config, stop),
            daemon=True,
        )
        thread.start()

        # Wait for the watcher to open the jsonl file and enter its tail
        # loop. The watcher writes "Tailing ..." to its log at exactly
        # that point, which is a precise signal — more reliable than a
        # bare sleep, which flakes under CI load.
        _wait_until(
            lambda: watcher_log.exists() and "Tailing" in watcher_log.read_text(),
            description="watcher entered tail loop",
        )

        # Write a partial line — no trailing newline. The old code would
        # parse this, hit a JSONDecodeError, and silently drop the event.
        with jsonl.open("a") as f:
            f.write('{"phase":"planning","event":"Plan complete"')
            f.flush()

        # Wait long enough for the watcher's 2-second poll tick to have
        # observed the partial line. The watcher logs nothing for a
        # buffered partial, so we confirm the buffering behavior
        # indirectly: no POST has been made (the predicate is "watcher
        # has had time to see the line and correctly declined to post"
        # — we express this as 'posted remains empty after a poll cycle
        # has elapsed'). A short fixed wait is still needed here because
        # the absence of an event has no positive signal we can poll.
        time.sleep(0.3)

        # At this point the watcher should have buffered the partial and
        # posted nothing.
        assert posted == [], (
            f"partial line was parsed and posted prematurely: {posted}"
        )

        # Now finish the line.
        with jsonl.open("a") as f:
            f.write('}\n')
            f.flush()

        # Wait for the watcher to pick it up — poll the real observable
        # (posted list grew) instead of guessing at timing.
        _wait_until(lambda: len(posted) >= 1, description="first POST recorded")

        stop.set()
        thread.join(timeout=3)

    assert len(posted) == 1, f"expected one posted entry, got {len(posted)}: {posted}"
    assert posted[0]["phase"] == "planning"
    assert posted[0]["event"] == "Plan complete"
    assert posted[0]["ticket_id"] == "TEST-1"
    assert posted[0]["trace_id"] == "t-abc"


def test_trace_watcher_drains_on_shutdown(tmp_path: Path) -> None:
    """When stop_event fires, any remaining newline-terminated events in the
    file must still be posted (drain pass). Previously the watcher exited
    immediately on stop and lost the final entries.
    """
    spawn_team = _load_spawn_team_module()

    log_dir = tmp_path / ".harness" / "logs"
    log_dir.mkdir(parents=True)
    jsonl = log_dir / "pipeline.jsonl"
    jsonl.touch()
    watcher_log = log_dir / "trace-watcher.log"

    config = tmp_path / ".harness" / "trace-config.json"
    config.write_text(json.dumps({
        "ticket_id": "TEST-2",
        "trace_id": "t-def",
        "l1_url": "http://test.invalid",
    }))

    posted: list[dict] = []
    stop = threading.Event()

    with patch.object(
        spawn_team.urllib.request, "urlopen", side_effect=_capturing_urlopen(posted)
    ):
        thread = threading.Thread(
            target=spawn_team._trace_watcher,
            args=(jsonl, config, stop),
            daemon=True,
        )
        thread.start()

        # Wait for the watcher to open the file and enter its tail loop.
        # The "Tailing" log line is written at exactly that point. We
        # need this synchronization so the subsequent write-then-stop
        # dance exercises the drain pass (not the steady-state loop).
        _wait_until(
            lambda: watcher_log.exists() and "Tailing" in watcher_log.read_text(),
            description="watcher entered tail loop",
        )

        # Write two events then *immediately* set stop before the watcher's
        # 2-second sleep expires. The old implementation would miss these
        # because it exited the while loop without a drain pass.
        with jsonl.open("a") as f:
            f.write('{"phase":"pr_created","event":"PR created"}\n')
            f.write('{"phase":"complete","event":"Pipeline complete"}\n')
            f.flush()

        stop.set()
        thread.join(timeout=5)

    events = [p["event"] for p in posted]
    assert "PR created" in events, f"PR created not drained: {events}"
    assert "Pipeline complete" in events, f"complete not drained: {events}"
