"""Concurrency stress test for ``autonomy_store`` writes.

Motivation: ``autonomy_store`` is a plain sqlite3 file shared across the
webhook handler, the autonomy ingest path, the lesson detector workers,
and the dashboard polling endpoints. Under load these share a single
DB and SQLite's default file-lock + per-connection isolation is the
only serialization; a missing WAL pragma or a global shared connection
would surface as SQLITE_BUSY / unique-constraint noise in production
but only under contention.

This test spawns 20 workers, each opening its OWN sqlite connection
and issuing 15 write ops (5 ``upsert_pr_run`` + 5
``upsert_lesson_candidate`` + 5 ``insert_lesson_outcome``) against the
same DB. Under WAL + ``PRAGMA busy_timeout = 5000`` (both set by
``open_connection``) the 300 writes should land cleanly with no
integrity errors.

If the test ever surfaces a real SQLITE_BUSY / IntegrityError it is
left failing — Phase 6 charter is discovery, not production fixes.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from autonomy_store import (
    LessonCandidateUpsert,
    LessonOutcomeInsert,
    PrRunUpsert,
    ensure_schema,
    insert_lesson_outcome,
    open_connection,
    upsert_lesson_candidate,
    upsert_pr_run,
)

WORKERS = 20
OPS_PER_KIND = 5  # 5 pr_runs + 5 lessons + 5 outcomes per worker
EXPECTED_ROWS = WORKERS * OPS_PER_KIND  # 100 per table


def _worker(db_path: Path, worker_id: int) -> list[str]:
    """One worker's batch of writes. Returns a list of error strings.

    Each worker opens its own connection so the test reflects the
    real production pattern (FastAPI request handlers open per-request
    connections rather than sharing a module-level one).
    """
    errors: list[str] = []
    conn = open_connection(db_path)
    try:
        # pr_runs: unique key is (repo, pr_number, head_sha). Salt each
        # row with the worker id + op index so every insert is a true
        # INSERT (not an UPDATE) and we actually stress the insert path
        # across workers.
        for op_idx in range(OPS_PER_KIND):
            try:
                upsert_pr_run(
                    conn,
                    PrRunUpsert(
                        ticket_id=f"T-{worker_id}-{op_idx}",
                        pr_number=worker_id * 1000 + op_idx,
                        repo_full_name=f"acme/worker-{worker_id}",
                        head_sha=f"sha-{worker_id}-{op_idx}",
                        client_profile="concurrent-test",
                        opened_at="2026-04-17T00:00:00+00:00",
                    ),
                )
            except Exception as exc:  # capturing unknown sqlite error types
                errors.append(f"upsert_pr_run[{worker_id}-{op_idx}]: {exc!r}")

        # lesson_candidates: unique key is (detector_name, pattern_key,
        # scope_key). Salt pattern_key per-(worker,op) for true inserts.
        # Stash the lesson_ids so the outcomes-insert below can satisfy
        # the FK lesson_outcomes.lesson_id → lesson_candidates.lesson_id.
        created_lesson_ids: list[str] = []
        for op_idx in range(OPS_PER_KIND):
            lesson_id = f"LSN-{worker_id}-{op_idx}-{uuid.uuid4().hex[:6]}"
            try:
                upsert_lesson_candidate(
                    conn,
                    LessonCandidateUpsert(
                        lesson_id=lesson_id,
                        detector_name="concurrent_test",
                        pattern_key=f"pat-{worker_id}-{op_idx}",
                        scope_key=f"scope-{worker_id}-{op_idx}",
                    ),
                )
                created_lesson_ids.append(lesson_id)
            except Exception as exc:  # capturing unknown sqlite error types
                errors.append(
                    f"upsert_lesson_candidate[{worker_id}-{op_idx}]: {exc!r}"
                )

        # lesson_outcomes: FK to lesson_candidates.lesson_id requires
        # the candidate row to already exist. Pair each outcome to the
        # same-index candidate we just inserted above so the insert
        # path is exercised under concurrency AND the FK holds.
        for op_idx, lesson_id in enumerate(created_lesson_ids):
            try:
                insert_lesson_outcome(
                    conn,
                    LessonOutcomeInsert(
                        lesson_id=lesson_id,
                        measured_at="2026-04-17T00:00:00+00:00",
                        window_days=7,
                        verdict="pending",
                    ),
                )
            except Exception as exc:  # capturing unknown sqlite error types
                errors.append(
                    f"insert_lesson_outcome[{worker_id}-{op_idx}]: {exc!r}"
                )
    finally:
        conn.close()
    return errors


def test_concurrent_writes_to_autonomy_store(tmp_path: Path) -> None:
    """300 writes across 20 threads land without SQLITE_BUSY or integrity errors.

    Relies on ``open_connection`` enabling WAL journaling + a 5s
    busy_timeout (pragmas set at connection open). If a future
    refactor drops either pragma, expect intermittent flakes here
    before the production path starts logging real failures.
    """
    db_path = tmp_path / "concurrent.db"

    # Initialize schema on a dedicated connection + close BEFORE
    # spawning workers, so all workers see a fully-migrated DB via
    # their own connections — no writer happening during migration
    # (which would itself be a race).
    init_conn = open_connection(db_path)
    try:
        ensure_schema(init_conn)
    finally:
        init_conn.close()

    # Synchronize worker starts with a barrier so contention is maximal.
    barrier = threading.Barrier(WORKERS)

    def _run(worker_id: int) -> list[str]:
        barrier.wait()
        return _worker(db_path, worker_id)

    all_errors: list[str] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_run, i) for i in range(WORKERS)]
        for fut in as_completed(futures):
            all_errors.extend(fut.result())

    # Fail loud if any writer saw sqlite contention. If this ever fires
    # in CI, do NOT swallow it — the production pattern is the same and
    # the fix (bigger busy_timeout / retry loop / connection pool) is
    # out of Phase 6 scope. Just flag.
    assert not all_errors, (
        "Concurrent write failures surfaced — possible SQLITE_BUSY / "
        "integrity race. First few:\n" + "\n".join(all_errors[:10])
    )

    # Verify row counts match inputs exactly — no missed inserts, no
    # silent UPSERT collisions.
    verify_conn = open_connection(db_path)
    try:
        pr_count = verify_conn.execute("SELECT COUNT(*) FROM pr_runs").fetchone()[0]
        cand_count = verify_conn.execute(
            "SELECT COUNT(*) FROM lesson_candidates"
        ).fetchone()[0]
        outcome_count = verify_conn.execute(
            "SELECT COUNT(*) FROM lesson_outcomes"
        ).fetchone()[0]
    finally:
        verify_conn.close()

    assert pr_count == EXPECTED_ROWS, (
        f"pr_runs: expected {EXPECTED_ROWS}, got {pr_count}"
    )
    assert cand_count == EXPECTED_ROWS, (
        f"lesson_candidates: expected {EXPECTED_ROWS}, got {cand_count}"
    )
    assert outcome_count == EXPECTED_ROWS, (
        f"lesson_outcomes: expected {EXPECTED_ROWS}, got {outcome_count}"
    )

    # Spot-check retrievability by key from one worker's rows — proves
    # the inserts are actually queryable (not just counted).
    pytest.importorskip("sqlite3")  # Defensive — stdlib should always resolve.
    sample_conn = open_connection(db_path)
    try:
        row = sample_conn.execute(
            "SELECT ticket_id FROM pr_runs WHERE repo_full_name = ? "
            "AND pr_number = ? AND head_sha = ?",
            ("acme/worker-3", 3 * 1000 + 2, "sha-3-2"),
        ).fetchone()
        assert row is not None, "Could not retrieve pr_run inserted by worker 3"
        assert row["ticket_id"] == "T-3-2"
    finally:
        sample_conn.close()
