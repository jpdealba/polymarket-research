"""Parse one Raw Store /activity payload into normalized WalletEvent rows.

Deterministic: the same raw row always produces the same dedupe_key, so
re-ingesting the same (or an overlapping) raw payload is always safe — see
pmresearch.ingest.runner, which relies on this for idempotency.
"""

from __future__ import annotations

import hashlib
import logging
from decimal import Decimal

from ..ledger.model import WalletEvent, normalize_condition_id

logger = logging.getLogger(__name__)


def _fixed(value: object) -> str:
    """Fixed-precision string for dedupe-key stability across repeated
    parses of the same stored JSON (the source reports 6-decimal sizes)."""
    return f"{Decimal(str(value)):.6f}"


def _dedupe_key(
    wallet: str, tx_hash: str, event_type: str, asset: str, side: str, size: object, price: object, ts: int
) -> str:
    raw = "|".join(
        [wallet, tx_hash, event_type, asset or "", side or "", _fixed(size), _fixed(price), str(ts)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_activity_row(row: dict, *, wallet: str, raw_fetch_id: int, source: str = "dataapi") -> WalletEvent:
    event_type = row["type"]
    asset = row.get("asset") or None
    side = row.get("side") or None
    size = Decimal(str(row.get("size", 0)))
    price = Decimal(str(row.get("price", 0)))
    usdc_size = Decimal(str(row.get("usdcSize", 0)))
    ts = int(row["timestamp"])
    tx_hash = row["transactionHash"]
    condition_id = normalize_condition_id(row.get("conditionId") or None)

    delta_shares = Decimal(0)
    delta_usdc = Decimal(0)
    token_id = asset

    if event_type == "TRADE":
        if side == "BUY":
            delta_shares, delta_usdc = size, -usdc_size
        elif side == "SELL":
            delta_shares, delta_usdc = -size, usdc_size
        else:
            logger.warning(
                "TRADE event with unrecognized side %r (wallet=%s tx=%s); zero delta.",
                side,
                wallet,
                tx_hash,
            )
    elif event_type in {"REWARD", "MAKER_REBATE", "TAKER_REBATE"}:
        delta_usdc = usdc_size
        token_id = None
    elif event_type == "MERGE":
        delta_shares, delta_usdc = -size, usdc_size
        token_id = None
    elif event_type == "SPLIT":
        delta_shares, delta_usdc = size, -usdc_size
        token_id = None
    elif event_type == "REDEEM":
        delta_shares, delta_usdc = -size, usdc_size
        token_id = None
    else:
        logger.warning(
            "Unrecognized activity type %r (wallet=%s tx=%s); preserving as-is with zero delta.",
            event_type,
            wallet,
            tx_hash,
        )
        token_id = None

    dedupe_key = _dedupe_key(wallet, tx_hash, event_type, asset or "", side or "", size, price, ts)

    return WalletEvent(
        wallet=wallet,
        event_type=event_type,
        ts=ts,
        tx_hash=tx_hash,
        condition_id=condition_id,
        token_id=token_id,
        side=side,
        delta_shares=delta_shares,
        delta_usdc=delta_usdc,
        price=price,
        usdc_size=usdc_size,
        source=source,
        dedupe_key=dedupe_key,
        raw_ref=raw_fetch_id,
    )


def parse_activity_payload(
    rows: list[dict], *, wallet: str, raw_fetch_id: int, source: str = "dataapi"
) -> list[WalletEvent]:
    return [
        parse_activity_row(row, wallet=wallet, raw_fetch_id=raw_fetch_id, source=source) for row in rows
    ]
