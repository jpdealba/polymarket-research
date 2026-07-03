"""Wallet Event: one atomic, immutable action by a wallet (ADR 0002,
CONTEXT.md "Wallet Event"). This module defines the event dataclass, the
(open) event_type set, and the signed delta_shares/delta_usdc convention.

`wallet_events` is append-only; corrections are new events, never updates.

Sign conventions (delta_shares / delta_usdc), applied in pmresearch.ingest.activity:

  TRADE  BUY  = +shares / -usdc
  TRADE  SELL = -shares / +usdc

  REWARD      =  0 / +usdc
      Participation/liquidity income, unscoped to a market (source reports no
      conditionId/asset for these rows) — token_id is NULL.

  MERGE       = -shares / +usdc (as reported)
      A complementary token pair converts back to $1 USDC each. The source
      gives one row per condition, not per token (asset is empty), so
      token_id is NULL — this event affects both of the condition's tokens
      conceptually, but we can only record the one magnitude the API gives us.

  SPLIT       = +shares / -usdc (as reported)
      The mirror of MERGE: $1 USDC splits into a complementary token pair.
      Same token_id-is-NULL reasoning as MERGE.

  REDEEM      = -shares / +usdc (as reported)
      usdc_size is currently always 0 in the source feed for REDEEM rows —
      true payout derivation (qty * resolution price) is deferred to Phase 8.
      token_id is NULL: the source gives no per-token attribution for REDEEM
      (only conditionId), and Phase 2 does not guess which token it was.

  Anything else — TRANSFER included, plus types the source reports that
  aren't in this documented set at all (e.g. MAKER_REBATE, CONVERSION,
  observed live against RN1 but not part of the frozen design's enum) — gets
  delta_shares = delta_usdc = 0. We do not invent a sign for a type whose
  direction we're not certain of; the raw price/size/usdc_size are preserved
  verbatim and a warning is logged so the type is visible in `pmr ledger
  stats` rather than silently dropped or silently guessed (see ADR 0006:
  never silently invented).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

# The set of event types with a documented sign convention above. Anything
# outside this set is still stored — never dropped — just with zero deltas.
KNOWN_EVENT_TYPES = {"TRADE", "MERGE", "SPLIT", "REDEEM", "REWARD", "MAKER_REBATE", "TAKER_REBATE"}


@dataclass(frozen=True)
class WalletEvent:
    wallet: str
    event_type: str
    ts: int
    tx_hash: str
    condition_id: Optional[str]
    token_id: Optional[str]
    side: Optional[str]
    delta_shares: Decimal
    delta_usdc: Decimal
    price: Decimal
    usdc_size: Decimal
    source: str
    dedupe_key: str
    raw_ref: int
    is_derived: bool = False
