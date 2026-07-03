"""Derived ledger events for source-reported zero cash legs and for resolved
positions the source never sends any closing event for at all.

Phase 8 deliberately keeps source rows immutable:

  * When the Data API reports a REDEEM cash leg as zero for a resolved
    market, `derive_redeem_payouts` appends one deterministic `REDEEM_PAYOUT`
    event instead of updating the original REDEEM.
  * When a resolved market never produces *any* REDEEM row for a wallet's
    token at all (a worthless losing balance nobody bothered to redeem
    on-chain, or an unclaimed winning one), `derive_resolution_settlements`
    appends one deterministic `RESOLUTION_SETTLEMENT` event closing it out.
    This is a distinct case from the zero-valued-REDEEM one above: there, an
    observed REDEEM exists and only its cash leg is missing; here, no REDEEM
    was ever observed for that token in the first place.

Both jobs are idempotent through the ledger's `dedupe_key` uniqueness.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Callable, Optional

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


@dataclass(frozen=True)
class DeriveProgress:
    wallet: str
    stage: str
    events_processed: int
    events_total: int
    derived_inserted: int
    current_ts: int | None = None


@dataclass(frozen=True)
class ResolutionSettlementStats:
    wallet: str
    resolved_open_tokens_seen: int
    derived_events_inserted: int
    dust_skipped: int


_EVENTS_SQL = text(
    "SELECT id, wallet, event_type, ts, tx_hash, condition_id, token_id, "
    "delta_shares, delta_usdc, usdc_size, raw_ref "
    "FROM wallet_events "
    "WHERE wallet = :wallet AND is_derived = 0 "
    "ORDER BY ts, id"
)

_COUNT_SQL = text(
    "SELECT COUNT(*) AS event_count FROM wallet_events "
    "WHERE wallet = :wallet AND is_derived = 0"
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
    session: Session,
    wallet: str,
    *,
    dust_epsilon: Decimal = Decimal("0.000001"),
    on_progress: Callable[[DeriveProgress], None] | None = None,
    event_progress_interval: int = 100000,
    insert_commit_batch_size: int = 500,
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
    pending_inserted = 0
    nonzero_skipped = 0
    unresolved_skipped = 0
    ingested_at = datetime.now(timezone.utc).isoformat()
    events_total = int(session.execute(_COUNT_SQL, {"wallet": wallet}).scalar_one() or 0)
    events_processed = 0
    _emit_progress(on_progress, wallet, "start", 0, events_total, 0, None)

    def add(token_id: str, shares: Decimal) -> None:
        positions[token_id] = positions.get(token_id, _ZERO) + shares

    def zero_condition(condition_id: Optional[str]) -> None:
        for token_id in condition_tokens.get(condition_id or "", []):
            positions[token_id] = _ZERO

    for event in session.execute(
        _EVENTS_SQL.execution_options(stream_results=True), {"wallet": wallet}
    ):
        events_processed += 1
        etype = event.event_type
        condition_id = event.condition_id
        if event_progress_interval > 0 and events_processed % event_progress_interval == 0:
            _emit_progress(
                on_progress,
                wallet,
                "events",
                events_processed,
                events_total,
                inserted,
                int(event.ts),
            )

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
            pending_inserted += 1
            if pending_inserted >= insert_commit_batch_size:
                session.commit()
                pending_inserted = 0
                _emit_progress(
                    on_progress,
                    wallet,
                    "insert_flush",
                    events_processed,
                    events_total,
                    inserted,
                    int(event.ts),
                )
        zero_condition(condition_id)

    session.commit()
    _emit_progress(
        on_progress,
        wallet,
        "insert_flush",
        events_processed,
        events_total,
        inserted,
        None,
    )
    return DeriveStats(
        wallet=wallet,
        zero_redeems_seen=zero_redeems_seen,
        derived_events_inserted=inserted,
        nonzero_redeems_skipped=nonzero_skipped,
        unresolved_redeems_skipped=unresolved_skipped,
    )


def _emit_progress(
    on_progress: Callable[[DeriveProgress], None] | None,
    wallet: str,
    stage: str,
    events_processed: int,
    events_total: int,
    derived_inserted: int,
    current_ts: int | None,
) -> None:
    if on_progress is None:
        return
    on_progress(
        DeriveProgress(
            wallet=wallet,
            stage=stage,
            events_processed=events_processed,
            events_total=events_total,
            derived_inserted=derived_inserted,
            current_ts=current_ts,
        )
    )


_SETTLEMENT_EVENTS_SQL = text(
    "SELECT id, event_type, ts, tx_hash, condition_id, token_id, "
    "delta_shares, raw_ref "
    "FROM wallet_events "
    "WHERE wallet = :wallet AND is_derived = 0 "
    "ORDER BY ts, id"
)

_INSERT_SETTLEMENT_SQL = text(
    "INSERT INTO wallet_events "
    "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
    "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
    "dedupe_key, ingested_at) "
    "VALUES (:wallet, 'RESOLUTION_SETTLEMENT', :ts, :tx_hash, :condition_id, :token_id, NULL, "
    ":delta_shares, :delta_usdc, :price, :usdc_size, 'derived/resolution_settlement', 1, "
    ":raw_ref, :dedupe_key, :ingested_at) "
    "ON CONFLICT(dedupe_key) DO NOTHING"
)


def _settlement_dedupe_key(wallet: str, condition_id: str, token_id: str) -> str:
    material = f"{wallet}|{condition_id}|{token_id}|RESOLUTION_SETTLEMENT"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _parse_closed_time(value: Optional[str]) -> Optional[int]:
    """Parse `markets.closed_time` ("YYYY-MM-DD HH:MM:SS+00") to a unix ts."""
    if not value:
        return None
    normalized = value.strip().replace(" ", "T")
    if normalized.endswith("+00"):
        normalized = normalized + ":00"
    try:
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return None


def _load_resolved_markets_with_close_ts(
    session: Session,
) -> tuple[dict[str, list[str]], dict[str, dict[str, Decimal]], dict[str, Optional[int]]]:
    rows = session.execute(
        text(
            "SELECT m.condition_id, m.resolution_prices_json, m.closed_time, t.token_id "
            "FROM markets m "
            "JOIN tokens t ON t.condition_id = m.condition_id "
            "WHERE m.closed = 1 AND m.resolution_prices_json IS NOT NULL "
            "ORDER BY m.condition_id, t.outcome_index"
        )
    ).fetchall()
    tokens: dict[str, list[str]] = {}
    prices: dict[str, dict[str, Decimal]] = {}
    closed_ts: dict[str, Optional[int]] = {}
    for row in rows:
        tokens.setdefault(row.condition_id, []).append(row.token_id)
        if row.condition_id not in prices:
            payload = json.loads(row.resolution_prices_json or "{}")
            prices[row.condition_id] = {
                str(token_id): _decimal(price) for token_id, price in payload.items()
            }
            closed_ts[row.condition_id] = _parse_closed_time(row.closed_time)
    return tokens, prices, closed_ts


def derive_resolution_settlements(
    session: Session,
    wallet: str,
    *,
    dust_epsilon: Decimal = Decimal("0.000001"),
    on_progress: Callable[[DeriveProgress], None] | None = None,
    event_progress_interval: int = 100000,
    insert_commit_batch_size: int = 500,
) -> ResolutionSettlementStats:
    """Close out resolved-market positions the source never sent a REDEEM for.

    Some resolved markets never produce any REDEEM row at all for a wallet's
    holding in one of the outcome tokens — nobody redeems a token worth $0
    on-chain, and an unclaimed winning balance is just as possible. Left
    alone, Phase 8 treats that holding as still "open" forever, with its cost
    basis intact, which understates realized PnL and makes Phase 9's
    unrealized PnL look far worse than the economics actually are.

    This replays every observed TRADE/SPLIT/MERGE/REDEEM event (mirroring
    `holdings.py`'s per-token quantity semantics) to find the wallet's final
    quantity per token. For any token that is (a) above dust, (b) in a market
    with a known resolution price, and (c) was never zeroed by an observed
    REDEEM, it appends one deterministic RESOLUTION_SETTLEMENT event closing
    it at `remaining_qty * resolution_price`. A token a real REDEEM already
    touched is untouched here — REDEEM always zeroes every outcome token of
    its condition (CTF redemption burns the whole balance), so nothing is
    left to settle.

    Token-scoped (unlike REDEEM/REDEEM_PAYOUT, which are condition-scoped
    because the source gives no per-token attribution): there is no source
    row to inherit that limitation from, and per-token attribution avoids
    misattributing proceeds to a condition's other outcome the way REDEEM's
    even split does.

    Idempotent via `dedupe_key`. Order-independent with respect to
    `derive_redeem_payouts` (REDEEM_PAYOUT never moves quantity), though the
    CLI runs redeem-payout derivation first for a logical before/after story.
    """
    wallet = wallet.lower()
    condition_tokens, resolution_prices, closed_ts = _load_resolved_markets_with_close_ts(session)
    token_conditions = {
        token_id: condition_id
        for condition_id, tokens in condition_tokens.items()
        for token_id in tokens
    }

    qty: dict[str, Decimal] = {}
    last_ts: dict[str, int] = {}
    last_raw_ref: dict[str, int] = {}
    last_tx_hash: dict[str, str] = {}
    events_total = int(session.execute(_COUNT_SQL, {"wallet": wallet}).scalar_one() or 0)
    events_processed = 0
    _emit_progress(on_progress, wallet, "start", 0, events_total, 0, None)

    def touch(token_id: str, ts: int, raw_ref: int, tx_hash: str) -> None:
        last_ts[token_id] = ts
        last_raw_ref[token_id] = raw_ref
        last_tx_hash[token_id] = tx_hash

    def add(token_id: str, shares: Decimal, event: object) -> None:
        qty[token_id] = qty.get(token_id, _ZERO) + shares
        touch(token_id, event.ts, event.raw_ref, event.tx_hash)

    def zero_condition(condition_id: Optional[str], event: object) -> None:
        for token_id in condition_tokens.get(condition_id or "", []):
            qty[token_id] = _ZERO
            touch(token_id, event.ts, event.raw_ref, event.tx_hash)

    for event in session.execute(
        _SETTLEMENT_EVENTS_SQL.execution_options(stream_results=True), {"wallet": wallet}
    ):
        events_processed += 1
        etype = event.event_type
        condition_id = event.condition_id
        if event_progress_interval > 0 and events_processed % event_progress_interval == 0:
            _emit_progress(
                on_progress, wallet, "events", events_processed, events_total, 0, int(event.ts)
            )

        if etype == "TRADE":
            if event.token_id is not None:
                add(event.token_id, _decimal(event.delta_shares), event)
            continue

        if etype in ("SPLIT", "MERGE"):
            delta = _decimal(event.delta_shares)
            shares = delta if etype == "SPLIT" else -delta
            for token_id in condition_tokens.get(condition_id or "", []):
                add(token_id, shares, event)
            continue

        if etype == "REDEEM":
            # CTF redemption burns the wallet's entire balance of every
            # outcome token of the condition — nothing survives a REDEEM,
            # regardless of its reported cash leg (see holdings.py).
            zero_condition(condition_id, event)
            continue

        # REDEEM_PAYOUT / anything else observed with is_derived=0 doesn't
        # apply here (query already filters to is_derived=0, so in practice
        # only TRADE/SPLIT/MERGE/REDEEM/REWARD-family rows reach this loop).
        continue

    ingested_at = datetime.now(timezone.utc).isoformat()
    resolved_open_tokens_seen = 0
    inserted = 0
    pending_inserted = 0
    dust_skipped = 0

    for token_id in sorted(qty):
        remaining = qty[token_id]
        if abs(remaining) <= dust_epsilon:
            if remaining != _ZERO:
                dust_skipped += 1
            continue
        condition_id = token_conditions.get(token_id)
        if condition_id is None:
            continue
        prices = resolution_prices.get(condition_id)
        if prices is None:
            continue
        raw_ref = last_raw_ref.get(token_id)
        if raw_ref is None:
            # No observed activity to anchor provenance to — should not
            # happen (qty only becomes nonzero via an observed event), but
            # skip rather than invent a raw_fetches reference (ADR 0006).
            continue

        resolved_open_tokens_seen += 1
        price = prices.get(token_id, _ZERO)
        payout = remaining * price
        fallback_ts = last_ts.get(token_id, 0) + 1
        resolution_ts = closed_ts.get(condition_id)
        ts = max(resolution_ts, fallback_ts) if resolution_ts is not None else fallback_ts

        result = session.execute(
            _INSERT_SETTLEMENT_SQL,
            {
                "wallet": wallet,
                "ts": ts,
                "tx_hash": last_tx_hash.get(token_id, "derived"),
                "condition_id": condition_id,
                "token_id": token_id,
                "delta_shares": str(-remaining),
                "delta_usdc": str(payout),
                "price": str(price),
                "usdc_size": str(payout),
                "raw_ref": raw_ref,
                "dedupe_key": _settlement_dedupe_key(wallet, condition_id, token_id),
                "ingested_at": ingested_at,
            },
        )
        if result.rowcount:
            inserted += 1
            pending_inserted += 1
            if pending_inserted >= insert_commit_batch_size:
                session.commit()
                pending_inserted = 0
                _emit_progress(
                    on_progress, wallet, "insert_flush", events_processed, events_total, inserted, ts
                )

    session.commit()
    _emit_progress(
        on_progress, wallet, "insert_flush", events_processed, events_total, inserted, None
    )
    return ResolutionSettlementStats(
        wallet=wallet,
        resolved_open_tokens_seen=resolved_open_tokens_seen,
        derived_events_inserted=inserted,
        dust_skipped=dust_skipped,
    )
