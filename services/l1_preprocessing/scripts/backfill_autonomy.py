"""Phase 0 backfill — import historical PR runs from trace files.

Walks <logs-dir>/*.jsonl, extracts one pr_run per distinct (repo, pr_number)
per ticket, and upserts into the autonomy DB marked backfilled=1. Rows with
unresolvable client_profile are dropped per spec §17 Phase 0 hard rule.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

# Import parent modules
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autonomy_ingest import resolve_client_profile
from autonomy_store import (
    PrRunUpsert,
    ensure_schema,
    open_connection,
    resolve_db_path,
    upsert_pr_run,
)
from config import settings
from tracer import LOGS_DIR, read_trace

logger = structlog.get_logger()

PLACEHOLDER_SHA_PREFIX = "backfill:"
_PR_URL_RE = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")


@dataclass
class BackfillRow:
    ticket_id: str
    pr_number: int
    repo_full_name: str
    pr_url: str
    head_sha: str
    opened_at: str
    merged_at: str
    ticket_type: str
    pipeline_mode: str
    client_profile: str
    merged: int
    escalated: int


@dataclass
class BackfillStats:
    files_scanned: int = 0
    rows_extracted: int = 0
    rows_dropped_no_pr: int = 0
    rows_dropped_no_profile: int = 0
    rows_dropped_out_of_range: int = 0
    rows_written: int = 0
    unresolved_profiles: list[str] = field(default_factory=list)


def parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """Parse a GitHub PR URL into (repo_full_name, pr_number) or None."""
    if not pr_url:
        return None
    m = _PR_URL_RE.match(pr_url.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


def extract_ticket_rows(
    ticket_id: str, entries: list[dict[str, Any]]
) -> list[BackfillRow]:
    """Extract one BackfillRow per distinct pr_url group in the trace.

    Rules:
      - ticket_type := first entry with non-empty 'ticket_type'
      - Group by pr_url; skip groups with unparseable urls
      - opened_at := earliest timestamp of PR-created/updated events for that
        pr_url, falling back to earliest agent_finished timestamp
      - pipeline_mode := from most-recent 'Pipeline complete' for this pr_url,
        or ""
      - merged_at := "" (historical traces have no merge webhook)
      - escalated := 1 if the most-recent agent_finished with a known status
        for this pr_url is "escalated" with no later Pipeline complete, else 0
      - head_sha := f"backfill:{ticket_id}:{pr_number}"
    """
    # ticket_type: first non-empty occurrence
    ticket_type = ""
    for e in entries:
        tt = e.get("ticket_type", "")
        if tt:
            ticket_type = str(tt)
            break

    # Group entries by parseable pr_url
    groups: dict[str, dict[str, Any]] = {}
    for e in entries:
        url = e.get("pr_url", "")
        if not url:
            continue
        parsed = parse_pr_url(str(url))
        if not parsed:
            continue
        repo, pr_number = parsed
        g = groups.setdefault(
            url, {"entries": [], "repo": repo, "pr_number": pr_number}
        )
        g["entries"].append(e)

    rows: list[BackfillRow] = []
    for pr_url, g in groups.items():
        group_entries: list[dict[str, Any]] = g["entries"]

        # PR-created/updated events (source=agent)
        pr_created_ts = sorted(
            str(e.get("timestamp", ""))
            for e in group_entries
            if e.get("source") == "agent"
            and (
                "PR created" in str(e.get("event", ""))
                or "PR updated" in str(e.get("event", ""))
            )
            and e.get("timestamp")
        )
        finished_entries = sorted(
            [
                e
                for e in group_entries
                if e.get("phase") == "completion"
                and e.get("event") == "agent_finished"
                and e.get("timestamp")
            ],
            key=lambda e: str(e.get("timestamp", "")),
        )
        finished_ts = [str(e.get("timestamp", "")) for e in finished_entries]
        opened_at = (
            pr_created_ts[0]
            if pr_created_ts
            else (finished_ts[0] if finished_ts else "")
        )

        # Pipeline complete events for this group
        pipeline_completes = sorted(
            [
                e
                for e in group_entries
                if e.get("source") == "agent"
                and "Pipeline complete" in str(e.get("event", ""))
            ],
            key=lambda e: str(e.get("timestamp", "")),
        )
        pipeline_mode = ""
        if pipeline_completes:
            pipeline_mode = str(pipeline_completes[-1].get("pipeline_mode", ""))

        # escalated: look at most-recent agent_finished with a known status
        escalated = 0
        for fe in reversed(finished_entries):
            status = fe.get("status", "")
            if status not in ("complete", "escalated", "partial"):
                continue
            if status == "escalated":
                fe_ts = str(fe.get("timestamp", ""))
                has_later_complete = any(
                    str(pc.get("timestamp", "")) > fe_ts
                    for pc in pipeline_completes
                )
                if not has_later_complete:
                    escalated = 1
            break

        _, pr_number_int = parse_pr_url(pr_url) or ("", 0)
        rows.append(
            BackfillRow(
                ticket_id=ticket_id,
                pr_number=int(g["pr_number"]),
                repo_full_name=str(g["repo"]),
                pr_url=pr_url,
                head_sha=(
                    f"{PLACEHOLDER_SHA_PREFIX}{ticket_id}:{g['pr_number']}"
                ),
                opened_at=opened_at,
                merged_at="",
                ticket_type=ticket_type,
                pipeline_mode=pipeline_mode,
                client_profile="",  # filled by caller
                merged=0,
                escalated=escalated,
            )
        )
    return rows


def _in_range(opened_at: str, since: str, until: str) -> bool:
    """ISO string prefix comparison. No bounds → True; missing ts → False if bounds set."""
    if not since and not until:
        return True
    if not opened_at:
        return False
    if since and opened_at < since:
        return False
    return not (until and opened_at > until)


def iter_trace_files(logs_dir: Path) -> list[Path]:
    """Return non-empty *.jsonl files directly in logs_dir (no recursion)."""
    if not logs_dir.exists():
        return []
    out: list[Path] = []
    for p in sorted(logs_dir.glob("*.jsonl")):
        if not p.is_file():
            continue
        try:
            if p.stat().st_size == 0:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def _read_trace_file(path: Path) -> list[dict[str, Any]]:
    """Read a trace file directly (not via tracer.read_trace, which uses LOGS_DIR)."""
    import contextlib
    import json

    entries: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                entries.append(json.loads(line))
    return entries


def run_backfill(
    *,
    logs_dir: Path,
    db_path: Path,
    since: str = "",
    until: str = "",
    tickets: list[str] | None = None,
    dry_run: bool = False,
) -> BackfillStats:
    """Walk trace files and upsert backfilled pr_runs rows."""
    stats = BackfillStats()
    conn = None
    if not dry_run:
        conn = open_connection(db_path)
        ensure_schema(conn)
    try:
        for path in iter_trace_files(logs_dir):
            ticket_id = path.stem
            if tickets and ticket_id not in tickets:
                continue
            stats.files_scanned += 1

            # Prefer tracer.read_trace if logs_dir matches LOGS_DIR;
            # otherwise read file directly.
            if logs_dir.resolve() == LOGS_DIR.resolve():
                entries = read_trace(ticket_id)
            else:
                entries = _read_trace_file(path)

            rows = extract_ticket_rows(ticket_id, entries)
            if not rows:
                stats.rows_dropped_no_pr += 1
                continue

            profile, degraded = resolve_client_profile(ticket_id, "")
            if degraded or not profile:
                stats.rows_dropped_no_profile += len(rows)
                stats.unresolved_profiles.append(ticket_id)
                continue

            for r in rows:
                if (since or until) and not _in_range(r.opened_at, since, until):
                    stats.rows_dropped_out_of_range += 1
                    continue
                r.client_profile = profile
                stats.rows_extracted += 1
                if dry_run:
                    logger.info("backfill_would_insert", **asdict(r))
                    continue
                assert conn is not None
                upsert = PrRunUpsert(
                    ticket_id=r.ticket_id,
                    pr_number=r.pr_number,
                    repo_full_name=r.repo_full_name,
                    pr_url=r.pr_url,
                    ticket_type=r.ticket_type,
                    pipeline_mode=r.pipeline_mode,
                    head_sha=r.head_sha,
                    client_profile=r.client_profile,
                    opened_at=r.opened_at,
                    merged_at=r.merged_at,
                    merged=r.merged,
                    escalated=r.escalated,
                    first_pass_accepted=None,  # unknown — excluded from FPA metric
                    backfilled=1,
                )
                upsert_pr_run(conn, upsert)
                stats.rows_written += 1
    finally:
        if conn is not None:
            conn.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 0 backfill — import historical PR runs from trace files."
        ),
    )
    parser.add_argument("--logs-dir", type=Path, default=LOGS_DIR)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument(
        "--since", default="", help="ISO date lower bound for opened_at"
    )
    parser.add_argument(
        "--until", default="", help="ISO date upper bound for opened_at"
    )
    parser.add_argument(
        "--ticket",
        action="append",
        default=[],
        help="Restrict to specific ticket id(s). Repeatable.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    db_path = args.db_path or resolve_db_path(settings.autonomy_db_path)
    tickets: list[str] | None = list(args.ticket) if args.ticket else None

    stats = run_backfill(
        logs_dir=args.logs_dir,
        db_path=db_path,
        since=args.since,
        until=args.until,
        tickets=tickets,
        dry_run=args.dry_run,
    )

    print("Backfill summary:")
    print(f"  files_scanned:              {stats.files_scanned}")
    print(f"  rows_extracted:             {stats.rows_extracted}")
    print(f"  rows_written:               {stats.rows_written}")
    print(f"  rows_dropped_no_pr:         {stats.rows_dropped_no_pr}")
    print(f"  rows_dropped_no_profile:    {stats.rows_dropped_no_profile}")
    print(f"  rows_dropped_out_of_range:  {stats.rows_dropped_out_of_range}")
    if stats.unresolved_profiles:
        print(
            f"  unresolved_profiles: {', '.join(stats.unresolved_profiles)}"
        )
    if args.dry_run:
        print("  (dry-run: no rows written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
