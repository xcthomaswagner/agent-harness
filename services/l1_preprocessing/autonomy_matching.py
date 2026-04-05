"""Issue matcher for autonomy metrics dashboard.

Matches human_review issues against AI-emitted issues (ai_review, judge, qa)
on the same pr_run using a four-tier priority scheme. See spec §13.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher

import structlog

from autonomy_store import (
    insert_issue_match,
    list_issue_matches_for_human,
    list_review_issues_by_pr_run,
)

logger = structlog.get_logger()

AI_SOURCES = ("ai_review", "judge", "qa")

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class MatchResult:
    """Result of matching a single human issue to the best AI issue."""

    ai_issue_id: int
    match_type: str  # 'line_overlap' | 'category_summary' | 'summary_moderate' | 'ac_ref'
    confidence: float  # 1.0 | 0.9 | 0.8 | 0.7


def normalize_summary(s: str) -> str:
    """Lowercase, strip punctuation (keep alphanumerics + whitespace),
    collapse multiple whitespace to single space, trim."""
    if not s:
        return ""
    lowered = s.lower()
    no_punct = _PUNCT_RE.sub(" ", lowered)
    collapsed = _WS_RE.sub(" ", no_punct)
    return collapsed.strip()


def similarity(a: str, b: str) -> float:
    """Return SequenceMatcher ratio on normalized strings.
    Returns 0.0 if either (normalized) string is empty."""
    na = normalize_summary(a)
    nb = normalize_summary(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _lines_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Two closed ranges overlap when a_start <= b_end AND b_start <= a_end."""
    return a_start <= b_end and b_start <= a_end


def _has_line_anchor(line_start: int, line_end: int) -> bool:
    return not (line_start == 0 and line_end == 0)


def _fetch_issue(conn: sqlite3.Connection, issue_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM review_issues WHERE id = ?", (issue_id,)
    ).fetchone()
    return row


def match_human_issue(
    conn: sqlite3.Connection, human_issue_id: int
) -> MatchResult | None:
    """Find the best matching AI issue for a given human issue.

    Returns MatchResult for the highest-confidence match found, or None.
    Tie-break: higher confidence wins; on equal confidence, smaller ai_issue_id
    wins (earlier finding).
    """
    human = _fetch_issue(conn, human_issue_id)
    if human is None:
        return None

    pr_run_id = human["pr_run_id"]
    all_issues = list_review_issues_by_pr_run(conn, pr_run_id)
    ai_issues = [
        r for r in all_issues if r["source"] in AI_SOURCES and r["is_valid"] == 1
    ]

    # best = (confidence, tier_rank, ai_issue_id, match_type)
    best: tuple[float, int, int, str] | None = None

    h_file = human["file_path"]
    h_ls = human["line_start"]
    h_le = human["line_end"]
    h_category = human["category"]
    h_summary = human["summary"]
    h_ac = human["acceptance_criterion_ref"]
    h_has_anchor = _has_line_anchor(h_ls, h_le)

    for ai in ai_issues:
        ai_id = ai["id"]
        ai_file = ai["file_path"]
        ai_ls = ai["line_start"]
        ai_le = ai["line_end"]
        ai_category = ai["category"]
        ai_summary = ai["summary"]
        ai_ac = ai["acceptance_criterion_ref"]

        candidate: tuple[float, int, int, str] | None = None

        # Tier 1: same file + overlapping line range
        if (
            h_file
            and ai_file == h_file
            and h_has_anchor
            and _has_line_anchor(ai_ls, ai_le)
            and _lines_overlap(h_ls, h_le, ai_ls, ai_le)
        ):
            candidate = (1.0, 1, ai_id, "line_overlap")
        else:
            # Tier 2: same file + same category + highly similar summary
            if (
                h_file
                and ai_file == h_file
                and h_category
                and ai_category == h_category
                and similarity(h_summary, ai_summary) >= 0.75
            ):
                candidate = (0.9, 2, ai_id, "category_summary")
            elif h_file and ai_file == h_file:
                # Tier 3: same file + moderate similarity
                sim = similarity(h_summary, ai_summary)
                if 0.55 <= sim < 0.75:
                    candidate = (0.8, 3, ai_id, "summary_moderate")

            # Tier 4: same AC ref (check even if tier 2/3 didn't match for
            # this ai, but only if no tier 1-3 candidate was set above)
            if candidate is None and h_ac and ai_ac == h_ac:
                candidate = (0.7, 4, ai_id, "ac_ref")

        if candidate is None:
            continue

        if best is None:
            best = candidate
        else:
            # higher confidence wins; tie → smaller ai_issue_id
            if candidate[0] > best[0] or (candidate[0] == best[0] and candidate[2] < best[2]):
                best = candidate

    if best is None:
        return None
    return MatchResult(ai_issue_id=best[2], match_type=best[3], confidence=best[0])


def match_human_issues_for_pr_run(
    conn: sqlite3.Connection, pr_run_id: int
) -> dict[str, int]:
    """Match all unmatched human_review issues for a pr_run.

    Writes to issue_matches via insert_issue_match. Tier 1-3 → matched_by=
    'system'. Tier 4 → matched_by='suggested'. Skips human issues that
    already have any row in issue_matches.

    Returns {'auto_matched': n, 'suggested': m, 'unmatched': k}.
    """
    summary = {"auto_matched": 0, "suggested": 0, "unmatched": 0}

    humans = list_review_issues_by_pr_run(conn, pr_run_id, source="human_review")
    for h in humans:
        if h["is_valid"] != 1:
            continue
        existing = list_issue_matches_for_human(conn, h["id"])
        if existing:
            continue

        result = match_human_issue(conn, h["id"])
        if result is None:
            summary["unmatched"] += 1
            continue

        matched_by = "suggested" if result.confidence < 0.8 else "system"
        insert_issue_match(
            conn,
            human_issue_id=h["id"],
            ai_issue_id=result.ai_issue_id,
            match_type=result.match_type,
            confidence=result.confidence,
            matched_by=matched_by,
        )
        if result.confidence >= 0.8:
            summary["auto_matched"] += 1
        else:
            summary["suggested"] += 1

    logger.info(
        "autonomy_matcher_batch_complete",
        pr_run_id=pr_run_id,
        auto_matched=summary["auto_matched"],
        suggested=summary["suggested"],
        unmatched=summary["unmatched"],
    )
    return summary
