"""Lesson outcomes — post-merge metric measurement for applied lessons.

Two independent steps, run back-to-back by ``run_outcomes()``:

1. **Merge-state poll.** For each lesson that's at ``status='applied'``
   with a real PR URL (dry-runs excluded) and no recorded merge SHA,
   shell out to ``gh pr view --json state,mergeCommit`` to learn
   whether the PR has merged yet. When merged, record the merge
   commit SHA on the candidate row — outcome measurement uses the
   merge timestamp as the pivot for pre/post windows.

2. **Outcome measurement.** For each lesson whose PR has been merged
   for at least ``settings.learning_outcomes_window_days``:

   - Compute pre/post FPA + escape + catch rates for the lesson's
     client_profile over symmetric windows either side of merge.
   - Run the lesson's detector against the post-merge window and
     count how many instances of the same pattern still showed up
     (``pattern_recurrence_count`` — Tier-1 is a placeholder, Phase
     F reruns the detector).
   - ``git log`` the edited skill file post-merge for commits by
     an author that is NOT xcagentrockwell AND whose patch touches
     the lesson's anchor. Non-zero = ``Verdict.HUMAN_REEDIT``, the
     direct "this lesson was wrong" signal that trumps metric
     verdicts.

Both steps are gated by ``settings.learning_outcomes_enabled``.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import structlog

from autonomy_store import (
    LessonOutcomeInsert,
    autonomy_conn,
    count_merged_pr_runs_with_escape,
    get_latest_outcome,
    insert_lesson_outcome,
    list_applied_lessons,
    list_pr_runs,
    set_lesson_merged_commit_sha,
)
from config import settings
from redaction import redact_token_urls

from ._subprocess import build_env, run_bin

logger = structlog.get_logger()


# Verdict categorisation thresholds. An absolute FPA delta inside
# ``_METRIC_EPSILON`` is treated as "no change" — noise in small-sample
# metrics shouldn't flip the verdict. Escape rate improvements are
# measured as decreases, not increases.
_METRIC_EPSILON = 0.02

_AGENT_EMAIL_LOWER = "xcagent.rockwell@xcentium.com"


class Verdict(StrEnum):
    """Stable identifier for outcome verdicts.

    Shared across outcomes.py (write), autonomy_store (schema
    docstring), and learning_dashboard (badge mapping) so there's
    one source of truth.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    NO_CHANGE = "no_change"
    REGRESSED = "regressed"
    HUMAN_REEDIT = "human_reedit"


@dataclass
class OutcomesRunStats:
    """Accounting for one ``run_outcomes`` invocation."""

    applied_lessons_seen: int = 0
    merge_polls_attempted: int = 0
    merge_polls_resolved: int = 0
    outcomes_measured: int = 0
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


def run_outcomes(*, repo_root: Path | None = None) -> OutcomesRunStats:
    """Single outcomes-job pass. Intended to be run ~daily.

    ``repo_root`` override exists for tests; production clones the
    harness repo into a single scratch directory per invocation and
    reuses it for every lesson's ``git log`` scan. One ``git clone``
    per tick instead of one per lesson.
    """
    stats = OutcomesRunStats()
    start = time.perf_counter()
    scratch_root: Path | None = None
    cleanup_scratch = False
    try:
        with autonomy_conn() as conn:
            applied = list_applied_lessons(
                conn, exclude_terminal_verdicts=True
            )
        stats.applied_lessons_seen = len(applied)
        if not applied:
            return stats

        scratch_root, cleanup_scratch = _prepare_scratch_root(repo_root)
        for lesson in applied:
            _process_one_lesson(lesson, stats, scratch_root=scratch_root)
    except Exception as exc:
        logger.exception("learning_outcomes_run_failed")
        stats.errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if cleanup_scratch and scratch_root is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(scratch_root.parent)
        stats.duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "learning_outcomes_run",
            applied=stats.applied_lessons_seen,
            merge_polled=stats.merge_polls_attempted,
            merge_resolved=stats.merge_polls_resolved,
            measured=stats.outcomes_measured,
            errors=len(stats.errors),
            duration_ms=stats.duration_ms,
        )
    return stats


def _prepare_scratch_root(
    repo_root: Path | None,
) -> tuple[Path | None, bool]:
    """Return ``(scratch, cleanup_owned)``.

    If the caller supplied ``repo_root`` (tests), we use it verbatim
    and don't own cleanup. Otherwise we mkdtemp + clone once, and the
    caller removes the parent on exit.
    """
    if repo_root is not None:
        return repo_root, False
    url = settings.learning_harness_repo_url
    if not url:
        return None, False
    scratch = Path(
        tempfile.mkdtemp(prefix="learning-outcomes-")
    ) / "harness"
    env = build_env()
    proc = run_bin(
        "git",
        ["clone", "--depth", "50", url, str(scratch)],
        timeout=120,
        env=env,
    )
    if proc.returncode != 0:
        logger.info(
            "learning_outcomes_clone_failed",
            stderr=redact_token_urls((proc.stderr or "")[-200:]),
        )
        with contextlib.suppress(OSError):
            shutil.rmtree(scratch.parent)
        return None, False
    return scratch, True


def _process_one_lesson(
    lesson: sqlite3.Row,
    stats: OutcomesRunStats,
    *,
    scratch_root: Path | None,
) -> None:
    """Dispatch: poll merge state first, then measure if window has elapsed."""
    lesson_id = str(lesson["lesson_id"])
    pr_url = str(lesson["pr_url"] or "")
    merged_commit_sha = str(lesson["merged_commit_sha"] or "")

    if not pr_url:
        # Dry-run or some other applied-without-PR state; nothing to
        # measure.
        return

    if not merged_commit_sha:
        stats.merge_polls_attempted += 1
        merge_info = _poll_merge_state(pr_url)
        if merge_info is None:
            return
        stats.merge_polls_resolved += 1
        with autonomy_conn() as conn:
            try:
                set_lesson_merged_commit_sha(
                    conn, lesson_id, merge_info.commit_sha
                )
            except sqlite3.DatabaseError as exc:
                stats.errors.append(
                    f"merge-state write failed for {lesson_id}: {exc}"
                )
        # Poll resolves the sha; measurement waits for the window.
        return

    if not _outcome_window_ready(lesson, merged_commit_sha):
        return

    if _outcome_already_recorded(lesson_id):
        return

    try:
        _measure_lesson(
            lesson, merged_commit_sha, scratch_root=scratch_root
        )
        stats.outcomes_measured += 1
    except Exception as exc:
        logger.exception(
            "learning_outcomes_measure_failed", lesson_id=lesson_id
        )
        stats.errors.append(f"{lesson_id}: {exc}")


# ---------------------------------------------------------------------------
# Merge-state poll (via `gh pr view`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MergeInfo:
    commit_sha: str
    merged_at: str


def _poll_merge_state(pr_url: str) -> _MergeInfo | None:
    """Return merge info for ``pr_url`` when MERGED, else None.

    Runs ``gh pr view <url> --json state,mergeCommit,mergedAt``.
    Short-circuits on non-zero exit (PR doesn't exist, auth failure,
    network blip). Any failure is logged and treated as "not merged
    yet" — the next scheduler tick will retry.
    """
    proc = run_bin(
        "gh",
        [
            "pr", "view", pr_url,
            "--json", "state,mergeCommit,mergedAt",
        ],
        timeout=30,
        env=build_env(),
    )
    if proc.returncode != 0:
        logger.info(
            "learning_outcomes_merge_poll_failed",
            pr_url=pr_url,
            stderr=redact_token_urls((proc.stderr or "")[-200:]),
        )
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if str(payload.get("state", "")).upper() != "MERGED":
        return None
    merge_commit = payload.get("mergeCommit") or {}
    sha = str(merge_commit.get("oid") or "")
    merged_at = str(payload.get("mergedAt") or "")
    if not sha:
        return None
    return _MergeInfo(commit_sha=sha, merged_at=merged_at)


# ---------------------------------------------------------------------------
# Outcome measurement
# ---------------------------------------------------------------------------


def _outcome_window_ready(
    lesson: sqlite3.Row, merged_commit_sha: str
) -> bool:
    """Return True when merge + window_days have elapsed.

    The pivot for pre/post is ``lesson_candidates.updated_at`` at the
    time the merge-state was recorded — the merged_commit_sha write
    bumps updated_at, so that column tracks "when did we learn this
    merged." Close-enough for the purposes of windowing.
    """
    if not merged_commit_sha:
        return False
    updated_at = str(lesson["updated_at"] or "")
    if not updated_at:
        return False
    try:
        updated_dt = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    if updated_dt.tzinfo is None:
        updated_dt = updated_dt.replace(tzinfo=UTC)
    age_days = (datetime.now(UTC) - updated_dt).days
    return age_days >= settings.learning_outcomes_window_days


def _outcome_already_recorded(lesson_id: str) -> bool:
    """Any existing outcome for this lesson blocks re-measurement.

    Tier-1 records one outcome per lesson. Phase F's re-measurement
    loop will tighten this (e.g. compare ``measured_at`` against the
    configured window_days).
    """
    with autonomy_conn() as conn:
        return get_latest_outcome(conn, lesson_id) is not None


def _measure_lesson(
    lesson: sqlite3.Row,
    merged_commit_sha: str,
    *,
    scratch_root: Path | None,
) -> None:
    """Compute + write a ``lesson_outcomes`` row for a merged lesson."""
    lesson_id = str(lesson["lesson_id"])
    client_profile = str(lesson["client_profile"] or "")
    window_days = int(settings.learning_outcomes_window_days)

    # Pivot: the updated_at that captured the merge-state write.
    pivot_iso = str(lesson["updated_at"] or _now_iso())
    pivot_dt = datetime.fromisoformat(pivot_iso)
    if pivot_dt.tzinfo is None:
        pivot_dt = pivot_dt.replace(tzinfo=UTC)

    pre_cut = (pivot_dt - timedelta(days=window_days)).isoformat()
    post_cut = (pivot_dt + timedelta(days=window_days)).isoformat()

    with autonomy_conn() as conn:
        pre, post = _pre_post_metrics(
            conn,
            client_profile=client_profile,
            pre_cut=pre_cut,
            pivot_iso=pivot_iso,
            post_cut=post_cut,
        )
        pattern_recurrence = _pattern_recurrence(
            conn,
            lesson=lesson,
            since_iso=pivot_iso,
            until_iso=post_cut,
        )

    human_reedit_count, human_reedit_refs = _detect_human_reedits(
        lesson=lesson,
        merged_commit_sha=merged_commit_sha,
        scratch_root=scratch_root,
    )

    verdict = _classify_verdict(
        pre=pre,
        post=post,
        pattern_recurrence=pattern_recurrence,
        human_reedit_count=human_reedit_count,
    )

    payload = LessonOutcomeInsert(
        lesson_id=lesson_id,
        measured_at=_now_iso(),
        window_days=window_days,
        pre_fpa=pre["fpa"],
        post_fpa=post["fpa"],
        pre_escape_rate=pre["escape_rate"],
        post_escape_rate=post["escape_rate"],
        pre_catch_rate=pre["catch_rate"],
        post_catch_rate=post["catch_rate"],
        pattern_recurrence_count=pattern_recurrence,
        human_reedit_count=human_reedit_count,
        human_reedit_refs=json.dumps(human_reedit_refs, sort_keys=True),
        verdict=verdict.value,
    )
    with autonomy_conn() as conn:
        insert_lesson_outcome(conn, payload)
    logger.info(
        "learning_outcomes_recorded",
        lesson_id=lesson_id,
        verdict=verdict.value,
        human_reedit_count=human_reedit_count,
    )


def _null_metrics() -> dict[str, float | None]:
    return {"fpa": None, "escape_rate": None, "catch_rate": None}


def _pre_post_metrics(
    conn: sqlite3.Connection,
    *,
    client_profile: str,
    pre_cut: str,
    pivot_iso: str,
    post_cut: str,
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Return (pre-window metrics, post-window metrics)."""
    pre = _scoped_metrics(
        conn,
        client_profile=client_profile,
        since_iso=pre_cut,
        until_iso=pivot_iso,
    )
    post = _scoped_metrics(
        conn,
        client_profile=client_profile,
        since_iso=pivot_iso,
        until_iso=post_cut,
    )
    return pre, post


def _pattern_recurrence(
    conn: sqlite3.Connection,
    *,
    lesson: sqlite3.Row,
    since_iso: str,
    until_iso: str,
) -> int:
    """Ask the lesson's detector to count post-merge pattern hits.

    Detectors without a ``recurrence_for`` implementation contribute 0
    — see ``count_pattern_recurrence`` in detectors.base. Unknown
    detectors (e.g. deleted after a lesson was applied) likewise
    contribute 0 rather than blocking outcomes measurement.
    """
    detector_name = str(lesson["detector_name"] or "")
    if not detector_name:
        return 0
    # Inline imports: learning_miner/__init__.py pulls in
    # runner.py which transitively imports outcomes.py; keeping these
    # deferred avoids a circular-import at module load.
    from learning_miner import get_detector
    from learning_miner.detectors.base import count_pattern_recurrence

    detector = get_detector(detector_name)
    if detector is None:
        return 0
    return count_pattern_recurrence(
        detector,
        conn,
        lesson=lesson,
        since_iso=since_iso,
        until_iso=until_iso,
    )


def _scoped_metrics(
    conn: sqlite3.Connection,
    *,
    client_profile: str,
    since_iso: str,
    until_iso: str,
) -> dict[str, float | None]:
    """Compute fpa/escape/catch rates for one ``[since, until)`` window.

    Mirrors the three metrics in ``autonomy_metrics.compute_profile_metrics``
    but supports an explicit window upper bound. Returns ``None`` for
    any metric whose denominator is zero (we don't want "0/0" to look
    like a regression in the classifier).
    """
    if not client_profile:
        return _null_metrics()

    rows = list_pr_runs(
        conn,
        client_profile=client_profile,
        since_iso=since_iso,
        until_iso=until_iso,
    )
    live_rows = [r for r in rows if not int(r["backfilled"])]
    live_count = len(live_rows)
    fpa = (
        round(
            sum(1 for r in live_rows if int(r["first_pass_accepted"]) == 1)
            / live_count,
            3,
        )
        if live_count
        else None
    )
    merged_ids = [
        int(r["id"]) for r in rows if int(r["merged"]) == 1 and r["merged_at"]
    ]
    escape_rate: float | None
    if merged_ids:
        escaped = count_merged_pr_runs_with_escape(
            conn, merged_ids, window_days=30
        )
        escape_rate = round(escaped / len(merged_ids), 3)
    else:
        escape_rate = None

    # _count_human_issues_and_matches is deliberately reused here —
    # it's the same math autonomy_metrics uses for the catch-rate
    # display, just scoped to a different pr-id list. A shared
    # non-private helper would be better; deferred to Phase F along
    # with the metric-refactor.
    from autonomy_metrics import _count_human_issues_and_matches

    pr_ids = [int(r["id"]) for r in rows]
    h_count, matched = _count_human_issues_and_matches(conn, pr_ids)
    catch_rate = round(matched / h_count, 3) if h_count else None

    return {
        "fpa": fpa,
        "escape_rate": escape_rate,
        "catch_rate": catch_rate,
    }


def _classify_verdict(
    *,
    pre: dict[str, float | None],
    post: dict[str, float | None],
    pattern_recurrence: int,
    human_reedit_count: int,
) -> Verdict:
    """Categorise an outcome using a priority-ordered rule chain.

    Rules evaluate in order; the first match wins. A ``HUMAN_REEDIT``
    outranks any metric signal because it's a direct "this lesson was
    wrong" vote. ``PENDING`` fires only when both windows have no
    samples at all (small-deployment / cold-start case).
    """
    if human_reedit_count > 0:
        return Verdict.HUMAN_REEDIT
    if pre["fpa"] is None and post["fpa"] is None:
        return Verdict.PENDING

    # FPA: higher is better.
    fpa_delta = _delta(post["fpa"], pre["fpa"])
    # Escape rate: lower is better, so flip the sign for apples-to-apples.
    escape_delta = _delta(pre["escape_rate"], post["escape_rate"])
    # Catch rate: higher is better.
    catch_delta = _delta(post["catch_rate"], pre["catch_rate"])

    aggregate = fpa_delta + escape_delta + catch_delta

    if pattern_recurrence >= 3 or aggregate < -_METRIC_EPSILON:
        return Verdict.REGRESSED
    if aggregate > _METRIC_EPSILON:
        return Verdict.CONFIRMED
    return Verdict.NO_CHANGE


def _delta(newer: float | None, older: float | None) -> float:
    """Signed delta, treating missing values as zero change."""
    if newer is None or older is None:
        return 0.0
    return newer - older


# ---------------------------------------------------------------------------
# Human-reedit detection
# ---------------------------------------------------------------------------


def _detect_human_reedits(
    *,
    lesson: sqlite3.Row,
    merged_commit_sha: str,
    scratch_root: Path | None,
) -> tuple[int, list[dict[str, str]]]:
    """Count commits touching the lesson's edited files authored by
    someone other than the agent, post-merge.

    The scratch root is cloned once per ``run_outcomes`` invocation —
    this function just ``git log``s each edited file within it.
    Returns (count, refs) where refs is a truncated list of
    ``{sha, author, committed_at, message}`` dicts.
    """
    parsed = _lesson_edited_paths(lesson)
    if not parsed or scratch_root is None:
        return 0, []

    env = build_env()
    refs: list[dict[str, str]] = []
    count = 0
    for rel in parsed:
        proc = run_bin(
            "git",
            [
                "log",
                f"{merged_commit_sha}..HEAD",
                "-n", "100",
                "--pretty=format:%H%x09%ae%x09%an%x09%cI%x09%s",
                "--", rel,
            ],
            cwd=scratch_root,
            timeout=30,
            env=env,
        )
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            sha, email, name, committed_at, message = parts[:5]
            if email.lower() == _AGENT_EMAIL_LOWER:
                continue
            count += 1
            if len(refs) < 10:
                refs.append({
                    "sha": sha,
                    "author": f"{name} <{email}>",
                    "committed_at": committed_at,
                    "message": message[:200],
                })
    return count, refs


def _lesson_edited_paths(lesson: sqlite3.Row) -> list[str]:
    """Pull the edited file paths out of ``proposed_delta_json``.

    Phase D's ``_merge_diff_into_delta`` stamps ``unified_diff`` onto
    the JSON blob; we parse ``+++ b/<path>`` lines to know which
    files the merge changed. Falls back to ``target_path`` when the
    delta doesn't carry a full diff (e.g. pre-Phase-D rows).
    """
    raw = lesson["proposed_delta_json"] or ""
    try:
        obj = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return []
    if not isinstance(obj, dict):
        return []
    diff = str(obj.get("unified_diff") or "")
    if not diff:
        target = str(obj.get("target_path") or "")
        return [target] if target else []

    # Delegated to pr_opener's parser so the two flows can't drift.
    from .pr_opener import _edited_paths_from_diff
    return _edited_paths_from_diff(diff)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
