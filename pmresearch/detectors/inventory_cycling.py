"""inventory_cycling detector.

Hypothesis: the wallet accumulates paired (bond) inventory and recycles capital
through the MERGE/REDEEM machinery — buying complementary tokens, merging them
back to USDC (or redeeming at resolution), and repeating — rather than taking
directional views.

Weighted feature scoring (weights sum to 1.0):

    bond_inventory_ratio   0.40  paired inventory is the raw material of cycling
    merge_frequency        0.35  merges per active day = the recycling cadence
    redeem_frequency       0.25  redemptions per active day = settlement cadence

bond_inventory_ratio is already a [0, 1] share (used directly). merge_frequency
and redeem_frequency are unbounded per-active-day rates, mapped through a
saturating transform value/(value+half): half = 0.5 merges/day and 0.5
redeems/day reach sub-score 0.5 — i.e. a wallet merging on ~half its active
days scores 0.5 on that signal. A scope with no active days yields NULL
frequencies (from Phase 13), which drop out and lower confidence.
"""

from __future__ import annotations

from decimal import Decimal

from .base import Detector, saturating_signal, scalar_signal

DETECTOR = Detector(
    name="inventory_cycling",
    version=1,
    signals=[
        scalar_signal("bond_inventory_ratio", Decimal("0.40")),
        saturating_signal("merge_frequency", Decimal("0.35"), Decimal("0.5")),
        saturating_signal("redeem_frequency", Decimal("0.25"), Decimal("0.5")),
    ],
    blind_spots=(
        "Cadence is measured per active day, so a bursty cycler (many merges in "
        "a few days) and a steady one look alike. Merge/redeem counts ignore "
        "size - a wallet cycling large notional and a small one score the same. "
        "Redemption attribution depends on resolution data being present; "
        "unresolved holdings are not yet counted as cycled."
    ),
)
