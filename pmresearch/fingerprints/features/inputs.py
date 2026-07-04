"""Shared contracts for fingerprint features.

`ScopeInput` is the single, already-materialised bundle a feature reads. It is
scoped (``all`` or ``category:<Label>``) and windowed (``all`` or ``90d``) by
``compute.py`` *before* it reaches any feature, so a feature is a trivially
testable pure function: build a `ScopeInput` by hand, assert the result.

A feature returns a `FeatureResult`: either a value (Decimal scalar or a JSON-
serialisable dict distribution) or ``None`` with a mandatory ``null_reason``.
Returning ``None`` without a reason is a programming error and is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median as _stat_median
from typing import Callable, Optional, Union

_ZERO = Decimal("0")

# 10 equal price buckets over [0, 1]. 1.0 falls in the last bucket.
PRICE_BUCKETS = (
    "[0.0,0.1)",
    "[0.1,0.2)",
    "[0.2,0.3)",
    "[0.3,0.4)",
    "[0.4,0.5)",
    "[0.5,0.6)",
    "[0.6,0.7)",
    "[0.7,0.8)",
    "[0.8,0.9)",
    "[0.9,1.0]",
)
_BUCKET_MIDPOINTS = tuple(Decimal(str(i)) / 10 + Decimal("0.05") for i in range(10))


def price_bucket(price: Decimal) -> Optional[str]:
    """Bucket a price in [0, 1] into one of ten deciles. None if out of range."""
    if price < _ZERO or price > Decimal("1"):
        return None
    idx = int(price * 10)
    if idx >= 10:  # price == 1.0
        idx = 9
    return PRICE_BUCKETS[idx]


def bucket_midpoint(bucket: str) -> Decimal:
    return _BUCKET_MIDPOINTS[PRICE_BUCKETS.index(bucket)]


def median(values: list[Decimal]) -> Decimal:
    """Median as a Decimal (statistics.median averages the two middle values)."""
    return Decimal(str(_stat_median(values)))


def percentile_nearest_rank(values: list[Decimal], q: Decimal) -> Decimal:
    """Nearest-rank percentile, matching the episodes projection's p90 rule:
    index = int((n - 1) * q) into the ascending-sorted values."""
    ordered = sorted(values)
    idx = int((len(ordered) - 1) * q)
    return ordered[idx]


@dataclass(frozen=True)
class EpisodeRec:
    """One flat-to-flat episode, joined to its market's category / dates /
    resolution price (of the episode's own token)."""

    token_id: str
    condition_id: Optional[str]
    category: str
    open_ts: int
    close_ts: Optional[int]
    close_reason: str  # open | flat | resolution
    peak_qty: Decimal
    wac_entry: Decimal
    num_adds: int
    num_partial_exits: int
    realized_pnl: Decimal
    reward_income: Decimal
    start_date_ts: Optional[int]  # market.start_date as epoch seconds
    resolution_price: Optional[Decimal]  # this token's payout (0/1) if resolved

    @property
    def duration(self) -> Optional[int]:
        if self.close_ts is None or self.close_ts < self.open_ts:
            return None
        return self.close_ts - self.open_ts

    @property
    def position_size(self) -> Decimal:
        """USDC committed at peak = peak shares × weighted-average entry price."""
        return self.peak_qty * self.wac_entry


@dataclass(frozen=True)
class PnlRec:
    """One pnl_decomposition scope row (the authoritative realized-PnL and
    reward-income source; full-history only)."""

    directional: Decimal
    bond_merge: Decimal
    reward_income: Decimal
    redemption: Decimal
    fees: Decimal


@dataclass(frozen=True)
class ExposureDayAgg:
    """A single UTC day's exposure totals within the scope (already summed over
    the scope's conditions)."""

    date: str
    bond_abs: Decimal
    directional_abs: Decimal


@dataclass(frozen=True)
class ScopeInput:
    """Everything a feature may read for one (wallet, scope, window)."""

    wallet: str
    scope: str
    window: str
    episodes: list[EpisodeRec] = field(default_factory=list)
    exposure_days: list[ExposureDayAgg] = field(default_factory=list)
    # Ledger aggregates (already scoped/windowed).
    trade_total: int = 0
    trade_maker: int = 0
    trade_taker: int = 0
    trade_enriched: int = 0  # maker + taker (non-ambiguous) enriched fills
    merge_count: int = 0
    redeem_count: int = 0
    active_days: int = 0
    # pnl_decomposition row for this scope; only populated for window == "all".
    pnl: Optional["PnlRec"] = None
    # daily_equity-derived; only populated for scope == "all".
    latest_unrealized: Optional[Decimal] = None
    stale_equity_shares: list[Decimal] = field(default_factory=list)
    # Cross-category context; only populated for scope == "all".
    category_episode_counts: Optional[dict[str, int]] = None

    @property
    def closed_episodes(self) -> list[EpisodeRec]:
        return [e for e in self.episodes if e.duration is not None]

    @property
    def is_all_scope(self) -> bool:
        return self.scope == "all"


FeatureValue = Union[Decimal, dict, None]


@dataclass(frozen=True)
class FeatureResult:
    value: FeatureValue
    null_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.value is None and not self.null_reason:
            raise ValueError("NULL FeatureResult requires a null_reason")
        if self.value is not None and self.null_reason:
            raise ValueError("non-NULL FeatureResult must not carry a null_reason")

    @property
    def is_null(self) -> bool:
        return self.value is None

    @property
    def is_distribution(self) -> bool:
        return isinstance(self.value, dict)


def null(reason: str) -> FeatureResult:
    return FeatureResult(value=None, null_reason=reason)


def scalar(value: Decimal) -> FeatureResult:
    return FeatureResult(value=value)


def distribution(value: dict) -> FeatureResult:
    return FeatureResult(value=value)


@dataclass(frozen=True)
class Feature:
    name: str
    family: str
    fn: Callable[[ScopeInput], FeatureResult]
