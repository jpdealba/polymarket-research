"""Feature families for behavioral fingerprints.

One module per family (execution, inventory, income, calibration, quality).
Each family exposes a list of `Feature` records; `compute.py` assembles them
into the global registry. Every feature is a pure function of a `ScopeInput`.
"""

from . import calibration, execution, income, inventory, quality
from .inputs import (
    EpisodeRec,
    ExposureDayAgg,
    Feature,
    FeatureResult,
    ScopeInput,
    median,
    percentile_nearest_rank,
    price_bucket,
)

FEATURE_MODULES = (execution, inventory, income, calibration, quality)


def all_features() -> list[Feature]:
    """The ordered global feature registry (family order, then declared order)."""
    features: list[Feature] = []
    for module in FEATURE_MODULES:
        features.extend(module.FEATURES)
    return features


__all__ = [
    "EpisodeRec",
    "ExposureDayAgg",
    "Feature",
    "FeatureResult",
    "ScopeInput",
    "median",
    "percentile_nearest_rank",
    "price_bucket",
    "all_features",
    "FEATURE_MODULES",
]
