"""market_making detector.

Hypothesis: the wallet earns by providing liquidity — resting maker quotes on
both sides, harvesting the maker-fill share and reward income, carrying paired
(bond) inventory, and turning positions over rapidly (micro episodes).

Weighted feature scoring (weights sum to 1.0; each sub-score is already a
[0, 1] share used directly):

    maker_fill_share       0.35  more maker fills ⇒ more MM
    reward_income_share    0.25  liquidity rewards are a core MM income source
    bond_inventory_ratio   0.20  paired YES+NO inventory = two-sided by build
    micro_episode_share    0.20  rapid flat-to-flat turnover

Two-sided quoting itself is unobservable (no historical quote/book data was
collected for this wallet's past), so bond_inventory_ratio is the observable
proxy for two-sidedness — see blind spots. maker_fill_share is NULL when the
scope has no enriched fills, in which case it drops out and lowers confidence
rather than scoring the wallet down.
"""

from __future__ import annotations

from decimal import Decimal

from .base import Detector, scalar_signal

DETECTOR = Detector(
    name="market_making",
    version=1,
    signals=[
        scalar_signal("maker_fill_share", Decimal("0.35")),
        scalar_signal("reward_income_share", Decimal("0.25")),
        scalar_signal("bond_inventory_ratio", Decimal("0.20")),
        scalar_signal("micro_episode_share", Decimal("0.20")),
    ],
    blind_spots=(
        "Quote placement is unobservable - no historical order-book/quote data "
        "was collected, so passive liquidity provision cannot be distinguished "
        "from active market making, and true two-sided quoting is only proxied "
        "by bond (paired) inventory. maker_fill_share is conditioned on "
        "enrichment coverage: fills outside the subgraph-covered window carry no "
        "maker/taker role and are invisible to this score."
    ),
)
