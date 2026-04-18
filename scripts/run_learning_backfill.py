#!/usr/bin/env python3
"""One-shot backfill of the self-learning miner over historical data.

Writes ``lesson_candidates`` + ``lesson_evidence`` rows so humans
can triage patterns via SQL or the ``/autonomy/learning`` dashboard
before flipping ``LEARNING_MINER_ENABLED=true``. Never touches
runtime files, opens PRs, or calls LLMs.

``--dry-run`` runs against a scratch copy of the DB instead, so
threshold tweaks can be tried without polluting the live store.
"""

from __future__ import annotations

import argparse
import json as json_module
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

# Make the service package importable when running from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_SRC = REPO_ROOT / "services" / "l1_preprocessing"
sys.path.insert(0, str(SERVICE_SRC))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-days",
        type=int,
        default=60,
        help="How far back to look for pattern evidence (default: 60).",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="",
        help=(
            "Optional explicit path to autonomy.db. When omitted, uses "
            "the same resolver as the L1 service (settings.autonomy_db_path "
            "or <repo_root>/data/autonomy.db)."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON summary instead of the human-readable report.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run detectors but roll back any writes. Use this to try "
            "threshold or detector tweaks without polluting the DB."
        ),
    )
    return parser.parse_args()


def _open_conn(db_path: str):
    # Imports inside the function so ``--help`` doesn't require the service env.
    from autonomy_store import ensure_schema, open_connection, resolve_db_path

    path = resolve_db_path(db_path)
    conn = open_connection(path)
    ensure_schema(conn)
    return conn, path


def _open_dry_run_conn(db_path: str) -> tuple[object, Path, Path]:
    """Copy the live DB to a scratch file and return a connection to the copy.

    SAVEPOINT-based rollback doesn't survive ``with conn:`` commits
    in the autonomy_store helpers, so a full copy is simpler. The
    caller closes ``conn`` and deletes ``scratch_path`` when done.
    """
    from autonomy_store import ensure_schema, open_connection, resolve_db_path

    source = resolve_db_path(db_path)
    tmpdir = Path(tempfile.mkdtemp(prefix="learning-backfill-dry-"))
    scratch = tmpdir / "autonomy.db"
    if source.exists():
        shutil.copy2(source, scratch)
        # WAL sidecars carry uncheckpointed writes. Missing sidecars are
        # normal — only copy what exists.
        for sidecar_suffix in ("-wal", "-shm"):
            sidecar = source.with_name(source.name + sidecar_suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, scratch.with_name(scratch.name + sidecar_suffix))
    conn = open_connection(scratch)
    ensure_schema(conn)
    return conn, scratch, source


def _load_detectors():
    """Return fresh instances of every registered production detector.

    Delegates to ``learning_miner.all_production_detectors`` so the
    backfill script and the nightly miner stay in lock-step. Prior
    versions only loaded the ``human_issue_cluster`` detector — a
    silent bug that kept every other detector out of the backfill.
    """
    from learning_miner import all_production_detectors

    return list(all_production_detectors())


def _top_scope_keys(conn, limit: int = 10) -> list[tuple[str, int]]:
    """Return (scope_key, frequency) for the top-frequency candidates."""
    rows = conn.execute(
        "SELECT scope_key, frequency FROM lesson_candidates "
        "ORDER BY frequency DESC, detected_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [(r["scope_key"], int(r["frequency"])) for r in rows]


def _candidates_by_detector(conn) -> Counter:
    rows = conn.execute(
        "SELECT detector_name FROM lesson_candidates"
    ).fetchall()
    return Counter(r["detector_name"] for r in rows)


def _evidence_total(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM lesson_evidence"
    ).fetchone()
    return int(row["n"]) if row else 0


def _redirect_logs_to_stderr() -> None:
    """Route structlog to stderr so stdout stays pure JSON under ``--json``."""
    import structlog

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def main() -> int:
    args = _parse_args()
    _redirect_logs_to_stderr()
    from learning_miner import run_miner

    scratch_dir_to_clean: Path | None = None
    if args.dry_run:
        conn, scratch_path, display_path = _open_dry_run_conn(args.db_path)
        scratch_dir_to_clean = scratch_path.parent
    else:
        conn, display_path = _open_conn(args.db_path)

    try:
        detectors = _load_detectors()
        result = run_miner(conn, detectors, window_days=args.window_days)

        summary: dict[str, object] = {
            "db_path": str(display_path),
            "window_days": args.window_days,
            "dry_run": bool(args.dry_run),
            "total_candidates": result.total_candidates,
            "total_evidence": result.total_evidence,
            "total_failures": result.total_failures,
            "duration_ms": result.total_duration_ms,
            "per_detector": [
                {
                    "name": s.detector_name,
                    "proposals_emitted": s.proposals_emitted,
                    "candidates_inserted_or_updated": (
                        s.candidates_inserted_or_updated
                    ),
                    "evidence_inserted": s.evidence_inserted,
                    "evidence_deduped": s.evidence_deduped,
                    "failed": s.failed,
                    "error": s.error,
                    "duration_ms": s.duration_ms,
                }
                for s in result.per_detector
            ],
            "db_totals": {
                "candidates_by_detector": dict(_candidates_by_detector(conn)),
                "total_evidence_rows": _evidence_total(conn),
            },
            "top_scope_keys": _top_scope_keys(conn),
        }

        if args.json:
            print(json_module.dumps(summary, indent=2, sort_keys=True))
        else:
            _print_human_report(summary)
    finally:
        conn.close()
        if scratch_dir_to_clean is not None:
            # Best-effort cleanup; keep forensic copy if removal fails.
            try:
                shutil.rmtree(scratch_dir_to_clean)
            except OSError:
                pass
    return 0


def _print_human_report(summary: dict[str, object]) -> None:
    db_totals = summary["db_totals"]
    assert isinstance(db_totals, dict)

    print(f"DB path: {summary['db_path']}")
    print(f"Window: last {summary['window_days']} days")
    if summary["dry_run"]:
        print("Mode: DRY RUN (rolled back, no DB writes retained)")
    print()
    print("Per-detector run stats:")
    per_det = summary["per_detector"]
    assert isinstance(per_det, list)
    for s in per_det:
        assert isinstance(s, dict)
        flag = "FAILED" if s["failed"] else "ok"
        print(
            f"  - {s['name']}: "
            f"proposals={s['proposals_emitted']}, "
            f"persisted={s['candidates_inserted_or_updated']}, "
            f"ev+{s['evidence_inserted']}/dup{s['evidence_deduped']}, "
            f"{s['duration_ms']}ms [{flag}]"
        )
        if s["failed"]:
            print(f"      error: {s['error']}")
    print()
    print(
        f"DB totals after run: "
        f"candidates={summary['total_candidates']} "
        f"new/updated this run, "
        f"evidence rows in DB={db_totals['total_evidence_rows']}"
    )
    print("Candidates by detector (all time):")
    by_det = db_totals["candidates_by_detector"]
    assert isinstance(by_det, dict)
    for name, n in sorted(by_det.items()):
        print(f"  - {name}: {n}")
    print()
    print("Top scope keys by frequency:")
    top = summary["top_scope_keys"]
    assert isinstance(top, list)
    if not top:
        print("  (none)")
    for scope, freq in top:
        print(f"  - freq={freq:>3}  {scope}")


if __name__ == "__main__":
    raise SystemExit(main())
