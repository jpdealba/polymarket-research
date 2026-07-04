"""Quality family: how trustworthy the wallet's derived numbers are.

enrichment_coverage measures how much of the trade flow has a maker/taker role
attributed (it conditions the execution family). stale_mark_share measures how
much marked equity leaned on stale marks. Both are always reported as 0 when
genuinely zero-with-data, but NULL when there is no data to measure at all.
"""

from __future__ import annotations

from decimal import Decimal

from .inputs import Feature, FeatureResult, ScopeInput, null, scalar

_ZERO = Decimal("0")


def enrichment_coverage(inp: ScopeInput) -> FeatureResult:
    """Share of TRADE fills that have a maker/taker role attributed."""
    if inp.trade_total == 0:
        return null("no trades in scope")
    return scalar(Decimal(inp.trade_enriched) / Decimal(inp.trade_total))


def stale_mark_share(inp: ScopeInput) -> FeatureResult:
    """Time-weighted mean of the daily stale-equity share. Only defined at
    all-scope (daily_equity is portfolio-level, not per-category)."""
    if not inp.is_all_scope:
        return null("stale-mark share is portfolio-level (all-scope only)")
    shares = inp.stale_equity_shares
    if not shares:
        return null("no daily_equity rows to measure staleness")
    return scalar(sum(shares, _ZERO) / Decimal(len(shares)))


FEATURES = [
    Feature("enrichment_coverage", "quality", enrichment_coverage),
    Feature("stale_mark_share", "quality", stale_mark_share),
]
