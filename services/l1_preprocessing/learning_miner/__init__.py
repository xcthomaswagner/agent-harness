"""Self-learning pattern-mining package.

Reads existing autonomy signals (pr_runs, review_issues, tool_index,
diagnostic artifacts) and emits lesson candidates to autonomy.db.
Every lesson stays at ``status='proposed'`` until a human approves
it via the ``/autonomy/learning`` dashboard — no prompt or config
change happens without review.

See docs/self-learning-plan.md for the full design.
"""

from __future__ import annotations

from learning_miner.detectors.base import CandidateProposal, Detector
from learning_miner.runner import MinerRunResult, run_miner

__all__ = [
    "CandidateProposal",
    "Detector",
    "MinerRunResult",
    "run_miner",
]
