"""Self-learning lesson tables (v5) + lesson outcomes.

Extracted from ``autonomy_store.py``. Holds every helper concerned
with ``lesson_candidates``, ``lesson_evidence``, and
``lesson_outcomes``: the upsert / evidence-insert / state-machine /
outcomes-measurement surface.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel

from .schema import _now_iso

# Cap per lesson. Evidence rows past this are trimmed oldest-first on insert
# so the dashboard stays responsive and the ``evidence`` JOIN stays bounded.
LESSON_EVIDENCE_CAP = 20

# Max length of a ``lesson_evidence.snippet``. Enforced here (not by the
# detector) so truncation happens AFTER redaction — which prevents
# splitting a redaction marker in half.
LESSON_SNIPPET_MAX_LEN = 500

# Max length of a ``lesson_candidates.status_reason``. Applied by both
# ``update_lesson_status`` and ``set_lesson_status_reason`` so reason
# length is predictable across the two writers — previously only
# set_lesson_status_reason truncated, letting a verbose pr_opener
# error survive one path and not the other.
LESSON_REASON_MAX_LEN = 500


class LessonCandidateUpsert(BaseModel):
    """Patch-style upsert for a detected lesson candidate.

    Keyed on ``(detector_name, pattern_key, scope_key)`` so rescans
    refresh the existing row instead of duplicating. See
    ``upsert_lesson_candidate`` for MAX-frequency semantics and
    fields that are preserved across rescans.
    """

    lesson_id: str
    detector_name: str
    detector_version: int = 1
    pattern_key: str
    client_profile: str = ""
    platform_profile: str = ""
    scope_key: str = ""
    proposed_delta_json: str = "{}"
    severity: str = "info"
    window_frequency: int = 1


def upsert_lesson_candidate(
    conn: sqlite3.Connection,
    row: LessonCandidateUpsert,
    *,
    now: str | None = None,
) -> int:
    """Insert or refresh a lesson_candidates row.

    On first detection: inserts with ``status='proposed'`` and
    ``frequency=window_frequency``.

    On repeat detection: refreshes ``severity``,
    ``proposed_delta_json``, ``detector_version``, and
    ``last_seen_at``; sets ``frequency = MAX(current,
    window_frequency)`` so a narrower later scan doesn't regress
    the count. Does NOT touch ``status``, ``status_reason``,
    ``next_review_at``, ``pr_url``, or ``merged_commit_sha`` —
    those are driven by the approval flow.

    Returns the candidate id.
    """
    ts = now or _now_iso()
    initial_freq = max(1, int(row.window_frequency))
    existing = conn.execute(
        "SELECT id FROM lesson_candidates "
        "WHERE detector_name = ? AND pattern_key = ? AND scope_key = ?",
        (row.detector_name, row.pattern_key, row.scope_key),
    ).fetchone()

    if existing is None:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO lesson_candidates (
                    lesson_id, detector_name, detector_version, pattern_key,
                    client_profile, platform_profile, scope_key,
                    frequency, severity, detected_at, last_seen_at,
                    proposed_delta_json, status, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.lesson_id,
                    row.detector_name,
                    row.detector_version,
                    row.pattern_key,
                    row.client_profile,
                    row.platform_profile,
                    row.scope_key,
                    initial_freq,
                    row.severity,
                    ts,
                    ts,
                    row.proposed_delta_json,
                    "proposed",
                    ts,
                    ts,
                ),
            )
        return int(cur.lastrowid or 0)

    # Preserve proposed_delta_json once the lesson has advanced past
    # ``proposed``: the /draft endpoint merges the Claude-drafted
    # ``unified_diff`` into the delta, and a nightly rescan must not
    # overwrite that with the mechanical starter — losing the drafted
    # diff would force a re-draft (and re-spend Anthropic tokens) on
    # every scan. The CASE expression keeps the existing delta on any
    # non-``proposed`` status.
    with conn:
        conn.execute(
            """
            UPDATE lesson_candidates
            SET frequency = MAX(frequency, ?),
                last_seen_at = ?,
                updated_at = ?,
                severity = ?,
                proposed_delta_json = CASE
                    WHEN status = 'proposed' THEN ?
                    ELSE proposed_delta_json
                END,
                detector_version = ?
            WHERE id = ?
            """,
            (
                initial_freq,
                ts,
                ts,
                row.severity,
                row.proposed_delta_json,
                row.detector_version,
                int(existing["id"]),
            ),
        )
    return int(existing["id"])


def insert_lesson_evidence(
    conn: sqlite3.Connection,
    *,
    lesson_id: str,
    trace_id: str,
    source_ref: str,
    observed_at: str,
    snippet: str = "",
    pr_run_id: int | None = None,
    cap: int | None = None,
) -> int | None:
    """Insert an evidence row, trimming oldest beyond ``cap``.

    Returns the new id, or ``None`` on UNIQUE collision (which is
    the normal no-op path when a rescan revisits an existing
    trace). Snippets longer than ``LESSON_SNIPPET_MAX_LEN`` are
    truncated with an ellipsis.
    """
    effective_cap = cap if cap is not None else LESSON_EVIDENCE_CAP
    if effective_cap < 1:
        raise ValueError(
            f"insert_lesson_evidence: cap must be >= 1, got {effective_cap}"
        )
    if len(snippet) > LESSON_SNIPPET_MAX_LEN:
        snippet = snippet[: LESSON_SNIPPET_MAX_LEN - 3] + "..."
    try:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO lesson_evidence
                    (lesson_id, pr_run_id, trace_id, observed_at,
                     source_ref, snippet)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lesson_id,
                    pr_run_id,
                    trace_id,
                    observed_at,
                    source_ref,
                    snippet,
                ),
            )
    except sqlite3.IntegrityError:
        return None
    new_id = int(cur.lastrowid or 0)

    with conn:
        conn.execute(
            """
            DELETE FROM lesson_evidence
            WHERE lesson_id = ?
              AND id NOT IN (
                  SELECT id FROM lesson_evidence
                  WHERE lesson_id = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
            """,
            (lesson_id, lesson_id, effective_cap),
        )
    return new_id


def list_lesson_evidence(
    conn: sqlite3.Connection,
    lesson_id: str,
    *,
    limit: int = LESSON_EVIDENCE_CAP,
) -> list[sqlite3.Row]:
    """Return evidence rows for a lesson, newest first."""
    rows = conn.execute(
        "SELECT * FROM lesson_evidence WHERE lesson_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (lesson_id, limit),
    ).fetchall()
    return list(rows)


def list_evidence_for_lessons(
    conn: sqlite3.Connection,
    lesson_ids: list[str],
    *,
    limit_per_lesson: int = LESSON_EVIDENCE_CAP,
) -> dict[str, list[sqlite3.Row]]:
    """Batch ``list_lesson_evidence`` — one SELECT + in-memory bucket.

    The triage dashboard renders up to ``limit`` candidates per page
    (500 max), so the per-row ``list_lesson_evidence`` loop was up to
    500 SQL round-trips. This replaces that with a single
    ``WHERE lesson_id IN (...)`` query; callers dict-lookup per row.
    Lesson ids with no evidence are simply absent from the result.

    ``limit_per_lesson`` caps the rows returned per lesson at the
    same evidence cap the single-lesson call uses, so the dashboard's
    rendered output is identical.
    """
    if not lesson_ids:
        return {}
    # Chunk to stay under SQLITE_MAX_VARIABLE_NUMBER (default 999).
    chunk_size = 900
    buckets: dict[str, list[sqlite3.Row]] = {lid: [] for lid in lesson_ids}
    for i in range(0, len(lesson_ids), chunk_size):
        chunk = lesson_ids[i : i + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            "SELECT * FROM lesson_evidence "
            f"WHERE lesson_id IN ({placeholders}) "
            "ORDER BY id DESC"
        )
        for row in conn.execute(sql, tuple(chunk)).fetchall():
            lid = str(row["lesson_id"])
            bucket = buckets.get(lid)
            if bucket is None or len(bucket) >= limit_per_lesson:
                continue
            bucket.append(row)
    # Drop empty buckets so callers' `.get(id)` reflects "no evidence"
    # as None rather than []; matches the single-lesson call's
    # downstream `if evidence:` idioms.
    return {lid: rows for lid, rows in buckets.items() if rows}


def get_lesson_by_id(
    conn: sqlite3.Connection, lesson_id: str
) -> sqlite3.Row | None:
    """Fetch a lesson_candidates row by its ``LSN-<hex>`` id."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM lesson_candidates WHERE lesson_id = ?",
        (lesson_id,),
    ).fetchone()
    return row


def list_lesson_candidates(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    client_profile: str | None = None,
    detector_name: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """List lesson_candidates with optional filters.

    Ordered by ``detected_at DESC`` so the most recent patterns
    appear first — matches the dashboard's default sort order and
    rides the ``idx_lesson_candidates_profile_status_seen`` index
    when ``status`` and ``client_profile`` are both specified.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if client_profile is not None:
        clauses.append("client_profile = ?")
        params.append(client_profile)
    if detector_name is not None:
        clauses.append("detector_name = ?")
        params.append(detector_name)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT * FROM lesson_candidates {where} "
        "ORDER BY detected_at DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    return list(conn.execute(sql, params).fetchall())


def count_lesson_candidates(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    client_profile: str | None = None,
    detector_name: str | None = None,
) -> int:
    """Count lesson_candidates with the same optional filters as list."""
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if client_profile is not None:
        clauses.append("client_profile = ?")
        params.append(client_profile)
    if detector_name is not None:
        clauses.append("detector_name = ?")
        params.append(detector_name)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM lesson_candidates {where}",
        params,
    ).fetchone()
    return int(row["n"] if row else 0)


# Allowed (current -> {next}) transitions. Linear approval flow
# (proposed -> draft_ready -> approved -> applied) with snooze
# cycles and rejected/reverted/stale as terminal states.
_LESSON_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"draft_ready", "rejected", "snoozed", "stale"},
    "draft_ready": {"approved", "proposed", "rejected"},
    "snoozed": {"proposed", "rejected", "stale"},
    "approved": {"applied", "rejected"},
    "applied": {"reverted"},
    "rejected": set(),
    "reverted": set(),
    "stale": set(),
}


def update_lesson_status(
    conn: sqlite3.Connection,
    lesson_id: str,
    new_status: str,
    *,
    reason: str = "",
    pr_url: str | None = None,
    merged_commit_sha: str | None = None,
    next_review_at: str | None = None,
    proposed_delta_json: str | None = None,
    now: str | None = None,
) -> sqlite3.Row:
    """Transition a lesson's status, validating against the allowed
    transition table. Raises ``ValueError`` on unknown lesson or
    disallowed transition. Side-channel fields (``pr_url``,
    ``merged_commit_sha``, ``next_review_at``, ``proposed_delta_json``)
    only get written when the caller supplies a non-None value.
    """
    ts = now or _now_iso()
    row = get_lesson_by_id(conn, lesson_id)
    if row is None:
        raise ValueError(f"unknown lesson_id: {lesson_id}")
    current = str(row["status"])
    allowed = _LESSON_STATUS_TRANSITIONS.get(current)
    if allowed is None:
        raise ValueError(f"unknown current status: {current}")
    if new_status not in allowed:
        raise ValueError(
            f"invalid transition {current} -> {new_status} "
            f"for lesson {lesson_id}"
        )

    sets = ["status = ?", "status_reason = ?", "updated_at = ?"]
    params: list[Any] = [new_status, reason[:LESSON_REASON_MAX_LEN], ts]
    if pr_url is not None:
        sets.append("pr_url = ?")
        params.append(pr_url)
    if merged_commit_sha is not None:
        sets.append("merged_commit_sha = ?")
        params.append(merged_commit_sha)
    if next_review_at is not None:
        sets.append("next_review_at = ?")
        params.append(next_review_at)
    if proposed_delta_json is not None:
        sets.append("proposed_delta_json = ?")
        params.append(proposed_delta_json)
    params.append(lesson_id)

    with conn:
        conn.execute(
            f"UPDATE lesson_candidates SET {', '.join(sets)} "
            "WHERE lesson_id = ?",
            params,
        )
    updated = get_lesson_by_id(conn, lesson_id)
    assert updated is not None  # just updated
    return updated


def set_lesson_status_reason(
    conn: sqlite3.Connection,
    lesson_id: str,
    reason: str,
    *,
    now: str | None = None,
) -> None:
    """Update ``status_reason`` without changing ``status``.

    Used when a drafter run fails — the operator needs to see what
    went wrong, but the lesson remains at its existing status
    (usually ``proposed``). The transition validator does not apply
    since no transition is happening.
    """
    ts = now or _now_iso()
    with conn:
        conn.execute(
            "UPDATE lesson_candidates SET status_reason = ?, updated_at = ? "
            "WHERE lesson_id = ?",
            (reason[:LESSON_REASON_MAX_LEN], ts, lesson_id),
        )


def set_lesson_merged_commit_sha(
    conn: sqlite3.Connection,
    lesson_id: str,
    merged_commit_sha: str,
    *,
    now: str | None = None,
) -> None:
    """Record the merge commit sha for an applied lesson.

    Side-channel writer that sits alongside ``set_lesson_status_reason``
    so outcomes measurement doesn't need raw SQL against
    ``lesson_candidates``. Applied→applied is not a legal
    ``update_lesson_status`` transition, and we only ever need to
    stamp this column once per lesson.

    Rejects empty sha values — writing empty would CLEAR an existing
    sha and force outcomes.py to re-poll gh. Callers that have
    nothing to write should not call this function; raising makes the
    misuse obvious instead of silently reverting merge state.
    """
    if not merged_commit_sha:
        raise ValueError(
            "set_lesson_merged_commit_sha: sha must be non-empty; "
            "empty would clear existing merge state"
        )
    ts = now or _now_iso()
    with conn:
        conn.execute(
            "UPDATE lesson_candidates SET merged_commit_sha = ?, "
            "updated_at = ? WHERE lesson_id = ?",
            (merged_commit_sha, ts, lesson_id),
        )


# ---------------------------------------------------------------------------
# Lesson outcomes (v5 — pre/post metrics + human-reedit signals)
# ---------------------------------------------------------------------------


_TERMINAL_VERDICTS: tuple[str, ...] = ("regressed", "human_reedit", "confirmed")


_APPLIED_LESSONS_LIMIT = 10000


def list_applied_lessons(
    conn: sqlite3.Connection,
    *,
    exclude_terminal_verdicts: bool = False,
    limit: int = _APPLIED_LESSONS_LIMIT,
) -> list[sqlite3.Row]:
    """Return applied lessons in detected_at DESC order.

    With ``exclude_terminal_verdicts=True`` the query LEFT JOINs the
    latest ``lesson_outcomes`` row per lesson and drops any whose
    verdict is terminal (regressed / human_reedit / confirmed). The
    outcomes job uses that filter to avoid re-measuring lessons whose
    verdict is already final.

    ``limit`` caps the return at 10k rows by default. Applied lessons
    is expected to stay small (< ~100) once terminal verdicts are
    excluded, but a stray unbounded SELECT in a long-running L1 could
    eventually pull unbounded memory. The cap is a safety net, not a
    pagination control.
    """
    if exclude_terminal_verdicts:
        placeholders = ",".join("?" for _ in _TERMINAL_VERDICTS)
        # Narrow the MAX(id) subquery to outcomes of applied lessons.
        # Without the join + filter the optimizer has to compute MAX(id)
        # GROUP BY lesson_id across EVERY row in lesson_outcomes, even
        # when only a handful of lessons are currently applied. Scales
        # with total historical outcomes, not active work. The
        # ``applied`` filter here + the outer ``status='applied'``
        # filter are redundant semantically but the inner one lets
        # SQLite prune early.
        sql = f"""
            SELECT c.* FROM lesson_candidates c
            LEFT JOIN (
                SELECT lesson_id, verdict
                FROM lesson_outcomes
                WHERE id IN (
                    SELECT MAX(lo.id)
                    FROM lesson_outcomes lo
                    JOIN lesson_candidates lc
                      ON lc.lesson_id = lo.lesson_id
                    WHERE lc.status = 'applied'
                    GROUP BY lo.lesson_id
                )
            ) o ON o.lesson_id = c.lesson_id
            WHERE c.status = 'applied'
              AND (o.verdict IS NULL OR o.verdict NOT IN ({placeholders}))
            ORDER BY c.detected_at DESC
            LIMIT ?
        """
        rows = conn.execute(
            sql, (*_TERMINAL_VERDICTS, int(limit))
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM lesson_candidates WHERE status = 'applied' "
            "ORDER BY detected_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return list(rows)


def get_latest_outcome(
    conn: sqlite3.Connection, lesson_id: str
) -> sqlite3.Row | None:
    """Return the most recent ``lesson_outcomes`` row for a lesson, or None."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM lesson_outcomes WHERE lesson_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (lesson_id,),
    ).fetchone()
    return row


def list_latest_outcomes(
    conn: sqlite3.Connection, lesson_ids: list[str]
) -> dict[str, sqlite3.Row]:
    """Return ``{lesson_id: latest outcome row}`` for a batch of ids.

    Dashboard row rendering calls this once instead of issuing one
    SELECT per row (N+1). Lesson ids with no outcome are simply
    absent from the result dict.
    """
    if not lesson_ids:
        return {}
    placeholders = ",".join("?" for _ in lesson_ids)
    sql = f"""
        SELECT * FROM lesson_outcomes
        WHERE id IN (
            SELECT MAX(id) FROM lesson_outcomes
            WHERE lesson_id IN ({placeholders})
            GROUP BY lesson_id
        )
    """
    rows = conn.execute(sql, lesson_ids).fetchall()
    return {str(r["lesson_id"]): r for r in rows}


class LessonOutcomeInsert(BaseModel):
    """Write payload for a ``lesson_outcomes`` row.

    ``verdict`` is one of:
      - ``pending`` — measurement still within window
      - ``confirmed`` — post metrics better or equal + no pattern recurrence
      - ``no_change`` — metrics didn't move meaningfully
      - ``regressed`` — post metrics worse than pre
      - ``human_reedit`` — the lesson's anchor was re-edited by a human
        (direct "this lesson was wrong" signal; trumps metric verdict)
    """

    lesson_id: str
    measured_at: str
    window_days: int
    pre_fpa: float | None = None
    post_fpa: float | None = None
    pre_escape_rate: float | None = None
    post_escape_rate: float | None = None
    pre_catch_rate: float | None = None
    post_catch_rate: float | None = None
    pattern_recurrence_count: int = 0
    human_reedit_count: int = 0
    human_reedit_refs: str = "[]"
    verdict: str = "pending"
    notes: str = ""


def insert_lesson_outcome(
    conn: sqlite3.Connection, row: LessonOutcomeInsert
) -> int:
    """Insert a ``lesson_outcomes`` row; return the new id."""
    with conn:
        cur = conn.execute(
            """
            INSERT INTO lesson_outcomes (
                lesson_id, measured_at, window_days,
                pre_fpa, post_fpa,
                pre_escape_rate, post_escape_rate,
                pre_catch_rate, post_catch_rate,
                pattern_recurrence_count,
                human_reedit_count, human_reedit_refs,
                verdict, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row.lesson_id,
                row.measured_at,
                row.window_days,
                row.pre_fpa,
                row.post_fpa,
                row.pre_escape_rate,
                row.post_escape_rate,
                row.pre_catch_rate,
                row.post_catch_rate,
                row.pattern_recurrence_count,
                row.human_reedit_count,
                row.human_reedit_refs,
                row.verdict,
                row.notes,
            ),
        )
    return int(cur.lastrowid or 0)
