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
      usdc_size is often 0 in the source feed for REDEEM rows —
      Phase 8 appends a derived event for the missing payout when this is zero.
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

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# The set of event types with a documented sign convention above. Anything
# outside this set is still stored — never dropped — just with zero deltas.
KNOWN_EVENT_TYPES = {
    "TRADE",
    "MERGE",
    "SPLIT",
    "REDEEM",
    "REDEEM_PAYOUT",
    "REWARD",
    "MAKER_REBATE",
    "TAKER_REBATE",
}

_HEX_BODY_RE = re.compile(r"^[0-9a-fA-F]+$")


def normalize_condition_id(condition_id: Optional[str]) -> Optional[str]:
    """Canonicalize a condition_id to lowercase Ethereum hex form ("0x...").

    The Data API reports MERGE/REDEEM/CONVERSION rows' conditionId as a
    Postgres bytea literal ("\\xdead...") rather than the "0x..." form used
    by TRADE rows and by Gamma market metadata — same underlying bytes,
    different textual prefix (observed live against RN1: 230/232 ledger
    conditions with no matching market were this, not genuinely missing
    metadata). This rewrites that one known-equivalent encoding; anything
    else that isn't valid hex under a recognized prefix is preserved as-is
    with a logged warning — same "never silently invented, never dropped"
    policy as unrecognized event types above.
    """
    if condition_id is None:
        return None
    if condition_id.startswith("0x") or condition_id.startswith("\\x"):
        body = condition_id[2:]
        if _HEX_BODY_RE.match(body):
            return "0x" + body.lower()
        logger.warning("Malformed condition_id %r; preserving as-is.", condition_id)
        return condition_id
    return condition_id


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
