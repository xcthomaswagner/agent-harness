"""Ingest run-reflector retrospective.json files into lesson candidates.

The ``run-reflector`` agent runs at the end of every pipeline execution
and writes ``.harness/logs/retrospective.{md,json}``. The spawn-team
flow archives those files alongside the other trace artifacts. This
module walks a list of search roots, reads every ``retrospective.json``
underneath, validates the canonical schema (see
``runtime/skills/run-reflection/SKILL.md``), and returns a list of
``CandidateProposal`` objects the runner can persist.

The module is read-only — it does NOT write to the DB. The caller
(``run_miner``) is responsible for persistence so detectors and
reflector rows flow through the same upsert + redaction pipeline.

Gating rules:

* ``status != "ok"``  → skipped with an info log (failed reflectors
  shouldn't contribute noise).
* malformed JSON     → skipped with a warning log.
* unresolvable ``platform_profile`` → row skipped, same rule the
  existing detectors follow.
* Missing required keys (``ticket_id``, ``schema_version``,
  ``lesson_candidates``) → skipped with warning log.

See docs/self-learning-plan.md §4.4 for where this sits in the
overall detector graph.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import structlog

from learning_miner.detectors.base import CandidateProposal, EvidenceItem
from learning_miner.detectors.human_issue_cluster import (
    _resolve_platform_profile,
)

logger = structlog.get_logger()

DETECTOR_NAME = "run_reflector"
DETECTOR_VERSION = 1
RETROSPECTIVE_FILENAME = "retrospective.json"

# Accept both the legacy "warn" spelling (detector convention) and the
# "warning" spelling the skill uses. The runner normalizes here so the
# downstream rendering + outcomes logic sees just {info, warn, critical}.
_SEVERITY_MAP = {
    "critical": "critical",
    "warn": "warn",
    "warning": "warn",
    "info": "info",
}


def ingest_retrospectives(
    search_roots: Iterable[Path],
) -> list[CandidateProposal]:
    """Walk ``search_roots`` for retrospective.json files → proposals.

    Duplicate files (same canonical resolved path visited twice) are
    loaded once. An unreadable root (missing directory, permission
    error) is logged and skipped — one bad path doesn't sink the rest.
    Malformed individual files are logged and skipped for the same
    reason.

    Returns an ordered list following walk order; the runner dedupes
    via the existing upsert key ``(detector_name, pattern_key, scope_key)``
    so ordering only affects evidence ordering in the DB, which doesn't
    matter semantically.
    """
    proposals: list[CandidateProposal] = []
    seen_paths: set[str] = set()
    for root in search_roots:
        try:
            for path in _walk_retrospectives(root):
                canonical = _canonical_key(path)
                if canonical in seen_paths:
                    continue
                seen_paths.add(canonical)
                proposal = _load_one(path)
                if proposal is not None:
                    proposals.extend(proposal)
        except OSError as exc:
            logger.warning(
                "run_reflector_search_root_failed",
                root=str(root),
                error=f"{type(exc).__name__}: {exc}",
            )
    return proposals


def _walk_retrospectives(root: Path) -> Iterable[Path]:
    """Yield every retrospective.json file under ``root``.

    Missing roots yield nothing rather than raising — we're surveying
    optional trace archives, not required input.
    """
    if not root.exists() or not root.is_dir():
        return
    # Path.rglob handles the recursion and returns a generator — no need
    # to materialize the full list. Skip if the glob itself errors.
    try:
        yield from root.rglob(RETROSPECTIVE_FILENAME)
    except OSError as exc:
        logger.warning(
            "run_reflector_rglob_failed",
            root=str(root),
            error=f"{type(exc).__name__}: {exc}",
        )


def _canonical_key(path: Path) -> str:
    """Canonical form of ``path`` for dedup, resilient to symlinks."""
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _load_one(path: Path) -> list[CandidateProposal] | None:
    """Parse one retrospective.json → list of proposals (or None on skip)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(
            "run_reflector_read_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "run_reflector_json_decode_failed",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    if not isinstance(doc, dict):
        logger.warning(
            "run_reflector_doc_not_object",
            path=str(path),
            actual_type=type(doc).__name__,
        )
        return None
    return _proposals_from_doc(doc, path)


def _proposals_from_doc(
    doc: dict[str, Any], source_path: Path
) -> list[CandidateProposal] | None:
    """Validate the canonical schema → CandidateProposals.

    Returns None when the document should be skipped entirely
    (status=failed, bad shape, missing ticket). Returns [] when the
    document is valid but has no candidates.
    """
    schema_version = doc.get("schema_version")
    if schema_version != 1:
        logger.warning(
            "run_reflector_unknown_schema_version",
            path=str(source_path),
            schema_version=schema_version,
        )
        return None

    status = str(doc.get("status") or "")
    if status != "ok":
        # Failed retrospectives do not contribute lesson rows. This is
        # the documented behavior in run-reflection/SKILL.md.
        logger.info(
            "run_reflector_skipped_non_ok",
            path=str(source_path),
            status=status,
        )
        return None

    ticket_id = str(doc.get("ticket_id") or "")
    if not ticket_id:
        logger.warning(
            "run_reflector_missing_ticket_id",
            path=str(source_path),
        )
        return None

    candidates_raw = doc.get("lesson_candidates") or []
    if not isinstance(candidates_raw, list):
        logger.warning(
            "run_reflector_candidates_not_list",
            path=str(source_path),
            actual_type=type(candidates_raw).__name__,
        )
        return None

    trace_id = str(doc.get("trace_id") or "")
    observed_at = str(doc.get("generated_at") or "")

    proposals: list[CandidateProposal] = []
    for idx, c in enumerate(candidates_raw):
        proposal = _one_candidate_to_proposal(
            c,
            source_path=source_path,
            ticket_id=ticket_id,
            trace_id=trace_id,
            observed_at=observed_at,
            candidate_index=idx,
        )
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def _one_candidate_to_proposal(
    candidate: Any,
    *,
    source_path: Path,
    ticket_id: str,
    trace_id: str,
    observed_at: str,
    candidate_index: int,
) -> CandidateProposal | None:
    """Validate and convert one candidate dict."""
    if not isinstance(candidate, dict):
        logger.warning(
            "run_reflector_candidate_not_object",
            path=str(source_path),
            index=candidate_index,
        )
        return None

    pattern_key = str(candidate.get("pattern_key") or "").strip()
    if not pattern_key:
        logger.warning(
            "run_reflector_candidate_missing_pattern_key",
            path=str(source_path),
            index=candidate_index,
        )
        return None

    client_profile = str(candidate.get("client_profile") or "").strip()
    platform_profile_from_candidate = str(
        candidate.get("platform_profile") or ""
    ).strip()

    # Verify the client_profile resolves through the existing loader.
    # Unresolvable profiles mean the candidate would target a platform
    # supplement the drafter can't locate; drop to match the detector
    # convention (human_issue_cluster and mcp_drift both drop the row
    # when _resolve_platform_profile returns None).
    resolved_platform = _resolve_platform_profile(client_profile)
    if resolved_platform is None:
        logger.warning(
            "run_reflector_candidate_client_profile_unresolved",
            path=str(source_path),
            index=candidate_index,
            client_profile=client_profile,
        )
        return None
    # Use the resolver's answer as the authoritative platform. If the
    # candidate also declared a platform_profile and it disagrees, reject
    # — a reflector pinning the wrong platform is signal of confusion,
    # not something we want to propagate.
    if (
        platform_profile_from_candidate
        and platform_profile_from_candidate != resolved_platform
    ):
        logger.warning(
            "run_reflector_candidate_platform_mismatch",
            path=str(source_path),
            index=candidate_index,
            client_profile=client_profile,
            platform_from_candidate=platform_profile_from_candidate,
            platform_resolved=resolved_platform,
        )
        return None
    platform_profile = resolved_platform

    scope_key = str(candidate.get("scope_key") or "").strip()
    if not scope_key:
        # Mirror the detector convention so the lesson_id hashes cleanly.
        scope_key = (
            f"{client_profile}|{platform_profile}|{pattern_key}|{ticket_id}"
        )

    severity_raw = str(candidate.get("severity") or "info").lower().strip()
    severity = _SEVERITY_MAP.get(severity_raw, "info")

    proposed_delta_json = _coerce_proposed_delta(
        candidate.get("proposed_delta_json"),
        pattern_key=pattern_key,
    )

    evidence = _coerce_evidence(
        candidate.get("evidence_refs"),
        source_path=source_path,
        trace_id=trace_id or ticket_id,
        observed_at=observed_at,
    )

    return CandidateProposal(
        detector_name=DETECTOR_NAME,
        detector_version=DETECTOR_VERSION,
        pattern_key=pattern_key,
        client_profile=client_profile,
        platform_profile=platform_profile,
        scope_key=scope_key,
        severity=severity,
        proposed_delta_json=proposed_delta_json,
        window_frequency=1,
        evidence=tuple(evidence),
    )


def _coerce_proposed_delta(raw: Any, *, pattern_key: str) -> str:
    """Return a JSON-string proposed_delta.

    The skill schema says proposed_delta_json is a JSON STRING. Some
    reflector implementations write it as an object; accept both.
    Always return a stringified JSON doc so the downstream upsert +
    drafter paths see a consistent shape.
    """
    if isinstance(raw, str) and raw.strip():
        # Pass through verbatim — the drafter will re-validate later.
        return raw
    if isinstance(raw, dict):
        return json.dumps(raw, sort_keys=True)
    # Fallback: encode the pattern_key so the row doesn't carry an
    # empty ``{}`` that the drafter can't hydrate.
    return json.dumps({"pattern_key": pattern_key}, sort_keys=True)


def _coerce_evidence(
    raw: Any,
    *,
    source_path: Path,
    trace_id: str,
    observed_at: str,
) -> list[EvidenceItem]:
    """Convert reflector evidence_refs → EvidenceItem list.

    source_ref collision proofing: within one retrospective, multiple
    evidence items referencing the same artifact are disambiguated by
    index. Across retrospectives sharing a trace_id, the path hash is
    appended so two retrospective files can both emit a "#reflector-0"
    entry without colliding on the
    ``(lesson_id, trace_id, source_ref)`` UNIQUE constraint.
    """
    if not isinstance(raw, list):
        return []
    path_hash = hashlib.sha256(
        str(source_path).encode("utf-8", "replace")
    ).hexdigest()[:8]
    out: list[EvidenceItem] = []
    for i, ev in enumerate(raw):
        if not isinstance(ev, dict):
            continue
        source_ref = str(ev.get("source_ref") or "").strip()
        if not source_ref:
            source_ref = f"retrospective#{i}"
        # Collision-proof source_ref: two evidence items in the same
        # retrospective referencing the same artifact both need to land.
        # Append the index so the (lesson_id, trace_id, source_ref) UNIQUE
        # constraint doesn't reject the second one. Include a short hash
        # of source_path so retrospectives from different files also
        # can't collide on the same index.
        source_ref = f"{source_ref}#reflector-{path_hash}-{i}"
        snippet = str(ev.get("snippet") or "").strip()
        out.append(
            EvidenceItem(
                trace_id=trace_id,
                observed_at=observed_at,
                source_ref=source_ref,
                snippet=snippet,
            )
        )
    return out
