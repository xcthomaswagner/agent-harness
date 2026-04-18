"""Detector 2 — human_issue_cluster.

Surfaces patterns where human reviewers file the same category of
issue on similar files across multiple PRs on the same profile:
cases where AI code review is systematically missing something.

Groups valid ``human_review`` issues in the window by
``(client_profile, platform_profile, category, file_pattern)`` and
emits a proposal when a group has at least ``MIN_CLUSTER_SIZE``
distinct ``pr_run_id`` values. Rows whose profile YAML doesn't
resolve a ``platform_profile`` are dropped — we don't emit
cross-platform lessons.

See docs/self-learning-plan.md §4 for the activation threshold
and scope-key rationale.
"""

from __future__ import annotations

import os
import posixpath
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import structlog

from client_profile import load_profile
from learning_miner.detectors._archive import build_append_section_delta
from learning_miner.detectors.base import CandidateProposal, EvidenceItem

logger = structlog.get_logger()

# Bump VERSION when scan semantics change so outcome measurements
# can distinguish old-semantics vs new-semantics candidates.
NAME = "human_issue_cluster"
VERSION = 1

MIN_CLUSTER_SIZE = 3


@dataclass(frozen=True)
class _ClusterKey:
    client_profile: str
    platform_profile: str
    category: str
    file_pattern: str


def _normalize_category(value: str) -> str:
    """Lowercase + strip; empty collapses to '(unknown)'.

    Pipe characters are replaced with underscores because the
    ``pattern_key`` format is ``<category>|<file_pattern>`` —
    a category containing ``|`` would make ``pattern_key.split("|", 1)``
    in ``recurrence_for`` bind ``lesson_category`` to only the
    prefix, and the SQL ``LOWER(category) = LOWER(?)`` match would
    miss the real rows. Sidecar sources accept free-form category
    values, so defensive normalization is required.
    """
    v = (value or "").strip().lower().replace("|", "_")
    return v if v else "(unknown)"


@lru_cache(maxsize=256)
def _resolve_platform_profile(client_profile: str) -> str | None:
    """Return ``platform_profile`` for a client, or None if unresolvable.

    Cached per-process to avoid re-parsing YAML per row; the runner
    clears the cache at the start of every scan so the L1 service
    picks up profile edits without a restart.
    """
    if not client_profile:
        return None
    profile = load_profile(client_profile)
    if profile is None:
        return None
    return profile.platform_profile or None


def _path_matches_pattern(path: str, pattern: str) -> bool:
    """True when ``path`` fits the pattern ``_derive_file_pattern`` produces.

    Supports the two pattern shapes that detector emits:
    - ``*.<ext>`` — path shares the extension
    - ``<dir>/**`` — path's top-level directory matches
    Empty pattern matches every non-empty path.
    """
    if not path:
        return False
    if not pattern:
        return True
    if pattern.startswith("*."):
        ext = pattern[1:].lower()
        return os.path.splitext(path)[1].lower() == ext
    if pattern.endswith("/**"):
        top = pattern[: -len("/**")]
        normalized = posixpath.normpath(path)
        first = normalized.split("/", 1)[0]
        return first == top
    return False


def _derive_file_pattern(paths: list[str]) -> str:
    """Cheapest-common-denominator glob from a list of file paths.

    Returns ``*.<ext>`` when every (non-empty) path shares a suffix,
    ``<dir>/**`` when every (non-empty) multi-segment path shares
    a top-level directory, or ``''`` when no common pattern fits.
    Empty and ``.``-only paths are ignored.
    """
    exts: set[str] = set()
    tops: set[str] = set()
    for p in paths:
        if not p or p in {".", "./"}:
            continue
        ext = os.path.splitext(p)[1].lower()
        if ext:
            exts.add(ext)
        normalized = posixpath.normpath(p)
        if "/" in normalized:
            tops.add(normalized.split("/", 1)[0])
    if len(exts) == 1:
        (ext,) = exts
        return f"*{ext}"
    if len(tops) == 1:
        (top,) = tops
        if top and top != ".":
            return f"{top}/**"
    return ""


def _build_scope_key(key: _ClusterKey) -> str:
    """``<client>|<platform>|<category>|<file_pattern>``.

    Follows the naming convention in docs/self-learning-plan.md §3.2
    so future detectors can emit consistent scope keys.
    """
    return "|".join(
        [
            key.client_profile,
            key.platform_profile,
            key.category,
            key.file_pattern,
        ]
    )


def _build_pattern_key(category: str, file_pattern: str) -> str:
    """Detector-local pattern key — distinct from scope_key so two
    clients with the same category+file get the same pattern_key
    (useful for cross-profile comparisons) but different scope_keys.
    """
    return f"{category}|{file_pattern}"


def _build_proposed_delta(
    client_profile: str,
    platform_profile: str,
    category: str,
    file_pattern: str,
    frequency: int,
) -> str:
    """Mechanical starter diff; the Markdown drafter refines it post-approval."""
    # scan() drops rows where platform_profile resolves to None, but an
    # explicit guard is safer than ``assert`` — running Python with ``-O``
    # strips asserts, and a stripped empty check here would produce an
    # invalid ``runtime/platform-profiles//CODE_REVIEW_SUPPLEMENT.md``
    # path that the allowlist still accepts (double-slash normalizes)
    # but that points at a nonexistent file.
    if not platform_profile:
        raise ValueError("platform_profile is required for proposed delta")
    target = (
        f"runtime/platform-profiles/{platform_profile}"
        "/CODE_REVIEW_SUPPLEMENT.md"
    )

    pattern_desc = (
        f"category={category}"
        + (f", files matching {file_pattern}" if file_pattern else "")
    )
    rationale = (
        f"Human reviewers filed {frequency} issues matching "
        f"{pattern_desc} across distinct PR runs on "
        f"client_profile={client_profile!r} in the mining window. "
        "AI review did not surface them, so the supplement should add "
        "a check for this pattern."
    )
    after_line = (
        f"- When reviewing code, check for {category} issues"
        + (f" in {file_pattern}" if file_pattern else "")
        + "."
    )
    return build_append_section_delta(
        target_path=target,
        anchor="## Review Checklist",
        after_line=after_line,
        rationale_md=rationale,
    )


class HumanIssueClusterDetector:
    """Detector 2 — see module docstring."""

    name = NAME
    version = VERSION

    def scan(
        self, conn: sqlite3.Connection, window_days: int
    ) -> list[CandidateProposal]:
        cutoff_iso = (
            datetime.now(UTC) - timedelta(days=window_days)
        ).isoformat()

        # Window by pr_runs.opened_at — same column autonomy_metrics uses,
        # so the detector's window agrees with the dashboard's.
        rows = conn.execute(
            """
            SELECT
                ri.id           AS issue_id,
                ri.pr_run_id    AS pr_run_id,
                ri.category     AS category,
                ri.file_path    AS file_path,
                ri.summary      AS summary,
                ri.created_at   AS created_at,
                pr.client_profile AS client_profile,
                pr.ticket_id    AS ticket_id
            FROM review_issues ri
            JOIN pr_runs pr ON pr.id = ri.pr_run_id
            WHERE ri.source = 'human_review'
              AND ri.is_valid = 1
              AND pr.opened_at >= ?
            ORDER BY ri.id
            """,
            (cutoff_iso,),
        ).fetchall()

        if not rows:
            return []

        # First pass: derive the normalized file pattern per
        # (client, category) so all issues in that (client, category)
        # agree on which file_pattern to use. This prevents a single
        # issue with an outlier file path from spinning out into its
        # own micro-cluster.
        paths_per_key: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            platform = _resolve_platform_profile(row["client_profile"])
            if platform is None:
                continue
            category = _normalize_category(row["category"])
            k = (row["client_profile"], category)
            paths_per_key.setdefault(k, []).append(row["file_path"] or "")

        file_patterns: dict[tuple[str, str], str] = {
            k: _derive_file_pattern(paths) for k, paths in paths_per_key.items()
        }

        # Second pass: assign rows to clusters using the shared
        # file_pattern, so outlier file_paths don't spin out into
        # singleton clusters.
        by_cluster: dict[
            _ClusterKey, tuple[set[int], list[sqlite3.Row]]
        ] = {}
        for row in rows:
            platform = _resolve_platform_profile(row["client_profile"])
            if platform is None:
                continue
            category = _normalize_category(row["category"])
            file_pattern = file_patterns.get(
                (row["client_profile"], category), ""
            )
            key = _ClusterKey(
                client_profile=row["client_profile"],
                platform_profile=platform,
                category=category,
                file_pattern=file_pattern,
            )
            pr_run_ids, evidence_rows = by_cluster.setdefault(
                key, (set(), [])
            )
            pr_run_ids.add(int(row["pr_run_id"]))
            evidence_rows.append(row)

        proposals: list[CandidateProposal] = []
        for key, (pr_run_ids, evidence_rows) in by_cluster.items():
            if len(pr_run_ids) < MIN_CLUSTER_SIZE:
                continue
            proposals.append(
                self._build_proposal(key, pr_run_ids, evidence_rows)
            )
        return proposals

    def _build_proposal(
        self,
        key: _ClusterKey,
        pr_run_ids: set[int],
        evidence_rows: list[sqlite3.Row],
    ) -> CandidateProposal:
        pattern_key = _build_pattern_key(key.category, key.file_pattern)
        scope_key = _build_scope_key(key)
        cluster_size = len(pr_run_ids)
        proposed_delta = _build_proposed_delta(
            client_profile=key.client_profile,
            platform_profile=key.platform_profile,
            category=key.category,
            file_pattern=key.file_pattern,
            frequency=cluster_size,
        )
        evidence = self._build_evidence(key, evidence_rows)
        severity = "warn" if cluster_size >= MIN_CLUSTER_SIZE * 2 else "info"
        return CandidateProposal(
            detector_name=NAME,
            detector_version=VERSION,
            pattern_key=pattern_key,
            client_profile=key.client_profile,
            platform_profile=key.platform_profile,
            scope_key=scope_key,
            severity=severity,
            proposed_delta_json=proposed_delta,
            window_frequency=cluster_size,
            evidence=tuple(evidence),
        )

    def recurrence_for(
        self,
        conn: sqlite3.Connection,
        *,
        lesson: sqlite3.Row,
        since_iso: str,
        until_iso: str,
    ) -> int:
        """Count fresh human_review issues matching the lesson's pattern.

        The lesson's ``pattern_key`` is ``<category>|<file_pattern>``.
        We count ``review_issues`` rows with matching category whose
        pr_run falls in the post-merge window, restricted to the
        lesson's client_profile.

        Each row is tested individually against the lesson's
        file_pattern — previously we derived ONE pattern from all rows
        and compared, but a single outlier file_path collapsed the
        derived pattern to ``''`` and the comparison returned 0 even
        when most rows genuinely recurred. Per-row testing counts the
        matching recurrences while ignoring unrelated noise.
        """
        pattern_key = str(lesson["pattern_key"] or "")
        client_profile = str(lesson["client_profile"] or "")
        if "|" not in pattern_key or not client_profile:
            return 0
        lesson_category, lesson_pattern = pattern_key.split("|", 1)
        # Normalize the DB category the same way scan() normalizes at
        # write time: lowercase + replace ``|`` with ``_``. Without the
        # REPLACE, a raw sidecar category like ``"security|sqli"`` in
        # the DB wouldn't match the already-normalized lesson_category
        # ``"security_sqli"`` — recurrence would silently return 0.
        rows = conn.execute(
            """
            SELECT ri.file_path
            FROM review_issues ri
            JOIN pr_runs pr ON pr.id = ri.pr_run_id
            WHERE ri.source = 'human_review'
              AND ri.is_valid = 1
              AND REPLACE(LOWER(COALESCE(ri.category, '')), '|', '_') = LOWER(?)
              AND pr.client_profile = ?
              AND pr.opened_at >= ?
              AND pr.opened_at < ?
            """,
            (lesson_category, client_profile, since_iso, until_iso),
        ).fetchall()
        if not rows:
            return 0
        if not lesson_pattern:
            # Lessons without a specific file pattern can't be
            # file-filtered; count every matching-category row.
            return len(rows)
        return sum(
            1
            for r in rows
            if _path_matches_pattern(
                str(r["file_path"] or ""), lesson_pattern
            )
        )

    def _build_evidence(
        self, key: _ClusterKey, evidence_rows: list[sqlite3.Row]
    ) -> list[EvidenceItem]:
        """One EvidenceItem per issue.

        Encoding the issue id in source_ref lets a trace contribute
        multiple evidence rows without colliding with the
        (lesson_id, trace_id, source_ref) UNIQUE constraint.
        """
        out: list[EvidenceItem] = []
        for row in evidence_rows:
            summary = (row["summary"] or "").strip()
            file_part = row["file_path"] or "(no file)"
            snippet = f"{file_part}: {summary}" if summary else file_part
            out.append(
                EvidenceItem(
                    trace_id=row["ticket_id"] or "",
                    observed_at=row["created_at"] or "",
                    source_ref=f"review_issues#{int(row['issue_id'])}",
                    snippet=snippet,
                    pr_run_id=int(row["pr_run_id"]),
                )
            )
        return out


def build() -> HumanIssueClusterDetector:
    """Factory for the runner / tests."""
    return HumanIssueClusterDetector()
