"""Daily equity projection with explicit mark staleness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import Settings
from ..marks.base import Mark
from ..marks.base import Mark
from ..marks.service import MarkService
from .base import Projection

DAILY_EQUITY_PROJECTION_VERSION = 1
_ZERO = Decimal("0")
_DAY_END = time(23, 59, 59, tzinfo=timezone.utc)


@dataclass(frozen=True)
class DailyEquityStats:
    wallet: str
    rows_written: int
    first_date: str | None
    last_date: str | None
    latest_portfolio_value: Decimal
    latest_stale_equity_share: Decimal
    max_drawdown: Decimal


@dataclass(frozen=True)
class DailyEquityRow:
    wallet: str
    date: str
    portfolio_value: Decimal
    realized_pnl_cum: Decimal
    unrealized_pnl: Decimal
    reward_income_cum: Decimal
    drawdown: Decimal
    stale_equity_share: Decimal
    projection_version: int

    @property
    def total_equity(self) -> Decimal:
        return self.portfolio_value + self.realized_pnl_cum + self.reward_income_cum


class _Position:
    __slots__ = ("qty", "cost", "last_trade_price", "last_trade_ts")

    def __init__(self) -> None:
        self.qty = _ZERO
        self.cost = _ZERO
        self.last_trade_price: Decimal | None = None
        self.last_trade_ts: int | None = None

    def add(self, shares: Decimal, cost: Decimal) -> None:
        self.qty += shares
        self.cost += cost

    def remove(self, shares: Decimal, proceeds: Decimal = _ZERO) -> Decimal:
        qty_before = self.qty
        cost_before = self.cost
        if qty_before <= _ZERO:
            self.qty = qty_before - shares
            self.cost = _ZERO
            return proceeds
        if shares >= qty_before:
            self.qty = qty_before - shares
            self.cost = _ZERO
            return proceeds - cost_before
        basis = cost_before * shares / qty_before
        self.qty = qty_before - shares
        self.cost = cost_before - basis
        return proceeds - basis

    def close(self, proceeds: Decimal) -> Decimal:
        pnl = proceeds - self.cost
        self.qty = _ZERO
        self.cost = _ZERO
        return pnl


class _ReplayTradeMarkSource:
    name = "ledger_trade"

    def __init__(self, positions: dict[str, _Position], *, staleness_window_s: int) -> None:
        self.positions = positions
        self.staleness_window_s = staleness_window_s

    def get_mark(self, session: Session, token_id: str, ts: int) -> Mark | None:
        pos = self.positions.get(token_id)
        if pos is None or pos.last_trade_price is None or pos.last_trade_ts is None:
            return None
        age = max(0, ts - pos.last_trade_ts)
        return Mark(
            token_id=token_id,
            ts=ts,
            price=pos.last_trade_price,
            source=self.name,
            mark_age_s=age,
            stale=age > self.staleness_window_s,
            meta={"underlying_ts": pos.last_trade_ts, "fallback": "replay_observed_trade"},
        )


_EVENTS_SQL = text(
    "SELECT id, event_type, ts, condition_id, token_id, delta_shares, delta_usdc, price "
    "FROM wallet_events WHERE wallet = :wallet ORDER BY ts, id"
)

_BOUNDS_SQL = text(
    "SELECT COUNT(*) AS event_count, MIN(ts) AS min_ts, MAX(ts) AS max_ts "
    "FROM wallet_events WHERE wallet = :wallet"
)

_INSERT_SQL = text(
    "INSERT INTO daily_equity "
    "(wallet, date, portfolio_value, realized_pnl_cum, unrealized_pnl, "
    "reward_income_cum, drawdown, stale_equity_share, projection_version) "
    "VALUES (:wallet, :date, :portfolio_value, :realized_pnl_cum, :unrealized_pnl, "
    ":reward_income_cum, :drawdown, :stale_equity_share, :projection_version)"
)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _event_trade_price(event, delta: Decimal) -> Decimal | None:
    price = _decimal(event.price) if hasattr(event, "price") else _ZERO
    if price > _ZERO:
        return price
    if delta == _ZERO:
        return None
    implied = abs(_decimal(event.delta_usdc) / delta)
    return implied if implied > _ZERO else None


def _utc_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).date()


def _day_end_ts(day: date) -> int:
    return int(datetime.combine(day, _DAY_END).timestamp())


def _load_metadata(
    session: Session,
) -> tuple[dict[str, list[str]], dict[str, dict[str, Decimal]], set[str]]:
    rows = session.execute(
        text(
            "SELECT t.condition_id, t.token_id, m.resolution_prices_json, m.closed "
            "FROM tokens t JOIN markets m ON m.condition_id = t.condition_id "
            "ORDER BY t.condition_id, t.outcome_index"
        )
    ).fetchall()
    condition_tokens: dict[str, list[str]] = {}
    resolution_prices: dict[str, dict[str, Decimal]] = {}
    closed_conditions: set[str] = set()
    for row in rows:
        condition_tokens.setdefault(row.condition_id, []).append(row.token_id)
        if row.closed:
            closed_conditions.add(row.condition_id)
        if row.resolution_prices_json and row.condition_id not in resolution_prices:
            payload = json.loads(row.resolution_prices_json)
            resolution_prices[row.condition_id] = {
                str(token_id): _decimal(price) for token_id, price in payload.items()
            }
    return condition_tokens, resolution_prices, closed_conditions


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


def rebuild_daily_equity(
    session: Session,
    wallet: str,
    *,
    settings: Settings | None = None,
    mark_service: MarkService | None = None,
    dust_epsilon: Decimal = Decimal("0.000001"),
    through_date: date | None = None,
) -> DailyEquityStats:
    wallet = wallet.lower()
    bounds = session.execute(_BOUNDS_SQL, {"wallet": wallet}).fetchone()
    session.execute(text("DELETE FROM daily_equity WHERE wallet = :wallet"), {"wallet": wallet})
    if bounds is None or int(bounds.event_count or 0) == 0:
        session.commit()
        return DailyEquityStats(wallet, 0, None, None, _ZERO, _ZERO, _ZERO)

    condition_tokens, resolution_prices, _ = _load_metadata(session)
    derived_conditions = _derived_redeem_conditions(session, wallet)
    positions: dict[str, _Position] = {}
    owns_mark_service = mark_service is None
    if mark_service is None:
        mark_service = MarkService(
            [_ReplayTradeMarkSource(positions, staleness_window_s=24 * 60 * 60)],
            use_persistent_cache=False,
        )
    realized_pnl = _ZERO
    reward_income = _ZERO
    peak_equity: Decimal | None = None
    output_rows: list[dict] = []
    mark_buffer: list[Mark] = []

    first_day = _utc_date(int(bounds.min_ts))
    last_event_day = _utc_date(int(bounds.max_ts))
    end_day = through_date or max(last_event_day, datetime.now(timezone.utc).date())
    event_iter = iter(
        session.execute(
            _EVENTS_SQL.execution_options(stream_results=True),
            {"wallet": wallet},
        )
    )
    next_event = next(event_iter, None)
    current_day = first_day

    def position(token_id: str) -> _Position:
        pos = positions.get(token_id)
        if pos is None:
            pos = positions[token_id] = _Position()
        return pos

    def apply_event(event) -> None:
        nonlocal realized_pnl, reward_income
        etype = event.event_type
        condition_id = event.condition_id
        if etype == "TRADE":
            if event.token_id is None:
                return
            delta = _decimal(event.delta_shares)
            pos = position(event.token_id)
            if delta > _ZERO:
                pos.add(delta, -_decimal(event.delta_usdc))
            elif delta < _ZERO:
                realized_pnl += pos.remove(-delta, _decimal(event.delta_usdc))
            trade_price = _event_trade_price(event, delta)
            if trade_price is not None:
                pos.last_trade_price = trade_price
                pos.last_trade_ts = int(event.ts)
            return

        if etype == "SPLIT":
            tokens = condition_tokens.get(condition_id or "", [])
            if not tokens:
                return
            cost_per_token = -_decimal(event.delta_usdc) / len(tokens)
            for token_id in tokens:
                position(token_id).add(_decimal(event.delta_shares), cost_per_token)
            return

        if etype == "MERGE":
            tokens = condition_tokens.get(condition_id or "", [])
            if not tokens:
                return
            shares = -_decimal(event.delta_shares)
            proceeds_per_token = _decimal(event.delta_usdc) / len(tokens)
            for token_id in tokens:
                realized_pnl += position(token_id).remove(shares, proceeds_per_token)
            return

        if etype == "REDEEM":
            if condition_id in derived_conditions and _decimal(event.delta_usdc) == _ZERO:
                return
            tokens = condition_tokens.get(condition_id or "", [])
            if not tokens:
                return
            proceeds_per_token = _decimal(event.delta_usdc) / len(tokens)
            for token_id in tokens:
                pos = positions.get(token_id)
                if pos is not None:
                    realized_pnl += pos.close(proceeds_per_token)
            return

        if etype == "REDEEM_PAYOUT":
            tokens = condition_tokens.get(condition_id or "", [])
            prices = resolution_prices.get(condition_id or "", {})
            for token_id in tokens:
                pos = positions.get(token_id)
                if pos is None or abs(pos.qty) <= dust_epsilon:
                    continue
                realized_pnl += pos.close(pos.qty * prices.get(token_id, _ZERO))
            return

        if etype in {"REWARD", "MAKER_REBATE", "TAKER_REBATE"}:
            reward_income += _decimal(event.delta_usdc)

    def snapshot(day: date) -> None:
        nonlocal peak_equity
        ts = _day_end_ts(day)
        portfolio_value = _ZERO
        open_cost = _ZERO
        stale_value = _ZERO
        gross_value = _ZERO
        for token_id, pos in sorted(positions.items()):
            if abs(pos.qty) <= dust_epsilon:
                continue
            open_cost += pos.cost
            mark = mark_service.get_mark(session, token_id, ts, persist=False)
            if mark is None:
                continue
            mark_buffer.append(mark)
            if len(mark_buffer) >= 5000:
                mark_service.persist_marks(session, mark_buffer)
                mark_buffer.clear()
            value = pos.qty * mark.price
            portfolio_value += value
            gross_value += abs(value)
            if mark.stale:
                stale_value += abs(value)
        stale_share = stale_value / gross_value if gross_value else _ZERO
        unrealized = portfolio_value - open_cost
        total_equity = portfolio_value + realized_pnl + reward_income
        if peak_equity is None or total_equity > peak_equity:
            peak_equity = total_equity
        drawdown = (peak_equity - total_equity) if peak_equity is not None else _ZERO
        output_rows.append(
            {
                "wallet": wallet,
                "date": day.isoformat(),
                "portfolio_value": str(portfolio_value),
                "realized_pnl_cum": str(realized_pnl),
                "unrealized_pnl": str(unrealized),
                "reward_income_cum": str(reward_income),
                "drawdown": str(drawdown),
                "stale_equity_share": str(stale_share),
                "projection_version": DAILY_EQUITY_PROJECTION_VERSION,
            }
        )

    try:
        while current_day <= end_day:
            next_day = current_day + timedelta(days=1)
            while next_event is not None and _utc_date(int(next_event.ts)) < next_day:
                apply_event(next_event)
                next_event = next(event_iter, None)
            snapshot(current_day)
            current_day = next_day
    finally:
        if mark_buffer:
            mark_service.persist_marks(session, mark_buffer)
            mark_buffer.clear()
        if owns_mark_service and mark_service is not None:
            mark_service.close()

    for start in range(0, len(output_rows), 5000):
        session.execute(_INSERT_SQL, output_rows[start : start + 5000])
    session.commit()

    rows = fetch_daily_equity(session, wallet)
    latest = rows[-1] if rows else None
    max_drawdown = max((row.drawdown for row in rows), default=_ZERO)
    return DailyEquityStats(
        wallet=wallet,
        rows_written=len(output_rows),
        first_date=output_rows[0]["date"] if output_rows else None,
        last_date=output_rows[-1]["date"] if output_rows else None,
        latest_portfolio_value=latest.portfolio_value if latest else _ZERO,
        latest_stale_equity_share=latest.stale_equity_share if latest else _ZERO,
        max_drawdown=max_drawdown,
    )


def fetch_daily_equity(session: Session, wallet: str) -> list[DailyEquityRow]:
    rows = session.execute(
        text(
            "SELECT wallet, date, portfolio_value, realized_pnl_cum, unrealized_pnl, "
            "reward_income_cum, drawdown, stale_equity_share, projection_version "
            "FROM daily_equity WHERE wallet = :wallet ORDER BY date"
        ),
        {"wallet": wallet.lower()},
    ).fetchall()
    return [
        DailyEquityRow(
            wallet=row.wallet,
            date=row.date,
            portfolio_value=_decimal(row.portfolio_value),
            realized_pnl_cum=_decimal(row.realized_pnl_cum),
            unrealized_pnl=_decimal(row.unrealized_pnl),
            reward_income_cum=_decimal(row.reward_income_cum),
            drawdown=_decimal(row.drawdown),
            stale_equity_share=_decimal(row.stale_equity_share),
            projection_version=int(row.projection_version),
        )
        for row in rows
    ]


def latest_daily_equity(session: Session, wallet: str) -> DailyEquityRow | None:
    rows = fetch_daily_equity(session, wallet)
    return rows[-1] if rows else None


class DailyEquityProjection(Projection):
    name = "daily_equity"
    version = DAILY_EQUITY_PROJECTION_VERSION

    def __init__(self, settings: Settings, dust_epsilon: Decimal = Decimal("0.000001")) -> None:
        self.settings = settings
        self.dust_epsilon = dust_epsilon

    def rebuild(self, session: Session, wallet: str) -> DailyEquityStats:
        return rebuild_daily_equity(
            session, wallet, settings=self.settings, dust_epsilon=self.dust_epsilon
        )
