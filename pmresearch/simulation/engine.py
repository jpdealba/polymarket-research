"""Core Phase 22 counterfactual simulation engine.

The simulator builds an allowlist-only DecisionContext from each historical
snapshot row, derives prospective order price and size from book-before data,
and never uses observed fill price, fill size, markouts, realized PnL, close
path, resolution, or post-fill inventory as decision inputs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .attribution import insert_run_attribution
from .risk import RiskEvent, RiskLimits, check_all_risks
from .scenarios import ALL_SCENARIOS, ScenarioConfig, SimOrder, decide_fill

RN1_WALLET = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
GAP_WALLET = "0x83255595ba1fadd2e734cb30a0fb8110301a19cc"

SUPPORTED_RULES: dict[str, dict[str, object]] = {
    "completion_set_edge": {
        "wallet": RN1_WALLET,
        "version": 1,
        "parameters": {"max_bond_cost": "0.98"},
    },
    "spread_capture": {
        "wallet": GAP_WALLET,
        "version": 1,
        "parameters": {"min_spread_bps": "50", "min_edge_bps": "10"},
    },
}


@dataclass(frozen=True)
class StrategyConfig:
    """A named strategy variant layered on top of a base rule."""

    strategy_name: str
    wallet: str
    base_rule: str
    version: int
    rule_parameters: dict[str, object] = field(default_factory=dict)
    filters: dict[str, object] = field(default_factory=dict)
    execution_policy: dict[str, object] = field(default_factory=dict)
    pre_trade_risk_limits: tuple[str, ...] = ()

    @property
    def uses_pre_trade_risk(self) -> bool:
        return bool(self.pre_trade_risk_limits)

    def parameters(self) -> dict[str, object]:
        return {
            "strategy_name": self.strategy_name,
            "wallet": self.wallet,
            "base_rule": self.base_rule,
            "filters": self.filters,
            "execution_policy": self.execution_policy,
            "pre_trade_risk_limits": list(self.pre_trade_risk_limits),
            "base_rule_parameters": _rule_parameters(self),
        }


SUPPORTED_STRATEGIES: dict[str, StrategyConfig] = {
    "rn1_completion_set_edge_v1": StrategyConfig(
        strategy_name="rn1_completion_set_edge_v1",
        wallet=RN1_WALLET,
        base_rule="completion_set_edge",
        version=1,
        execution_policy={"candidate_order_price": "book_before"},
    ),
    "rn1_completion_set_edge_risk_v2": StrategyConfig(
        strategy_name="rn1_completion_set_edge_risk_v2",
        wallet=RN1_WALLET,
        base_rule="completion_set_edge",
        version=2,
        filters={
            "context_quality": ["excellent", "good", "usable"],
            "max_book_age_s": 30,
        },
        execution_policy={
            "candidate_order_price": "book_before",
            "risk_gate": "pre_order",
        },
        pre_trade_risk_limits=(
            "max_position_per_token",
            "max_event_exposure",
            "max_capital_deployed",
            "max_order_size",
        ),
    ),
    "gap_spread_capture_v1": StrategyConfig(
        strategy_name="gap_spread_capture_v1",
        wallet=GAP_WALLET,
        base_rule="spread_capture",
        version=1,
        execution_policy={"candidate_order_price": "book_before"},
    ),
    "gap_spread_capture_risk_v2": StrategyConfig(
        strategy_name="gap_spread_capture_risk_v2",
        wallet=GAP_WALLET,
        base_rule="spread_capture",
        version=2,
        filters={
            "context_quality": ["excellent", "good", "usable"],
            "max_book_age_s": 30,
        },
        execution_policy={
            "candidate_order_price": "book_before",
            "risk_gate": "pre_order",
            "daily_loss_stop_utc_day": True,
        },
        pre_trade_risk_limits=(
            "max_position_per_token",
            "max_event_exposure",
            "max_capital_deployed",
            "max_daily_loss",
            "max_order_size",
        ),
    ),
    "event_inventory_cycling_v1": StrategyConfig(
        strategy_name="event_inventory_cycling_v1",
        wallet="*",
        base_rule="completion_set_edge",
        version=1,
        rule_parameters={
            "max_bond_cost": "0.98",
            "min_bond_delta": "0",
            "max_unpaired_inventory": "100",
            "min_merge_qty": "1",
            "auto_merge_enabled": True,
            "recycle_capital_enabled": True,
        },
        filters={
            "context_quality": ["excellent", "good", "usable"],
            "max_book_age_s": 30,
        },
        execution_policy={
            "candidate_order_price": "book_before",
            "risk_gate": "pre_order",
            "auto_merge": True,
            "recycle_capital": True,
        },
        pre_trade_risk_limits=(
            "max_position_per_token",
            "max_event_exposure",
            "max_capital_deployed",
            "max_order_size",
        ),
    ),
}

PROHIBITED_DECISION_FIELDS: frozenset[str] = frozenset(
    {
        "book_after_age_s",
        "fill_price",
        "fill_size",
        "fill_shares",
        "fill_notional_usdc",
        "delta_usdc",
        "distance_fill_to_mid",
        "distance_fill_to_bid",
        "distance_fill_to_ask",
        "fill_inside_spread",
        "fill_at_best_bid",
        "fill_at_best_ask",
        "markout_5m",
        "markout_15m",
        "markout_1h",
        "markout_24h",
        "realized_pnl_wac",
        "realized_pnl_per_share",
        "realized_pnl_bps_on_cost",
        "pnl_episode",
        "pnl_at_resolution",
        "close_path",
        "close_ts",
        "hold_seconds",
        "qty_token_after",
        "qty_complement_after",
        "directional_after",
        "bond_after",
        "bond_ratio_after",
        "bond_delta",
        "directional_delta",
        "event_exposure_after",
        "event_exposure_delta",
        "remaining_open_qty_after_24h",
        "is_open_after_24h",
        "closed_by_merge",
        "closed_by_redeem",
        "closed_by_sell",
        "closed_by_resolution",
        "closed_by_unresolved_open",
    }
)

DECISION_CONTEXT_FIELDS: frozenset[str] = frozenset(
    {
        "event_id",
        "wallet",
        "token_id",
        "condition_id",
        "trade_ts",
        "trade_utc",
        "context_status",
        "book_before_age_s",
        "context_source",
        "best_bid_before",
        "best_ask_before",
        "mid_before",
        "spread_before",
        "spread_bps",
        "bid_depth_top1",
        "ask_depth_top1",
        "bid_depth_top5",
        "ask_depth_top5",
        "book_imbalance_top1",
        "book_imbalance_top5",
        "trade_hour_utc",
        "market_category",
        "time_to_event_start_s",
        "wallet_label",
        "qty_token_before",
        "qty_complement_before",
        "directional_before",
        "bond_before",
        "bond_ratio_before",
        "event_exposure_before",
        "null_reasons_json",
        "dataset_version",
        "watchlist",
        "built_at",
    }
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_FEE_RATE = Decimal("0.002")


def opt_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _opt_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class DecisionContext:
    """Allowlist-only decision context for prospective simulation."""

    values: dict[str, object]

    @classmethod
    def from_row(cls, row: dict) -> "DecisionContext":
        allowed = {k: row.get(k) for k in DECISION_CONTEXT_FIELDS if k in row}
        return cls(allowed)

    def get(self, key: str, default=None):
        if key not in DECISION_CONTEXT_FIELDS:
            raise KeyError(f"{key!r} is not part of the simulation decision allowlist")
        return self.values.get(key, default)

    def decimal(self, key: str) -> Optional[Decimal]:
        return opt_decimal(self.get(key))

    def integer(self, key: str) -> Optional[int]:
        return _opt_int(self.get(key))

    @property
    def event_id(self) -> int:
        return int(self.get("event_id", 0) or 0)

    @property
    def token_id(self) -> str:
        return str(self.get("token_id", "") or "")

    @property
    def condition_id(self) -> Optional[str]:
        value = self.get("condition_id")
        return None if value is None else str(value)

    @property
    def trade_ts(self) -> int:
        return int(self.get("trade_ts", 0) or 0)

    @property
    def context_status(self) -> str:
        return str(self.get("context_status", "missing") or "missing")

    def public_features(self) -> dict[str, object]:
        return dict(self.values)


@dataclass(frozen=True)
class RuleDecision:
    applies: bool
    order: Optional[SimOrder]
    explanation: str
    features_used: dict[str, object]


@dataclass
class TokenPosition:
    token_id: str
    condition_id: Optional[str] = None
    qty: Decimal = _ZERO
    mark_price: Decimal = _ZERO

    def value(self) -> Decimal:
        return self.qty * self.mark_price


@dataclass
class PortfolioState:
    positions: dict[str, TokenPosition] = field(default_factory=dict)
    cash: Decimal = _ZERO
    peak_value: Decimal = _ZERO
    max_drawdown_seen: Decimal = _ZERO
    max_inventory_seen: Decimal = _ZERO
    max_capital_seen: Decimal = _ZERO
    turnover: Decimal = _ZERO
    fill_count: int = 0
    risk_breach_count: int = 0

    def position(self, token_id: str, condition_id: Optional[str]) -> TokenPosition:
        if token_id not in self.positions:
            self.positions[token_id] = TokenPosition(token_id, condition_id)
        return self.positions[token_id]

    def mark(self, token_id: str, condition_id: Optional[str], mark_price: Optional[Decimal]) -> None:
        if mark_price is None:
            return
        self.position(token_id, condition_id).mark_price = mark_price
        self._update_path_metrics()

    def apply_fill(
        self,
        *,
        token_id: str,
        condition_id: Optional[str],
        side: str,
        price: Decimal,
        size: Decimal,
        mark_price: Optional[Decimal],
    ) -> Decimal:
        notional = price * size
        fee = notional * _FEE_RATE
        pos = self.position(token_id, condition_id)
        if mark_price is not None:
            pos.mark_price = mark_price
        if side == "BUY":
            pos.qty += size
            self.cash -= notional + fee
        else:
            pos.qty -= size
            self.cash += notional - fee
        self.turnover += notional
        self.fill_count += 1
        self._update_path_metrics()
        return fee

    def value(self) -> Decimal:
        return self.cash + sum((p.value() for p in self.positions.values()), _ZERO)

    def exposure(self) -> Decimal:
        return sum((abs(p.value()) for p in self.positions.values()), _ZERO)

    def event_exposure(self, condition_id: Optional[str]) -> Decimal:
        if condition_id is None:
            return _ZERO
        return sum(
            (abs(p.value()) for p in self.positions.values() if p.condition_id == condition_id),
            _ZERO,
        )

    def max_inventory(self) -> Decimal:
        return max((abs(p.qty) for p in self.positions.values()), default=_ZERO)

    def _update_path_metrics(self) -> None:
        value = self.value()
        if value > self.peak_value:
            self.peak_value = value
        drawdown = self.peak_value - value
        if drawdown > self.max_drawdown_seen:
            self.max_drawdown_seen = drawdown
        inventory = self.max_inventory()
        if inventory > self.max_inventory_seen:
            self.max_inventory_seen = inventory
        capital = self.exposure()
        if capital > self.max_capital_seen:
            self.max_capital_seen = capital

    def project_fill(
        self,
        *,
        token_id: str,
        condition_id: Optional[str],
        side: str,
        price: Decimal,
        size: Decimal,
        mark_price: Optional[Decimal],
    ) -> "ProjectedPortfolioState":
        notional = price * size
        fee = notional * _FEE_RATE
        cash = self.cash
        if side == "BUY":
            cash -= notional + fee
            qty_delta = size
        else:
            cash += notional - fee
            qty_delta = -size

        total_value = cash
        total_exposure = _ZERO
        event_exposure = _ZERO
        seen = False
        for token, pos in self.positions.items():
            if token == token_id:
                seen = True
                qty = pos.qty + qty_delta
                effective_mark = mark_price if mark_price is not None else pos.mark_price
                pos_condition_id = condition_id
            else:
                qty = pos.qty
                effective_mark = pos.mark_price
                pos_condition_id = pos.condition_id
            value = qty * effective_mark
            total_value += value
            total_exposure += abs(value)
            if condition_id is not None and pos_condition_id == condition_id:
                event_exposure += abs(value)

        if not seen:
            effective_mark = mark_price if mark_price is not None else _ZERO
            value = qty_delta * effective_mark
            total_value += value
            total_exposure += abs(value)
            if condition_id is not None:
                event_exposure += abs(value)

        current_qty = self.positions.get(token_id).qty if token_id in self.positions else _ZERO
        return ProjectedPortfolioState(
            qty_token=current_qty + qty_delta,
            value=total_value,
            capital_used=total_exposure,
            event_exposure=event_exposure,
        )


@dataclass(frozen=True)
class ProjectedPortfolioState:
    qty_token: Decimal
    value: Decimal
    capital_used: Decimal
    event_exposure: Decimal


@dataclass(frozen=True)
class SimRunResult:
    run_id: int
    wallet: str
    rule_name: str
    strategy_name: str
    base_rule: str
    rule_version: int
    scenario: str
    parameters: dict
    risk_limits: dict
    candidate_signals_count: int
    accepted_orders_count: int
    orders_count: int
    simulated_fills_count: int
    fill_rate: Optional[Decimal]
    simulated_pnl: Decimal
    net_pnl: Decimal
    max_drawdown: Decimal
    max_inventory: Decimal
    capital_required: Decimal
    turnover: Decimal
    skipped_orders_count: int
    skipped_by_reason: dict[str, int]
    risk_prevented_count: int
    risk_breaches: int
    stale_context_excluded: int
    conservative_pass: bool
    ordering_violation: bool
    elapsed_ms: int


@dataclass
class _TransientResult:
    orders: list[dict]
    skipped_orders: list[dict]
    fills: list[dict]
    inventory: list[dict]
    daily_pnl: dict[str, dict]
    risk_events: list[RiskEvent]
    market_attribution: list[dict]
    candidate_signals_count: int
    orders_count: int
    fills_count: int
    fill_rate: Optional[Decimal]
    simulated_pnl: Decimal
    net_pnl: Decimal
    max_drawdown: Decimal
    max_inventory: Decimal
    capital_required: Decimal
    turnover: Decimal
    skipped_orders_count: int
    skipped_by_reason: dict[str, int]
    risk_prevented_count: int
    risk_breaches: int
    stale_context_excluded: int


def run_simulation(
    session: Session,
    wallet: str,
    rule_name: str,
    scenario_name: str = "conservative",
    *,
    risk_limits: Optional[RiskLimits] = None,
) -> SimRunResult:
    """Run one supported rule under one scenario and persist the result."""
    wallet = wallet.lower()
    rule_name = rule_name.strip().lower()
    _validate_supported_rule(wallet, rule_name)
    strategy = _rule_compat_strategy(wallet, rule_name)

    scenario = ALL_SCENARIOS.get(scenario_name.lower())
    if scenario is None:
        raise ValueError(
            f"Unknown scenario: {scenario_name!r}. "
            f"Choose from: {', '.join(ALL_SCENARIOS)}"
        )

    base_limits = risk_limits or RiskLimits()
    effective_limits = _scenario_limits(base_limits, scenario)
    rows = _load_dataset(session, wallet)
    if not rows:
        raise ValueError(f"No microstructure_lifecycle_dataset rows for wallet={wallet}")

    t0 = time.monotonic()
    if strategy.strategy_name == "event_inventory_cycling_v1":
        from .inventory_cycling import run_inventory_strategy

        transient = run_inventory_strategy(
            session,
            rows,
            scenario,
            effective_limits,
            parameters=_rule_parameters(strategy) | effective_limits.to_dict(),
        )
    else:
        transient = _simulate(rows, strategy, scenario, effective_limits)
    ordering_violation = False
    conservative_pass = False

    if scenario.name == "conservative":
        if strategy.strategy_name == "event_inventory_cycling_v1":
            from .inventory_cycling import run_inventory_strategy

            optimistic = run_inventory_strategy(
                session,
                rows,
                ALL_SCENARIOS["optimistic"],
                base_limits,
                parameters=_rule_parameters(strategy) | base_limits.to_dict(),
            )
        else:
            optimistic = _simulate(rows, strategy, ALL_SCENARIOS["optimistic"], base_limits)
        ordering_violation = _has_ordering_violation(transient, optimistic)
        conservative_pass = (
            transient.net_pnl > _ZERO
            and transient.risk_breaches == 0
            and not ordering_violation
            and transient.fills_count > 0
        )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    meta = SUPPORTED_RULES[rule_name]
    run_id = _insert_run(
        session=session,
        wallet=wallet,
        rule_name=rule_name,
        strategy_name=strategy.strategy_name,
        rule_version=int(meta["version"]),
        scenario=scenario.name,
        parameters=dict(meta["parameters"]),
        risk_limits=effective_limits,
        metrics=transient,
        conservative_pass=conservative_pass,
        ordering_violation=ordering_violation,
        elapsed_ms=elapsed_ms,
    )
    order_ids = _insert_orders(session, run_id, transient.orders)
    _insert_skipped_orders(session, run_id, transient.skipped_orders)
    _insert_fills(session, run_id, transient.fills, order_ids)
    _insert_inventory(session, run_id, transient.inventory)
    _insert_daily_pnl(session, run_id, transient.daily_pnl)
    _insert_risk_events(session, run_id, transient.risk_events)
    if strategy.strategy_name == "event_inventory_cycling_v1":
        from .inventory_cycling import insert_lifecycle_outputs

        insert_lifecycle_outputs(session, run_id, transient)
    insert_run_attribution(session, run_id, transient.market_attribution)
    session.commit()

    return SimRunResult(
        run_id=run_id,
        wallet=wallet,
        rule_name=rule_name,
        strategy_name=strategy.strategy_name,
        base_rule=strategy.base_rule,
        rule_version=int(meta["version"]),
        scenario=scenario.name,
        parameters=dict(meta["parameters"]),
        risk_limits=effective_limits.to_dict(),
        candidate_signals_count=transient.candidate_signals_count,
        accepted_orders_count=transient.orders_count,
        orders_count=transient.orders_count,
        simulated_fills_count=transient.fills_count,
        fill_rate=transient.fill_rate,
        simulated_pnl=transient.simulated_pnl,
        net_pnl=transient.net_pnl,
        max_drawdown=transient.max_drawdown,
        max_inventory=transient.max_inventory,
        capital_required=transient.capital_required,
        turnover=transient.turnover,
        skipped_orders_count=transient.skipped_orders_count,
        skipped_by_reason=dict(transient.skipped_by_reason),
        risk_prevented_count=transient.risk_prevented_count,
        risk_breaches=transient.risk_breaches,
        stale_context_excluded=transient.stale_context_excluded,
        conservative_pass=conservative_pass,
        ordering_violation=ordering_violation,
        elapsed_ms=elapsed_ms,
    )


def run_strategy_simulation(
    session: Session,
    wallet: str,
    strategy_name: str,
    scenario_name: str = "conservative",
    *,
    risk_limits: Optional[RiskLimits] = None,
) -> SimRunResult:
    """Run one named strategy variant under one scenario and persist the result."""
    wallet = wallet.lower()
    strategy = _validate_supported_strategy(wallet, strategy_name)

    scenario = ALL_SCENARIOS.get(scenario_name.lower())
    if scenario is None:
        raise ValueError(
            f"Unknown scenario: {scenario_name!r}. "
            f"Choose from: {', '.join(ALL_SCENARIOS)}"
        )

    base_limits = risk_limits or RiskLimits()
    effective_limits = _scenario_limits(base_limits, scenario)
    rows = _load_dataset(session, wallet)
    if not rows:
        raise ValueError(f"No microstructure_lifecycle_dataset rows for wallet={wallet}")

    t0 = time.monotonic()
    if strategy.strategy_name == "event_inventory_cycling_v1":
        from .inventory_cycling import run_inventory_strategy

        transient = run_inventory_strategy(
            session,
            rows,
            scenario,
            effective_limits,
            parameters=_rule_parameters(strategy) | effective_limits.to_dict(),
        )
    else:
        transient = _simulate(rows, strategy, scenario, effective_limits)
    ordering_violation = False
    conservative_pass = False

    if scenario.name == "conservative":
        if strategy.strategy_name == "event_inventory_cycling_v1":
            from .inventory_cycling import run_inventory_strategy

            optimistic = run_inventory_strategy(
                session,
                rows,
                ALL_SCENARIOS["optimistic"],
                base_limits,
                parameters=_rule_parameters(strategy) | base_limits.to_dict(),
            )
        else:
            optimistic = _simulate(rows, strategy, ALL_SCENARIOS["optimistic"], base_limits)
        ordering_violation = _has_ordering_violation(transient, optimistic)
        conservative_pass = (
            transient.net_pnl > _ZERO
            and transient.risk_breaches == 0
            and not ordering_violation
            and transient.fills_count > 0
        )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    run_id = _insert_run(
        session=session,
        wallet=wallet,
        rule_name=strategy.base_rule,
        strategy_name=strategy.strategy_name,
        rule_version=strategy.version,
        scenario=scenario.name,
        parameters=strategy.parameters(),
        risk_limits=effective_limits,
        metrics=transient,
        conservative_pass=conservative_pass,
        ordering_violation=ordering_violation,
        elapsed_ms=elapsed_ms,
    )
    order_ids = _insert_orders(session, run_id, transient.orders)
    _insert_skipped_orders(session, run_id, transient.skipped_orders)
    _insert_fills(session, run_id, transient.fills, order_ids)
    _insert_inventory(session, run_id, transient.inventory)
    _insert_daily_pnl(session, run_id, transient.daily_pnl)
    _insert_risk_events(session, run_id, transient.risk_events)
    if strategy.strategy_name == "event_inventory_cycling_v1":
        from .inventory_cycling import insert_lifecycle_outputs

        insert_lifecycle_outputs(session, run_id, transient)
    insert_run_attribution(session, run_id, transient.market_attribution)
    session.commit()

    return SimRunResult(
        run_id=run_id,
        wallet=wallet,
        rule_name=strategy.base_rule,
        strategy_name=strategy.strategy_name,
        base_rule=strategy.base_rule,
        rule_version=strategy.version,
        scenario=scenario.name,
        parameters=strategy.parameters(),
        risk_limits=effective_limits.to_dict(),
        candidate_signals_count=transient.candidate_signals_count,
        accepted_orders_count=transient.orders_count,
        orders_count=transient.orders_count,
        simulated_fills_count=transient.fills_count,
        fill_rate=transient.fill_rate,
        simulated_pnl=transient.simulated_pnl,
        net_pnl=transient.net_pnl,
        max_drawdown=transient.max_drawdown,
        max_inventory=transient.max_inventory,
        capital_required=transient.capital_required,
        turnover=transient.turnover,
        skipped_orders_count=transient.skipped_orders_count,
        skipped_by_reason=dict(transient.skipped_by_reason),
        risk_prevented_count=transient.risk_prevented_count,
        risk_breaches=transient.risk_breaches,
        stale_context_excluded=transient.stale_context_excluded,
        conservative_pass=conservative_pass,
        ordering_violation=ordering_violation,
        elapsed_ms=elapsed_ms,
    )


def _validate_supported_rule(wallet: str, rule_name: str) -> None:
    if rule_name == "event_timing":
        raise ValueError("event_timing is rejected for Phase 22 simulation")
    meta = SUPPORTED_RULES.get(rule_name)
    if meta is None:
        raise ValueError(
            "Unsupported simulation rule. Supported pairs are "
            f"{RN1_WALLET}: completion_set_edge and {GAP_WALLET}: spread_capture"
        )
    expected_wallet = str(meta["wallet"])
    if wallet != expected_wallet:
        raise ValueError(f"{rule_name} is supported only for wallet {expected_wallet}")


def _rule_compat_strategy(wallet: str, rule_name: str) -> StrategyConfig:
    return StrategyConfig(
        strategy_name=rule_name,
        wallet=wallet,
        base_rule=rule_name,
        version=int(SUPPORTED_RULES[rule_name]["version"]),
        rule_parameters=dict(SUPPORTED_RULES[rule_name]["parameters"]),
        execution_policy={"candidate_order_price": "book_before"},
    )


def _validate_supported_strategy(wallet: str, strategy_name: str) -> StrategyConfig:
    strategy_key = strategy_name.strip().lower()
    strategy = SUPPORTED_STRATEGIES.get(strategy_key)
    if strategy is None:
        raise ValueError(
            "Unsupported simulation strategy. Supported strategies are "
            f"{', '.join(SUPPORTED_STRATEGIES)}"
        )
    if strategy.wallet == "*":
        return replace(strategy, wallet=wallet)
    if wallet != strategy.wallet:
        raise ValueError(f"{strategy.strategy_name} is supported only for wallet {strategy.wallet}")
    return strategy


def _load_dataset(session: Session, wallet: str) -> list[dict]:
    rows = session.execute(
        text(
            "SELECT * FROM microstructure_lifecycle_dataset "
            "WHERE wallet = :w ORDER BY trade_ts, event_id"
        ),
        {"w": wallet},
    ).mappings().fetchall()
    return [dict(r) for r in rows]


def _scenario_limits(limits: RiskLimits, scenario: ScenarioConfig) -> RiskLimits:
    factor = scenario.risk_limit_multiplier
    return RiskLimits(
        max_position_per_token=limits.max_position_per_token * factor,
        max_directional_per_market=limits.max_directional_per_market * factor,
        max_event_exposure=limits.max_event_exposure * factor,
        max_daily_loss=limits.max_daily_loss * factor,
        max_capital_deployed=limits.max_capital_deployed * factor,
        max_order_size=min(limits.max_order_size * factor, scenario.max_order_size),
        max_stale_book_age_s=min(
            limits.max_stale_book_age_s,
            scenario.max_book_age_s if scenario.max_book_age_s is not None else limits.max_stale_book_age_s,
        ),
    )


def _simulate(
    rows: list[dict],
    strategy: StrategyConfig,
    scenario: ScenarioConfig,
    risk_limits: RiskLimits,
    *,
    collect_details: bool = True,
) -> _TransientResult:
    portfolio = PortfolioState()
    orders: list[dict] = []
    skipped_orders: list[dict] = []
    fills: list[dict] = []
    inventory: list[dict] = []
    daily_pnl: dict[str, dict] = {}
    risk_events: list[RiskEvent] = []
    market_attribution: dict[str, dict] = {}
    skipped_by_reason: dict[str, int] = {}
    day_start_values: dict[str, Decimal] = {}
    stopped_days: set[str] = set()
    rule_fires = 0
    orders_count = 0
    skipped_orders_count = 0
    stale_excluded = 0
    risk_prevented_count = 0
    fill_seq = 0

    for row in rows:
        ctx = DecisionContext.from_row(row)
        mid = ctx.decimal("mid_before")
        portfolio.mark(ctx.token_id, ctx.condition_id, mid)
        if collect_details:
            _update_market_path_attribution(market_attribution, portfolio, ctx)
        day = _ts_to_date(ctx.trade_ts)
        day_start_values.setdefault(day, portfolio.value())

        decision = _decide_rule_order(strategy, ctx, scenario)
        if not decision.applies or decision.order is None:
            continue

        rule_fires += 1
        filter_reason = _strategy_filter_skip_reason(strategy, ctx, decision)
        if filter_reason is not None:
            _record_skip(
                skipped_orders=skipped_orders,
                skipped_by_reason=skipped_by_reason,
                ctx=ctx,
                decision=decision,
                strategy=strategy,
                reason=filter_reason,
                collect_details=collect_details,
            )
            skipped_orders_count += 1
            continue

        if strategy.uses_pre_trade_risk:
            if day in stopped_days:
                _record_skip(
                    skipped_orders=skipped_orders,
                    skipped_by_reason=skipped_by_reason,
                    ctx=ctx,
                    decision=decision,
                    strategy=strategy,
                    reason="max_daily_loss_day_stopped",
                    collect_details=collect_details,
                )
                skipped_orders_count += 1
                risk_prevented_count += 1
                continue

            risk_reason = _pre_trade_risk_skip_reason(
                portfolio=portfolio,
                ctx=ctx,
                order=decision.order,
                strategy=strategy,
                scenario=scenario,
                limits=risk_limits,
                day_start_value=day_start_values[day],
            )
            if risk_reason is not None:
                _record_skip(
                    skipped_orders=skipped_orders,
                    skipped_by_reason=skipped_by_reason,
                    ctx=ctx,
                    decision=decision,
                    strategy=strategy,
                    reason=risk_reason,
                    collect_details=collect_details,
                )
                skipped_orders_count += 1
                risk_prevented_count += 1
                if (
                    strategy.execution_policy.get("daily_loss_stop_utc_day")
                    and risk_reason == "max_daily_loss"
                ):
                    stopped_days.add(day)
                continue

        is_stale = _is_stale(ctx, risk_limits)
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
                    "stale_excluded": 1 if is_stale else 0,
                    "created_ts": ctx.trade_ts,
                }
            )

        if is_stale:
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
        if not assumption.would_fill:
            continue
        if assumption.fill_price is None or assumption.fill_size is None:
            continue

        fee = portfolio.apply_fill(
            token_id=ctx.token_id,
            condition_id=ctx.condition_id,
            side=decision.order.side,
            price=assumption.fill_price,
            size=assumption.fill_size,
            mark_price=mid,
        )
        notional = assumption.fill_price * assumption.fill_size
        if collect_details:
            _record_market_fill_attribution(
                market_attribution=market_attribution,
                portfolio=portfolio,
                ctx=ctx,
                side=decision.order.side,
                notional=notional,
                fee=fee,
            )
        pnl_value = portfolio.value()
        day_row = daily_pnl.setdefault(
            day,
            {"pnl": _ZERO, "turnover": _ZERO, "breaches": 0, "fills": 0},
        )
        day_row["pnl"] = pnl_value
        day_row["turnover"] += notional
        day_row["fills"] += 1

        pos = portfolio.position(ctx.token_id, ctx.condition_id)
        daily_risk_pnl = (
            pnl_value - day_start_values[day]
            if strategy.uses_pre_trade_risk
            else pnl_value
        )
        event_exposure = (
            portfolio.event_exposure(ctx.condition_id)
            if strategy.uses_pre_trade_risk
            else (ctx.decimal("event_exposure_before") or _ZERO)
        )
        breaches = check_all_risks(
            token_id=ctx.token_id,
            qty_token=pos.qty,
            daily_pnl=daily_risk_pnl,
            capital_used=portfolio.exposure(),
            event_exposure=event_exposure,
            condition_id=ctx.condition_id,
            limits=risk_limits,
            ts=ctx.trade_ts,
        )
        for ev in breaches:
            if collect_details:
                risk_events.append(ev)
            portfolio.risk_breach_count += 1
            day_row["breaches"] += 1

        if collect_details:
            fills.append(
                {
                    "run_id": 0,
                    "order_index": order_index,
                    "event_id": ctx.event_id,
                    "token_id": ctx.token_id,
                    "condition_id": ctx.condition_id,
                    "side": decision.order.side,
                    "fill_price": str(assumption.fill_price),
                    "fill_size": str(assumption.fill_size),
                    "fill_notional_usdc": str(notional),
                    "estimated_fee": str(fee),
                    "scenario": scenario.name,
                    "fill_reason": assumption.fill_reason,
                    "filled_ts": ctx.trade_ts,
                }
            )
            inventory.append(
                {
                    "run_id": 0,
                    "event_id": ctx.event_id,
                    "token_id": ctx.token_id,
                    "condition_id": ctx.condition_id,
                    "qty_token": str(pos.qty),
                    "qty_complement": "0",
                    "directional": str(pos.qty),
                    "bond": "0",
                    "cost_basis": "0",
                    "mark_price": str(pos.mark_price),
                    "unrealized_pnl": str(pos.value()),
                    "event_exposure": str(ctx.decimal("event_exposure_before") or _ZERO),
                    "snapshot_ts": ctx.trade_ts,
                }
            )

    net_pnl = portfolio.value()
    fill_rate = Decimal(portfolio.fill_count) / Decimal(rule_fires) if rule_fires else None
    market_rows = _finalize_market_attribution(market_attribution, portfolio) if collect_details else []
    return _TransientResult(
        orders=orders,
        skipped_orders=skipped_orders,
        fills=fills,
        inventory=inventory,
        daily_pnl=daily_pnl,
        risk_events=risk_events,
        market_attribution=market_rows,
        candidate_signals_count=rule_fires,
        orders_count=orders_count,
        fills_count=portfolio.fill_count,
        fill_rate=fill_rate,
        simulated_pnl=net_pnl,
        net_pnl=net_pnl,
        max_drawdown=portfolio.max_drawdown_seen,
        max_inventory=portfolio.max_inventory_seen,
        capital_required=portfolio.max_capital_seen,
        turnover=portfolio.turnover,
        skipped_orders_count=skipped_orders_count,
        skipped_by_reason=skipped_by_reason,
        risk_prevented_count=risk_prevented_count,
        risk_breaches=portfolio.risk_breach_count,
        stale_context_excluded=stale_excluded,
    )


def _decide_rule_order(strategy: StrategyConfig, ctx: DecisionContext, scenario: ScenarioConfig) -> RuleDecision:
    rule_name = strategy.base_rule
    if rule_name == "spread_capture":
        return _spread_capture_order(ctx, scenario, _rule_parameters(strategy))
    if rule_name == "completion_set_edge":
        return _completion_set_order(ctx, scenario, _rule_parameters(strategy))
    raise ValueError(f"Unsupported simulation rule: {rule_name}")


def _rule_parameters(strategy: StrategyConfig) -> dict[str, object]:
    params = dict(SUPPORTED_RULES[strategy.base_rule]["parameters"])
    params.update(strategy.rule_parameters)
    return params


def _spread_capture_order(
    ctx: DecisionContext,
    scenario: ScenarioConfig,
    parameters: dict[str, object],
) -> RuleDecision:
    spread_bps = ctx.decimal("spread_bps")
    bid = ctx.decimal("best_bid_before")
    ask = ctx.decimal("best_ask_before")
    mid = ctx.decimal("mid_before")
    qty = ctx.decimal("qty_token_before") or _ZERO
    features = {
        "spread_bps": ctx.get("spread_bps"),
        "best_bid_before": ctx.get("best_bid_before"),
        "best_ask_before": ctx.get("best_ask_before"),
        "mid_before": ctx.get("mid_before"),
        "qty_token_before": ctx.get("qty_token_before"),
    }
    threshold = Decimal(str(parameters["min_spread_bps"]))
    edge_threshold = Decimal(str(parameters.get("min_edge_bps", parameters.get("min_fill_improvement_bps", "10"))))
    if spread_bps is None or bid is None or ask is None or mid in (None, _ZERO):
        return RuleDecision(False, None, "missing book-before spread fields", features)
    if spread_bps < threshold:
        return RuleDecision(False, None, f"spread {spread_bps} < {threshold}", features)

    if qty > _ZERO:
        side = "SELL"
        order_price = ask
        depth = ctx.decimal("bid_depth_top1")
        edge_bps = (order_price - mid) / mid * Decimal("10000")
    else:
        side = "BUY"
        order_price = bid
        depth = ctx.decimal("ask_depth_top1")
        edge_bps = (mid - order_price) / mid * Decimal("10000")

    if edge_bps < edge_threshold:
        return RuleDecision(False, None, f"edge {edge_bps:.1f} bps < {edge_threshold}", features)

    size = _order_size(depth, scenario)
    return RuleDecision(
        True,
        SimOrder(side=side, order_price=order_price, order_size=size, reason="spread_capture"),
        f"spread {spread_bps:.1f} bps, prospective {side} at book-before price",
        features,
    )


def _completion_set_order(
    ctx: DecisionContext,
    scenario: ScenarioConfig,
    parameters: dict[str, object],
) -> RuleDecision:
    bid = ctx.decimal("best_bid_before")
    ask = ctx.decimal("best_ask_before")
    features = {
        "best_bid_before": ctx.get("best_bid_before"),
        "best_ask_before": ctx.get("best_ask_before"),
        "qty_token_before": ctx.get("qty_token_before"),
        "qty_complement_before": ctx.get("qty_complement_before"),
        "bond_before": ctx.get("bond_before"),
    }
    if bid is None or ask is None:
        return RuleDecision(False, None, "missing book-before bid/ask", features)

    max_bond_cost = Decimal(str(parameters["max_bond_cost"]))
    total_cost = bid + ask
    if total_cost > max_bond_cost:
        return RuleDecision(False, None, f"prospective bond cost {total_cost:.4f} > {max_bond_cost}", features)

    depth = ctx.decimal("ask_depth_top1")
    size = _order_size(depth, scenario)
    return RuleDecision(
        True,
        SimOrder(side="BUY", order_price=bid, order_size=size, reason="completion_set_edge"),
        f"prospective bond cost {total_cost:.4f} <= {max_bond_cost}",
        features,
    )


def _order_size(depth: Optional[Decimal], scenario: ScenarioConfig) -> Decimal:
    if depth is None or depth <= _ZERO:
        return scenario.max_order_size
    return max(Decimal("1"), min(scenario.max_order_size, depth * scenario.depth_fraction))


def _strategy_filter_skip_reason(
    strategy: StrategyConfig,
    ctx: DecisionContext,
    decision: RuleDecision,
) -> Optional[str]:
    if not strategy.filters:
        return None

    allowed_quality = strategy.filters.get("context_quality")
    if allowed_quality is not None and ctx.context_status not in set(allowed_quality):
        return "context_quality"

    max_book_age_s = strategy.filters.get("max_book_age_s")
    age = ctx.integer("book_before_age_s")
    if max_book_age_s is not None and age is not None and age > int(max_book_age_s):
        return "book_age_s"

    min_depth = strategy.filters.get("min_depth")
    if min_depth is not None and decision.order is not None:
        depth_key = "ask_depth_top1" if decision.order.side == "BUY" else "bid_depth_top1"
        depth = ctx.decimal(depth_key)
        if depth is None or depth < Decimal(str(min_depth)):
            return "min_depth"

    return None


def _pre_trade_risk_skip_reason(
    *,
    portfolio: PortfolioState,
    ctx: DecisionContext,
    order: SimOrder,
    strategy: StrategyConfig,
    scenario: ScenarioConfig,
    limits: RiskLimits,
    day_start_value: Decimal,
) -> Optional[str]:
    active_limits = set(strategy.pre_trade_risk_limits)

    if "max_order_size" in active_limits and order.order_size > limits.max_order_size:
        return "max_order_size"

    if "max_daily_loss" in active_limits:
        current_daily_pnl = portfolio.value() - day_start_value
        if current_daily_pnl < _ZERO and abs(current_daily_pnl) > limits.max_daily_loss:
            return "max_daily_loss"

    projected_price = _scenario_adjusted_order_price(order, scenario)
    projected = portfolio.project_fill(
        token_id=ctx.token_id,
        condition_id=ctx.condition_id,
        side=order.side,
        price=projected_price,
        size=order.order_size,
        mark_price=ctx.decimal("mid_before"),
    )

    if (
        "max_position_per_token" in active_limits
        and abs(projected.qty_token) > limits.max_position_per_token
    ):
        return "max_position_per_token"

    if (
        "max_event_exposure" in active_limits
        and abs(projected.event_exposure) > limits.max_event_exposure
    ):
        return "max_event_exposure"

    if (
        "max_capital_deployed" in active_limits
        and projected.capital_used > limits.max_capital_deployed
    ):
        return "max_capital_deployed"

    if "max_daily_loss" in active_limits:
        projected_daily_pnl = projected.value - day_start_value
        if projected_daily_pnl < _ZERO and abs(projected_daily_pnl) > limits.max_daily_loss:
            return "max_daily_loss"

    return None


def _scenario_adjusted_order_price(order: SimOrder, scenario: ScenarioConfig) -> Decimal:
    if scenario.slippage_bps <= _ZERO or order.order_price <= _ZERO:
        return order.order_price
    slippage = scenario.slippage_bps / Decimal("10000")
    if order.side == "BUY":
        return order.order_price * (_ONE + slippage)
    return order.order_price * (_ONE - slippage)


def _record_skip(
    *,
    skipped_orders: list[dict],
    skipped_by_reason: dict[str, int],
    ctx: DecisionContext,
    decision: RuleDecision,
    strategy: StrategyConfig,
    reason: str,
    collect_details: bool = True,
) -> None:
    skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
    if not collect_details:
        return
    order = decision.order
    skipped_orders.append(
        {
            "run_id": 0,
            "event_id": ctx.event_id,
            "token_id": ctx.token_id,
            "condition_id": ctx.condition_id,
            "strategy_name": strategy.strategy_name,
            "base_rule": strategy.base_rule,
            "side": order.side if order is not None else None,
            "order_price": str(order.order_price) if order is not None else None,
            "order_size": str(order.order_size) if order is not None else None,
            "skipped_reason": reason,
            "context_status": ctx.context_status,
            "book_age_s": ctx.integer("book_before_age_s"),
            "created_ts": ctx.trade_ts,
        }
    )


def _is_stale(ctx: DecisionContext, limits: RiskLimits) -> bool:
    if ctx.context_status in {"missing", "stale"}:
        return True
    age = ctx.integer("book_before_age_s")
    return bool(age is not None and age > limits.max_stale_book_age_s)


def _has_ordering_violation(conservative: _TransientResult, optimistic: _TransientResult) -> bool:
    return (
        conservative.fills_count > optimistic.fills_count
        or conservative.net_pnl > optimistic.net_pnl
    )


def _market_attribution_key(ctx: DecisionContext) -> str:
    return ctx.condition_id or f"token:{ctx.token_id}"


def _ensure_market_attribution(
    market_attribution: dict[str, dict],
    ctx: DecisionContext,
) -> dict:
    key = _market_attribution_key(ctx)
    if key not in market_attribution:
        market_attribution[key] = {
            "condition_id": ctx.condition_id or key,
            "event_id": str(ctx.event_id),
            "fills_count": 0,
            "fill_notional": _ZERO,
            "realized_pnl": _ZERO,
            "unrealized_pnl": _ZERO,
            "total_pnl": _ZERO,
            "max_inventory": _ZERO,
            "max_exposure": _ZERO,
            "turnover": _ZERO,
        }
    return market_attribution[key]


def _update_market_path_attribution(
    market_attribution: dict[str, dict],
    portfolio: PortfolioState,
    ctx: DecisionContext,
) -> None:
    pos = portfolio.positions.get(ctx.token_id)
    if pos is None:
        return
    bucket = _ensure_market_attribution(market_attribution, ctx)
    if abs(pos.qty) > bucket["max_inventory"]:
        bucket["max_inventory"] = abs(pos.qty)
    exposure = abs(pos.value())
    if exposure > bucket["max_exposure"]:
        bucket["max_exposure"] = exposure


def _record_market_fill_attribution(
    *,
    market_attribution: dict[str, dict],
    portfolio: PortfolioState,
    ctx: DecisionContext,
    side: str,
    notional: Decimal,
    fee: Decimal,
) -> None:
    bucket = _ensure_market_attribution(market_attribution, ctx)
    bucket["fills_count"] += 1
    bucket["fill_notional"] += notional
    bucket["turnover"] += notional
    if side == "BUY":
        bucket["realized_pnl"] -= notional + fee
    else:
        bucket["realized_pnl"] += notional - fee
    _update_market_path_attribution(market_attribution, portfolio, ctx)


def _finalize_market_attribution(
    market_attribution: dict[str, dict],
    portfolio: PortfolioState,
) -> list[dict]:
    values_by_condition: dict[str, Decimal] = {}
    for pos in portfolio.positions.values():
        key = pos.condition_id or f"token:{pos.token_id}"
        values_by_condition[key] = values_by_condition.get(key, _ZERO) + pos.value()

    rows: list[dict] = []
    for key, bucket in sorted(market_attribution.items()):
        unrealized = values_by_condition.get(key, _ZERO)
        realized = bucket["realized_pnl"]
        total = realized + unrealized
        rows.append(
            {
                "condition_id": bucket["condition_id"],
                "event_id": bucket["event_id"],
                "fills_count": bucket["fills_count"],
                "fill_notional": str(bucket["fill_notional"]),
                "realized_pnl": str(realized),
                "unrealized_pnl": str(unrealized),
                "total_pnl": str(total),
                "max_inventory": str(bucket["max_inventory"]),
                "max_exposure": str(bucket["max_exposure"]),
                "turnover": str(bucket["turnover"]),
            }
        )
    return rows


def _insert_run(
    *,
    session: Session,
    wallet: str,
    rule_name: str,
    strategy_name: str,
    rule_version: int,
    scenario: str,
    parameters: dict,
    risk_limits: RiskLimits,
    metrics: _TransientResult,
    conservative_pass: bool,
    ordering_violation: bool,
    elapsed_ms: int,
) -> int:
    result = session.execute(
        text(
            "INSERT INTO simulation_runs "
            "(wallet, rule_name, strategy_name, rule_version, scenario, parameters_json, "
            " risk_limits_json, orders_count, simulated_fills_count, fill_rate, "
            " simulated_pnl, net_pnl, max_drawdown, max_inventory, capital_required, "
            " turnover, skipped_orders_count, skipped_by_reason_json, risk_prevented_count, "
            " risk_breaches, stale_context_excluded, conservative_pass, "
            " ordering_violation, run_ts, elapsed_ms) "
            "VALUES "
            "(:wallet, :rule_name, :strategy_name, :rule_version, :scenario, :parameters_json, "
            " :risk_limits_json, :orders_count, :fills_count, :fill_rate, "
            " :simulated_pnl, :net_pnl, :max_drawdown, :max_inventory, :capital_required, "
            " :turnover, :skipped_orders_count, :skipped_by_reason_json, :risk_prevented_count, "
            " :risk_breaches, :stale_context_excluded, :conservative_pass, "
            " :ordering_violation, :run_ts, :elapsed_ms)"
        ),
        {
            "wallet": wallet,
            "rule_name": rule_name,
            "strategy_name": strategy_name,
            "rule_version": rule_version,
            "scenario": scenario,
            "parameters_json": json.dumps(parameters, sort_keys=True),
            "risk_limits_json": json.dumps(risk_limits.to_dict(), sort_keys=True),
            "orders_count": metrics.orders_count,
            "fills_count": metrics.fills_count,
            "fill_rate": str(metrics.fill_rate) if metrics.fill_rate is not None else None,
            "simulated_pnl": str(metrics.simulated_pnl),
            "net_pnl": str(metrics.net_pnl),
            "max_drawdown": str(metrics.max_drawdown),
            "max_inventory": str(metrics.max_inventory),
            "capital_required": str(metrics.capital_required),
            "turnover": str(metrics.turnover),
            "skipped_orders_count": metrics.skipped_orders_count,
            "skipped_by_reason_json": json.dumps(metrics.skipped_by_reason, sort_keys=True),
            "risk_prevented_count": metrics.risk_prevented_count,
            "risk_breaches": metrics.risk_breaches,
            "stale_context_excluded": metrics.stale_context_excluded,
            "conservative_pass": 1 if conservative_pass else 0,
            "ordering_violation": 1 if ordering_violation else 0,
            "run_ts": int(time.time()),
            "elapsed_ms": elapsed_ms,
        },
    )
    return int(result.lastrowid)


def _insert_orders(session: Session, run_id: int, orders: list[dict]) -> list[int]:
    ids: list[int] = []
    for order in orders:
        order = dict(order)
        order["run_id"] = run_id
        result = session.execute(
            text(
                "INSERT INTO simulation_orders "
                "(run_id, event_id, token_id, condition_id, side, order_price, "
                " order_size, rule_fires, rule_explanation, context_status, "
                " book_age_s, stale_excluded, created_ts) "
                "VALUES "
                "(:run_id, :event_id, :token_id, :condition_id, :side, :order_price, "
                " :order_size, :rule_fires, :rule_explanation, :context_status, "
                " :book_age_s, :stale_excluded, :created_ts)"
            ),
            order,
        )
        ids.append(int(result.lastrowid))
    return ids


def _insert_skipped_orders(session: Session, run_id: int, skipped_orders: list[dict]) -> None:
    if not skipped_orders:
        return
    rows = []
    for skipped in skipped_orders:
        row = dict(skipped)
        row["run_id"] = run_id
        rows.append(row)
    session.execute(
        text(
            "INSERT INTO simulation_skipped_orders "
            "(run_id, event_id, token_id, condition_id, strategy_name, base_rule, "
            " side, order_price, order_size, skipped_reason, context_status, "
            " book_age_s, created_ts) "
            "VALUES "
            "(:run_id, :event_id, :token_id, :condition_id, :strategy_name, :base_rule, "
            " :side, :order_price, :order_size, :skipped_reason, :context_status, "
            " :book_age_s, :created_ts)"
        ),
        rows,
    )


def _insert_fills(session: Session, run_id: int, fills: list[dict], order_ids: list[int]) -> None:
    if not fills:
        return
    rows = []
    for fill in fills:
        row = dict(fill)
        order_index = int(row.pop("order_index"))
        row["run_id"] = run_id
        row["order_id"] = order_ids[order_index]
        rows.append(row)
    session.execute(
        text(
            "INSERT INTO simulation_fills "
            "(run_id, order_id, event_id, token_id, condition_id, side, "
            " fill_price, fill_size, fill_notional_usdc, estimated_fee, "
            " scenario, fill_reason, filled_ts) "
            "VALUES "
            "(:run_id, :order_id, :event_id, :token_id, :condition_id, :side, "
            " :fill_price, :fill_size, :fill_notional_usdc, :estimated_fee, "
            " :scenario, :fill_reason, :filled_ts)"
        ),
        rows,
    )


def _insert_inventory(session: Session, run_id: int, snapshots: list[dict]) -> None:
    if not snapshots:
        return
    rows = []
    for snapshot in snapshots:
        row = dict(snapshot)
        row["run_id"] = run_id
        rows.append(row)
    session.execute(
        text(
            "INSERT INTO simulation_inventory "
            "(run_id, event_id, token_id, condition_id, qty_token, qty_complement, "
            " directional, bond, cost_basis, mark_price, unrealized_pnl, "
            " event_exposure, snapshot_ts) "
            "VALUES "
            "(:run_id, :event_id, :token_id, :condition_id, :qty_token, :qty_complement, "
            " :directional, :bond, :cost_basis, :mark_price, :unrealized_pnl, "
            " :event_exposure, :snapshot_ts)"
        ),
        rows,
    )


def _insert_daily_pnl(session: Session, run_id: int, daily: dict[str, dict]) -> None:
    if not daily:
        return
    cumulative = _ZERO
    rows = []
    peak = _ZERO
    for date_str in sorted(daily):
        row = daily[date_str]
        total = row.get("pnl", _ZERO)
        cumulative = total
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        rows.append(
            {
                "run_id": run_id,
                "date_utc": date_str,
                "realized_pnl": str(total),
                "unrealized_pnl": "0",
                "total_pnl": str(total),
                "cumulative_pnl": str(cumulative),
                "peak_portfolio": str(peak),
                "drawdown": str(drawdown),
                "fills_count": int(row.get("fills", 0)),
                "turnover": str(row.get("turnover", _ZERO)),
                "risk_breaches": int(row.get("breaches", 0)),
            }
        )
    session.execute(
        text(
            "INSERT INTO simulation_pnl_daily "
            "(run_id, date_utc, realized_pnl, unrealized_pnl, total_pnl, "
            " cumulative_pnl, peak_portfolio, drawdown, fills_count, "
            " turnover, risk_breaches) "
            "VALUES "
            "(:run_id, :date_utc, :realized_pnl, :unrealized_pnl, :total_pnl, "
            " :cumulative_pnl, :peak_portfolio, :drawdown, :fills_count, "
            " :turnover, :risk_breaches)"
        ),
        rows,
    )


def _insert_risk_events(session: Session, run_id: int, events: list[RiskEvent]) -> None:
    if not events:
        return
    rows = [
        {
            "run_id": run_id,
            "event_type": ev.event_type,
            "limit_name": ev.limit_name,
            "limit_value": ev.limit_value,
            "actual_value": ev.actual_value,
            "token_id": ev.token_id,
            "condition_id": ev.condition_id,
            "description": ev.description,
            "timestamp": ev.timestamp,
        }
        for ev in events
    ]
    session.execute(
        text(
            "INSERT INTO simulation_risk_events "
            "(run_id, event_type, limit_name, limit_value, actual_value, "
            " token_id, condition_id, description, timestamp) "
            "VALUES "
            "(:run_id, :event_type, :limit_name, :limit_value, :actual_value, "
            " :token_id, :condition_id, :description, :timestamp)"
        ),
        rows,
    )


def _ts_to_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
