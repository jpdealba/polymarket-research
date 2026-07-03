"""Binary market exposure: the directional + bond decomposition.

Complementarity is determined solely by token index within the condition
(index 0 vs index 1), never by outcome labels ("Yes"/"No"/team names). The
two tokens must be passed ordered by `tokens.outcome_index` (index 0 first).

    directional = qty0 - qty1   (signed, in token0-equivalent shares)
    bond        = min(qty0, qty1)   (complete pairs redeemable for $1)

See CONTEXT.md "Directional + Bond decomposition".
"""

from __future__ import annotations

from decimal import Decimal


def decompose(qty0: Decimal, qty1: Decimal) -> tuple[Decimal, Decimal]:
    """Return (directional, bond) for a binary market's two ordered tokens."""
    directional = qty0 - qty1
    bond = qty0 if qty0 < qty1 else qty1
    return directional, bond
