"""Execution family: how the wallet's fills sit on the book.

maker/taker share are conditioned on enrichment coverage: with zero enriched
fills in scope there is nothing to attribute a role to, so the share is NULL
(not 0). This is the "maker_share over unenriched periods" failure mode the
plan calls out — never report a role share the data can't support.
"""

from __future__ import annotations

from decimal import Decimal

from .inputs import Feature, FeatureResult, ScopeInput, null, scalar

_ZERO = Decimal("0")


def maker_fill_share(inp: ScopeInput) -> FeatureResult:
    """Share of role-attributed fills where the wallet was the maker."""
    attributed = inp.trade_maker + inp.trade_taker
    if attributed == 0:
        return null("no enriched fills with a maker/taker role in scope")
    return scalar(Decimal(inp.trade_maker) / Decimal(attributed))


def taker_fill_share(inp: ScopeInput) -> FeatureResult:
    """Share of role-attributed fills where the wallet was the taker."""
    attributed = inp.trade_maker + inp.trade_taker
    if attributed == 0:
        return null("no enriched fills with a maker/taker role in scope")
    return scalar(Decimal(inp.trade_taker) / Decimal(attributed))


FEATURES = [
    Feature("maker_fill_share", "execution", maker_fill_share),
    Feature("taker_fill_share", "execution", taker_fill_share),
]
