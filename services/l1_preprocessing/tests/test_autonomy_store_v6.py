"""Tests for the v6 pipeline_metrics schema migration + helpers.

Covers:

- v6 migration applies on a fresh DB (full chain v1..v6).
- v6 is idempotent on repeat ensure_schema calls.
- ``pipeline_metrics`` table exists with the expected columns + UNIQUE.
- ``upsert_pipeline_metric`` is idempotent on (trace_id, metric_name) —
  the second call REPLACES, it does not accumulate.
- ``list_recent_metrics`` returns rows in observed_at DESC order.
- ``count_metrics`` reports only the named metric.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autonomy_store import (
    count_metrics,
    ensure_schema,
    list_recent_metrics,
    open_connection,
    upsert_pipeline_metric,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "autonomy.db"


@pytest.fixture
def conn(db_path: Path):
    c = open_connection(db_path)
    try:
        ensure_schema(c)
        yield c
    finally:
        c.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        r["name"]
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


class TestMigrationApplies:
    def test_ensure_schema_reaches_v6_on_fresh_db(self, db_path: Path) -> None:
        c = open_connection(db_path)
        try:
            version = ensure_schema(c)
            assert version >= 6
            row = c.execute(
                "SELECT version FROM schema_version WHERE version = 6"
            ).fetchone()
            assert row is not None
        finally:
            c.close()

    def test_v6_is_idempotent(self, db_path: Path) -> None:
        c = open_connection(db_path)
        try:
            ensure_schema(c)
            ensure_schema(c)
            rows = c.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ).fetchall()
            versions = [int(r["version"]) for r in rows]
            assert 6 in versions
        finally:
            c.close()

    def test_pipeline_metrics_table_columns(self, conn) -> None:
        cols = _table_columns(conn, "pipeline_metrics")
        assert cols == {
            "id",
            "ticket_id",
            "trace_id",
            "metric_name",
            "metric_value",
            "observed_at",
        }

    def test_pipeline_metrics_unique_constraint(self, conn) -> None:
        conn.execute(
            "INSERT INTO pipeline_metrics "
            "(ticket_id, trace_id, metric_name, metric_value, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("T-1", "trace-1", "m", 1.0, "2026-04-17T00:00:00Z"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO pipeline_metrics "
                "(ticket_id, trace_id, metric_name, metric_value, observed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("T-1", "trace-1", "m", 2.0, "2026-04-17T01:00:00Z"),
            )
            conn.commit()


class TestUpsertPipelineMetric:
    def test_first_insert_creates_row(self, conn) -> None:
        rid = upsert_pipeline_metric(
            conn,
            ticket_id="T-1",
            trace_id="trace-1",
            metric_name="reviewer_judge_rejection_rate",
            metric_value=0.8,
            observed_at="2026-04-17T10:00:00Z",
        )
        assert rid > 0
        row = conn.execute(
            "SELECT metric_value, observed_at FROM pipeline_metrics WHERE id = ?",
            (rid,),
        ).fetchone()
        assert row["metric_value"] == 0.8
        assert row["observed_at"] == "2026-04-17T10:00:00Z"

    def test_repeat_upsert_replaces_not_accumulates(self, conn) -> None:
        upsert_pipeline_metric(
            conn,
            ticket_id="T-1",
            trace_id="trace-1",
            metric_name="m",
            metric_value=0.5,
            observed_at="2026-04-17T10:00:00Z",
        )
        upsert_pipeline_metric(
            conn,
            ticket_id="T-1",
            trace_id="trace-1",
            metric_name="m",
            metric_value=0.9,
            observed_at="2026-04-17T11:00:00Z",
        )
        # Still one row — upsert replaced.
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM pipeline_metrics "
            "WHERE trace_id = 'trace-1' AND metric_name = 'm'"
        ).fetchone()["n"]
        assert count == 1
        row = conn.execute(
            "SELECT metric_value, observed_at FROM pipeline_metrics "
            "WHERE trace_id = 'trace-1' AND metric_name = 'm'"
        ).fetchone()
        assert row["metric_value"] == 0.9
        assert row["observed_at"] == "2026-04-17T11:00:00Z"

    def test_different_metric_on_same_trace_coexists(self, conn) -> None:
        upsert_pipeline_metric(
            conn, ticket_id="T-1", trace_id="trace-1",
            metric_name="a", metric_value=1.0, observed_at="2026-04-17T10:00:00Z",
        )
        upsert_pipeline_metric(
            conn, ticket_id="T-1", trace_id="trace-1",
            metric_name="b", metric_value=2.0, observed_at="2026-04-17T10:00:00Z",
        )
        assert count_metrics(conn, metric_name="a") == 1
        assert count_metrics(conn, metric_name="b") == 1

    def test_missing_trace_id_raises(self, conn) -> None:
        with pytest.raises(ValueError):
            upsert_pipeline_metric(
                conn, ticket_id="T-1", trace_id="",
                metric_name="m", metric_value=1.0,
            )

    def test_missing_metric_name_raises(self, conn) -> None:
        with pytest.raises(ValueError):
            upsert_pipeline_metric(
                conn, ticket_id="T-1", trace_id="trace-1",
                metric_name="", metric_value=1.0,
            )

    def test_empty_observed_at_falls_back_to_now(self, conn) -> None:
        upsert_pipeline_metric(
            conn, ticket_id="T-1", trace_id="trace-1",
            metric_name="m", metric_value=1.0, observed_at="",
        )
        row = conn.execute(
            "SELECT observed_at FROM pipeline_metrics WHERE trace_id = 'trace-1'"
        ).fetchone()
        # Non-empty ISO string from _now_iso().
        assert row["observed_at"]


class TestListRecentMetrics:
    def test_orders_desc_by_observed_at(self, conn) -> None:
        for i, ts in enumerate(
            [
                "2026-04-15T00:00:00Z",
                "2026-04-16T00:00:00Z",
                "2026-04-17T00:00:00Z",
            ]
        ):
            upsert_pipeline_metric(
                conn, ticket_id=f"T-{i}", trace_id=f"trace-{i}",
                metric_name="m", metric_value=float(i), observed_at=ts,
            )
        got = list_recent_metrics(conn, metric_name="m", limit=5)
        assert [m.trace_id for m in got] == [
            "trace-2", "trace-1", "trace-0"
        ]

    def test_limit_truncates(self, conn) -> None:
        for i in range(10):
            upsert_pipeline_metric(
                conn, ticket_id=f"T-{i}", trace_id=f"trace-{i}",
                metric_name="m", metric_value=float(i),
                observed_at=f"2026-04-{10 + i:02d}T00:00:00Z",
            )
        got = list_recent_metrics(conn, metric_name="m", limit=3)
        assert len(got) == 3

    def test_filters_by_metric_name(self, conn) -> None:
        upsert_pipeline_metric(
            conn, ticket_id="T-1", trace_id="trace-1",
            metric_name="a", metric_value=1.0, observed_at="2026-04-17T00:00:00Z",
        )
        upsert_pipeline_metric(
            conn, ticket_id="T-2", trace_id="trace-2",
            metric_name="b", metric_value=2.0, observed_at="2026-04-17T00:00:00Z",
        )
        got_a = list_recent_metrics(conn, metric_name="a", limit=5)
        got_b = list_recent_metrics(conn, metric_name="b", limit=5)
        assert [m.metric_value for m in got_a] == [1.0]
        assert [m.metric_value for m in got_b] == [2.0]

    def test_zero_limit_returns_empty(self, conn) -> None:
        upsert_pipeline_metric(
            conn, ticket_id="T-1", trace_id="trace-1",
            metric_name="m", metric_value=1.0, observed_at="2026-04-17T00:00:00Z",
        )
        assert list_recent_metrics(conn, metric_name="m", limit=0) == []


class TestCountMetrics:
    def test_zero_on_empty(self, conn) -> None:
        assert count_metrics(conn, metric_name="anything") == 0

    def test_counts_only_named(self, conn) -> None:
        # Distinct (trace_id, metric_name) so the UNIQUE upsert doesn't
        # collapse rows — we want count=2 for "a", count=1 for "b".
        rows_to_insert = [
            ("trace-a1", "a"),
            ("trace-b1", "b"),
            ("trace-a2", "a"),
        ]
        for trace_id, name in rows_to_insert:
            upsert_pipeline_metric(
                conn,
                ticket_id=f"T-{trace_id}",
                trace_id=trace_id,
                metric_name=name,
                metric_value=1.0,
                observed_at="2026-04-17T00:00:00Z",
            )
        assert count_metrics(conn, metric_name="a") == 2
        assert count_metrics(conn, metric_name="b") == 1
