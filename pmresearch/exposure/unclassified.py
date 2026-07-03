"""Unclassified market exposure: no decomposition, raw per-token vector.

For any market whose structure the descriptor could not classify (token count
!= 2, or an unknown/missing structure_type routed here by the engine), we
never guess a decomposition. We return the wallet's raw per-token quantity
vector, flagged "unclassified", so downstream analysis can see the position
without a fabricated directional/bond reading (CONTEXT.md "Market Structure
Descriptor").
"""

from __future__ import annotations

from decimal import Decimal


def raw_vector(
    ordered_tokens: list[str], qty_by_token: dict[str, Decimal]
) -> dict[str, str]:
    """Per-token qty as decimal strings, keyed by token_id, order-preserving."""
    return {token_id: str(qty_by_token.get(token_id, Decimal("0"))) for token_id in ordered_tokens}
