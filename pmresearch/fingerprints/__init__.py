"""Phase 13 — Behavioral fingerprints.

The measurement layer for strategy analysis (CONTEXT.md: Behavioral
Fingerprint). Every feature is a *pure function* over projections — it reads
already-built projection rows (episodes, exposures, daily_equity) plus ledger
aggregates, never raw API data — and returns either a value or NULL with a
reason. Features never silently return 0 for "uncomputable".
"""

from .compute import (
    FINGERPRINT_VERSION,
    compute_fingerprints,
    fetch_fingerprints,
)

__all__ = [
    "FINGERPRINT_VERSION",
    "compute_fingerprints",
    "fetch_fingerprints",
]
