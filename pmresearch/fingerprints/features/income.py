"""Income family: realized/unrealized PnL and how much of the wallet's gross
positive income is rewards rather than trading/settlement.

realized_pnl and reward_income_share read the pnl_decomposition projection —
the authoritative, category-scoped decomposition that reconciles with
daily_equity. (The episodes projection deliberately does not carry a wallet's
full realized PnL — e.g. reward income that never lands on an active token
episode — so it must not be used here.) pnl_decomposition is full-history only,
so these features are NULL-with-reason for the 90d window.

unrealized_pnl comes from the marked daily_equity projection, which is only
defined at all-scope full-history — for any other scope/window it is
NULL-with-reason, never fabricated.
"""

from __future__ import annotations

from decimal import Decimal

from .inputs import Feature, FeatureResult, ScopeInput, null, scalar

_ZERO = Decimal("0")


def realized_pnl(inp: ScopeInput) -> FeatureResult:
    """Realized PnL = directional + bond/merge + redemption - fees (excludes
    reward income, which is a separate feature)."""
    if inp.window != "all":
        return null("realized PnL (pnl_decomposition) is full-history only")
    if inp.pnl is None:
        return null("no pnl_decomposition row for scope")
    p = inp.pnl
    return scalar(p.directional + p.bond_merge + p.redemption - p.fees)


def reward_income_share(inp: ScopeInput) -> FeatureResult:
    """Reward income as a share of gross positive income
    (reward + the positive part of each realized component). Bounded [0, 1]."""
    if inp.window != "all":
        return null("reward income share (pnl_decomposition) is full-history only")
    if inp.pnl is None:
        return null("no pnl_decomposition row for scope")
    p = inp.pnl
    gross_positive = (
        p.reward_income
        + max(p.directional, _ZERO)
        + max(p.bond_merge, _ZERO)
        + max(p.redemption, _ZERO)
    )
    if gross_positive <= _ZERO:
        return null("no gross positive income in scope")
    return scalar(p.reward_income / gross_positive)


def unrealized_pnl(inp: ScopeInput) -> FeatureResult:
    """Marked unrealized PnL on open positions, from daily_equity's latest row.
    Only defined at all-scope full-history (daily_equity is not decomposed by
    category, and 90d marks are not re-derived)."""
    if not inp.is_all_scope:
        return null("unrealized PnL is not decomposed by category")
    if inp.window != "all":
        return null("unrealized PnL is only marked over full history")
    if inp.latest_unrealized is None:
        return null("no daily_equity marks available")
    return scalar(inp.latest_unrealized)


FEATURES = [
    Feature("realized_pnl", "income", realized_pnl),
    Feature("unrealized_pnl", "income", unrealized_pnl),
    Feature("reward_income_share", "income", reward_income_share),
]
