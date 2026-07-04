"""Inventory family: episode shape, position sizing, bond carrying, and the
cadence of the merge/redeem capital cycle.

Everything here is derived from the episodes projection and the exposures
projection, so it scopes by category and windows by time naturally. Division-
by-zero on empty scopes returns NULL-with-reason, never 0.
"""

from __future__ import annotations

from decimal import Decimal

from .inputs import (
    Feature,
    FeatureResult,
    ScopeInput,
    median,
    null,
    percentile_nearest_rank,
    scalar,
)

_ZERO = Decimal("0")


def bond_inventory_ratio(inp: ScopeInput) -> FeatureResult:
    """Time-weighted share of exposure carried as bond (paired YES+NO) rather
    than directional. Each snapshot day contributes equally, so the mean over
    days is time-weighted. bond / (bond + |directional|) per day."""
    day_ratios: list[Decimal] = []
    for day in inp.exposure_days:
        denom = day.bond_abs + day.directional_abs
        if denom > _ZERO:
            day_ratios.append(day.bond_abs / denom)
    if not day_ratios:
        return null("no exposure days with nonzero exposure in scope")
    return scalar(sum(day_ratios, _ZERO) / Decimal(len(day_ratios)))


def merge_frequency(inp: ScopeInput) -> FeatureResult:
    """MERGE events per active day (a day with any ledger event in scope)."""
    if inp.active_days == 0:
        return null("no active days in scope")
    return scalar(Decimal(inp.merge_count) / Decimal(inp.active_days))


def redeem_frequency(inp: ScopeInput) -> FeatureResult:
    """REDEEM / REDEEM_PAYOUT events per active day in scope."""
    if inp.active_days == 0:
        return null("no active days in scope")
    return scalar(Decimal(inp.redeem_count) / Decimal(inp.active_days))


def episode_count(inp: ScopeInput) -> FeatureResult:
    return scalar(Decimal(len(inp.episodes)))


def episode_duration_p50(inp: ScopeInput) -> FeatureResult:
    """Median closed-episode duration in seconds."""
    durations = [Decimal(e.duration) for e in inp.closed_episodes]
    if not durations:
        return null("no closed episodes in scope")
    return scalar(median(durations))


def episode_duration_p90(inp: ScopeInput) -> FeatureResult:
    """90th-percentile (nearest-rank) closed-episode duration in seconds."""
    durations = [Decimal(e.duration) for e in inp.closed_episodes]
    if not durations:
        return null("no closed episodes in scope")
    return scalar(percentile_nearest_rank(durations, Decimal("0.9")))


def micro_episode_share(inp: ScopeInput) -> FeatureResult:
    """Share of closed episodes lasting <= 60 seconds."""
    closed = inp.closed_episodes
    if not closed:
        return null("no closed episodes in scope")
    micro = sum(1 for e in closed if e.duration <= 60)
    return scalar(Decimal(micro) / Decimal(len(closed)))


def adds_per_episode(inp: ScopeInput) -> FeatureResult:
    """Mean number of scale-in adds per episode."""
    if not inp.episodes:
        return null("no episodes in scope")
    total = sum(e.num_adds for e in inp.episodes)
    return scalar(Decimal(total) / Decimal(len(inp.episodes)))


def partial_exit_frequency(inp: ScopeInput) -> FeatureResult:
    """Mean number of partial exits per episode."""
    if not inp.episodes:
        return null("no episodes in scope")
    total = sum(e.num_partial_exits for e in inp.episodes)
    return scalar(Decimal(total) / Decimal(len(inp.episodes)))


def avg_position_size(inp: ScopeInput) -> FeatureResult:
    """Mean USDC committed per episode (peak shares × WAC entry price)."""
    if not inp.episodes:
        return null("no episodes in scope")
    sizes = [e.position_size for e in inp.episodes]
    return scalar(sum(sizes, _ZERO) / Decimal(len(sizes)))


def median_position_size(inp: ScopeInput) -> FeatureResult:
    """Median USDC committed per episode."""
    if not inp.episodes:
        return null("no episodes in scope")
    return scalar(median([e.position_size for e in inp.episodes]))


def market_category_concentration(inp: ScopeInput) -> FeatureResult:
    """Herfindahl-Hirschman index of episode counts across market categories.
    1.0 = single-category; -> 0 = evenly spread. Only meaningful across
    categories, so it is an all-scope feature."""
    if not inp.is_all_scope:
        return null("concentration is defined across categories (all-scope only)")
    counts = inp.category_episode_counts or {}
    total = sum(counts.values())
    if total == 0:
        return null("no episodes to measure category concentration")
    hhi = sum((Decimal(c) / Decimal(total)) ** 2 for c in counts.values())
    return scalar(hhi)


FEATURES = [
    Feature("bond_inventory_ratio", "inventory", bond_inventory_ratio),
    Feature("merge_frequency", "inventory", merge_frequency),
    Feature("redeem_frequency", "inventory", redeem_frequency),
    Feature("episode_count", "inventory", episode_count),
    Feature("episode_duration_p50", "inventory", episode_duration_p50),
    Feature("episode_duration_p90", "inventory", episode_duration_p90),
    Feature("micro_episode_share", "inventory", micro_episode_share),
    Feature("adds_per_episode", "inventory", adds_per_episode),
    Feature("partial_exit_frequency", "inventory", partial_exit_frequency),
    Feature("avg_position_size", "inventory", avg_position_size),
    Feature("median_position_size", "inventory", median_position_size),
    Feature("market_category_concentration", "inventory", market_category_concentration),
]
