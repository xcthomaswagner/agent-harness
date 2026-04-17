"""Pattern detectors for the self-learning miner.

Each detector is a standalone module implementing the ``Detector``
Protocol from ``base``. Detectors are deliberately independent —
the runner treats them as a flat list and isolates failures per
detector so one bad scan can't bring the whole nightly run down.
"""
