"""Detector Protocol + CandidateProposal dataclass.

Detectors are pure — they read from the sqlite ``conn`` and hand
back ``CandidateProposal`` objects. The runner owns persistence,
redaction, and any other I/O.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol


def compute_lesson_id(
    detector_name: str, pattern_key: str, scope_key: str
) -> str:
    """Deterministic ``LSN-<8hex>`` id from (detector, pattern, scope)."""
    raw = f"{detector_name}|{pattern_key}|{scope_key}".encode()
    digest = hashlib.sha256(raw).hexdigest()[:8]
    return f"LSN-{digest}"


@dataclass(frozen=True)
class EvidenceItem:
    """One observation supporting a candidate. pr_run_id is optional
    for detectors that key off traces directly rather than PRs."""

    trace_id: str
    observed_at: str
    source_ref: str
    snippet: str = ""
    pr_run_id: int | None = None


@dataclass(frozen=True)
class CandidateProposal:
    """What a detector emits per pattern instance.

    window_frequency is persisted as MAX against any existing row,
    so a narrower later scan doesn't decrease the stored count.
    """

    detector_name: str
    detector_version: int
    pattern_key: str
    client_profile: str
    platform_profile: str
    scope_key: str
    severity: str
    proposed_delta_json: str
    window_frequency: int = 1
    evidence: tuple[EvidenceItem, ...] = field(default_factory=tuple)

    @property
    def lesson_id(self) -> str:
        return compute_lesson_id(
            self.detector_name, self.pattern_key, self.scope_key
        )


class Detector(Protocol):
    """The runner calls ``scan`` on every registered detector.

    Implementations must be deterministic for the same (conn,
    window_days) so repeat scans idempotently upsert instead of
    churning frequency.
    """

    name: str
    version: int

    def scan(
        self, conn: sqlite3.Connection, window_days: int
    ) -> list[CandidateProposal]:
        ...
