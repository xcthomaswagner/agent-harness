"""Self-learning pattern-mining package.

Reads existing autonomy signals (pr_runs, review_issues, tool_index,
diagnostic artifacts) and emits lesson candidates to autonomy.db.
Every lesson stays at ``status='proposed'`` until a human approves
it via the ``/autonomy/learning`` dashboard — no prompt or config
change happens without review.

See docs/self-learning-plan.md for the full design.
"""

from __future__ import annotations

from collections.abc import Callable

from learning_miner.detectors.base import CandidateProposal, Detector
from learning_miner.retrospective_ingest import ingest_retrospectives
from learning_miner.runner import MinerRunResult, run_miner


# Detector name → lazy factory. Keyed on the same ``NAME`` string
# each detector publishes; the factory is a no-arg callable that
# imports the module and returns a fresh instance. Adding a detector
# is one line here. Imports are deferred so a malformed detector
# module doesn't block package import.
def _build_human_issue_cluster() -> Detector:
    from learning_miner.detectors.human_issue_cluster import build
    return build()


def _build_mcp_drift() -> Detector:
    from learning_miner.detectors.mcp_drift import build
    return build()


def _build_form_controls_ac_gaps() -> Detector:
    from learning_miner.detectors.form_controls_ac_gaps import build
    return build()


def _build_cross_unit_object_pivot() -> Detector:
    from learning_miner.detectors.cross_unit_object_pivot import build
    return build()


def _build_simplify_no_sidecar() -> Detector:
    from learning_miner.detectors.simplify_no_sidecar import build
    return build()


_DETECTOR_BUILDERS: dict[str, Callable[[], Detector]] = {
    "human_issue_cluster": _build_human_issue_cluster,
    "mcp_drift": _build_mcp_drift,
    "form_controls_ac_gaps": _build_form_controls_ac_gaps,
    "cross_unit_object_pivot": _build_cross_unit_object_pivot,
    "simplify_no_sidecar": _build_simplify_no_sidecar,
}


def get_detector(name: str) -> Detector | None:
    """Return the registered detector instance for ``name`` or None."""
    builder = _DETECTOR_BUILDERS.get(name)
    return builder() if builder else None


def all_production_detectors() -> list[Detector]:
    """Return a fresh instance of every registered detector.

    Single source of truth for the set of detectors the nightly miner
    and backfill script run. Callers must not cache the list — each
    call returns freshly-built detectors so per-run caches don't leak
    between invocations.
    """
    return [builder() for builder in _DETECTOR_BUILDERS.values()]


__all__ = [
    "CandidateProposal",
    "Detector",
    "MinerRunResult",
    "all_production_detectors",
    "get_detector",
    "ingest_retrospectives",
    "run_miner",
]
