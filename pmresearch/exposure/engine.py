"""Exposure Engine dispatch (CONTEXT.md "Exposure Engine").

Market-level exposure is computed strictly from the Market Structure
Descriptor (`structure_type`) — the engine dispatches on machine structure,
never on outcome-label text. Complementarity is mapped only through token
index (the caller passes tokens ordered by `tokens.outcome_index`).

Dispatch:
  binary / negRisk-event-member (2 ordered tokens) -> directional + bond
  unclassified                                       -> raw per-token vector
  unknown / missing structure_type                   -> unclassified path,
      counted as a warning (dispatch never guesses a decomposition).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from . import binary, unclassified
from .descriptors import (
    STRUCTURE_BINARY,
    STRUCTURE_NEG_RISK_EVENT_MEMBER,
    STRUCTURE_UNCLASSIFIED,
)

_ZERO = Decimal("0")

# structure_type values that decompose into directional + bond.
_DECOMPOSABLE = (STRUCTURE_BINARY, STRUCTURE_NEG_RISK_EVENT_MEMBER)


@dataclass(frozen=True)
class MarketExposure:
    structure_type: str
    directional: Decimal | None
    bond: Decimal | None
    raw_vector: dict[str, str] | None
    unknown_structure: bool


def market_exposure(
    structure_type: str | None,
    ordered_tokens: list[str],
    qty_by_token: dict[str, Decimal],
) -> MarketExposure:
    """Compute market-level exposure by dispatching on structure_type.

    `ordered_tokens` must be the condition's tokens ordered by outcome_index.
    Returns a decomposition for 2-token binary/negRisk markets, otherwise the
    raw per-token vector flagged unclassified. Unknown/missing structure_type
    is routed to the unclassified path and flagged `unknown_structure=True`.
    """
    if structure_type in _DECOMPOSABLE and len(ordered_tokens) == 2:
        qty0 = qty_by_token.get(ordered_tokens[0], _ZERO)
        qty1 = qty_by_token.get(ordered_tokens[1], _ZERO)
        directional, bond = binary.decompose(qty0, qty1)
        return MarketExposure(
            structure_type=structure_type,
            directional=directional,
            bond=bond,
            raw_vector=None,
            unknown_structure=False,
        )

    unknown = structure_type not in (
        STRUCTURE_BINARY,
        STRUCTURE_NEG_RISK_EVENT_MEMBER,
        STRUCTURE_UNCLASSIFIED,
    )
    return MarketExposure(
        structure_type=STRUCTURE_UNCLASSIFIED,
        directional=None,
        bond=None,
        raw_vector=unclassified.raw_vector(ordered_tokens, qty_by_token),
        unknown_structure=unknown,
    )
