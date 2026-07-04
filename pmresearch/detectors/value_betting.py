"""value_betting detector.

Hypothesis: the wallet takes directional views it believes are mispriced —
crossing the spread (taker-dominant), holding to resolution (long episodes),
in a concentrated set of categories, and earning a positive calibration edge
(its held tokens resolve in-the-money more often than the entry price implied).

Weighted feature scoring (weights sum to 1.0):

    taker_fill_share                0.30  crossing the spread to express a view
    episode_duration_p50            0.25  holding to resolution, not scalping
    calibration_edge                0.25  realised win rate beats entry-implied
    market_category_concentration   0.20  a focused, researched book

Sub-score transforms (each documented, monotone):
  - taker_fill_share, market_category_concentration: already [0, 1], used
    directly.
  - episode_duration_p50 (seconds): saturating value/(value+half), half = 1 day
    (86400 s) — a median hold of a day scores 0.5.
  - calibration_edge: read from the resolution_outcome_calibration distribution
    as the n-weighted mean of (actual_win_rate − implied) across buckets, then
    mapped clamp01(0.5 + edge). A zero edge (calibrated) scores 0.5; a full
    +0.5 edge saturates to 1.0. NULL (no resolved episodes) drops out and
    lowers confidence — small samples are the headline blind spot.
"""

from __future__ import annotations

from decimal import Decimal

from .base import (
    Detector,
    DetectorInput,
    Signal,
    SignalReading,
    clamp01,
    saturating_signal,
    scalar_signal,
)

_ZERO = Decimal("0")
_HALF = Decimal("0.5")


def _read_calibration_edge(inp: DetectorInput) -> SignalReading:
    """n-weighted mean win-rate minus implied over resolved entry-price buckets."""
    feature = "resolution_outcome_calibration"
    dist = inp.distribution(feature)
    if not dist:
        return SignalReading(raw=inp.raw(feature), sub_score=None, note=inp.reason(feature))
    total_n = 0
    weighted_edge = _ZERO
    for bucket in dist.values():
        n = int(bucket["n"])
        actual = Decimal(str(bucket["actual_win_rate"]))
        implied = Decimal(str(bucket["implied"]))
        total_n += n
        weighted_edge += Decimal(n) * (actual - implied)
    if total_n == 0:
        return SignalReading(raw=inp.raw(feature), sub_score=None, note="no resolved samples")
    edge = weighted_edge / Decimal(total_n)
    return SignalReading(raw=str(edge), sub_score=clamp01(_HALF + edge))


_calibration_edge_signal = Signal(
    feature="calibration_edge",
    weight=Decimal("0.25"),
    read=_read_calibration_edge,
)


DETECTOR = Detector(
    name="value_betting",
    version=1,
    signals=[
        scalar_signal("taker_fill_share", Decimal("0.30")),
        saturating_signal("episode_duration_p50", Decimal("0.25"), Decimal("86400")),
        _calibration_edge_signal,
        scalar_signal("market_category_concentration", Decimal("0.20")),
    ],
    blind_spots=(
        "Calibration edge is only as trustworthy as its resolved-episode sample "
        "- few resolutions make the edge noisy, and it is survivorship-scoped to "
        "markets that actually resolved. ~1-minute price fidelity limits any "
        "read of entry timing or momentum. taker_fill_share is conditioned on "
        "enrichment coverage; market_category_concentration is only defined at "
        "the all scope and is NULL (drops out) inside a single-category scope."
    ),
)
