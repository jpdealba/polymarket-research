"""PnL decomposition projection for Phase 8."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .base import Projection

PNL_DECOMPOSITION_PROJECTION_VERSION = 1
_ZERO = Decimal("0")


@dataclass(frozen=True)
class PnlRebuildStats:
    wallet: str
    events_processed: int
    rows_written: int
    total_pnl: Decimal


@dataclass(frozen=True)
class PnlDecompositionRow:
    wallet: str
    scope: str
    period: str
    directional_pnl: Decimal
    bond_merge_pnl: Decimal
    reward_income: Decimal
    redemption_pnl: Decimal
    fees: Decimal
    computed_at: str
    projection_version: int

    @property
    def total_pnl(self) -> Decimal:
        return (
            self.directional_pnl
            + self.bond_merge_pnl
            + self.reward_income
            + self.redemption_pnl
            - self.fees
        )


class _Position:
    __slots__ = ("qty", "cost")

    def __init__(self) -> None:
        self.qty = _ZERO
        self.cost = _ZERO

    def add(self, shares: Decimal, cost: Decimal) -> None:
        self.qty += shares
        self.cost += cost

    def remove(self, shares: Decimal, proceeds: Decimal) -> Decimal:
        qty_before = self.qty
        cost_before = self.cost
        if qty_before <= _ZERO:
            self.qty = qty_before - shares
            self.cost = _ZERO
            return proceeds
        if shares >= qty_before:
            pnl = proceeds - cost_before
            self.qty = qty_before - shares
            self.cost = _ZERO
            return pnl
        basis = cost_before * shares / qty_before
        self.qty = qty_before - shares
        self.cost = cost_before - basis
        return proceeds - basis

    def close(self, proceeds: Decimal) -> Decimal:
        pnl = proceeds - self.cost
        self.qty = _ZERO
        self.cost = _ZERO
        return pnl


_EVENTS_SQL = text(
    "SELECT id, event_type, ts, condition_id, token_id, delta_shares, delta_usdc "
    "FROM wallet_events WHERE wallet = :wallet ORDER BY ts, id"
)

_INSERT_SQL = text(
    "INSERT INTO pnl_decomposition "
    "(wallet, scope, period, directional_pnl, bond_merge_pnl, reward_income, "
    "redemption_pnl, fees, computed_at, projection_version) "
    "VALUES (:wallet, :scope, 'all', :directional_pnl, :bond_merge_pnl, "
    ":reward_income, :redemption_pnl, :fees, :computed_at, :projection_version)"
)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _load_metadata(
    session: Session,
) -> tuple[dict[str, list[str]], dict[str, str], dict[str, str], dict[str, dict[str, Decimal]]]:
    token_rows = session.execute(
        text(
            "SELECT t.condition_id, t.token_id, COALESCE(m.category, 'unknown') AS category, "
            "m.resolution_prices_json "
            "FROM tokens t JOIN markets m ON m.condition_id = t.condition_id "
            "ORDER BY t.condition_id, t.outcome_index"
        )
    ).fetchall()
    condition_tokens: dict[str, list[str]] = {}
    token_conditions: dict[str, str] = {}
    condition_categories: dict[str, str] = {}
    resolution_prices: dict[str, dict[str, Decimal]] = {}
    for row in token_rows:
        condition_tokens.setdefault(row.condition_id, []).append(row.token_id)
        token_conditions[row.token_id] = row.condition_id
        condition_categories[row.condition_id] = row.category or "unknown"
        if row.resolution_prices_json and row.condition_id not in resolution_prices:
            payload = json.loads(row.resolution_prices_json)
            resolution_prices[row.condition_id] = {
                str(token_id): _decimal(price) for token_id, price in payload.items()
            }
    return condition_tokens, token_conditions, condition_categories, resolution_prices


def _derived_redeem_conditions(session: Session, wallet: str) -> set[str]:
    rows = session.execute(
        text(
            "SELECT DISTINCT condition_id FROM wallet_events "
            "WHERE wallet = :wallet AND event_type = 'REDEEM_PAYOUT' AND is_derived = 1 "
            "AND condition_id IS NOT NULL"
        ),
        {"wallet": wallet.lower()},
    ).fetchall()
    return {row.condition_id for row in rows}


def rebuild_pnl_decomposition(
    session: Session, wallet: str, *, dust_epsilon: Decimal = Decimal("0.000001")
) -> PnlRebuildStats:
    wallet = wallet.lower()
    condition_tokens, token_conditions, condition_categories, resolution_prices = _load_metadata(
        session
    )
    derived_conditions = _derived_redeem_conditions(session, wallet)
    positions: dict[str, _Position] = {}
    buckets: dict[str, dict[str, Decimal]] = {
        "all": {
            "directional_pnl": _ZERO,
            "bond_merge_pnl": _ZERO,
            "reward_income": _ZERO,
            "redemption_pnl": _ZERO,
            "fees": _ZERO,
        }
    }
    events_processed = 0

    def position(token_id: str) -> _Position:
        pos = positions.get(token_id)
        if pos is None:
            pos = positions[token_id] = _Position()
        return pos

    def category_scope(condition_id: Optional[str], token_id: Optional[str] = None) -> str:
        if condition_id is None and token_id is not None:
            condition_id = token_conditions.get(token_id)
        category = condition_categories.get(condition_id or "", "uncategorized")
        return f"category:{category}"

    def add_component(
        component: str,
        amount: Decimal,
        *,
        condition_id: Optional[str] = None,
        token_id: Optional[str] = None,
    ) -> None:
        for scope in ("all", category_scope(condition_id, token_id)):
            row = buckets.setdefault(
                scope,
                {
                    "directional_pnl": _ZERO,
                    "bond_merge_pnl": _ZERO,
                    "reward_income": _ZERO,
                    "redemption_pnl": _ZERO,
                    "fees": _ZERO,
                },
            )
            row[component] += amount

    for event in session.execute(_EVENTS_SQL, {"wallet": wallet}).fetchall():
        events_processed += 1
        etype = event.event_type
        condition_id = event.condition_id

        if etype == "TRADE":
            if event.token_id is None:
                continue
            delta = _decimal(event.delta_shares)
            pos = position(event.token_id)
            if delta > _ZERO:
                pos.add(delta, -_decimal(event.delta_usdc))
            elif delta < _ZERO:
                pnl = pos.remove(-delta, _decimal(event.delta_usdc))
                add_component("directional_pnl", pnl, condition_id=condition_id, token_id=event.token_id)
            continue

        if etype == "SPLIT":
            tokens = condition_tokens.get(condition_id or "", [])
            if not tokens:
                continue
            cost_per_token = -_decimal(event.delta_usdc) / len(tokens)
            for token_id in tokens:
                position(token_id).add(_decimal(event.delta_shares), cost_per_token)
            continue

        if etype == "MERGE":
            tokens = condition_tokens.get(condition_id or "", [])
            if not tokens:
                continue
            shares = -_decimal(event.delta_shares)
            proceeds_per_token = _decimal(event.delta_usdc) / len(tokens)
            for token_id in tokens:
                pnl = position(token_id).remove(shares, proceeds_per_token)
                add_component("bond_merge_pnl", pnl, condition_id=condition_id, token_id=token_id)
            continue

        if etype == "REDEEM":
            if (
                condition_id in derived_conditions
                and _decimal(event.delta_usdc) == _ZERO
            ):
                continue
            tokens = condition_tokens.get(condition_id or "", [])
            if not tokens:
                continue
            proceeds_per_token = _decimal(event.delta_usdc) / len(tokens)
            for token_id in tokens:
                pos = positions.get(token_id)
                if pos is None:
                    continue
                pnl = pos.close(proceeds_per_token)
                add_component("redemption_pnl", pnl, condition_id=condition_id, token_id=token_id)
            continue

        if etype == "REDEEM_PAYOUT":
            tokens = condition_tokens.get(condition_id or "", [])
            prices = resolution_prices.get(condition_id or "", {})
            for token_id in tokens:
                pos = positions.get(token_id)
                if pos is None or abs(pos.qty) <= dust_epsilon:
                    continue
                proceeds = pos.qty * prices.get(token_id, _ZERO)
                pnl = pos.close(proceeds)
                add_component("redemption_pnl", pnl, condition_id=condition_id, token_id=token_id)
            continue

        if etype in {"REWARD", "MAKER_REBATE", "TAKER_REBATE"}:
            add_component("reward_income", _decimal(event.delta_usdc), condition_id=condition_id, token_id=event.token_id)

    computed_at = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "wallet": wallet,
            "scope": scope,
            "directional_pnl": str(values["directional_pnl"]),
            "bond_merge_pnl": str(values["bond_merge_pnl"]),
            "reward_income": str(values["reward_income"]),
            "redemption_pnl": str(values["redemption_pnl"]),
            "fees": str(values["fees"]),
            "computed_at": computed_at,
            "projection_version": PNL_DECOMPOSITION_PROJECTION_VERSION,
        }
        for scope, values in sorted(buckets.items())
    ]
    session.execute(text("DELETE FROM pnl_decomposition WHERE wallet = :wallet"), {"wallet": wallet})
    session.execute(_INSERT_SQL, rows)
    session.commit()
    total = sum(
        (
            values["directional_pnl"]
            + values["bond_merge_pnl"]
            + values["reward_income"]
            + values["redemption_pnl"]
            - values["fees"]
        )
        for scope, values in buckets.items()
        if scope == "all"
    )
    return PnlRebuildStats(wallet=wallet, events_processed=events_processed, rows_written=len(rows), total_pnl=total)


def fetch_pnl_decomposition(
    session: Session, wallet: str, *, by_category: bool = False
) -> list[PnlDecompositionRow]:
    where = "wallet = :wallet"
    params = {"wallet": wallet.lower()}
    if by_category:
        where += " AND scope LIKE 'category:%'"
    else:
        where += " AND scope = 'all'"
    rows = session.execute(
        text(
            "SELECT wallet, scope, period, directional_pnl, bond_merge_pnl, reward_income, "
            "redemption_pnl, fees, computed_at, projection_version "
            f"FROM pnl_decomposition WHERE {where} ORDER BY scope"
        ),
        params,
    ).fetchall()
    return [
        PnlDecompositionRow(
            wallet=row.wallet,
            scope=row.scope,
            period=row.period,
            directional_pnl=_decimal(row.directional_pnl),
            bond_merge_pnl=_decimal(row.bond_merge_pnl),
            reward_income=_decimal(row.reward_income),
            redemption_pnl=_decimal(row.redemption_pnl),
            fees=_decimal(row.fees),
            computed_at=row.computed_at,
            projection_version=int(row.projection_version),
        )
        for row in rows
    ]


class PnlDecompositionProjection(Projection):
    name = "pnl_decomposition"
    version = PNL_DECOMPOSITION_PROJECTION_VERSION

    def __init__(self, dust_epsilon: Decimal = Decimal("0.000001")) -> None:
        self.dust_epsilon = dust_epsilon

    def rebuild(self, session: Session, wallet: str) -> PnlRebuildStats:
        return rebuild_pnl_decomposition(session, wallet, dust_epsilon=self.dust_epsilon)
