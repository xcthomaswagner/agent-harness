"""L3 → L1 event forwarding backlog.

Durable storage for autonomy events that failed to forward to L1 after
retry-once. Replayed on startup and via admin endpoint. Thread-safe via
asyncio.Lock; append-only JSONL.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

BACKLOG_PATH = Path(os.getenv("L3_BACKLOG_PATH", "data/l3-autonomy-backlog.jsonl"))
MAX_BACKLOG_BYTES = 50 * 1024 * 1024  # 50MB
MAX_DRAIN_ATTEMPTS = 10

_lock: asyncio.Lock = asyncio.Lock()

ForwarderFn = Callable[[dict[str, Any]], Awaitable[bool]]


async def append_backlog(
    endpoint: str, payload: dict[str, Any], attempts: int = 1
) -> None:
    """Append a failed-forward entry. Creates parent dir if missing.
    Drops oldest lines if file exceeds MAX_BACKLOG_BYTES."""
    async with _lock:
        BACKLOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        await _enforce_size_cap()
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "endpoint": endpoint,
            "payload": payload,
            "attempts": attempts,
        }
        with BACKLOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info("l3_backlog_appended", endpoint=endpoint, attempts=attempts)


async def drain_backlog(forwarders: dict[str, ForwarderFn]) -> dict[str, int]:
    """Replay all backlog entries. Hold lock for entire drain to prevent
    concurrent appends from being overwritten. Returns summary dict."""
    async with _lock:
        if not BACKLOG_PATH.exists():
            return {"drained": 0, "remaining": 0, "dropped": 0, "corrupt": 0}
        lines = BACKLOG_PATH.read_text(encoding="utf-8").splitlines()
        logger.info("l3_backlog_drain_started", entries=len(lines))
        survivors: list[dict[str, Any]] = []
        drained = 0
        dropped = 0
        corrupt = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                endpoint = entry["endpoint"]
                payload = entry["payload"]
                attempts = int(entry.get("attempts", 1))
            except (json.JSONDecodeError, KeyError, ValueError):
                corrupt += 1
                logger.warning("l3_backlog_corrupt_line", line=line[:200])
                continue
            if attempts >= MAX_DRAIN_ATTEMPTS:
                dropped += 1
                logger.warning(
                    "l3_backlog_entry_dropped", endpoint=endpoint, attempts=attempts
                )
                continue
            forwarder = forwarders.get(endpoint)
            if forwarder is None:
                corrupt += 1
                logger.warning("l3_backlog_unknown_endpoint", endpoint=endpoint)
                continue
            try:
                ok = await forwarder(payload)
            except Exception:
                ok = False
                logger.exception("l3_backlog_forward_raised", endpoint=endpoint)
            if ok:
                drained += 1
            else:
                entry["attempts"] = attempts + 1
                survivors.append(entry)
        # Write survivors (atomic rename)
        tmp = BACKLOG_PATH.with_suffix(".jsonl.tmp")
        if survivors:
            with tmp.open("w", encoding="utf-8") as f:
                for s in survivors:
                    f.write(json.dumps(s) + "\n")
            tmp.replace(BACKLOG_PATH)
        else:
            BACKLOG_PATH.unlink(missing_ok=True)
        result = {
            "drained": drained,
            "remaining": len(survivors),
            "dropped": dropped,
            "corrupt": corrupt,
        }
        logger.info("l3_backlog_drain_result", **result)
        return result


def backlog_status() -> dict[str, Any]:
    """Return file size + count + oldest/newest timestamps."""
    if not BACKLOG_PATH.exists():
        return {"entries": 0, "bytes": 0, "oldest_ts": "", "newest_ts": ""}
    size = BACKLOG_PATH.stat().st_size
    lines = BACKLOG_PATH.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    oldest = entries[0].get("ts", "") if entries else ""
    newest = entries[-1].get("ts", "") if entries else ""
    return {
        "entries": len(entries),
        "bytes": size,
        "oldest_ts": oldest,
        "newest_ts": newest,
    }


async def _enforce_size_cap() -> None:
    """If file size > MAX_BACKLOG_BYTES, drop oldest lines until under cap."""
    if not BACKLOG_PATH.exists():
        return
    size = BACKLOG_PATH.stat().st_size
    if size <= MAX_BACKLOG_BYTES:
        return
    logger.warning("l3_backlog_overflow_trimming", size=size)
    lines = BACKLOG_PATH.read_text(encoding="utf-8").splitlines()
    # Drop oldest 20% of lines
    keep_count = max(1, len(lines) * 4 // 5)
    kept = lines[-keep_count:]
    BACKLOG_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
    logger.warning("l3_backlog_overflow_dropped", dropped=len(lines) - keep_count)
