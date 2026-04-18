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

import structlog

logger = structlog.get_logger()


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

    Detectors MAY implement a ``recurrence_for`` method — see
    ``count_pattern_recurrence`` below for the expected signature.
    Detectors without one contribute 0 to the post-merge
    pattern-recurrence count (outcomes.py's default).
    """

    name: str
    version: int

    def scan(
        self, conn: sqlite3.Connection, window_days: int
    ) -> list[CandidateProposal]:
        ...


def count_pattern_recurrence(
    detector: Detector,
    conn: sqlite3.Connection,
    *,
    lesson: sqlite3.Row,
    since_iso: str,
    until_iso: str,
) -> int:
    """Dispatch to ``detector.recurrence_for`` if implemented, else 0.

    Kept as a free function so the ``Detector`` Protocol stays
    interface-only and detectors without a recurrence implementation
    don't need to stub it.

    Catches the narrow set of errors we expect from well-formed
    detectors — SQL schema mismatch, bad row access, bad pattern_key
    split. ``ValueError`` is included because a detector that does
    ``a, b = pattern_key.split("|", 1)`` on a malformed key raises
    ValueError from tuple unpacking; the docstring explicitly promises
    that case degrades to 0 rather than erroring the whole outcome.
    Non-data bugs (import errors, assertion failures) propagate so
    operators see real regressions in the miner rather than having
    them silently downgrade a lesson's verdict.
    """
    fn = getattr(detector, "recurrence_for", None)
    if fn is None:
        return 0
    try:
        result = fn(
            conn, lesson=lesson, since_iso=since_iso, until_iso=until_iso
        )
    except (sqlite3.DatabaseError, KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "learning_pattern_recurrence_failed",
            detector=getattr(detector, "name", ""),
            error=f"{type(exc).__name__}: {exc}",
        )
        return 0
    try:
        return max(0, int(result))
    except (TypeError, ValueError):
        return 0
