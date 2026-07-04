"""Calibration family: entry timing relative to event start, the distribution
of entry prices, and how realised win rates line up with entry-price-implied
probabilities.

Survivorship is handled explicitly: resolution calibration counts only
episodes whose market actually resolved (a known token payout). Unresolved
positions are excluded, never assumed lost.
"""

from __future__ import annotations

from decimal import Decimal

from .inputs import (
    PRICE_BUCKETS,
    Feature,
    FeatureResult,
    ScopeInput,
    bucket_midpoint,
    distribution,
    median,
    null,
    price_bucket,
    scalar,
)

_ZERO = Decimal("0")


def time_to_event_start_at_entry(inp: ScopeInput) -> FeatureResult:
    """Median seconds between episode entry (open_ts) and the market's event
    start (positive = entered after start, negative = before). Only episodes
    whose Gamma market carries a start_date contribute — most meaningful for
    sports scopes, per the plan."""
    deltas = [
        Decimal(e.open_ts - e.start_date_ts)
        for e in inp.episodes
        if e.start_date_ts is not None
    ]
    if not deltas:
        return null("no episodes with a known event start_date in scope")
    return scalar(median(deltas))


def entry_price_distribution(inp: ScopeInput) -> FeatureResult:
    """Share of episodes whose WAC entry price falls in each price decile.
    Values sum to 1 over the buckets present. Entries outside [0, 1] are
    dropped from the denominator (recorded as reason if none remain)."""
    counts = {b: 0 for b in PRICE_BUCKETS}
    total = 0
    for e in inp.episodes:
        bucket = price_bucket(e.wac_entry)
        if bucket is None:
            continue
        counts[bucket] += 1
        total += 1
    if total == 0:
        return null("no episodes with an in-range entry price in scope")
    return distribution(
        {b: str(Decimal(c) / Decimal(total)) for b, c in counts.items() if c > 0}
    )


def resolution_outcome_calibration(inp: ScopeInput) -> FeatureResult:
    """Per entry-price bucket: realised win rate vs the bucket's implied
    probability (midpoint), over resolved episodes only (survivorship-safe).
    A win = the held token paid out (resolution_price >= 0.5)."""
    buckets: dict[str, list[int]] = {}
    for e in inp.episodes:
        if e.close_reason != "resolution" or e.resolution_price is None:
            continue
        bucket = price_bucket(e.wac_entry)
        if bucket is None:
            continue
        won = 1 if e.resolution_price >= Decimal("0.5") else 0
        agg = buckets.setdefault(bucket, [0, 0])  # [n, wins]
        agg[0] += 1
        agg[1] += won
    if not buckets:
        return null("no resolved episodes with an in-range entry price in scope")
    out = {}
    for bucket in PRICE_BUCKETS:
        if bucket not in buckets:
            continue
        n, wins = buckets[bucket]
        out[bucket] = {
            "n": n,
            "actual_win_rate": str(Decimal(wins) / Decimal(n)),
            "implied": str(bucket_midpoint(bucket)),
        }
    return distribution(out)


FEATURES = [
    Feature("time_to_event_start_at_entry", "calibration", time_to_event_start_at_entry),
    Feature("entry_price_distribution", "calibration", entry_price_distribution),
    Feature("resolution_outcome_calibration", "calibration", resolution_outcome_calibration),
]
