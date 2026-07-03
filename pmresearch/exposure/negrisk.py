"""Event-level exposure for negRisk events (mutually-exclusive siblings).

A negRisk Event groups sibling binary Markets (e.g. "Mexico" / "Draw" /
"Korea"), each a 2-token market that resolves independently but whose YES
outcomes are mutually exclusive across the event (at most one wins).

Exposure vector
    Per-sibling *directional* exposure, keyed by condition_id, computed by
    token index (qty0 - qty1) — never by outcome labels. Only siblings the
    wallet holds a nonzero position in appear in the vector.

Netting rule (net_after_exclusivity)
    Because exactly one sibling's index-0 outcome can pay off, the per-sibling
    directional exposures are summed into a single net event-level figure:

        net_after_exclusivity = Σ directional_i   over siblings i

    This is the wallet's net long-minus-short across the mutually-exclusive
    outcomes: symmetric long positions across every sibling net toward zero
    (a fully hedged basket), while a concentrated long in one sibling nets to
    that sibling's directional. MVP scope: simple within-event netting only,
    no cross-event correlation (CONTEXT.md "Event-level Exposure").
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from . import binary


@dataclass(frozen=True)
class EventExposure:
    event_id: str
    exposure_vector: dict[str, str]  # condition_id -> directional (decimal string)
    net_after_exclusivity: Decimal


def event_exposure(
    event_id: str, siblings: list[tuple[str, Decimal, Decimal]]
) -> EventExposure:
    """Build the per-condition directional vector and its net for one event.

    `siblings` is a list of (condition_id, qty0, qty1) for the event's member
    markets in which the wallet holds any nonzero position. Ordering is
    normalised by condition_id here so the serialised vector is deterministic.
    """
    vector: dict[str, str] = {}
    net = Decimal("0")
    for condition_id, qty0, qty1 in sorted(siblings, key=lambda s: s[0]):
        directional, _bond = binary.decompose(qty0, qty1)
        vector[condition_id] = str(directional)
        net += directional
    return EventExposure(event_id=event_id, exposure_vector=vector, net_after_exclusivity=net)


def vector_json(vector: dict[str, str]) -> str:
    """Deterministic JSON serialisation of an exposure vector."""
    return json.dumps(vector, sort_keys=True, separators=(",", ":"))
