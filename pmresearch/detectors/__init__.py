"""Strategy detectors (Phase 14).

Named, versioned rules that read Behavioral Fingerprints (Phase 13) only and
emit scored Strategy Labels — a score in [0, 1] with machine-readable evidence
and explicit blind spots. Never a boolean verdict (CONTEXT.md: Strategy Label).

`base` holds the contract and the weighted-scoring framework; one module per
detector (`market_making`, `inventory_cycling`, `value_betting`); `compute`
loads fingerprints, runs every detector over every scope, and persists labels.
"""

from .base import (
    Detector,
    DetectorInput,
    DetectorResult,
    Signal,
    evaluate,
)
from .compute import (
    DETECTOR_REGISTRY,
    all_detectors,
    fetch_labels,
    run_detectors,
)

__all__ = [
    "Detector",
    "DetectorInput",
    "DetectorResult",
    "Signal",
    "evaluate",
    "DETECTOR_REGISTRY",
    "all_detectors",
    "fetch_labels",
    "run_detectors",
]
