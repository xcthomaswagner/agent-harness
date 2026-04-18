"""Defect-link and manual-override helpers + match-promotion glue.

Extracted from ``autonomy_store.py``. Covers the v3 additions
(``defect_links``, ``manual_overrides``, match promotion + manual
match) and the defect-sweep heartbeat audit rows. Uses
``AI_SOURCES`` and ``_now_iso`` from ``schema``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from .schema import AI_SOURCES, _now_iso


def insert_defect_link(
    conn: sqlite3.Connection,
    *,
    pr_run_id: int,
    defect_key: str,
    source: str,
    reported_at: str,
    severity: str = "",
    confirmed: int = 1,
    notes: str = "",
    category: str = "escaped",
) -> int:
    """Insert a defect_links row, upserting on (pr_run_id, defect_key, source).

    On conflict, updates severity/reported_at/confirmed/notes/category.
    Returns the row id.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO defect_links (
                pr_run_id, defect_key, source, severity, reported_at,
                confirmed, notes, category
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT (pr_run_id, defect_key, source) DO UPDATE SET
                severity = excluded.severity,
                reported_at = excluded.reported_at,
                confirmed = excluded.confirmed,
                notes = excluded.notes,
                category = excluded.category
            """,
            (
                pr_run_id,
                defect_key,
                source,
                severity,
                reported_at,
                int(confirmed),
                notes,
                category,
            ),
        )
    row = conn.execute(
        "SELECT id FROM defect_links WHERE pr_run_id = ? AND defect_key = ? "
        "AND source = ?",
        (pr_run_id, defect_key, source),
    ).fetchone()
    return int(row["id"]) if row is not None else 0


def get_defect_link(
    conn: sqlite3.Connection,
    pr_run_id: int,
    defect_key: str,
    source: str,
) -> sqlite3.Row | None:
    """Lookup a defect_link by unique triple."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM defect_links WHERE pr_run_id = ? AND defect_key = ? "
        "AND source = ?",
        (pr_run_id, defect_key, source),
    ).fetchone()
    return row


def _parse_iso(value: str) -> datetime | None:
    """Parse ISO-8601 string; return None on empty/malformed input.

    Always returns a UTC-aware datetime so callers can safely subtract
    any two parsed values without a naive-vs-aware TypeError.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def list_confirmed_escaped_defects(
    conn: sqlite3.Connection,
    pr_run_ids: list[int],
    *,
    window_days: int = 30,
) -> list[sqlite3.Row]:
    """Return confirmed escaped defect_links joined to pr_runs.

    Filters:
    - pr_run_id in pr_run_ids
    - pr_run.merged=1 and merged_at non-empty
    - defect_links.confirmed=1
    - defect_links.category='escaped'
    - reported_at >= merged_at
    - reported_at < merged_at + window_days

    Date-window filter is applied in Python over ISO strings.
    """
    if not pr_run_ids:
        return []
    # Chunk to stay under SQLite's SQLITE_MAX_VARIABLE_NUMBER (default 999).
    chunk_size = 900
    rows: list[sqlite3.Row] = []
    for i in range(0, len(pr_run_ids), chunk_size):
        chunk = pr_run_ids[i : i + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            "SELECT dl.*, pr.pr_number, pr.pr_url, pr.client_profile, "
            "pr.ticket_id, pr.merged_at "
            "FROM defect_links dl "
            "JOIN pr_runs pr ON pr.id = dl.pr_run_id "
            f"WHERE dl.pr_run_id IN ({placeholders}) "
            "AND pr.merged = 1 "
            "AND pr.merged_at != '' "
            "AND dl.confirmed = 1 "
            "AND dl.category = 'escaped'"
        )
        rows.extend(conn.execute(sql, tuple(chunk)).fetchall())

    out: list[sqlite3.Row] = []
    for r in rows:
        merged_dt = _parse_iso(r["merged_at"])
        reported_dt = _parse_iso(r["reported_at"])
        if merged_dt is None or reported_dt is None:
            continue
        delta_days = (reported_dt - merged_dt).total_seconds() / 86400.0
        if delta_days < 0:
            continue
        if delta_days >= window_days:
            continue
        out.append(r)
    return out


def count_merged_pr_runs_with_escape(
    conn: sqlite3.Connection,
    pr_run_ids: list[int],
    *,
    window_days: int = 30,
) -> int:
    """Return count of distinct pr_run_ids with at least one escaped defect
    in the post-merge window.
    """
    rows = list_confirmed_escaped_defects(
        conn, pr_run_ids, window_days=window_days
    )
    return len({int(r["pr_run_id"]) for r in rows})


def insert_manual_override(
    conn: sqlite3.Connection,
    *,
    override_type: str,
    target_id: str,
    payload_json: str,
    created_by: str = "admin",
) -> int:
    """Insert a manual_overrides audit row. Returns the new row id."""
    now = _now_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO manual_overrides (
                override_type, target_id, payload_json, created_at, created_by
            ) VALUES (?,?,?,?,?)
            """,
            (override_type, target_id, payload_json, now, created_by),
        )
    return int(cur.lastrowid or 0)


def promote_match_to_counted(
    conn: sqlite3.Connection,
    *,
    match_id: int,
    created_by: str = "admin",
) -> bool:
    """Promote a 'suggested' issue_matches row to 'manual' with confidence=1.0.

    Returns True if promoted. Returns False if the match does not exist or
    is not currently matched_by='suggested'.
    """
    row = conn.execute(
        "SELECT id, matched_by FROM issue_matches WHERE id = ?",
        (match_id,),
    ).fetchone()
    if row is None:
        return False
    if row["matched_by"] != "suggested":
        return False
    with conn:
        conn.execute(
            "UPDATE issue_matches SET matched_by = 'manual', confidence = 1.0 "
            "WHERE id = ?",
            (match_id,),
        )
    insert_manual_override(
        conn,
        override_type="promote_match",
        target_id=str(match_id),
        payload_json=json.dumps({"match_id": match_id}),
        created_by=created_by,
    )
    return True


def create_manual_match(
    conn: sqlite3.Connection,
    *,
    human_issue_id: int,
    ai_issue_id: int,
    created_by: str = "admin",
) -> int:
    """Create a manual issue_matches row linking a human-review issue to an
    AI-origin issue on the same PR run.

    Validates that:
    - both review_issues rows exist
    - they share pr_run_id
    - human source == 'human_review'
    - ai source in AI_SOURCES
    - ai.is_valid == 1

    Also writes a manual_overrides audit row. Returns the new match id.
    """
    human = conn.execute(
        "SELECT id, pr_run_id, source, is_valid FROM review_issues WHERE id = ?",
        (human_issue_id,),
    ).fetchone()
    if human is None:
        raise ValueError(f"human_issue_id {human_issue_id} not found")
    ai = conn.execute(
        "SELECT id, pr_run_id, source, is_valid FROM review_issues WHERE id = ?",
        (ai_issue_id,),
    ).fetchone()
    if ai is None:
        raise ValueError(f"ai_issue_id {ai_issue_id} not found")
    if human["pr_run_id"] != ai["pr_run_id"]:
        raise ValueError(
            "human and ai issues must belong to the same pr_run"
        )
    if human["source"] != "human_review":
        raise ValueError(
            f"human_issue_id source must be 'human_review', got "
            f"{human['source']!r}"
        )
    if ai["source"] not in AI_SOURCES:
        raise ValueError(
            f"ai_issue_id source must be one of {AI_SOURCES}, got "
            f"{ai['source']!r}"
        )
    if int(ai["is_valid"]) != 1:
        raise ValueError("ai_issue_id must have is_valid=1")

    # Emulated uniqueness guard — issue_matches has no UNIQUE constraint
    # in v1 schema so we check here to prevent duplicate manual matches.
    existing = conn.execute(
        "SELECT id FROM issue_matches WHERE human_issue_id = ? "
        "AND ai_issue_id = ?",
        (human_issue_id, ai_issue_id),
    ).fetchone()
    if existing is not None:
        raise ValueError(
            f"match already exists between human_issue_id={human_issue_id} "
            f"and ai_issue_id={ai_issue_id}"
        )

    now = _now_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO issue_matches (
                human_issue_id, ai_issue_id, match_type, confidence,
                matched_at, matched_by
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                human_issue_id,
                ai_issue_id,
                "manual",
                1.0,
                now,
                "manual",
            ),
        )
    match_id = int(cur.lastrowid or 0)
    insert_manual_override(
        conn,
        override_type="create_manual_match",
        target_id=str(match_id),
        payload_json=json.dumps(
            {
                "match_id": match_id,
                "human_issue_id": human_issue_id,
                "ai_issue_id": ai_issue_id,
            }
        ),
        created_by=created_by,
    )
    return match_id


def list_defect_links_for_profile(
    conn: sqlite3.Connection,
    client_profile: str,
    *,
    since_iso: str | None = None,
    limit: int = 50,
    confirmed: int | None = None,
    category: str | None = None,
) -> list[sqlite3.Row]:
    """Return defect_links for PRs in the given profile, most recent first.

    Joins defect_links to pr_runs on pr_runs.client_profile. Optional
    since_iso filters on defect_links.reported_at. ``confirmed`` and
    ``category`` are optional SQL-side filters — push them in here
    rather than filtering in Python after LIMIT. Without this, the
    escaped-defects dashboard panel could silently drop escaped rows
    whenever a profile had more recent non-escaped/unconfirmed rows
    within the same window: SQL LIMIT applies first, and the Python
    ``[r for r in rows if confirmed==1 and category=='escaped']``
    filter then saw an empty set.
    """
    clauses = ["pr.client_profile = ?"]
    params: list[object] = [client_profile]
    if since_iso is not None:
        clauses.append("dl.reported_at >= ?")
        params.append(since_iso)
    if confirmed is not None:
        clauses.append("dl.confirmed = ?")
        params.append(int(confirmed))
    if category is not None:
        clauses.append("dl.category = ?")
        params.append(category)
    sql = (
        "SELECT dl.*, pr.pr_number, pr.pr_url, pr.client_profile, "
        "pr.ticket_id, pr.merged_at "
        "FROM defect_links dl "
        "JOIN pr_runs pr ON pr.id = dl.pr_run_id "
        "WHERE " + " AND ".join(clauses) + " "
        "ORDER BY dl.reported_at DESC, dl.id DESC LIMIT ?"
    )
    params.append(int(limit))
    return list(conn.execute(sql, tuple(params)).fetchall())


def record_defect_sweep_heartbeat(
    conn: sqlite3.Connection,
    *,
    client_profile: str,
    swept_through_iso: str,
    created_by: str = "admin",
) -> int:
    """Record a 'defect sweep heartbeat' for a client profile.

    Stored as a manual_overrides row with override_type='defect_sweep_heartbeat',
    target_id=client_profile, payload_json={'swept_through': swept_through_iso}.
    """
    return insert_manual_override(
        conn,
        override_type="defect_sweep_heartbeat",
        target_id=client_profile,
        payload_json=json.dumps({"swept_through": swept_through_iso}),
        created_by=created_by,
    )


def get_latest_defect_sweep_heartbeat(
    conn: sqlite3.Connection,
    client_profile: str,
) -> str | None:
    """Return the most recent swept_through timestamp for the profile."""
    rows = conn.execute(
        "SELECT payload_json FROM manual_overrides "
        "WHERE override_type = 'defect_sweep_heartbeat' AND target_id = ? "
        "ORDER BY id DESC",
        (client_profile,),
    ).fetchall()
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            continue
        value = payload.get("swept_through")
        if isinstance(value, str) and value:
            return value
    return None
