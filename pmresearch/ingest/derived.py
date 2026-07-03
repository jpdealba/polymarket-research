"""Derived ledger events for source-reported zero cash legs.

Phase 8 deliberately keeps source rows immutable: when the Data API reports a
REDEEM cash leg as zero for a resolved market, this module appends one
deterministic `REDEEM_PAYOUT` event instead of updating the original REDEEM.
The job is idempotent through the ledger's `dedupe_key` uniqueness.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

_ZERO = Decimal("0")


@dataclass(frozen=True)
class DeriveStats:
    wallet: str
    zero_redeems_seen: int
    derived_events_inserted: int
    nonzero_redeems_skipped: int
    unresolved_redeems_skipped: int


_EVENTS_SQL = text(
    "SELECT id, wallet, event_type, ts, tx_hash, condition_id, token_id, "
    "delta_shares, delta_usdc, usdc_size, raw_ref "
    "FROM wallet_events "
    "WHERE wallet = :wallet AND is_derived = 0 "
    "ORDER BY ts, id"
)

_INSERT_DERIVED_SQL = text(
    "INSERT INTO wallet_events "
    "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
    "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
    "dedupe_key, ingested_at) "
    "VALUES (:wallet, 'REDEEM_PAYOUT', :ts, :tx_hash, :condition_id, NULL, NULL, "
    "'0', :delta_usdc, '0', :delta_usdc, 'derived/redeem_payout', 1, :raw_ref, "
    ":dedupe_key, :ingested_at) "
    "ON CONFLICT(dedupe_key) DO NOTHING"
)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _dedupe_key(wallet: str, condition_id: str) -> str:
    material = f"{wallet}|{condition_id}|DERIVED_REDEEM"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_resolved_markets(session: Session) -> tuple[dict[str, list[str]], dict[str, dict[str, Decimal]]]:
    rows = session.execute(
        text(
            "SELECT m.condition_id, m.resolution_prices_json, t.token_id "
            "FROM markets m "
            "JOIN tokens t ON t.condition_id = m.condition_id "
            "WHERE m.closed = 1 AND m.resolution_prices_json IS NOT NULL "
            "ORDER BY m.condition_id, t.outcome_index"
        )
    ).fetchall()
    tokens: dict[str, list[str]] = {}
    prices: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        tokens.setdefault(row.condition_id, []).append(row.token_id)
        if row.condition_id not in prices:
            payload = json.loads(row.resolution_prices_json or "{}")
            prices[row.condition_id] = {
                str(token_id): _decimal(price) for token_id, price in payload.items()
            }
    return tokens, prices


def derive_redeem_payouts(
    session: Session, wallet: str, *, dust_epsilon: Decimal = Decimal("0.000001")
) -> DeriveStats:
    """Append deterministic REDEEM_PAYOUT rows for zero-valued REDEEM events.

    Only source rows whose reported `delta_usdc` and `usdc_size` are both zero
    are filled. Nonzero REDEEM rows are left alone so a future API correction
    is never double-counted.
    """
    wallet = wallet.lower()
    condition_tokens, resolution_prices = _load_resolved_markets(session)
    positions: dict[str, Decimal] = {}
    zero_redeems_seen = 0
    inserted = 0
    nonzero_skipped = 0
    unresolved_skipped = 0
    ingested_at = datetime.now(timezone.utc).isoformat()

    def add(token_id: str, shares: Decimal) -> None:
        positions[token_id] = positions.get(token_id, _ZERO) + shares

    def zero_condition(condition_id: Optional[str]) -> None:
        for token_id in condition_tokens.get(condition_id or "", []):
            positions[token_id] = _ZERO

    for event in session.execute(_EVENTS_SQL, {"wallet": wallet}).fetchall():
        etype = event.event_type
        condition_id = event.condition_id

        if etype == "TRADE":
            if event.token_id is not None:
                add(event.token_id, _decimal(event.delta_shares))
            continue

        if etype == "SPLIT":
            for token_id in condition_tokens.get(condition_id or "", []):
                add(token_id, _decimal(event.delta_shares))
            continue

        if etype == "MERGE":
            for token_id in condition_tokens.get(condition_id or "", []):
                add(token_id, _decimal(event.delta_shares))
            continue

        if etype != "REDEEM":
            continue

        if _decimal(event.delta_usdc) != _ZERO or _decimal(event.usdc_size) != _ZERO:
            nonzero_skipped += 1
            zero_condition(condition_id)
            continue

        zero_redeems_seen += 1
        tokens = condition_tokens.get(condition_id or "")
        prices = resolution_prices.get(condition_id or "")
        if not condition_id or not tokens or prices is None:
            unresolved_skipped += 1
            zero_condition(condition_id)
            continue

        payout = sum(
            (
                (qty if abs(qty) > dust_epsilon and qty > _ZERO else _ZERO)
                * prices.get(token_id, _ZERO)
            )
            for token_id in tokens
            for qty in [positions.get(token_id, _ZERO)]
        )
        result = session.execute(
            _INSERT_DERIVED_SQL,
            {
                "wallet": wallet,
                "ts": event.ts,
                "tx_hash": event.tx_hash,
                "condition_id": condition_id,
                "delta_usdc": str(payout),
                "raw_ref": event.raw_ref,
                "dedupe_key": _dedupe_key(wallet, condition_id),
                "ingested_at": ingested_at,
            },
        )
        if result.rowcount:
            inserted += 1
        zero_condition(condition_id)

    session.commit()
    return DeriveStats(
        wallet=wallet,
        zero_redeems_seen=zero_redeems_seen,
        derived_events_inserted=inserted,
        nonzero_redeems_skipped=nonzero_skipped,
        unresolved_redeems_skipped=unresolved_skipped,
    )
