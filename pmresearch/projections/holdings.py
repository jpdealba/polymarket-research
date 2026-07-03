"""Holdings projection: replay wallet_events into current per-token quantity
and running weighted-average cost (Phase 4, ADR 0002/0003).

Quantity/cost semantics per event type (signed deltas per ledger/model.py):

  TRADE   token-scoped. BUY adds shares at the trade's USDC outflow; SELL
          removes shares and releases cost basis proportionally at the
          running WAC (never recomputed from remaining fills).

  SPLIT   condition-scoped (token_id is NULL in the ledger): $1 USDC mints
          one share of every outcome token of the condition. Each mapped
          token gains `size` shares; the reported USDC outflow is allocated
          evenly across the pair (0.50/share for a binary market).

  MERGE   condition-scoped: the mirror of SPLIT. Each mapped token loses
          `size` shares, releasing basis at that token's own WAC. The USDC
          proceeds do not touch holdings (realized PnL is Phase 6's job).

  REDEEM  condition-scoped: redeemPositions burns the wallet's entire
          balance of every outcome token of the condition (partial
          redemption does not exist on the CTF), so REDEEM zeroes all of the
          condition's tokens — the winning token per the plan's convention,
          and any worthless losing-side remainder with it.

  REWARD / MAKER_REBATE / TAKER_REBATE / unknown types — USDC-only or
          zero-delta; no quantity effect.

MERGE/SPLIT/REDEEM rows carry token_id NULL, so they resolve to tokens via
the Phase 3 `tokens` dimension. Conditions without token rows are skipped
and surfaced as data-quality warnings (run `pmr markets sync` first).

A holding driven negative beyond the dust epsilon means missed events; it is
logged and reported in the rebuild stats, never clamped (ADR 0006). All
arithmetic is Decimal end-to-end; qty/wac are persisted as decimal strings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..ledger.replay import stream_events
from .base import Projection

logger = logging.getLogger(__name__)

HOLDINGS_PROJECTION_VERSION = 1

_ZERO = Decimal("0")

# How many individual negative-qty tokens get their own detailed warning
# line before we collapse into the aggregate count.
_NEGATIVE_DETAIL_LIMIT = 25

_INSERT_SQL = text(
    "INSERT INTO holdings (wallet, token_id, qty, wac_cost, as_of_ts, projection_version) "
    "VALUES (:wallet, :token_id, :qty, :wac_cost, :as_of_ts, :projection_version)"
)


@dataclass(frozen=True)
class HoldingsRebuildStats:
    wallet: str
    events_processed: int
    tokens_written: int
    nonzero_tokens: int
    negative_qty_tokens: int
    negative_qty_events: int
    unmapped_condition_events: int
    unmapped_condition_ids: int
    as_of_ts: int


class _Position:
    __slots__ = ("qty", "cost", "as_of_ts")

    def __init__(self) -> None:
        self.qty = _ZERO
        self.cost = _ZERO  # total cost basis of the current holding
        self.as_of_ts = 0

    def add(self, shares: Decimal, cost: Decimal) -> None:
        self.qty += shares
        self.cost += cost

    def remove(self, shares: Decimal) -> None:
        """Remove `shares`, releasing basis proportionally at the running WAC."""
        if self.qty > _ZERO:
            matched = min(shares, self.qty)
            if matched == self.qty:
                self.cost = _ZERO
            else:
                self.cost -= self.cost * matched / self.qty
        self.qty -= shares
        if self.qty <= _ZERO:
            self.cost = _ZERO

    def zero(self) -> None:
        self.qty = _ZERO
        self.cost = _ZERO


def _load_condition_tokens(session: Session) -> dict[str, list[str]]:
    rows = session.execute(
        text("SELECT condition_id, token_id FROM tokens ORDER BY condition_id, outcome_index")
    )
    mapping: dict[str, list[str]] = {}
    for row in rows:
        mapping.setdefault(row.condition_id, []).append(row.token_id)
    return mapping


def rebuild_holdings(
    session: Session, wallet: str, *, dust_epsilon: Decimal = Decimal("0.000001")
) -> HoldingsRebuildStats:
    """Drop and rebuild the holdings rows for one wallet from its ledger."""
    wallet = wallet.lower()
    condition_tokens = _load_condition_tokens(session)

    positions: dict[str, _Position] = {}
    events_processed = 0
    negative_qty_events = 0
    negative_tokens_seen: set[str] = set()
    unmapped_condition_events = 0
    unmapped_conditions: set[str] = set()
    max_ts = 0

    def position(token_id: str) -> _Position:
        pos = positions.get(token_id)
        if pos is None:
            pos = positions[token_id] = _Position()
        return pos

    def check_negative(token_id: str, pos: _Position, event_id: int) -> None:
        nonlocal negative_qty_events
        if pos.qty < -dust_epsilon:
            negative_qty_events += 1
            if token_id not in negative_tokens_seen:
                negative_tokens_seen.add(token_id)
                if len(negative_tokens_seen) <= _NEGATIVE_DETAIL_LIMIT:
                    logger.warning(
                        "Data quality: holdings went negative (wallet=%s token=%s qty=%s "
                        "event_id=%d) — likely missed events upstream; not clamped.",
                        wallet,
                        token_id,
                        pos.qty,
                        event_id,
                    )

    for event in stream_events(session, wallet=wallet):
        events_processed += 1
        etype = event.event_type
        ts = event.ts
        if ts > max_ts:
            max_ts = ts

        if etype == "TRADE":
            if event.token_id is None:
                logger.warning(
                    "TRADE event without token_id skipped (wallet=%s event_id=%d).",
                    wallet,
                    event.id,
                )
                continue
            delta = Decimal(event.delta_shares)
            pos = position(event.token_id)
            if delta >= _ZERO:
                # BUY: delta_usdc is the negative outflow.
                pos.add(delta, -Decimal(event.delta_usdc))
            else:
                pos.remove(-delta)
                check_negative(event.token_id, pos, event.id)
            pos.as_of_ts = ts

        elif etype in ("MERGE", "SPLIT", "REDEEM"):
            tokens = condition_tokens.get(event.condition_id or "")
            if not tokens:
                unmapped_condition_events += 1
                if event.condition_id:
                    unmapped_conditions.add(event.condition_id)
                continue
            if etype == "REDEEM":
                for token_id in tokens:
                    pos = positions.get(token_id)
                    if pos is not None:
                        pos.zero()
                        pos.as_of_ts = ts
            elif etype == "MERGE":
                size = -Decimal(event.delta_shares)
                for token_id in tokens:
                    pos = position(token_id)
                    pos.remove(size)
                    check_negative(token_id, pos, event.id)
                    pos.as_of_ts = ts
            else:  # SPLIT
                size = Decimal(event.delta_shares)
                cost_per_token = -Decimal(event.delta_usdc) / len(tokens)
                for token_id in tokens:
                    pos = position(token_id)
                    pos.add(size, cost_per_token)
                    pos.as_of_ts = ts

        # REWARD / rebates / unknown types: no quantity effect.

    if unmapped_conditions:
        sample = sorted(unmapped_conditions)[:5]
        logger.warning(
            "Data quality: %d MERGE/SPLIT/REDEEM events over %d conditions without token "
            "metadata were skipped (wallet=%s, sample=%s) — run `pmr markets sync`.",
            unmapped_condition_events,
            len(unmapped_conditions),
            wallet,
            sample,
        )

    rows = []
    nonzero_tokens = 0
    negative_final = 0
    for token_id in sorted(positions):
        pos = positions[token_id]
        if abs(pos.qty) > dust_epsilon:
            nonzero_tokens += 1
            wac = pos.cost / pos.qty if pos.qty > _ZERO else _ZERO
        else:
            wac = _ZERO
        if pos.qty < -dust_epsilon:
            negative_final += 1
        rows.append(
            {
                "wallet": wallet,
                "token_id": token_id,
                "qty": str(pos.qty),
                "wac_cost": str(wac),
                "as_of_ts": pos.as_of_ts,
                "projection_version": HOLDINGS_PROJECTION_VERSION,
            }
        )

    if negative_final:
        logger.warning(
            "Data quality: %d tokens ended with negative holdings for wallet=%s.",
            negative_final,
            wallet,
        )

    session.execute(text("DELETE FROM holdings WHERE wallet = :w"), {"w": wallet})
    for start in range(0, len(rows), 5000):
        session.execute(_INSERT_SQL, rows[start : start + 5000])
    session.commit()

    return HoldingsRebuildStats(
        wallet=wallet,
        events_processed=events_processed,
        tokens_written=len(rows),
        nonzero_tokens=nonzero_tokens,
        negative_qty_tokens=negative_final,
        negative_qty_events=negative_qty_events,
        unmapped_condition_events=unmapped_condition_events,
        unmapped_condition_ids=len(unmapped_conditions),
        as_of_ts=max_ts,
    )


class HoldingsProjection(Projection):
    name = "holdings"
    version = HOLDINGS_PROJECTION_VERSION

    def __init__(self, dust_epsilon: Decimal = Decimal("0.000001")) -> None:
        self.dust_epsilon = dust_epsilon

    def rebuild(self, session: Session, wallet: str) -> HoldingsRebuildStats:
        return rebuild_holdings(session, wallet, dust_epsilon=self.dust_epsilon)


def fetch_holdings(
    session: Session, wallet: str, *, nonzero: bool = False, dust_epsilon: Optional[Decimal] = None
) -> list:
    """Holdings rows for display, joined to token/market metadata when present."""
    query = (
        "SELECT h.token_id, h.qty, h.wac_cost, h.as_of_ts, "
        "t.condition_id, t.outcome_label, m.question "
        "FROM holdings h "
        "LEFT JOIN tokens t ON t.token_id = h.token_id "
        "LEFT JOIN markets m ON m.condition_id = t.condition_id "
        "WHERE h.wallet = :w "
        "ORDER BY h.token_id"
    )
    rows = session.execute(text(query), {"w": wallet.lower()}).fetchall()
    if nonzero:
        eps = dust_epsilon if dust_epsilon is not None else Decimal("0.000001")
        rows = [row for row in rows if abs(Decimal(row.qty)) > eps]
    return rows
