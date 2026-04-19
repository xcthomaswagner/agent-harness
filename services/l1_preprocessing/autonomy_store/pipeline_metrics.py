"""Pipeline metrics helpers (v6 — scalar per-run observations).

Thin upsert + read layer over the v6 ``pipeline_metrics`` table. One
row per ``(trace_id, metric_name)`` tuple. Detectors compute scalars
(e.g. the reviewer/judge rejection rate for one run) and persist them
here so other detectors can scan rolling windows without re-reading
worktree artifacts.

Semantics worth knowing:

- Upserts are idempotent: repeat scans of the same ``trace_id`` +
  ``metric_name`` REPLACE the prior value rather than accumulating.
- ``observed_at`` is the timestamp of the underlying observation
  (pr_run opened_at, artifact mtime, etc.), not the insert time.
- The module purposely does not expose a DELETE helper — metrics are
  append-only-by-observation and replaced on re-scan.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .schema import _now_iso


@dataclass(frozen=True)
class PipelineMetric:
    """One scalar observation for a pipeline run."""

    ticket_id: str
    trace_id: str
    metric_name: str
    metric_value: float
    observed_at: str


def upsert_pipeline_metric(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    trace_id: str,
    metric_name: str,
    metric_value: float,
    observed_at: str = "",
) -> int:
    """Insert or replace a pipeline_metrics row.

    Keyed on ``(trace_id, metric_name)`` via the table UNIQUE constraint.
    When ``observed_at`` is empty, falls back to now() so the caller
    doesn't have to compute it for synthetic metrics. Returns the row id.
    """
    if not trace_id:
        raise ValueError("trace_id is required")
    if not metric_name:
        raise ValueError("metric_name is required")
    ts = observed_at or _now_iso()
    # Use INSERT OR REPLACE to keep the UNIQUE constraint honored while
    # allowing idempotent rescans. Row id may change on replace — that's
    # fine because we key everything on (trace_id, metric_name).
    with conn:
        cur = conn.execute(
            """
            INSERT INTO pipeline_metrics (
                ticket_id, trace_id, metric_name, metric_value, observed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (trace_id, metric_name) DO UPDATE SET
                ticket_id = excluded.ticket_id,
                metric_value = excluded.metric_value,
                observed_at = excluded.observed_at
            """,
            (ticket_id, trace_id, metric_name, float(metric_value), ts),
        )
    lookup = conn.execute(
        "SELECT id FROM pipeline_metrics WHERE trace_id = ? AND metric_name = ?",
        (trace_id, metric_name),
    ).fetchone()
    return int(lookup["id"]) if lookup else int(cur.lastrowid or 0)


def list_recent_metrics(
    conn: sqlite3.Connection,
    *,
    metric_name: str,
    limit: int = 5,
) -> list[PipelineMetric]:
    """Return the most recent ``limit`` rows for ``metric_name``.

    Ordered by ``observed_at`` DESC then ``id`` DESC so ties break on
    the insertion order. Used by the rolling-window detectors.
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        """
        SELECT ticket_id, trace_id, metric_name, metric_value, observed_at
        FROM pipeline_metrics
        WHERE metric_name = ?
        ORDER BY observed_at DESC, id DESC
        LIMIT ?
        """,
        (metric_name, int(limit)),
    ).fetchall()
    return [
        PipelineMetric(
            ticket_id=str(r["ticket_id"] or ""),
            trace_id=str(r["trace_id"] or ""),
            metric_name=str(r["metric_name"] or ""),
            metric_value=float(r["metric_value"]),
            observed_at=str(r["observed_at"] or ""),
        )
        for r in rows
    ]


def count_metrics(
    conn: sqlite3.Connection, *, metric_name: str
) -> int:
    """Return the number of rows for ``metric_name``."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pipeline_metrics WHERE metric_name = ?",
        (metric_name,),
    ).fetchone()
    return int(row["n"]) if row else 0
