"""Inventory lifecycle simulation for event_inventory_cycling_v1."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from .engine import DecisionContext, RuleDecision, _TransientResult
from .risk import RiskLimits
from .scenarios import ScenarioConfig, SimOrder, decide_fill

_ZERO = Decimal("0")
_ONE = Decimal("1")
_FEE_RATE = Decimal("0.002")


@dataclass(frozen=True)
class InventoryCyclingConfig:
    max_bond_cost: Decimal = Decimal("0.98")
    min_bond_delta: Decimal = Decimal("0")
    max_unpaired_inventory: Decimal = Decimal("100")
    max_event_exposure: Decimal = Decimal("250")
    max_position_per_token: Decimal = Decimal("100")
    max_capital: Decimal = Decimal("250")
    max_order_size: Decimal = Decimal("10")
    min_merge_qty: Decimal = Decimal("1")
    auto_merge_enabled: bool = True
    recycle_capital_enabled: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "max_bond_cost": str(self.max_bond_cost),
            "min_bond_delta": str(self.min_bond_delta),
            "max_unpaired_inventory": str(self.max_unpaired_inventory),
            "max_event_exposure": str(self.max_event_exposure),
            "max_position_per_token": str(self.max_position_per_token),
            "max_capital": str(self.max_capital),
            "max_order_size": str(self.max_order_size),
            "min_merge_qty": str(self.min_merge_qty),
            "auto_merge_enabled": self.auto_merge_enabled,
            "recycle_capital_enabled": self.recycle_capital_enabled,
        }


@dataclass
class LifecycleMetrics:
    merge_count: int = 0
    merged_qty: Decimal = _ZERO
    released_capital_total: Decimal = _ZERO
    capital_recycled_total: Decimal = _ZERO
    redeem_count: int = 0
    redeem_pnl: Decimal = _ZERO
    trading_pnl: Decimal = _ZERO
    merge_pnl: Decimal = _ZERO
    unresolved_inventory_value: Decimal = _ZERO
    max_unpaired_inventory: Decimal = _ZERO
    avg_unpaired_inventory: Decimal = _ZERO
    bond_inventory_created: Decimal = _ZERO
    bond_inventory_merged: Decimal = _ZERO
    capital_turnover_ratio: Decimal = _ZERO

    def to_dict(self) -> dict[str, object]:
        return {
            "merge_count": self.merge_count,
            "merged_qty": str(self.merged_qty),
            "released_capital_total": str(self.released_capital_total),
            "capital_recycled_total": str(self.capital_recycled_total),
            "redeem_count": self.redeem_count,
            "redeem_pnl": str(self.redeem_pnl),
            "trading_pnl": str(self.trading_pnl),
            "merge_pnl": str(self.merge_pnl),
            "unresolved_inventory_value": str(self.unresolved_inventory_value),
            "max_unpaired_inventory": str(self.max_unpaired_inventory),
            "avg_unpaired_inventory": str(self.avg_unpaired_inventory),
            "bond_inventory_created": str(self.bond_inventory_created),
            "bond_inventory_merged": str(self.bond_inventory_merged),
            "capital_turnover_ratio": str(self.capital_turnover_ratio),
        }


@dataclass
class InventoryLot:
    qty: Decimal = _ZERO
    cost: Decimal = _ZERO
    mark_price: Decimal = _ZERO

    @property
    def avg_cost(self) -> Decimal:
        if self.qty <= _ZERO:
            return _ZERO
        return self.cost / self.qty

    @property
    def value(self) -> Decimal:
        return self.qty * self.mark_price


@dataclass
class InventoryLifecycleState:
    positions: dict[str, dict[str, InventoryLot]] = field(default_factory=dict)
    cash: Decimal = _ZERO
    locked_capital: Decimal = _ZERO
    released_credit: Decimal = _ZERO
    turnover: Decimal = _ZERO
    max_locked_capital: Decimal = _ZERO
    peak_value: Decimal = _ZERO
    max_drawdown: Decimal = _ZERO
    unpaired_samples: list[Decimal] = field(default_factory=list)

    def lot(self, condition_id: str, token_id: str) -> InventoryLot:
        return self.positions.setdefault(condition_id, {}).setdefault(token_id, InventoryLot())

    def tokens(self, condition_id: str) -> dict[str, InventoryLot]:
        return self.positions.setdefault(condition_id, {})

    def mark(self, condition_id: str, token_id: str, price: Optional[Decimal]) -> None:
        if price is not None:
            self.lot(condition_id, token_id).mark_price = price
        self._update_path()

    def value(self) -> Decimal:
        return self.cash + sum((lot.value for tokens in self.positions.values() for lot in tokens.values()), _ZERO)

    def event_exposure(self, condition_id: str) -> Decimal:
        return sum((lot.value for lot in self.tokens(condition_id).values()), _ZERO)

    def unpaired_inventory(self, condition_id: str) -> Decimal:
        return sum((lot.qty for lot in self.tokens(condition_id).values() if lot.qty > _ZERO), _ZERO)

    def bond_inventory(self, condition_id: str) -> Decimal:
        lots = [lot.qty for lot in self.tokens(condition_id).values() if lot.qty > _ZERO]
        if len(lots) < 2:
            return _ZERO
        return min(lots)

    def available_capital(self, config: InventoryCyclingConfig) -> Decimal:
        credit = self.released_credit if config.recycle_capital_enabled else _ZERO
        return config.max_capital + credit - self.locked_capital

    def apply_buy(self, condition_id: str, token_id: str, price: Decimal, size: Decimal) -> Decimal:
        notional = price * size
        fee = notional * _FEE_RATE
        lot = self.lot(condition_id, token_id)
        lot.qty += size
        lot.cost += notional + fee
        lot.mark_price = price
        self.cash -= notional + fee
        self.locked_capital += notional + fee
        self.turnover += notional
        self._update_path()
        return fee

    def merge(self, condition_id: str, merge_qty: Decimal) -> tuple[Decimal, Decimal, dict, dict]:
        before = self.inventory_json(condition_id)
        tokens = self.tokens(condition_id)
        token_ids = sorted([token_id for token_id, lot in tokens.items() if lot.qty > _ZERO])
        if len(token_ids) < 2 or merge_qty <= _ZERO:
            return _ZERO, _ZERO, before, before
        t0, t1 = token_ids[:2]
        lot0 = tokens[t0]
        lot1 = tokens[t1]
        lot0_avg = lot0.avg_cost
        lot1_avg = lot1.avg_cost
        removed_cost = min(lot0.cost, lot0_avg * merge_qty) + min(lot1.cost, lot1_avg * merge_qty)
        lot0.qty -= merge_qty
        lot1.qty -= merge_qty
        lot0.cost = max(_ZERO, lot0.cost - lot0_avg * merge_qty)
        lot1.cost = max(_ZERO, lot1.cost - lot1_avg * merge_qty)
        proceeds = merge_qty
        self.cash += proceeds
        self.locked_capital = max(_ZERO, self.locked_capital - removed_cost)
        self.released_credit += proceeds
        self._update_path()
        return proceeds, proceeds - removed_cost, before, self.inventory_json(condition_id)

    def inventory_json(self, condition_id: str) -> dict[str, str]:
        return {
            token_id: str(lot.qty)
            for token_id, lot in sorted(self.tokens(condition_id).items())
            if lot.qty != _ZERO
        }

    def sample_unpaired(self) -> Decimal:
        total = sum((self.unpaired_inventory(cid) for cid in self.positions), _ZERO)
        self.unpaired_samples.append(total)
        return total

    def _update_path(self) -> None:
        if self.locked_capital > self.max_locked_capital:
            self.max_locked_capital = self.locked_capital
        value = self.value()
        if value > self.peak_value:
            self.peak_value = value
        drawdown = self.peak_value - value
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown


def merge_qty_for_condition(state: InventoryLifecycleState, condition_id: str) -> Decimal:
    lots = [lot.qty for lot in state.tokens(condition_id).values() if lot.qty > _ZERO]
    if len(lots) < 2:
        return _ZERO
    return min(lots)


def simulate_redeem(
    state: InventoryLifecycleState,
    resolution_prices: dict[str, dict[str, Decimal]],
    *,
    ts: int,
) -> tuple[list[dict], LifecycleMetrics]:
    events: list[dict] = []
    metrics = LifecycleMetrics()
    for condition_id, tokens in sorted(state.positions.items()):
        prices = resolution_prices.get(condition_id, {})
        if not prices:
            metrics.unresolved_inventory_value += sum((lot.value for lot in tokens.values()), _ZERO)
            continue
        for token_id, lot in sorted(tokens.items()):
            if lot.qty <= _ZERO:
                continue
            price = prices.get(token_id, _ZERO)
            payout = lot.qty * price
            pnl = payout - lot.cost
            before = state.inventory_json(condition_id)
            state.cash += payout
            state.locked_capital = max(_ZERO, state.locked_capital - lot.cost)
            qty = lot.qty
            lot.qty = _ZERO
            lot.cost = _ZERO
            after = state.inventory_json(condition_id)
            metrics.redeem_count += 1
            metrics.redeem_pnl += pnl
            events.append(
                {
                    "ts": ts,
                    "event_type": "REDEEM_SIMULATED",
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "qty": str(qty),
                    "usdc_delta": str(payout),
                    "capital_released": str(payout),
                    "inventory_before_json": json.dumps(before, sort_keys=True),
                    "inventory_after_json": json.dumps(after, sort_keys=True),
                }
            )
    return events, metrics


def simulate_inventory_cycling(
    rows: list[dict],
    scenario: ScenarioConfig,
    risk_limits: RiskLimits,
    *,
    config: InventoryCyclingConfig,
    resolution_prices: Optional[dict[str, dict[str, Decimal]]] = None,
    collect_details: bool = True,
    pre_order_filter: Optional[Callable[[DecisionContext, InventoryLifecycleState, SimOrder], Optional[str]]] = None,
) -> _TransientResult:
    state = InventoryLifecycleState()
    resolution_prices = resolution_prices or {}
    orders: list[dict] = []
    skipped_orders: list[dict] = []
    fills: list[dict] = []
    inventory: list[dict] = []
    daily_pnl: dict[str, dict] = {}
    lifecycle_events: list[dict] = []
    skipped_by_reason: dict[str, int] = {}
    fill_seq = 0
    rule_fires = 0
    orders_count = 0
    fills_count = 0
    risk_prevented = 0
    stale_excluded = 0
    max_event_exposure_seen = _ZERO
    max_unpaired_seen = _ZERO
    bond_created = _ZERO
    latest_ts = 0

    for row in rows:
        ctx = DecisionContext.from_row(row)
        latest_ts = max(latest_ts, ctx.trade_ts)
        condition_id = ctx.condition_id or f"token:{ctx.token_id}"
        mid = ctx.decimal("mid_before")
        state.mark(condition_id, ctx.token_id, mid)
        max_unpaired_seen = max(max_unpaired_seen, state.sample_unpaired())

        decision = _decide_inventory_order(ctx, state, scenario, config)
        if not decision.applies or decision.order is None:
            continue
        rule_fires += 1

        if pre_order_filter is not None:
            filter_reason = pre_order_filter(ctx, state, decision.order)
            if filter_reason is not None:
                skipped_by_reason[filter_reason] = skipped_by_reason.get(filter_reason, 0) + 1
                if collect_details:
                    skipped_orders.append(_skip_row(ctx, decision, filter_reason))
                continue

        reason = _risk_skip_reason(ctx, state, decision.order, config, risk_limits)
        if reason is not None:
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
            risk_prevented += 1
            if collect_details:
                skipped_orders.append(_skip_row(ctx, decision, reason))
            continue

        order_index = orders_count
        orders_count += 1
        if collect_details:
            orders.append(
                {
                    "run_id": 0,
                    "event_id": ctx.event_id,
                    "token_id": ctx.token_id,
                    "condition_id": ctx.condition_id,
                    "side": decision.order.side,
                    "order_price": str(decision.order.order_price),
                    "order_size": str(decision.order.order_size),
                    "rule_fires": 1,
                    "rule_explanation": decision.explanation,
                    "context_status": ctx.context_status,
                    "book_age_s": ctx.integer("book_before_age_s"),
                    "stale_excluded": 0,
                    "created_ts": ctx.trade_ts,
                }
            )

        if _is_stale(ctx, risk_limits):
            stale_excluded += 1
            continue

        assumption = decide_fill(
            order=decision.order,
            scenario=scenario,
            context_status=ctx.context_status,
            book_age_s=ctx.integer("book_before_age_s"),
            spread_bps=ctx.decimal("spread_bps"),
            bid_depth_top1=ctx.decimal("bid_depth_top1"),
            ask_depth_top1=ctx.decimal("ask_depth_top1"),
            deterministic_seq=fill_seq,
        )
        fill_seq += 1
        if not assumption.would_fill or assumption.fill_price is None or assumption.fill_size is None:
            continue

        fee = state.apply_buy(condition_id, ctx.token_id, assumption.fill_price, assumption.fill_size)
        notional = assumption.fill_price * assumption.fill_size
        fills_count += 1
        max_event_exposure_seen = max(max_event_exposure_seen, state.event_exposure(condition_id))
        bond_created += max(_ZERO, state.bond_inventory(condition_id) - bond_created)
        day = _ts_to_date(ctx.trade_ts)
        daily = daily_pnl.setdefault(day, {"pnl": _ZERO, "turnover": _ZERO, "breaches": 0, "fills": 0})
        daily["pnl"] = state.value()
        daily["turnover"] += notional
        daily["fills"] += 1

        if collect_details:
            fills.append(
                {
                    "run_id": 0,
                    "order_index": order_index,
                    "event_id": ctx.event_id,
                    "token_id": ctx.token_id,
                    "condition_id": ctx.condition_id,
                    "side": "BUY",
                    "fill_price": str(assumption.fill_price),
                    "fill_size": str(assumption.fill_size),
                    "fill_notional_usdc": str(notional),
                    "estimated_fee": str(fee),
                    "scenario": scenario.name,
                    "fill_reason": assumption.fill_reason,
                    "filled_ts": ctx.trade_ts,
                }
            )
            inventory.append(_inventory_row(ctx, state))

        if config.auto_merge_enabled:
            merge_qty = merge_qty_for_condition(state, condition_id)
            if merge_qty >= config.min_merge_qty:
                proceeds, merge_pnl, before, after = state.merge(condition_id, merge_qty)
                lifecycle_events.append(
                    {
                        "ts": ctx.trade_ts,
                        "event_type": "MERGE_SIMULATED",
                        "condition_id": condition_id,
                        "token_id": None,
                        "qty": str(merge_qty),
                        "usdc_delta": str(proceeds),
                        "merge_pnl": str(merge_pnl),
                        "capital_released": str(proceeds),
                        "inventory_before_json": json.dumps(before, sort_keys=True),
                        "inventory_after_json": json.dumps(after, sort_keys=True),
                    }
                )
                lifecycle_events.append(
                    {
                        "ts": ctx.trade_ts,
                        "event_type": "CAPITAL_RELEASED",
                        "condition_id": condition_id,
                        "token_id": None,
                        "qty": str(merge_qty),
                        "usdc_delta": str(proceeds),
                        "capital_released": str(proceeds),
                        "inventory_before_json": json.dumps(before, sort_keys=True),
                        "inventory_after_json": json.dumps(after, sort_keys=True),
                    }
                )
                max_unpaired_seen = max(max_unpaired_seen, state.sample_unpaired())

    redeem_events, redeem_metrics = simulate_redeem(state, resolution_prices, ts=latest_ts + 1)
    lifecycle_events.extend(redeem_events)
    lifecycle = _lifecycle_metrics(state, lifecycle_events, redeem_metrics, max_unpaired_seen, bond_created, config)
    net_pnl = state.value()
    transient = _TransientResult(
        orders=orders,
        skipped_orders=skipped_orders,
        fills=fills,
        inventory=inventory,
        daily_pnl=daily_pnl,
        risk_events=[],
        market_attribution=[],
        candidate_signals_count=rule_fires,
        orders_count=orders_count,
        fills_count=fills_count,
        fill_rate=(Decimal(fills_count) / Decimal(rule_fires) if rule_fires else None),
        simulated_pnl=net_pnl,
        net_pnl=net_pnl,
        max_drawdown=state.max_drawdown,
        max_inventory=max_unpaired_seen,
        capital_required=state.max_locked_capital,
        turnover=state.turnover,
        skipped_orders_count=sum(skipped_by_reason.values()),
        skipped_by_reason=skipped_by_reason,
        risk_prevented_count=risk_prevented,
        risk_breaches=0,
        stale_context_excluded=stale_excluded,
    )
    transient.lifecycle_events = lifecycle_events
    transient.lifecycle_metrics = lifecycle
    transient.max_event_exposure_seen = max_event_exposure_seen
    return transient


def insert_lifecycle_outputs(session: Session, run_id: int, transient: _TransientResult) -> None:
    metrics: LifecycleMetrics = getattr(transient, "lifecycle_metrics", LifecycleMetrics())
    session.execute(
        text(
            "INSERT INTO simulation_lifecycle_summary "
            "(run_id, merge_count, merged_qty, released_capital_total, capital_recycled_total, "
            "redeem_count, redeem_pnl, trading_pnl, merge_pnl, unresolved_inventory_value, "
            "max_unpaired_inventory, avg_unpaired_inventory, bond_inventory_created, "
            "bond_inventory_merged, capital_turnover_ratio) "
            "VALUES (:run_id, :merge_count, :merged_qty, :released_capital_total, :capital_recycled_total, "
            ":redeem_count, :redeem_pnl, :trading_pnl, :merge_pnl, :unresolved_inventory_value, "
            ":max_unpaired_inventory, :avg_unpaired_inventory, :bond_inventory_created, "
            ":bond_inventory_merged, :capital_turnover_ratio)"
        ),
        {"run_id": run_id, **metrics.to_dict()},
    )
    events = getattr(transient, "lifecycle_events", [])
    if events:
        session.execute(
            text(
                "INSERT INTO simulation_lifecycle_events "
                "(run_id, ts, event_type, condition_id, token_id, qty, usdc_delta, "
                "capital_released, inventory_before_json, inventory_after_json) "
                "VALUES (:run_id, :ts, :event_type, :condition_id, :token_id, :qty, :usdc_delta, "
                ":capital_released, :inventory_before_json, :inventory_after_json)"
            ),
            [{**event, "run_id": run_id} for event in events],
        )


def fetch_lifecycle_summary(session: Session, run_id: int) -> Optional[LifecycleMetrics]:
    row = session.execute(
        text("SELECT * FROM simulation_lifecycle_summary WHERE run_id = :run_id"),
        {"run_id": run_id},
    ).mappings().fetchone()
    if row is None:
        return None
    return LifecycleMetrics(
        merge_count=int(row["merge_count"]),
        merged_qty=Decimal(str(row["merged_qty"])),
        released_capital_total=Decimal(str(row["released_capital_total"])),
        capital_recycled_total=Decimal(str(row["capital_recycled_total"])),
        redeem_count=int(row["redeem_count"]),
        redeem_pnl=Decimal(str(row["redeem_pnl"])),
        trading_pnl=Decimal(str(row["trading_pnl"])),
        merge_pnl=Decimal(str(row["merge_pnl"])),
        unresolved_inventory_value=Decimal(str(row["unresolved_inventory_value"])),
        max_unpaired_inventory=Decimal(str(row["max_unpaired_inventory"])),
        avg_unpaired_inventory=Decimal(str(row["avg_unpaired_inventory"])),
        bond_inventory_created=Decimal(str(row["bond_inventory_created"])),
        bond_inventory_merged=Decimal(str(row["bond_inventory_merged"])),
        capital_turnover_ratio=Decimal(str(row["capital_turnover_ratio"])),
    )


def _decide_inventory_order(
    ctx: DecisionContext,
    state: InventoryLifecycleState,
    scenario: ScenarioConfig,
    config: InventoryCyclingConfig,
) -> RuleDecision:
    bid = ctx.decimal("best_bid_before")
    ask = ctx.decimal("best_ask_before")
    if bid is None or ask is None:
        return RuleDecision(False, None, "missing book-before bid/ask", {})
    condition_id = ctx.condition_id or f"token:{ctx.token_id}"
    token_before = state.lot(condition_id, ctx.token_id).qty
    bond_before = state.bond_inventory(condition_id)
    directional_before = _directional(state, condition_id)
    unpaired_before = state.unpaired_inventory(condition_id)
    event_exposure_before = state.event_exposure(condition_id)
    available_capital_before = state.available_capital(config)
    depth = ctx.decimal("ask_depth_top1")
    size = _order_size(depth, scenario, config)
    estimated_bond_cost = bid + ask
    expected = _project_buy(state, condition_id, ctx.token_id, size, bid)
    features = {
        "qty_token_before": str(token_before),
        "qty_complement_before": str(_complement_qty(state, condition_id, ctx.token_id)),
        "bond_before": str(bond_before),
        "directional_before": str(directional_before),
        "unpaired_inventory_before": str(unpaired_before),
        "event_exposure_before": str(event_exposure_before),
        "capital_used_before": str(state.locked_capital),
        "available_capital_before": str(available_capital_before),
        "estimated_bond_cost": str(estimated_bond_cost),
        "expected_bond_delta": str(expected["bond_delta"]),
        "expected_directional_delta": str(expected["directional_delta"]),
        "expected_unpaired_inventory_delta": str(expected["unpaired_delta"]),
        "expected_event_exposure_after": str(expected["event_exposure_after"]),
    }
    would_merge_soon = expected["bond_delta"] >= config.min_merge_qty
    applies = (
        estimated_bond_cost <= config.max_bond_cost
        or expected["bond_delta"] >= config.min_bond_delta > _ZERO
        or abs(expected["directional_after"]) < abs(directional_before)
        or expected["unpaired_after"] < unpaired_before
        or would_merge_soon
    )
    if not applies:
        return RuleDecision(False, None, "inventory cycle conditions not met", features)
    if expected["unpaired_after"] > config.max_unpaired_inventory:
        return RuleDecision(False, None, "max unpaired inventory", features)
    if expected["event_exposure_after"] > config.max_event_exposure:
        return RuleDecision(False, None, "max event exposure", features)
    return RuleDecision(
        True,
        SimOrder(side="BUY", order_price=bid, order_size=size, reason="event_inventory_cycling_v1"),
        "completion-set edge with simulated inventory cycling",
        features,
    )


def _project_buy(
    state: InventoryLifecycleState,
    condition_id: str,
    token_id: str,
    size: Decimal,
    price: Decimal,
) -> dict[str, Decimal]:
    before_bond = state.bond_inventory(condition_id)
    before_directional = _directional(state, condition_id)
    before_unpaired = state.unpaired_inventory(condition_id)
    before_qty = state.lot(condition_id, token_id).qty
    state.lot(condition_id, token_id).qty = before_qty + size
    after_bond = state.bond_inventory(condition_id)
    after_directional = _directional(state, condition_id)
    after_unpaired = state.unpaired_inventory(condition_id)
    event_exposure_after = state.event_exposure(condition_id) + size * price
    state.lot(condition_id, token_id).qty = before_qty
    return {
        "bond_delta": after_bond - before_bond,
        "directional_delta": abs(after_directional) - abs(before_directional),
        "directional_after": after_directional,
        "unpaired_delta": after_unpaired - before_unpaired,
        "unpaired_after": after_unpaired,
        "event_exposure_after": event_exposure_after,
    }


def _risk_skip_reason(
    ctx: DecisionContext,
    state: InventoryLifecycleState,
    order: SimOrder,
    config: InventoryCyclingConfig,
    risk_limits: RiskLimits,
) -> Optional[str]:
    condition_id = ctx.condition_id or f"token:{ctx.token_id}"
    notional = order.order_price * order.order_size
    if order.order_size > min(config.max_order_size, risk_limits.max_order_size):
        return "max_order_size"
    if state.lot(condition_id, ctx.token_id).qty + order.order_size > config.max_position_per_token:
        return "max_position_per_token"
    if notional > state.available_capital(config):
        return "max_capital"
    projected = _project_buy(state, condition_id, ctx.token_id, order.order_size, order.order_price)
    if projected["event_exposure_after"] > min(config.max_event_exposure, risk_limits.max_event_exposure):
        return "max_event_exposure"
    return None


def _lifecycle_metrics(
    state: InventoryLifecycleState,
    events: list[dict],
    redeem: LifecycleMetrics,
    max_unpaired_seen: Decimal,
    bond_created: Decimal,
    config: InventoryCyclingConfig,
) -> LifecycleMetrics:
    merge_events = [event for event in events if event["event_type"] == "MERGE_SIMULATED"]
    released = sum((Decimal(event["capital_released"]) for event in events if event["event_type"] == "CAPITAL_RELEASED"), _ZERO)
    merged_qty = sum((Decimal(event["qty"]) for event in merge_events), _ZERO)
    merge_pnl = sum((Decimal(str(event.get("merge_pnl", "0"))) for event in merge_events), _ZERO)
    avg_unpaired = (
        sum(state.unpaired_samples, _ZERO) / Decimal(len(state.unpaired_samples))
        if state.unpaired_samples
        else _ZERO
    )
    metrics = LifecycleMetrics(
        merge_count=len(merge_events),
        merged_qty=merged_qty,
        released_capital_total=released,
        capital_recycled_total=released if config.recycle_capital_enabled else _ZERO,
        redeem_count=redeem.redeem_count,
        redeem_pnl=redeem.redeem_pnl,
        trading_pnl=state.value() - merge_pnl - redeem.redeem_pnl,
        merge_pnl=merge_pnl,
        unresolved_inventory_value=redeem.unresolved_inventory_value,
        max_unpaired_inventory=max_unpaired_seen,
        avg_unpaired_inventory=avg_unpaired,
        bond_inventory_created=bond_created,
        bond_inventory_merged=merged_qty,
        capital_turnover_ratio=state.turnover / max(state.max_locked_capital, _ONE),
    )
    return metrics


def _load_resolution_prices(session: Session, condition_ids: set[str]) -> dict[str, dict[str, Decimal]]:
    if not condition_ids:
        return {}
    rows = session.execute(
        text("SELECT condition_id, resolution_prices_json FROM markets WHERE condition_id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        ),
        {"ids": list(condition_ids)},
    ).mappings().fetchall()
    result: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        raw = row["resolution_prices_json"]
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            result[row["condition_id"]] = {str(k): Decimal(str(v)) for k, v in parsed.items()}
    return result


def build_inventory_config(risk_limits: RiskLimits, parameters: Optional[dict[str, object]] = None) -> InventoryCyclingConfig:
    parameters = parameters or {}
    return InventoryCyclingConfig(
        max_bond_cost=Decimal(str(parameters.get("max_bond_cost", "0.98"))),
        min_bond_delta=Decimal(str(parameters.get("min_bond_delta", "0"))),
        max_unpaired_inventory=Decimal(str(parameters.get("max_unpaired_inventory", risk_limits.max_position_per_token))),
        max_event_exposure=Decimal(str(parameters.get("max_event_exposure", risk_limits.max_event_exposure))),
        max_position_per_token=Decimal(str(parameters.get("max_position_per_token", risk_limits.max_position_per_token))),
        max_capital=Decimal(str(parameters.get("max_capital", risk_limits.max_capital_deployed))),
        max_order_size=Decimal(str(parameters.get("max_order_size", risk_limits.max_order_size))),
        min_merge_qty=Decimal(str(parameters.get("min_merge_qty", "1"))),
        auto_merge_enabled=bool(parameters.get("auto_merge_enabled", True)),
        recycle_capital_enabled=bool(parameters.get("recycle_capital_enabled", True)),
    )


def run_inventory_strategy(
    session: Session,
    rows: list[dict],
    scenario: ScenarioConfig,
    risk_limits: RiskLimits,
    parameters: Optional[dict[str, object]] = None,
) -> _TransientResult:
    config = build_inventory_config(risk_limits, parameters)
    condition_ids = {str(row.get("condition_id")) for row in rows if row.get("condition_id")}
    resolution_prices = _load_resolution_prices(session, condition_ids)
    return simulate_inventory_cycling(
        rows,
        scenario,
        risk_limits,
        config=config,
        resolution_prices=resolution_prices,
    )


def _directional(state: InventoryLifecycleState, condition_id: str) -> Decimal:
    lots = [lot.qty for lot in state.tokens(condition_id).values()]
    if not lots:
        return _ZERO
    if len(lots) == 1:
        return lots[0]
    return lots[0] - lots[1]


def _complement_qty(state: InventoryLifecycleState, condition_id: str, token_id: str) -> Decimal:
    return sum((lot.qty for other, lot in state.tokens(condition_id).items() if other != token_id), _ZERO)


def _order_size(depth: Optional[Decimal], scenario: ScenarioConfig, config: InventoryCyclingConfig) -> Decimal:
    if depth is None or depth <= _ZERO:
        return min(scenario.max_order_size, config.max_order_size)
    return max(Decimal("1"), min(scenario.max_order_size, config.max_order_size, depth * scenario.depth_fraction))


def _is_stale(ctx: DecisionContext, limits: RiskLimits) -> bool:
    if ctx.context_status in {"missing", "stale"}:
        return True
    age = ctx.integer("book_before_age_s")
    return bool(age is not None and age > limits.max_stale_book_age_s)


def _skip_row(ctx: DecisionContext, decision: RuleDecision, reason: str) -> dict:
    order = decision.order
    return {
        "run_id": 0,
        "event_id": ctx.event_id,
        "token_id": ctx.token_id,
        "condition_id": ctx.condition_id,
        "strategy_name": "event_inventory_cycling_v1",
        "base_rule": "completion_set_edge",
        "side": order.side if order else None,
        "order_price": str(order.order_price) if order else None,
        "order_size": str(order.order_size) if order else None,
        "skipped_reason": reason,
        "context_status": ctx.context_status,
        "book_age_s": ctx.integer("book_before_age_s"),
        "created_ts": ctx.trade_ts,
    }


def _inventory_row(ctx: DecisionContext, state: InventoryLifecycleState) -> dict:
    condition_id = ctx.condition_id or f"token:{ctx.token_id}"
    lot = state.lot(condition_id, ctx.token_id)
    return {
        "run_id": 0,
        "event_id": ctx.event_id,
        "token_id": ctx.token_id,
        "condition_id": ctx.condition_id,
        "qty_token": str(lot.qty),
        "qty_complement": str(_complement_qty(state, condition_id, ctx.token_id)),
        "directional": str(_directional(state, condition_id)),
        "bond": str(state.bond_inventory(condition_id)),
        "cost_basis": str(lot.cost),
        "mark_price": str(lot.mark_price),
        "unrealized_pnl": str(lot.value - lot.cost),
        "event_exposure": str(state.event_exposure(condition_id)),
        "snapshot_ts": ctx.trade_ts,
    }


def _ts_to_date(ts: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
