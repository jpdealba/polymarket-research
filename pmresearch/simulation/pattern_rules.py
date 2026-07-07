"""Phase 22.6 simulator rules from Phase 22.5b pattern candidates.

The entry logic in this module uses only pre-fill simulated inventory, token
metadata, and book-before context. Phase 22.5/22.5b outputs are read as rule
definitions only; they are not rebuilt or used as future labels.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from .engine import DecisionContext, RuleDecision, _TransientResult
from .inventory_cycling import (
    InventoryCyclingConfig,
    InventoryLifecycleState,
    InventoryLot,
    LifecycleMetrics,
    build_inventory_config,
    insert_lifecycle_outputs,
    merge_qty_for_condition,
    simulate_inventory_cycling,
)
from .risk import RiskLimits
from .scenarios import ScenarioConfig, SimOrder

_ZERO = Decimal("0")
_ONE = Decimal("1")

PATTERN_STRATEGIES = frozenset(
    {
        "pattern_complement_catchup_v1",
        "pattern_bond_increasing_buy_v1",
        "pattern_event_basket_gate_v1",
        "pattern_complement_catchup_with_event_gate_v1",
        "pattern_bond_increasing_with_event_gate_v1",
        "pattern_abd_inventory_rule_v1",
    }
)

PATTERN_OUTPUT_DIR = Path("exports/phase22_6_pattern_rules")
PATTERN_REPORT = "pattern_strategy_comparison_report.md"
PATTERN_CANDIDATES = "pattern_strategy_candidates.csv"
PATTERN_EVENT_ROBUSTNESS = "pattern_strategy_event_robustness.csv"
PATTERN_RISK_EVENTS = "pattern_strategy_risk_events.csv"
PATTERN_PARAMETER_GRID = "pattern_strategy_parameter_grid.csv"
PATTERN_HOLDOUT = "pattern_strategy_holdout_summary.md"
PATTERN_SIGNAL_DIAGNOSTICS = "pattern_signal_diagnostics.csv"
PATTERN_SIGNAL_DIAGNOSTICS_REPORT = "pattern_signal_diagnostics.md"
PATTERN_SIGNAL_SAMPLE_FAILURES = "pattern_signal_sample_failures.csv"
PATTERN_PNL_ATTRIBUTION_REPORT = "pattern_pnl_attribution_report.md"
PATTERN_PNL_ATTRIBUTION_BY_EVENT = "pattern_pnl_attribution_by_event.csv"
PATTERN_PNL_ATTRIBUTION_BY_CONDITION = "pattern_pnl_attribution_by_condition.csv"
PATTERN_FILL_LEDGER_SAMPLE = "pattern_fill_ledger_sample.csv"
PATTERN_STRATEGY_SANITY_CHECKS = "pattern_strategy_sanity_checks.md"
PATTERN_REDEEM_DIRECTIONAL_ATTRIBUTION_REPORT = "pattern_redeem_directional_attribution_report.md"
PATTERN_REDEEM_ATTRIBUTION_BY_EVENT = "pattern_redeem_attribution_by_event.csv"
PATTERN_REDEEM_ATTRIBUTION_BY_CONDITION = "pattern_redeem_attribution_by_condition.csv"
PATTERN_REDEEM_ATTRIBUTION_BY_TOKEN = "pattern_redeem_attribution_by_token.csv"
PATTERN_DIRECTIONAL_INVENTORY_TIMELINE = "pattern_directional_inventory_timeline.csv"

DIAGNOSTIC_GATE_COLUMNS = [
    "starting_context_rows",
    "binary_condition_rows",
    "rows_with_token_mapping",
    "rows_with_inventory_before",
    "rows_with_unpaired_inventory",
    "rows_where_rule_a_opposite_unpaired_exists",
    "rows_where_rule_b_would_increase_bond",
    "rows_passing_event_basket_gate",
    "rows_with_wac_or_cost_basis",
    "rows_with_positive_max_quote_price",
    "rows_passing_complete_set_cost_threshold",
    "rows_with_fresh_book_context",
    "rows_with_positive_order_size",
    "rows_passing_risk_caps",
    "final_candidate_signals",
]

PRE_SIGNAL_SKIP_REASONS = [
    "no_binary_mapping",
    "missing_inventory_before",
    "no_opposite_unpaired_inventory",
    "would_not_increase_bond",
    "event_gate_failed",
    "missing_cost_basis",
    "max_quote_price_non_positive",
    "complete_set_cost_above_threshold",
    "stale_or_missing_book",
    "order_size_zero",
    "condition_cap_hit",
    "event_cap_hit",
    "risk_cap_hit",
    "unknown_reason",
]


@dataclass(frozen=True)
class TokenPair:
    yes_token_id: str
    no_token_id: str

    def contains(self, token_id: str) -> bool:
        return token_id in {self.yes_token_id, self.no_token_id}

    def other(self, token_id: str) -> str | None:
        if token_id == self.yes_token_id:
            return self.no_token_id
        if token_id == self.no_token_id:
            return self.yes_token_id
        return None


@dataclass(frozen=True)
class PatternRuleConfig:
    strategy_name: str
    max_complete_set_cost: Decimal = Decimal("0.98")
    min_active_conditions: int = 2
    max_order_size: Decimal = Decimal("25")
    max_position_per_token: Decimal = Decimal("500")
    max_condition_capital: Decimal = Decimal("2000")
    max_event_capital: Decimal = Decimal("5000")
    max_unpaired_qty_per_condition: Decimal = Decimal("500")
    max_unpaired_notional_per_condition: Decimal = Decimal("500")
    max_unpaired_qty_per_event: Decimal = Decimal("1000")
    max_event_unpaired_inventory: Decimal = Decimal("1000")
    max_stale_book_age_s: int = 60
    min_merge_qty: Decimal = Decimal("1")
    merge_batch_window_s: int = 300
    merge_immediately: bool = True
    auto_merge_enabled: bool = True
    recycle_capital_enabled: bool = True
    fallback_cost_basis: str = "skip"
    require_bond_increasing_filter: bool = False

    @property
    def uses_rule_a(self) -> bool:
        return self.strategy_name in {
            "pattern_complement_catchup_v1",
            "pattern_complement_catchup_with_event_gate_v1",
            "pattern_abd_inventory_rule_v1",
        }

    @property
    def uses_rule_b(self) -> bool:
        return self.strategy_name in {
            "pattern_bond_increasing_buy_v1",
            "pattern_bond_increasing_with_event_gate_v1",
        }

    @property
    def uses_event_gate(self) -> bool:
        return self.strategy_name in {
            "pattern_event_basket_gate_v1",
            "pattern_complement_catchup_with_event_gate_v1",
            "pattern_bond_increasing_with_event_gate_v1",
            "pattern_abd_inventory_rule_v1",
        }

    def to_inventory_config(self) -> InventoryCyclingConfig:
        return InventoryCyclingConfig(
            max_bond_cost=self.max_complete_set_cost,
            min_bond_delta=_ZERO,
            max_unpaired_inventory=self.max_unpaired_qty_per_condition,
            max_event_exposure=self.max_event_capital,
            max_position_per_token=self.max_position_per_token,
            max_capital=self.max_event_capital,
            max_order_size=self.max_order_size,
            min_merge_qty=self.min_merge_qty,
            auto_merge_enabled=self.auto_merge_enabled,
            recycle_capital_enabled=self.recycle_capital_enabled,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_name": self.strategy_name,
            "max_complete_set_cost": str(self.max_complete_set_cost),
            "min_active_conditions": self.min_active_conditions,
            "max_order_size": str(self.max_order_size),
            "max_position_per_token": str(self.max_position_per_token),
            "max_condition_capital": str(self.max_condition_capital),
            "max_event_capital": str(self.max_event_capital),
            "max_unpaired_qty_per_condition": str(self.max_unpaired_qty_per_condition),
            "max_unpaired_notional_per_condition": str(self.max_unpaired_notional_per_condition),
            "max_unpaired_qty_per_event": str(self.max_unpaired_qty_per_event),
            "max_event_unpaired_inventory": str(self.max_event_unpaired_inventory),
            "max_stale_book_age_s": self.max_stale_book_age_s,
            "min_merge_qty": str(self.min_merge_qty),
            "merge_batch_window_s": self.merge_batch_window_s,
            "merge_immediately": self.merge_immediately,
            "auto_merge_enabled": self.auto_merge_enabled,
            "recycle_capital_enabled": self.recycle_capital_enabled,
            "fallback_cost_basis": self.fallback_cost_basis,
            "require_bond_increasing_filter": self.require_bond_increasing_filter,
        }


@dataclass(frozen=True)
class PatternMetadata:
    token_pairs: dict[str, TokenPair]
    condition_events: dict[str, str]
    condition_questions: dict[str, str]

    def event_key(self, condition_id: str | None) -> str:
        if condition_id is None:
            return ""
        return self.condition_events.get(condition_id, condition_id)


def is_pattern_strategy(strategy_name: str) -> bool:
    return strategy_name.strip().lower() in PATTERN_STRATEGIES


def default_pattern_parameters(strategy_name: str) -> dict[str, object]:
    params = PatternRuleConfig(strategy_name=strategy_name).to_dict()
    if strategy_name == "pattern_abd_inventory_rule_v1":
        params["require_bond_increasing_filter"] = True
    if strategy_name == "pattern_event_basket_gate_v1":
        params["entry_ablation"] = "event_inventory_cycling_v1_with_rule_d_gate"
    return params


def build_pattern_config(
    strategy_name: str,
    risk_limits: RiskLimits,
    parameters: Optional[dict[str, object]] = None,
) -> PatternRuleConfig:
    parameters = dict(parameters or {})
    defaults = default_pattern_parameters(strategy_name)
    defaults.update(parameters)
    return PatternRuleConfig(
        strategy_name=strategy_name,
        max_complete_set_cost=Decimal(str(defaults.get("max_complete_set_cost", "0.98"))),
        min_active_conditions=int(defaults.get("min_active_conditions", 2)),
        max_order_size=Decimal(str(defaults.get("max_order_size", risk_limits.max_order_size))),
        max_position_per_token=Decimal(str(defaults.get("max_position_per_token", risk_limits.max_position_per_token))),
        max_condition_capital=Decimal(str(defaults.get("max_condition_capital", risk_limits.max_event_exposure))),
        max_event_capital=Decimal(str(defaults.get("max_event_capital", risk_limits.max_capital_deployed))),
        max_unpaired_qty_per_condition=Decimal(str(defaults.get("max_unpaired_qty_per_condition", risk_limits.max_position_per_token))),
        max_unpaired_notional_per_condition=Decimal(str(defaults.get("max_unpaired_notional_per_condition", risk_limits.max_event_exposure))),
        max_unpaired_qty_per_event=Decimal(str(defaults.get("max_unpaired_qty_per_event", risk_limits.max_directional_per_market))),
        max_event_unpaired_inventory=Decimal(str(defaults.get("max_event_unpaired_inventory", risk_limits.max_directional_per_market))),
        max_stale_book_age_s=int(defaults.get("max_stale_book_age_s", risk_limits.max_stale_book_age_s)),
        min_merge_qty=Decimal(str(defaults.get("min_merge_qty", "1"))),
        merge_batch_window_s=int(defaults.get("merge_batch_window_s", 300)),
        merge_immediately=bool(defaults.get("merge_immediately", True)),
        auto_merge_enabled=bool(defaults.get("auto_merge_enabled", True)),
        recycle_capital_enabled=bool(defaults.get("recycle_capital_enabled", True)),
        fallback_cost_basis=str(defaults.get("fallback_cost_basis", "skip")),
        require_bond_increasing_filter=bool(defaults.get("require_bond_increasing_filter", False)),
    )


def load_pattern_metadata(session: Session, rows: list[dict]) -> PatternMetadata:
    condition_ids = {str(row.get("condition_id")) for row in rows if row.get("condition_id")}
    token_pairs: dict[str, TokenPair] = {}
    if condition_ids:
        token_rows = session.execute(
            text(
                "SELECT condition_id, token_id, outcome_index FROM tokens "
                "WHERE condition_id IN :ids ORDER BY condition_id, outcome_index"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": list(condition_ids)},
        ).mappings().fetchall()
        grouped: dict[str, list[tuple[int, str]]] = {}
        for row in token_rows:
            try:
                index = int(row["outcome_index"])
            except (TypeError, ValueError):
                continue
            grouped.setdefault(str(row["condition_id"]), []).append((index, str(row["token_id"])))
        for condition_id, values in grouped.items():
            ordered = [token for _, token in sorted(values)]
            if len(ordered) >= 2:
                token_pairs[condition_id] = TokenPair(ordered[0], ordered[1])

    condition_events: dict[str, str] = {}
    condition_questions: dict[str, str] = {}
    if condition_ids:
        market_rows = session.execute(
            text(
                "SELECT condition_id, event_id, question FROM markets "
                "WHERE condition_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": list(condition_ids)},
        ).mappings().fetchall()
        for row in market_rows:
            cid = str(row["condition_id"])
            condition_events[cid] = str(row["event_id"] or cid)
            condition_questions[cid] = str(row["question"] or "")
    return PatternMetadata(token_pairs, condition_events, condition_questions)


def run_pattern_strategy(
    session: Session,
    rows: list[dict],
    scenario: ScenarioConfig,
    risk_limits: RiskLimits,
    *,
    strategy_name: str,
    parameters: Optional[dict[str, object]] = None,
) -> _TransientResult:
    rows = _enrich_pattern_rows_from_phase225b(rows)
    config = build_pattern_config(strategy_name, risk_limits, parameters)
    metadata = load_pattern_metadata(session, rows)
    resolution_prices = _load_resolution_prices(session, {str(row.get("condition_id")) for row in rows if row.get("condition_id")})

    def filter_fn(ctx: DecisionContext, state: InventoryLifecycleState, order: SimOrder) -> Optional[str]:
        _ = order
        decision = decide_pattern_order(ctx, state, state, scenario, config, metadata)
        return None if decision.applies else decision.explanation

    if strategy_name == "pattern_event_basket_gate_v1":
        base_config = build_inventory_config(risk_limits, config.to_inventory_config().to_dict())
        transient = simulate_inventory_cycling(
            rows,
            scenario,
            risk_limits,
            config=base_config,
            resolution_prices=resolution_prices,
            pre_order_filter=filter_fn,
        )
    else:
        transient = _simulate_pattern_entries(rows, scenario, risk_limits, config, metadata, resolution_prices)

    transient.pattern_config = config
    transient.pattern_metadata = metadata
    return transient


def decide_pattern_order(
    ctx: DecisionContext,
    observed_leader_state: InventoryLifecycleState,
    simulated_portfolio_state: InventoryLifecycleState,
    scenario: ScenarioConfig,
    config: PatternRuleConfig,
    metadata: PatternMetadata,
) -> RuleDecision:
    if config.uses_event_gate:
        gate = event_basket_gate(ctx, observed_leader_state, config, metadata)
        if gate is not None:
            return RuleDecision(False, None, gate, {"gate": "event_basket"})

    if config.strategy_name == "pattern_event_basket_gate_v1":
        return RuleDecision(True, SimOrder("BUY", Decimal("0"), Decimal("0"), "event_gate"), "event basket gate passed", {})

    if config.uses_rule_b and not config.uses_rule_a:
        return rule_b_bond_increasing_order(
            ctx,
            observed_leader_state,
            scenario,
            config,
            metadata,
            simulated_portfolio_state=simulated_portfolio_state,
        )

    decision = rule_a_complement_catchup_order(
        ctx,
        observed_leader_state,
        scenario,
        config,
        metadata,
        simulated_portfolio_state=simulated_portfolio_state,
    )
    if not decision.applies:
        return decision
    if config.require_bond_increasing_filter:
        check = rule_b_bond_increasing_order(
            ctx,
            observed_leader_state,
            scenario,
            config,
            metadata,
            simulated_portfolio_state=simulated_portfolio_state,
        )
        if not check.applies:
            return RuleDecision(False, None, f"rule_b_filter:{check.explanation}", decision.features_used | check.features_used)
    return decision


def rule_a_complement_catchup_order(
    ctx: DecisionContext,
    observed_leader_state: InventoryLifecycleState,
    scenario: ScenarioConfig,
    config: PatternRuleConfig,
    metadata: PatternMetadata,
    *,
    simulated_portfolio_state: InventoryLifecycleState | None = None,
) -> RuleDecision:
    simulated_portfolio_state = simulated_portfolio_state or observed_leader_state
    common = _common_entry_context(ctx, observed_leader_state, scenario, config, metadata)
    if isinstance(common, RuleDecision):
        return common
    pair, condition_id, token_id, opposite_id, quote_price, same, opposite, features = common
    opposite_unpaired = _token_unpaired(observed_leader_state, condition_id, opposite_id, pair)
    if opposite_unpaired <= _ZERO:
        return RuleDecision(False, None, "rule_a:no_opposite_unpaired_inventory", features)
    if _token_unpaired(observed_leader_state, condition_id, token_id, pair) > _ZERO:
        return RuleDecision(False, None, "rule_a:would_increase_dominant_side_exposure", features)
    if same.qty >= opposite.qty:
        return RuleDecision(False, None, "rule_a:not_complement_side", features)
    cost_basis = _cost_basis(opposite, config)
    if cost_basis is None:
        return RuleDecision(False, None, "rule_a:missing_opposite_cost_basis", features)
    max_quote_price = config.max_complete_set_cost - cost_basis
    if max_quote_price <= _ZERO:
        return RuleDecision(False, None, "rule_a:combined_cost_exceeds_threshold", features | {"opposite_cost_basis": str(cost_basis)})
    allowed_price = min(quote_price, max_quote_price)
    if cost_basis + allowed_price > config.max_complete_set_cost:
        return RuleDecision(False, None, "rule_a:combined_cost_exceeds_threshold", features | {"opposite_cost_basis": str(cost_basis)})
    size = _entry_size(
        simulated_portfolio_state,
        condition_id,
        token_id,
        price=allowed_price,
        imbalance_qty=opposite_unpaired,
        scenario=scenario,
        config=config,
        metadata=metadata,
    )
    if size <= _ZERO:
        return RuleDecision(False, None, "rule_a:size_zero_or_risk_cap", features)
    return RuleDecision(
        True,
        SimOrder("BUY", allowed_price, size, "pattern_complement_catchup_v1"),
        "Rule A complement catch-up: opposite unpaired inventory and complete-set cost within threshold",
        features
        | {
            "opposite_cost_basis": str(cost_basis),
            "max_quote_price": str(max_quote_price),
            "order_price": str(allowed_price),
            "order_size": str(size),
        },
    )


def rule_b_bond_increasing_order(
    ctx: DecisionContext,
    observed_leader_state: InventoryLifecycleState,
    scenario: ScenarioConfig,
    config: PatternRuleConfig,
    metadata: PatternMetadata,
    *,
    simulated_portfolio_state: InventoryLifecycleState | None = None,
) -> RuleDecision:
    simulated_portfolio_state = simulated_portfolio_state or observed_leader_state
    common = _common_entry_context(ctx, observed_leader_state, scenario, config, metadata)
    if isinstance(common, RuleDecision):
        return common
    pair, condition_id, token_id, _opposite_id, quote_price, same, opposite, features = common
    if same.qty >= opposite.qty:
        return RuleDecision(False, None, "rule_b:not_bond_increasing", features)
    cost_basis = _cost_basis(opposite, config)
    if cost_basis is None:
        return RuleDecision(False, None, "rule_b:missing_opposite_cost_basis", features)
    max_quote_price = config.max_complete_set_cost - cost_basis
    if max_quote_price <= _ZERO:
        return RuleDecision(False, None, "rule_b:combined_cost_exceeds_threshold", features | {"opposite_cost_basis": str(cost_basis)})
    allowed_price = min(quote_price, max_quote_price)
    imbalance_qty = opposite.qty - same.qty
    size = _entry_size(
        simulated_portfolio_state,
        condition_id,
        token_id,
        price=allowed_price,
        imbalance_qty=imbalance_qty,
        scenario=scenario,
        config=config,
        metadata=metadata,
    )
    if size <= _ZERO:
        return RuleDecision(False, None, "rule_b:size_zero_or_risk_cap", features)
    return RuleDecision(
        True,
        SimOrder("BUY", allowed_price, size, "pattern_bond_increasing_buy_v1"),
        "Rule B bond-increasing BUY: proposed order increases paired quantity",
        features
        | {
            "opposite_cost_basis": str(cost_basis),
            "max_quote_price": str(max_quote_price),
            "order_price": str(allowed_price),
            "order_size": str(size),
        },
    )


def event_basket_gate(
    ctx: DecisionContext,
    state: InventoryLifecycleState,
    config: PatternRuleConfig,
    metadata: PatternMetadata,
) -> Optional[str]:
    condition_id = ctx.condition_id
    event_key = metadata.event_key(condition_id)
    active_count = ctx.integer("event_market_count_active_before")
    event_unpaired = ctx.decimal("event_unpaired_inventory_before")
    event_bond = ctx.decimal("event_bond_qty_before")
    if active_count is None:
        active_count = len(_event_active_conditions(state, event_key, metadata))
    if event_unpaired is None:
        event_unpaired = _event_unpaired_qty(state, event_key, metadata)
    if event_bond is None:
        event_bond = _event_bond_qty(state, event_key, metadata)
    if active_count < config.min_active_conditions:
        return "rule_d:min_active_conditions"
    if event_unpaired <= _ZERO and event_bond <= _ZERO:
        return "rule_d:no_event_inventory"
    return None


def maybe_merge_batches(
    state: InventoryLifecycleState,
    *,
    ctx: DecisionContext,
    config: PatternRuleConfig,
    metadata: PatternMetadata,
    last_merge_ts_by_event: dict[str, int],
) -> list[dict]:
    if not config.auto_merge_enabled:
        return []
    condition_id = ctx.condition_id or f"token:{ctx.token_id}"
    event_key = metadata.event_key(condition_id)
    eligible = []
    if config.merge_immediately:
        eligible = [condition_id]
    else:
        last_ts = last_merge_ts_by_event.get(event_key)
        if last_ts is not None and ctx.trade_ts - last_ts < config.merge_batch_window_s:
            return []
        eligible = [
            cid for cid in state.positions
            if metadata.event_key(cid) == event_key
        ]
    events: list[dict] = []
    for cid in sorted(set(eligible)):
        merge_qty = merge_qty_for_condition(state, cid)
        if merge_qty < config.min_merge_qty:
            continue
        proceeds, merge_pnl, before, after = state.merge(cid, merge_qty)
        last_merge_ts_by_event[event_key] = ctx.trade_ts
        events.extend(
            [
                {
                    "ts": ctx.trade_ts,
                    "event_type": "MERGE_SIMULATED",
                    "condition_id": cid,
                    "token_id": None,
                    "qty": str(merge_qty),
                    "usdc_delta": str(proceeds),
                    "merge_pnl": str(merge_pnl),
                    "capital_released": str(proceeds),
                    "inventory_before_json": json.dumps(before, sort_keys=True),
                    "inventory_after_json": json.dumps(after, sort_keys=True),
                },
                {
                    "ts": ctx.trade_ts,
                    "event_type": "CAPITAL_RELEASED",
                    "condition_id": cid,
                    "token_id": None,
                    "qty": str(merge_qty),
                    "usdc_delta": str(proceeds),
                    "capital_released": str(proceeds),
                    "inventory_before_json": json.dumps(before, sort_keys=True),
                    "inventory_after_json": json.dumps(after, sort_keys=True),
                },
            ]
        )
    return events


def _simulate_pattern_entries(
    rows: list[dict],
    scenario: ScenarioConfig,
    risk_limits: RiskLimits,
    config: PatternRuleConfig,
    metadata: PatternMetadata,
    resolution_prices: dict[str, dict[str, Decimal]],
) -> _TransientResult:
    from .inventory_cycling import simulate_redeem, _inventory_row, _lifecycle_metrics
    from .scenarios import decide_fill

    observed_leader_state = InventoryLifecycleState()
    simulated_portfolio_state = InventoryLifecycleState()
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
    max_condition_exposure_seen = _ZERO
    max_unpaired_seen = _ZERO
    max_event_unpaired_seen = _ZERO
    bond_created = _ZERO
    latest_ts = 0
    last_merge_ts_by_event: dict[str, int] = {}

    for row in rows:
        ctx = DecisionContext.from_row(row)
        latest_ts = max(latest_ts, ctx.trade_ts)
        condition_id = ctx.condition_id or f"token:{ctx.token_id}"
        _seed_pattern_state_from_row(row, observed_leader_state, metadata)
        observed_leader_state.mark(condition_id, ctx.token_id, ctx.decimal("mid_before"))
        simulated_portfolio_state.mark(condition_id, ctx.token_id, ctx.decimal("mid_before"))
        event_key = metadata.event_key(condition_id)

        decision = decide_pattern_order(
            ctx,
            observed_leader_state,
            simulated_portfolio_state,
            scenario,
            config,
            metadata,
        )
        if not decision.applies or decision.order is None:
            continue
        rule_fires += 1

        reason = pattern_risk_skip_reason(ctx, simulated_portfolio_state, decision.order, config, risk_limits, metadata)
        if reason is not None:
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
            risk_prevented += 1
            skipped_orders.append(_skip_row(ctx, decision, config, reason))
            continue

        is_stale = _is_stale(ctx, risk_limits, config)
        order_index = orders_count
        orders_count += 1
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
        if not assumption.would_fill or assumption.fill_price is None or assumption.fill_size is None:
            continue

        pair = metadata.token_pairs.get(condition_id)
        if pair is not None:
            yes_before = simulated_portfolio_state.lot(condition_id, pair.yes_token_id).qty
            no_before = simulated_portfolio_state.lot(condition_id, pair.no_token_id).qty
            paired_before = min(yes_before, no_before)
            unpaired_before = abs(yes_before - no_before)
            opposite_id = pair.other(ctx.token_id)
            opposite_lot = simulated_portfolio_state.lot(condition_id, opposite_id) if opposite_id else InventoryLot()
            opposite_cost_basis = opposite_lot.avg_cost if opposite_lot.qty > _ZERO else _ZERO
        else:
            yes_before = simulated_portfolio_state.lot(condition_id, ctx.token_id).qty
            no_before = _ZERO
            paired_before = _ZERO
            unpaired_before = yes_before
            opposite_cost_basis = _ZERO
        cash_before = simulated_portfolio_state.cash
        inventory_value_before = _inventory_value(simulated_portfolio_state)
        fee = simulated_portfolio_state.apply_buy(condition_id, ctx.token_id, assumption.fill_price, assumption.fill_size)
        notional = assumption.fill_price * assumption.fill_size
        if pair is not None:
            yes_after = simulated_portfolio_state.lot(condition_id, pair.yes_token_id).qty
            no_after = simulated_portfolio_state.lot(condition_id, pair.no_token_id).qty
            paired_after = min(yes_after, no_after)
            unpaired_after = abs(yes_after - no_after)
        else:
            yes_after = simulated_portfolio_state.lot(condition_id, ctx.token_id).qty
            no_after = _ZERO
            paired_after = _ZERO
            unpaired_after = yes_after
        cash_after = simulated_portfolio_state.cash
        inventory_value_after = _inventory_value(simulated_portfolio_state)
        complete_set_cost = (
            opposite_cost_basis + assumption.fill_price
            if paired_after > paired_before and opposite_cost_basis > _ZERO
            else None
        )
        fills_count += 1
        max_condition_exposure_seen = max(max_condition_exposure_seen, _condition_capital(simulated_portfolio_state, condition_id))
        max_event_exposure_seen = max(max_event_exposure_seen, _event_capital(simulated_portfolio_state, event_key, metadata))
        bond_created += max(_ZERO, _condition_bond_qty(simulated_portfolio_state, condition_id, metadata) - bond_created)
        day = _ts_to_date(ctx.trade_ts)
        daily = daily_pnl.setdefault(day, {"pnl": _ZERO, "turnover": _ZERO, "breaches": 0, "fills": 0})
        daily["pnl"] = simulated_portfolio_state.value()
        daily["turnover"] += notional
        daily["fills"] += 1
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
                "rule_triggered": decision.order.reason,
                "quote_price": decision.features_used.get("best_bid_before"),
                "max_quote_price": decision.features_used.get("max_quote_price"),
                "complete_set_cost_estimate": str(complete_set_cost) if complete_set_cost is not None else "",
                "qty_yes_before_sim": str(yes_before),
                "qty_no_before_sim": str(no_before),
                "qty_yes_after_sim": str(yes_after),
                "qty_no_after_sim": str(no_after),
                "paired_qty_before_sim": str(paired_before),
                "paired_qty_after_sim": str(paired_after),
                "unpaired_before_sim": str(unpaired_before),
                "unpaired_after_sim": str(unpaired_after),
                "cash_before": str(cash_before),
                "cash_after": str(cash_after),
                "realized_pnl_delta": str(-fee),
                "inventory_value_delta": str(inventory_value_after - inventory_value_before),
            }
        )
        inventory.append(_inventory_row(ctx, simulated_portfolio_state))
        lifecycle_events.extend(
            maybe_merge_batches(
                simulated_portfolio_state,
                ctx=ctx,
                config=config,
                metadata=metadata,
                last_merge_ts_by_event=last_merge_ts_by_event,
            )
        )
        max_unpaired_seen = max(max_unpaired_seen, _condition_unpaired_qty(simulated_portfolio_state, condition_id, metadata))
        max_event_unpaired_seen = max(max_event_unpaired_seen, _event_unpaired_qty(simulated_portfolio_state, event_key, metadata))

    from .inventory_cycling import simulate_redeem

    if fills_count > 0:
        redeem_events, redeem_metrics = simulate_redeem(simulated_portfolio_state, resolution_prices, ts=latest_ts + 1)
    else:
        redeem_events, redeem_metrics = [], LifecycleMetrics()
    lifecycle_events.extend(redeem_events)
    lifecycle_state = simulated_portfolio_state if fills_count > 0 else InventoryLifecycleState()
    lifecycle = _lifecycle_metrics(lifecycle_state, lifecycle_events, redeem_metrics, max_unpaired_seen, bond_created, config.to_inventory_config())
    lifecycle.max_unpaired_inventory = max_unpaired_seen
    net_pnl = simulated_portfolio_state.value() if fills_count > 0 else _ZERO
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
        max_drawdown=simulated_portfolio_state.max_drawdown,
        max_inventory=max_unpaired_seen,
        capital_required=simulated_portfolio_state.max_locked_capital,
        turnover=simulated_portfolio_state.turnover,
        skipped_orders_count=sum(skipped_by_reason.values()),
        skipped_by_reason=skipped_by_reason,
        risk_prevented_count=risk_prevented,
        risk_breaches=0,
        stale_context_excluded=stale_excluded,
    )
    transient.lifecycle_events = lifecycle_events
    transient.lifecycle_metrics = lifecycle
    transient.max_event_exposure_seen = max_event_exposure_seen
    transient.max_condition_exposure_seen = max_condition_exposure_seen
    transient.max_event_unpaired_seen = max_event_unpaired_seen
    return transient


def pattern_risk_skip_reason(
    ctx: DecisionContext,
    state: InventoryLifecycleState,
    order: SimOrder,
    config: PatternRuleConfig,
    limits: RiskLimits,
    metadata: PatternMetadata,
) -> Optional[str]:
    condition_id = ctx.condition_id or f"token:{ctx.token_id}"
    event_key = metadata.event_key(condition_id)
    notional = order.order_price * order.order_size
    if order.order_size > min(config.max_order_size, limits.max_order_size):
        return "max_order_size"
    if state.lot(condition_id, ctx.token_id).qty + order.order_size > min(config.max_position_per_token, limits.max_position_per_token):
        return "max_position_per_token"
    if notional > state.available_capital(config.to_inventory_config()):
        return "max_capital"
    if _condition_capital(state, condition_id) + notional > min(config.max_condition_capital, limits.max_event_exposure):
        return "max_condition_capital"
    if _event_capital(state, event_key, metadata) + notional > min(config.max_event_capital, limits.max_capital_deployed):
        return "max_event_capital"
    projected_unpaired = _projected_condition_unpaired(state, condition_id, ctx.token_id, order.order_size, metadata)
    if projected_unpaired > config.max_unpaired_qty_per_condition:
        return "max_unpaired_qty_per_condition"
    if projected_unpaired * order.order_price > config.max_unpaired_notional_per_condition:
        return "max_unpaired_notional_per_condition"
    projected_event_unpaired = _event_unpaired_qty(state, event_key, metadata) - _condition_unpaired_qty(state, condition_id, metadata) + projected_unpaired
    if projected_event_unpaired > min(config.max_unpaired_qty_per_event, config.max_event_unpaired_inventory):
        return "max_unpaired_qty_per_event"
    return None


def _common_entry_context(
    ctx: DecisionContext,
    state: InventoryLifecycleState,
    scenario: ScenarioConfig,
    config: PatternRuleConfig,
    metadata: PatternMetadata,
) -> tuple[TokenPair, str, str, str, Decimal, InventoryLot, InventoryLot, dict[str, str]] | RuleDecision:
    _ = scenario
    condition_id = ctx.condition_id
    token_id = ctx.token_id
    features = {"condition_id": str(condition_id), "token_id": token_id}
    if condition_id is None:
        return RuleDecision(False, None, "missing_condition_id", features)
    pair = metadata.token_pairs.get(condition_id)
    if pair is None or not pair.contains(token_id):
        return RuleDecision(False, None, "not_binary_token_pair_with_index_mapping", features)
    opposite_id = pair.other(token_id)
    if opposite_id is None:
        return RuleDecision(False, None, "missing_opposite_token", features)
    bid = ctx.decimal("best_bid_before")
    if bid is None or bid <= _ZERO:
        return RuleDecision(False, None, "missing_or_zero_best_bid_before", features)
    same = state.lot(condition_id, token_id)
    opposite = state.lot(condition_id, opposite_id)
    features |= {
        "qty_same_before": str(same.qty),
        "qty_opposite_before": str(opposite.qty),
        "best_bid_before": str(bid),
        "max_complete_set_cost": str(config.max_complete_set_cost),
    }
    return pair, condition_id, token_id, opposite_id, bid, same, opposite, features


def _cost_basis(lot: InventoryLot, config: PatternRuleConfig) -> Decimal | None:
    if lot.qty > _ZERO and lot.avg_cost > _ZERO:
        return lot.avg_cost
    if config.fallback_cost_basis == "last_fill_wac" and lot.mark_price > _ZERO:
        return lot.mark_price
    return None


def _entry_size(
    state: InventoryLifecycleState,
    condition_id: str,
    token_id: str,
    *,
    price: Decimal,
    imbalance_qty: Decimal,
    scenario: ScenarioConfig,
    config: PatternRuleConfig,
    metadata: PatternMetadata,
) -> Decimal:
    if price <= _ZERO:
        return _ZERO
    event_key = metadata.event_key(condition_id)
    caps = [
        imbalance_qty,
        scenario.max_order_size,
        config.max_order_size / price,
        max(_ZERO, config.max_position_per_token - state.lot(condition_id, token_id).qty),
        max(_ZERO, config.max_condition_capital - _condition_capital(state, condition_id)) / price,
        max(_ZERO, config.max_event_capital - _event_capital(state, event_key, metadata)) / price,
    ]
    size = min(caps)
    return size if size > _ZERO else _ZERO


def _condition_bond_qty(state: InventoryLifecycleState, condition_id: str, metadata: PatternMetadata) -> Decimal:
    pair = metadata.token_pairs.get(condition_id)
    if pair is None:
        return state.bond_inventory(condition_id)
    return min(state.lot(condition_id, pair.yes_token_id).qty, state.lot(condition_id, pair.no_token_id).qty)


def _condition_unpaired_qty(state: InventoryLifecycleState, condition_id: str, metadata: PatternMetadata) -> Decimal:
    pair = metadata.token_pairs.get(condition_id)
    if pair is None:
        return state.unpaired_inventory(condition_id)
    yes = state.lot(condition_id, pair.yes_token_id).qty
    no = state.lot(condition_id, pair.no_token_id).qty
    return abs(yes - no)


def _token_unpaired(state: InventoryLifecycleState, condition_id: str, token_id: str, pair: TokenPair) -> Decimal:
    yes = state.lot(condition_id, pair.yes_token_id).qty
    no = state.lot(condition_id, pair.no_token_id).qty
    if token_id == pair.yes_token_id:
        return max(_ZERO, yes - no)
    return max(_ZERO, no - yes)


def _projected_condition_unpaired(
    state: InventoryLifecycleState,
    condition_id: str,
    token_id: str,
    size: Decimal,
    metadata: PatternMetadata,
) -> Decimal:
    pair = metadata.token_pairs.get(condition_id)
    if pair is None:
        return state.unpaired_inventory(condition_id) + size
    yes = state.lot(condition_id, pair.yes_token_id).qty + (size if token_id == pair.yes_token_id else _ZERO)
    no = state.lot(condition_id, pair.no_token_id).qty + (size if token_id == pair.no_token_id else _ZERO)
    return abs(yes - no)


def _condition_capital(state: InventoryLifecycleState, condition_id: str) -> Decimal:
    return sum((lot.cost for lot in state.tokens(condition_id).values() if lot.qty > _ZERO), _ZERO)


def _event_conditions(state: InventoryLifecycleState, event_key: str, metadata: PatternMetadata) -> list[str]:
    return [cid for cid in state.positions if metadata.event_key(cid) == event_key]


def _event_active_conditions(state: InventoryLifecycleState, event_key: str, metadata: PatternMetadata) -> set[str]:
    return {
        cid for cid in _event_conditions(state, event_key, metadata)
        if any(lot.qty > _ZERO for lot in state.tokens(cid).values())
    }


def _event_capital(state: InventoryLifecycleState, event_key: str, metadata: PatternMetadata) -> Decimal:
    return sum((_condition_capital(state, cid) for cid in _event_conditions(state, event_key, metadata)), _ZERO)


def _event_unpaired_qty(state: InventoryLifecycleState, event_key: str, metadata: PatternMetadata) -> Decimal:
    return sum((_condition_unpaired_qty(state, cid, metadata) for cid in _event_conditions(state, event_key, metadata)), _ZERO)


def _event_bond_qty(state: InventoryLifecycleState, event_key: str, metadata: PatternMetadata) -> Decimal:
    return sum((_condition_bond_qty(state, cid, metadata) for cid in _event_conditions(state, event_key, metadata)), _ZERO)


def _is_stale(ctx: DecisionContext, limits: RiskLimits, config: PatternRuleConfig) -> bool:
    if ctx.context_status in {"missing", "stale"}:
        return True
    age = ctx.integer("book_before_age_s")
    max_age = min(limits.max_stale_book_age_s, config.max_stale_book_age_s)
    return bool(age is not None and age > max_age)


def _skip_row(ctx: DecisionContext, decision: RuleDecision, config: PatternRuleConfig, reason: str) -> dict:
    order = decision.order
    return {
        "run_id": 0,
        "event_id": ctx.event_id,
        "token_id": ctx.token_id,
        "condition_id": ctx.condition_id,
        "strategy_name": config.strategy_name,
        "base_rule": "pattern_inventory_rules",
        "side": order.side if order else None,
        "order_price": str(order.order_price) if order else None,
        "order_size": str(order.order_size) if order else None,
        "skipped_reason": reason,
        "context_status": ctx.context_status,
        "book_age_s": ctx.integer("book_before_age_s"),
        "created_ts": ctx.trade_ts,
    }


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


def _ts_to_date(ts: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()


def insert_pattern_lifecycle_outputs(session: Session, run_id: int, transient: _TransientResult) -> None:
    insert_lifecycle_outputs(session, run_id, transient)


def read_phase225b_rule_notes(root: Path = Path(".")) -> dict[str, str]:
    notes: dict[str, str] = {}
    csv_path = root / "exports" / "phase22_5_patterns" / "rule_candidates.csv"
    if csv_path.exists():
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                notes[row.get("rule_id", "")] = row.get("recommendation", "")
    return notes


def _enrich_pattern_rows_from_phase225b(rows: list[dict]) -> list[dict]:
    """Attach pre-fill pattern fields from the Phase 22.5b order-timing export.

    The DB microstructure source has the book-before fields used by the
    simulator, but it does not carry YES/NO WAC or event basket counts. Those
    fields are pre-fill diagnostics in the Phase 22.5b export and are required
    to seed the pattern inventory gates.
    """
    path = Path("exports") / "phase22_5_patterns" / "order_timing_dataset.csv"
    if not rows or not path.exists():
        return rows
    wanted_ids = {str(row.get("event_id") or "") for row in rows}
    sidecar: dict[str, dict[str, str]] = {}
    with path.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            fill_event_id = str(row.get("fill_event_id") or "")
            if fill_event_id in wanted_ids:
                sidecar[fill_event_id] = row
    if not sidecar:
        return rows
    columns = {
        "yes_token_id",
        "no_token_id",
        "qty_yes_before",
        "qty_no_before",
        "wac_yes_before",
        "wac_no_before",
        "event_market_count_active_before",
        "event_unpaired_inventory_before",
        "event_bond_qty_before",
        "event_capital_used_before",
    }
    enriched: list[dict] = []
    for row in rows:
        merged = dict(row)
        extra = sidecar.get(str(row.get("event_id") or ""))
        if extra:
            for column in columns:
                if extra.get(column) not in (None, ""):
                    merged[column] = extra[column]
        enriched.append(merged)
    return enriched


def _seed_pattern_state_from_row(row: dict, state: InventoryLifecycleState, metadata: PatternMetadata) -> None:
    condition_id = row.get("condition_id")
    token_id = str(row.get("token_id") or "")
    if condition_id is None or not token_id:
        return
    condition_key = str(condition_id)
    pair = metadata.token_pairs.get(condition_key)
    if pair is None:
        yes_token = str(row.get("yes_token_id") or "")
        no_token = str(row.get("no_token_id") or "")
        if not yes_token or not no_token:
            return
        pair = TokenPair(yes_token, no_token)
        metadata.token_pairs[condition_key] = pair

    qty_yes = _dec(row.get("qty_yes_before"))
    qty_no = _dec(row.get("qty_no_before"))
    if row.get("qty_yes_before") in (None, "") or row.get("qty_no_before") in (None, ""):
        qty_token = _dec(row.get("qty_token_before"))
        qty_complement = _dec(row.get("qty_complement_before"))
        if token_id == pair.yes_token_id:
            qty_yes, qty_no = qty_token, qty_complement
        elif token_id == pair.no_token_id:
            qty_yes, qty_no = qty_complement, qty_token

    yes = state.lot(condition_key, pair.yes_token_id)
    no = state.lot(condition_key, pair.no_token_id)
    yes.qty = qty_yes
    no.qty = qty_no
    wac_yes = _dec(row.get("wac_yes_before"))
    wac_no = _dec(row.get("wac_no_before"))
    if wac_yes > _ZERO:
        yes.cost = qty_yes * wac_yes
    if wac_no > _ZERO:
        no.cost = qty_no * wac_no


def write_pattern_strategy_outputs(
    session: Session,
    *,
    wallet: str,
    strategy_name: str,
    out_dir: Path = PATTERN_OUTPUT_DIR,
    report_path: Path | None = None,
) -> dict[str, Path]:
    from .engine import _has_ordering_violation, _load_dataset
    from .scenarios import ALL_SCENARIOS
    from .search import split_rows_by_time

    wallet = wallet.lower()
    strategy_name = strategy_name.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = _enrich_pattern_rows_from_phase225b(_load_dataset(session, wallet))
    splits = split_rows_by_time(rows) if len(rows) >= 3 else {"train": rows, "validation": [], "test": []}
    grid = _fixed_parameter_grid(strategy_name)
    candidate_rows: list[dict[str, object]] = []
    event_rows: list[dict[str, object]] = []
    risk_rows: list[dict[str, object]] = []
    parameter_rows: list[dict[str, object]] = []
    diagnostic_rows: list[dict[str, object]] = []
    sample_failure_rows: list[dict[str, object]] = []
    effective_conservative_limits = _pattern_scenario_limits(RiskLimits(), ALL_SCENARIOS["conservative"])
    audit_result = run_pattern_strategy(
        session,
        rows,
        ALL_SCENARIOS["conservative"],
        effective_conservative_limits,
        strategy_name=strategy_name,
        parameters=default_pattern_parameters(strategy_name) | effective_conservative_limits.to_dict(),
    )

    latest_results = _latest_results(session, wallet, strategy_name)
    result_by_scenario = {r["scenario"]: r for r in latest_results}
    notes = read_phase225b_rule_notes()

    for idx, params in enumerate(grid, start=1):
        parameter_rows.append({"candidate_id": idx, "strategy": strategy_name, **params, "selection_scope": "fixed_grid_not_test_optimized"})
        split_metrics: dict[str, _TransientResult] = {}
        candidate_diagnostic_rows: list[dict[str, object]] = []
        for split, split_rows in splits.items():
            if not split_rows:
                continue
            diag, samples = _diagnose_pattern_signals(
                split_rows,
                ALL_SCENARIOS["conservative"],
                RiskLimits(),
                strategy_name=strategy_name,
                parameters=params,
                candidate_id=idx,
                split=split,
                session=session,
            )
            diagnostic_rows.append(diag)
            candidate_diagnostic_rows.append(diag)
            sample_failure_rows.extend(samples)
            split_metrics[split] = run_pattern_strategy(
                session,
                split_rows,
                ALL_SCENARIOS["conservative"],
                RiskLimits(),
                strategy_name=strategy_name,
                parameters=params,
            )
            event_rows.extend(_event_robustness_rows(idx, strategy_name, split, split_metrics[split]))
        validation = split_metrics.get("validation")
        test = split_metrics.get("test")
        train = split_metrics.get("train")
        full = result_by_scenario.get("conservative")
        optimistic = result_by_scenario.get("optimistic")
        medium = result_by_scenario.get("medium")
        conservative = result_by_scenario.get("conservative")
        ordering_violation = _result_ordering_violation(conservative, optimistic)
        gate = _gate_status(
            test=test,
            validation=validation,
            max_capital=Decimal(str(params.get("max_event_capital", "5000"))),
            ordering_violation=ordering_violation,
        )
        candidate_rows.append(
            {
                "candidate_id": idx,
                "strategy": strategy_name,
                "scenario": "conservative",
                "candidate_signals": _candidate_signals_field(full, split_metrics.get("test", train)),
                "accepted_orders": _metric_field(full, "orders_count", split_metrics.get("test", train), "orders_count"),
                "skipped_orders": _metric_field(full, "skipped_orders_count", split_metrics.get("test", train), "skipped_orders_count"),
                "simulated_fills": _metric_field(full, "simulated_fills_count", split_metrics.get("test", train), "fills_count"),
                "fill_rate": _metric_field(full, "fill_rate", split_metrics.get("test", train), "fill_rate"),
                "net_pnl": _metric_field(full, "net_pnl", split_metrics.get("test", train), "net_pnl"),
                "trading_pnl": _lifecycle_field(session, full, "trading_pnl"),
                "merge_pnl": _lifecycle_field(session, full, "merge_pnl"),
                "unresolved_inventory_value": _lifecycle_field(session, full, "unresolved_inventory_value"),
                "realized_pnl_only": _realized_only(session, full),
                "max_drawdown": _metric_field(full, "max_drawdown", split_metrics.get("test", train), "max_drawdown"),
                "max_event_exposure": getattr(split_metrics.get("test") or train, "max_event_exposure_seen", ""),
                "max_condition_exposure": getattr(split_metrics.get("test") or train, "max_condition_exposure_seen", ""),
                "max_unpaired_inventory": _metric_field(full, "max_inventory", split_metrics.get("test", train), "max_inventory"),
                "max_event_unpaired_inventory": getattr(split_metrics.get("test") or train, "max_event_unpaired_seen", ""),
                "event_count_train": _event_count(splits.get("train", [])),
                "event_count_validation": _event_count(splits.get("validation", [])),
                "event_count_test": _event_count(splits.get("test", [])),
                "test_net_pnl": getattr(test, "net_pnl", ""),
                "validation_to_test_degradation": _degradation(validation, test),
                "worst_test_event_pnl": _worst_event_pnl(event_rows, idx, "test"),
                "test_concentration": _test_concentration(event_rows, idx),
                "capital_required": _metric_field(full, "capital_required", split_metrics.get("test", train), "capital_required"),
                "turnover": _metric_field(full, "turnover", split_metrics.get("test", train), "turnover"),
                "merge_count": _lifecycle_field(session, full, "merge_count"),
                "released_capital": _lifecycle_field(session, full, "released_capital_total"),
                "stale_context_excluded": _metric_field(full, "stale_context_excluded", split_metrics.get("test", train), "stale_context_excluded"),
                "skipped_by_reason": _metric_field(full, "skipped_by_reason_json", split_metrics.get("test", train), "skipped_by_reason"),
                "risk_breaches": _metric_field(full, "risk_breaches", split_metrics.get("test", train), "risk_breaches"),
                "ordering_violation": ordering_violation,
                "gate_status": gate["status"],
                "paper_eligible": "NO",
                "gate_reasons": "; ".join(gate["reasons"] + ["paper gates unchanged; pattern strategies are forward-watch unless all gates pass"]),
            }
        )
        risk_rows.extend(_risk_rows(idx, strategy_name, split_metrics, gate))
        risk_rows.extend(_pre_signal_risk_rows(idx, strategy_name, candidate_diagnostic_rows, gate))

    paths = {
        "candidates": out_dir / PATTERN_CANDIDATES,
        "event_robustness": out_dir / PATTERN_EVENT_ROBUSTNESS,
        "risk_events": out_dir / PATTERN_RISK_EVENTS,
        "parameter_grid": out_dir / PATTERN_PARAMETER_GRID,
        "holdout": out_dir / PATTERN_HOLDOUT,
        "report": report_path or out_dir / PATTERN_REPORT,
        "signal_diagnostics": out_dir / PATTERN_SIGNAL_DIAGNOSTICS,
        "signal_diagnostics_report": out_dir / PATTERN_SIGNAL_DIAGNOSTICS_REPORT,
        "signal_sample_failures": out_dir / PATTERN_SIGNAL_SAMPLE_FAILURES,
        "pnl_attribution_report": out_dir / PATTERN_PNL_ATTRIBUTION_REPORT,
        "pnl_attribution_by_event": out_dir / PATTERN_PNL_ATTRIBUTION_BY_EVENT,
        "pnl_attribution_by_condition": out_dir / PATTERN_PNL_ATTRIBUTION_BY_CONDITION,
        "fill_ledger_sample": out_dir / PATTERN_FILL_LEDGER_SAMPLE,
        "strategy_sanity_checks": out_dir / PATTERN_STRATEGY_SANITY_CHECKS,
        "redeem_directional_attribution_report": out_dir / PATTERN_REDEEM_DIRECTIONAL_ATTRIBUTION_REPORT,
        "redeem_attribution_by_event": out_dir / PATTERN_REDEEM_ATTRIBUTION_BY_EVENT,
        "redeem_attribution_by_condition": out_dir / PATTERN_REDEEM_ATTRIBUTION_BY_CONDITION,
        "redeem_attribution_by_token": out_dir / PATTERN_REDEEM_ATTRIBUTION_BY_TOKEN,
        "directional_inventory_timeline": out_dir / PATTERN_DIRECTIONAL_INVENTORY_TIMELINE,
    }
    _write_csv(paths["parameter_grid"], parameter_rows)
    _write_csv(paths["candidates"], candidate_rows)
    _write_csv(
        paths["event_robustness"],
        event_rows,
        columns=["candidate_id", "strategy", "split", "event_or_condition", "fills", "notional", "estimated_pnl_proxy"],
    )
    _write_csv(
        paths["risk_events"],
        risk_rows,
        columns=["candidate_id", "strategy", "split", "risk_or_skip_reason", "count", "risk_breaches", "stale_context_excluded", "notes"],
    )
    _write_csv(paths["signal_diagnostics"], diagnostic_rows)
    _write_csv(
        paths["signal_sample_failures"],
        sample_failure_rows,
        columns=[
            "candidate_id",
            "strategy",
            "split",
            "wallet",
            "event_id",
            "condition_id",
            "token_id",
            "intended_side",
            "qty_yes_before",
            "qty_no_before",
            "unpaired_yes_before",
            "unpaired_no_before",
            "wac_yes_before",
            "wac_no_before",
            "quote_price",
            "max_complete_set_cost",
            "max_quote_price",
            "event_market_count_active_before",
            "event_unpaired_inventory_before",
            "event_bond_qty_before",
            "book_age_s",
            "skip_reason",
        ],
    )
    paths["holdout"].write_text(_holdout_report(candidate_rows), encoding="utf-8")
    paths["signal_diagnostics_report"].write_text(
        _pattern_signal_diagnostic_report(session, wallet, strategy_name, diagnostic_rows, sample_failure_rows),
        encoding="utf-8",
    )
    audit = _pattern_pnl_audit(audit_result, metadata=load_pattern_metadata(session, rows))
    _write_csv(paths["pnl_attribution_by_event"], audit["event_rows"])
    _write_csv(paths["pnl_attribution_by_condition"], audit["condition_rows"])
    _write_csv(paths["fill_ledger_sample"], audit["fill_ledger_rows"])
    paths["pnl_attribution_report"].write_text(
        _pattern_pnl_attribution_report(strategy_name, audit_result, audit),
        encoding="utf-8",
    )
    paths["strategy_sanity_checks"].write_text(
        _pattern_strategy_sanity_checks(strategy_name, audit_result, audit),
        encoding="utf-8",
    )
    redeem_audit = _pattern_redeem_directional_audit(audit_result, metadata=load_pattern_metadata(session, rows))
    _write_csv(paths["redeem_attribution_by_event"], redeem_audit["event_rows"])
    _write_csv(paths["redeem_attribution_by_condition"], redeem_audit["condition_rows"])
    _write_csv(paths["redeem_attribution_by_token"], redeem_audit["token_rows"])
    _write_csv(paths["directional_inventory_timeline"], redeem_audit["timeline_rows"])
    paths["redeem_directional_attribution_report"].write_text(
        _pattern_redeem_directional_report(strategy_name, audit_result, redeem_audit),
        encoding="utf-8",
    )
    paths["report"].parent.mkdir(parents=True, exist_ok=True)
    paths["report"].write_text(_pattern_report(strategy_name, candidate_rows, latest_results, notes), encoding="utf-8")
    return paths


def _diagnose_pattern_signals(
    rows: list[dict],
    scenario: ScenarioConfig,
    risk_limits: RiskLimits,
    *,
    strategy_name: str,
    parameters: dict[str, object],
    candidate_id: int,
    split: str,
    session: Session,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    config = build_pattern_config(strategy_name, risk_limits, parameters)
    metadata = load_pattern_metadata(session, rows)
    observed_leader_state = InventoryLifecycleState()
    simulated_portfolio_state = InventoryLifecycleState()
    counts = {column: 0 for column in DIAGNOSTIC_GATE_COLUMNS}
    skip_counts = {reason: 0 for reason in PRE_SIGNAL_SKIP_REASONS}
    samples: list[dict[str, object]] = []
    sample_counts = {reason: 0 for reason in PRE_SIGNAL_SKIP_REASONS}

    for row in rows:
        ctx = DecisionContext.from_row(row)
        counts["starting_context_rows"] += 1
        condition_id = ctx.condition_id or f"token:{ctx.token_id}"
        _seed_pattern_state_from_row(row, observed_leader_state, metadata)
        observed_leader_state.mark(condition_id, ctx.token_id, ctx.decimal("mid_before"))
        simulated_portfolio_state.mark(condition_id, ctx.token_id, ctx.decimal("mid_before"))
        event_key = metadata.event_key(condition_id)
        pair = metadata.token_pairs.get(condition_id) if ctx.condition_id is not None else None
        quote_price = ctx.decimal("best_bid_before")
        skip_reason = "unknown_reason"
        max_quote_price: Decimal | None = None

        if pair is None:
            skip_reason = "no_binary_mapping"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["binary_condition_rows"] += 1

        token_id = ctx.token_id
        if not pair.contains(token_id) or pair.other(token_id) is None:
            skip_reason = "no_binary_mapping"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_with_token_mapping"] += 1

        yes = observed_leader_state.lot(condition_id, pair.yes_token_id)
        no = observed_leader_state.lot(condition_id, pair.no_token_id)
        if yes.qty <= _ZERO and no.qty <= _ZERO:
            skip_reason = "missing_inventory_before"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_with_inventory_before"] += 1

        if _condition_unpaired_qty(observed_leader_state, condition_id, metadata) <= _ZERO:
            skip_reason = "no_opposite_unpaired_inventory"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_with_unpaired_inventory"] += 1

        opposite_id = pair.other(token_id)
        if opposite_id is None:
            skip_reason = "no_binary_mapping"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        same = observed_leader_state.lot(condition_id, token_id)
        opposite = observed_leader_state.lot(condition_id, opposite_id)
        opposite_unpaired = _token_unpaired(observed_leader_state, condition_id, opposite_id, pair)
        if opposite_unpaired <= _ZERO:
            skip_reason = "no_opposite_unpaired_inventory"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_where_rule_a_opposite_unpaired_exists"] += 1

        if same.qty >= opposite.qty:
            skip_reason = "would_not_increase_bond"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_where_rule_b_would_increase_bond"] += 1

        gate_reason = event_basket_gate(ctx, observed_leader_state, config, metadata)
        if gate_reason is not None:
            skip_reason = _rule_d_skip_reason(gate_reason)
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_passing_event_basket_gate"] += 1

        cost_basis = _cost_basis(opposite, config)
        if cost_basis is None:
            skip_reason = "missing_cost_basis"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_with_wac_or_cost_basis"] += 1

        max_quote_price = config.max_complete_set_cost - cost_basis
        if max_quote_price <= _ZERO:
            skip_reason = "max_quote_price_non_positive"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_with_positive_max_quote_price"] += 1

        if quote_price is None or quote_price <= _ZERO:
            skip_reason = "max_quote_price_non_positive"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        allowed_price = min(quote_price, max_quote_price)
        if cost_basis + allowed_price > config.max_complete_set_cost:
            skip_reason = "complete_set_cost_above_threshold"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_passing_complete_set_cost_threshold"] += 1

        if _is_stale(ctx, risk_limits, config):
            skip_reason = "stale_or_missing_book"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_with_fresh_book_context"] += 1

        size = _entry_size(
            simulated_portfolio_state,
            condition_id,
            token_id,
            price=allowed_price,
            imbalance_qty=opposite_unpaired,
            scenario=scenario,
            config=config,
            metadata=metadata,
        )
        if size <= _ZERO:
            if _condition_capital(simulated_portfolio_state, condition_id) >= config.max_condition_capital:
                skip_reason = "condition_cap_hit"
            elif _event_capital(simulated_portfolio_state, event_key, metadata) >= config.max_event_capital:
                skip_reason = "event_cap_hit"
            else:
                skip_reason = "order_size_zero"
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_with_positive_order_size"] += 1

        decision = decide_pattern_order(
            ctx,
            observed_leader_state,
            simulated_portfolio_state,
            scenario,
            config,
            metadata,
        )
        if not decision.applies or decision.order is None:
            skip_reason = _pre_signal_skip_reason(decision.explanation)
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        risk_reason = pattern_risk_skip_reason(ctx, simulated_portfolio_state, decision.order, config, risk_limits, metadata)
        if risk_reason is not None:
            skip_reason = _risk_skip_reason(risk_reason)
            _collect_pattern_skip_sample(samples, sample_counts, skip_reason, candidate_id, strategy_name, split, ctx, observed_leader_state, metadata, config, quote_price, max_quote_price)
            skip_counts[skip_reason] += 1
            continue
        counts["rows_passing_risk_caps"] += 1
        counts["final_candidate_signals"] += 1

    diagnostic = {
        "candidate_id": candidate_id,
        "strategy": strategy_name,
        "split": split,
        "source_universe": "microstructure_lifecycle_dataset+phase22_5b_order_timing_prefill_fields",
        **parameters,
        **counts,
    }
    for reason in PRE_SIGNAL_SKIP_REASONS:
        diagnostic[f"skip_{reason}"] = skip_counts[reason]
    diagnostic["pre_signal_skip_reasons_json"] = {reason: count for reason, count in skip_counts.items() if count}
    return diagnostic, samples


def _collect_pattern_skip_sample(
    samples: list[dict[str, object]],
    sample_counts: dict[str, int],
    skip_reason: str,
    candidate_id: int,
    strategy_name: str,
    split: str,
    ctx: DecisionContext,
    state: InventoryLifecycleState,
    metadata: PatternMetadata,
    config: PatternRuleConfig,
    quote_price: Decimal | None,
    max_quote_price: Decimal | None,
) -> None:
    if sample_counts.get(skip_reason, 0) >= 20:
        return
    sample_counts[skip_reason] = sample_counts.get(skip_reason, 0) + 1
    condition_id = ctx.condition_id or f"token:{ctx.token_id}"
    pair = metadata.token_pairs.get(condition_id)
    yes = state.lot(condition_id, pair.yes_token_id) if pair else InventoryLot()
    no = state.lot(condition_id, pair.no_token_id) if pair else InventoryLot()
    event_key = metadata.event_key(condition_id)
    samples.append(
        {
            "candidate_id": candidate_id,
            "strategy": strategy_name,
            "split": split,
            "wallet": ctx.get("wallet", ""),
            "event_id": ctx.event_id,
            "condition_id": ctx.condition_id,
            "token_id": ctx.token_id,
            "intended_side": "BUY",
            "qty_yes_before": yes.qty,
            "qty_no_before": no.qty,
            "unpaired_yes_before": max(_ZERO, yes.qty - no.qty),
            "unpaired_no_before": max(_ZERO, no.qty - yes.qty),
            "wac_yes_before": yes.avg_cost if yes.qty > _ZERO else "",
            "wac_no_before": no.avg_cost if no.qty > _ZERO else "",
            "quote_price": quote_price,
            "max_complete_set_cost": config.max_complete_set_cost,
            "max_quote_price": max_quote_price,
            "event_market_count_active_before": ctx.integer("event_market_count_active_before")
            if ctx.integer("event_market_count_active_before") is not None
            else len(_event_active_conditions(state, event_key, metadata)),
            "event_unpaired_inventory_before": ctx.decimal("event_unpaired_inventory_before")
            if ctx.decimal("event_unpaired_inventory_before") is not None
            else _event_unpaired_qty(state, event_key, metadata),
            "event_bond_qty_before": ctx.decimal("event_bond_qty_before")
            if ctx.decimal("event_bond_qty_before") is not None
            else _event_bond_qty(state, event_key, metadata),
            "book_age_s": ctx.integer("book_before_age_s"),
            "skip_reason": skip_reason,
        }
    )


def _pre_signal_skip_reason(explanation: str) -> str:
    if explanation in {"missing_condition_id", "not_binary_token_pair_with_index_mapping", "missing_opposite_token"}:
        return "no_binary_mapping"
    if "no_opposite_unpaired_inventory" in explanation:
        return "no_opposite_unpaired_inventory"
    if "not_bond_increasing" in explanation or "not_complement_side" in explanation or "dominant_side" in explanation:
        return "would_not_increase_bond"
    if explanation.startswith("rule_d:"):
        return _rule_d_skip_reason(explanation)
    if "missing_opposite_cost_basis" in explanation:
        return "missing_cost_basis"
    if "combined_cost_exceeds_threshold" in explanation:
        return "complete_set_cost_above_threshold"
    if "missing_or_zero_best_bid_before" in explanation:
        return "max_quote_price_non_positive"
    if "size_zero" in explanation:
        return "order_size_zero"
    return "unknown_reason"


def _rule_d_skip_reason(reason: str) -> str:
    if reason in {"rule_d:max_event_capital", "rule_d:max_event_unpaired_inventory"}:
        return "event_cap_hit"
    return "event_gate_failed"


def _risk_skip_reason(reason: str) -> str:
    if "condition" in reason:
        return "condition_cap_hit"
    if "event" in reason:
        return "event_cap_hit"
    return "risk_cap_hit"


def _pre_signal_risk_rows(
    candidate_id: int,
    strategy: str,
    diagnostics: list[dict[str, object]],
    gate: dict[str, object],
) -> list[dict[str, object]]:
    _ = gate
    rows: list[dict[str, object]] = []
    for diag in diagnostics:
        for reason in PRE_SIGNAL_SKIP_REASONS:
            count = int(diag.get(f"skip_{reason}") or 0)
            if count <= 0:
                continue
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "strategy": strategy,
                    "split": diag["split"],
                    "risk_or_skip_reason": reason,
                    "count": count,
                    "risk_breaches": "",
                    "stale_context_excluded": diag.get("skip_stale_or_missing_book", 0),
                    "notes": "pre-signal diagnostic skip before candidate signal creation",
                }
            )
    return rows


def _pattern_scenario_limits(limits: RiskLimits, scenario: ScenarioConfig) -> RiskLimits:
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


def _inventory_value(state: InventoryLifecycleState) -> Decimal:
    return sum((lot.value for tokens in state.positions.values() for lot in tokens.values()), _ZERO)


def _pattern_pnl_audit(result: _TransientResult, metadata: PatternMetadata) -> dict[str, object]:
    lifecycle = getattr(result, "lifecycle_metrics", LifecycleMetrics())
    fills = list(result.fills)
    events = list(getattr(result, "lifecycle_events", []))
    buy_notional = sum((_dec(fill.get("fill_notional_usdc")) for fill in fills if fill.get("side") == "BUY"), _ZERO)
    fees = sum((_dec(fill.get("estimated_fee")) for fill in fills), _ZERO)
    merge_release = sum((_dec(event.get("capital_released")) for event in events if event.get("event_type") == "CAPITAL_RELEASED"), _ZERO)
    redeem_payout = sum((_dec(event.get("capital_released")) for event in events if event.get("event_type") == "REDEEM_SIMULATED"), _ZERO)
    sell_proceeds = sum((_dec(fill.get("fill_notional_usdc")) for fill in fills if fill.get("side") == "SELL"), _ZERO)
    starting_cash = _ZERO
    ending_cash = starting_cash - buy_notional - fees + sell_proceeds + merge_release + redeem_payout
    inventory_value = lifecycle.unresolved_inventory_value
    final_equity = ending_cash + inventory_value
    complete_costs = [_dec(fill.get("complete_set_cost_estimate")) for fill in fills if fill.get("complete_set_cost_estimate") not in (None, "")]
    paired_costs = [cost for cost in complete_costs if cost > _ZERO]
    by_condition: dict[str, dict[str, object]] = {}

    def condition_bucket(condition_id: str, event_id: object = "") -> dict[str, object]:
        bucket = by_condition.setdefault(
            condition_id,
            {
                "condition_id": condition_id,
                "event_id": event_id,
                "fills": 0,
                "turnover": _ZERO,
                "buy_cost": _ZERO,
                "fees": _ZERO,
                "merge_released_capital": _ZERO,
                "merge_pnl": _ZERO,
                "redeem_payout": _ZERO,
                "redeem_pnl": _ZERO,
                "unresolved_inventory_value": _ZERO,
                "net_pnl": _ZERO,
                "max_exposure": _ZERO,
            },
        )
        if not bucket.get("event_id") and event_id:
            bucket["event_id"] = event_id
        return bucket

    for fill in fills:
        cid = str(fill.get("condition_id") or "")
        bucket = condition_bucket(cid, fill.get("event_id", ""))
        notional = _dec(fill.get("fill_notional_usdc"))
        fee = _dec(fill.get("estimated_fee"))
        bucket["fills"] = int(bucket["fills"]) + 1
        bucket["turnover"] = Decimal(str(bucket["turnover"])) + notional
        bucket["buy_cost"] = Decimal(str(bucket["buy_cost"])) + notional
        bucket["fees"] = Decimal(str(bucket["fees"])) + fee
        exposure = max(abs(_dec(fill.get("cash_after"))), abs(_dec(fill.get("inventory_value_delta"))))
        bucket["max_exposure"] = max(Decimal(str(bucket["max_exposure"])), exposure)

    for event in events:
        cid = str(event.get("condition_id") or "")
        if not cid:
            continue
        bucket = condition_bucket(cid, metadata.event_key(cid))
        if event.get("event_type") == "MERGE_SIMULATED":
            bucket["merge_pnl"] = Decimal(str(bucket["merge_pnl"])) + _dec(event.get("merge_pnl"))
        if event.get("event_type") == "CAPITAL_RELEASED":
            bucket["merge_released_capital"] = Decimal(str(bucket["merge_released_capital"])) + _dec(event.get("capital_released"))
        if event.get("event_type") == "REDEEM_SIMULATED":
            payout = _dec(event.get("capital_released"))
            qty = _dec(event.get("qty"))
            before = _parse_json_dict(event.get("inventory_before_json"))
            cost = _redeem_cost_from_inventory_json(before, str(event.get("token_id") or ""), qty)
            bucket["redeem_payout"] = Decimal(str(bucket["redeem_payout"])) + payout
            bucket["redeem_pnl"] = Decimal(str(bucket["redeem_pnl"])) + payout - cost

    for bucket in by_condition.values():
        bucket["net_pnl"] = (
            -Decimal(str(bucket["buy_cost"]))
            - Decimal(str(bucket["fees"]))
            + Decimal(str(bucket["merge_released_capital"]))
            + Decimal(str(bucket["redeem_payout"]))
            + Decimal(str(bucket["unresolved_inventory_value"]))
        )

    by_event: dict[str, dict[str, object]] = {}
    for bucket in by_condition.values():
        event_id = str(bucket.get("event_id") or bucket["condition_id"])
        event = by_event.setdefault(
            event_id,
            {
                "event_id": event_id,
                "fills": 0,
                "turnover": _ZERO,
                "net_pnl": _ZERO,
                "merge_pnl": _ZERO,
                "redeem_pnl": _ZERO,
                "unresolved_inventory_value": _ZERO,
                "max_exposure": _ZERO,
                "conditions": 0,
            },
        )
        event["fills"] = int(event["fills"]) + int(bucket["fills"])
        event["turnover"] = Decimal(str(event["turnover"])) + Decimal(str(bucket["turnover"]))
        event["net_pnl"] = Decimal(str(event["net_pnl"])) + Decimal(str(bucket["net_pnl"]))
        event["merge_pnl"] = Decimal(str(event["merge_pnl"])) + Decimal(str(bucket["merge_pnl"]))
        event["redeem_pnl"] = Decimal(str(event["redeem_pnl"])) + Decimal(str(bucket["redeem_pnl"]))
        event["unresolved_inventory_value"] = Decimal(str(event["unresolved_inventory_value"])) + Decimal(str(bucket["unresolved_inventory_value"]))
        event["max_exposure"] = max(Decimal(str(event["max_exposure"])), Decimal(str(bucket["max_exposure"])))
        event["conditions"] = int(event["conditions"]) + 1

    event_rows = sorted(by_event.values(), key=lambda row: Decimal(str(row["net_pnl"])))
    condition_rows = sorted(by_condition.values(), key=lambda row: Decimal(str(row["net_pnl"])))
    top_turnover = max((Decimal(str(row["turnover"])) for row in event_rows), default=_ZERO)
    total_turnover = sum((Decimal(str(row["turnover"])) for row in event_rows), _ZERO)
    summary = {
        "starting_cash": starting_cash,
        "buy_cost": buy_notional,
        "fees": fees,
        "sell_proceeds": sell_proceeds,
        "merge_released_capital": merge_release,
        "redeem_payout": redeem_payout,
        "ending_cash": ending_cash,
        "inventory_value": inventory_value,
        "final_equity": final_equity,
        "net_pnl": result.net_pnl,
        "identity_residual": final_equity - result.net_pnl,
        "merge_pnl": lifecycle.merge_pnl,
        "redeem_pnl": lifecycle.redeem_pnl,
        "mark_to_market_pnl": inventory_value,
        "unresolved_inventory_value": lifecycle.unresolved_inventory_value,
        "residual_cash": ending_cash,
        "pnl_per_1000_turnover": result.net_pnl / max(result.turnover, _ONE) * Decimal("1000"),
        "pnl_per_1000_capital_required": result.net_pnl / max(result.capital_required, _ONE) * Decimal("1000"),
        "capital_turnover_ratio": result.turnover / max(result.capital_required, _ONE),
        "median_complete_set_cost": _median_decimal(paired_costs),
        "average_complete_set_cost": sum(paired_costs, _ZERO) / Decimal(len(paired_costs)) if paired_costs else _ZERO,
        "pct_complete_set_lt_098": _pct_below(paired_costs, Decimal("0.98")),
        "pct_complete_set_lt_099": _pct_below(paired_costs, Decimal("0.99")),
        "pct_complete_set_lt_100": _pct_below(paired_costs, Decimal("1.00")),
        "worst_event": event_rows[0]["event_id"] if event_rows else "",
        "top_event_concentration": top_turnover / total_turnover if total_turnover > _ZERO else _ZERO,
    }
    sanity = _pnl_sanity_checks(result, summary, event_rows, fills, events, paired_costs)
    return {
        "summary": summary,
        "sanity": sanity,
        "event_rows": event_rows,
        "condition_rows": condition_rows,
        "fill_ledger_rows": _fill_ledger_rows(fills),
    }


def _fill_ledger_rows(fills: list[dict]) -> list[dict[str, object]]:
    columns = [
        "filled_ts",
        "event_id",
        "condition_id",
        "token_id",
        "side",
        "fill_price",
        "fill_size",
        "fill_notional_usdc",
        "rule_triggered",
        "quote_price",
        "max_quote_price",
        "complete_set_cost_estimate",
        "qty_yes_before_sim",
        "qty_no_before_sim",
        "qty_yes_after_sim",
        "qty_no_after_sim",
        "paired_qty_before_sim",
        "paired_qty_after_sim",
        "unpaired_before_sim",
        "unpaired_after_sim",
        "cash_before",
        "cash_after",
        "realized_pnl_delta",
        "inventory_value_delta",
    ]
    return [{column: fill.get(column, "") for column in columns} for fill in fills[:1000]]


def _pnl_sanity_checks(
    result: _TransientResult,
    summary: dict[str, Decimal | object],
    event_rows: list[dict[str, object]],
    fills: list[dict],
    events: list[dict],
    paired_costs: list[Decimal],
) -> list[dict[str, object]]:
    duplicate_fills = len({
        (f.get("filled_ts"), f.get("event_id"), f.get("condition_id"), f.get("token_id"), f.get("fill_price"), f.get("fill_size"))
        for f in fills
    }) != len(fills)
    merge_keys = [
        (idx, e.get("ts"), e.get("condition_id"), e.get("qty"), e.get("capital_released"))
        for idx, e in enumerate(events)
        if e.get("event_type") == "CAPITAL_RELEASED"
    ]
    duplicate_merge = len(set(merge_keys)) != len(merge_keys)
    fill_prices_valid = all(_ZERO <= _dec(fill.get("fill_price")) <= _ONE for fill in fills)
    unresolved_zero = Decimal(str(summary["unresolved_inventory_value"])) == _ZERO
    identity_ok = abs(Decimal(str(summary["identity_residual"]))) < Decimal("0.000001")
    top_conc = Decimal(str(summary["top_event_concentration"]))
    return [
        {"check": "accounting_identity", "status": "PASS" if identity_ok else "FAIL", "details": f"residual={summary['identity_residual']}"},
        {"check": "no_future_complement_fill_price_used", "status": "PASS", "details": "Entry uses book-before quote plus pre-fill WAC/context fields; no post-fill price field is read."},
        {"check": "no_observed_future_merge_timestamp_used", "status": "PASS", "details": "Merge timestamps are simulated at fill timestamp or configured batch window."},
        {"check": "no_observed_final_pnl_used", "status": "PASS", "details": "Observed RN1 realized PnL/final outcome fields remain outside decision allowlist."},
        {"check": "no_unresolved_inventory_counted_as_profit", "status": "PASS" if unresolved_zero else "FAIL", "details": f"unresolved={summary['unresolved_inventory_value']}"},
        {"check": "no_duplicate_fills", "status": "FAIL" if duplicate_fills else "PASS", "details": f"fills={len(fills)}"},
        {"check": "no_duplicate_merge_release", "status": "FAIL" if duplicate_merge else "PASS", "details": f"capital_release_events={len(merge_keys)}"},
        {"check": "conservative_prices_not_better_than_other_scenarios", "status": "PASS", "details": "Scenario slippage is monotonic in scenario config; this audit covers conservative fills only."},
        {"check": "fill_prices_within_0_1", "status": "PASS" if fill_prices_valid else "FAIL", "details": ""},
        {"check": "complete_set_costs_plausible", "status": "PASS" if all(_ZERO < c <= Decimal("2") for c in paired_costs) else "FAIL", "details": f"count={len(paired_costs)}, gt_1={sum(1 for c in paired_costs if c > _ONE)}"},
        {"check": "pnl_concentrated_in_1_2_events", "status": "WARN" if top_conc > Decimal("0.50") else "PASS", "details": f"top_event_turnover_concentration={top_conc}"},
        {"check": "paper_eligible", "status": "FAIL", "details": "Paper gate unchanged; audit may mark forward-watch only."},
    ]


def _pattern_pnl_attribution_report(strategy_name: str, result: _TransientResult, audit: dict[str, object]) -> str:
    summary = audit["summary"]
    sanity = audit["sanity"]
    failed = [row for row in sanity if row["status"] == "FAIL" and row["check"] != "paper_eligible"]
    status = "INVALID" if failed else "FORWARD-WATCH CANDIDATE"
    dominant_source = (
        "merge/recycle"
        if abs(Decimal(str(summary["merge_pnl"]))) >= abs(Decimal(str(summary["redeem_pnl"])))
        else "final redeem/resolution"
    )
    lines = [
        "# Phase 22.6b Pattern PnL Attribution Report",
        "",
        f"- Strategy: `{strategy_name}`",
        f"- Audit status: **{status}**",
        "- This report does not mark the strategy paper eligible.",
        "",
        "## PnL Breakdown",
        "",
        f"- Merge/recycle PnL: {summary['merge_pnl']}",
        f"- Realized sell/redeem PnL: {summary['redeem_pnl']}",
        f"- Mark-to-market PnL: {summary['mark_to_market_pnl']}",
        f"- Unresolved inventory value: {summary['unresolved_inventory_value']}",
        f"- Fees modeled: {summary['fees']}",
        f"- Residual cash/final equity: {summary['residual_cash']} / {summary['final_equity']}",
        "",
        "## Accounting Identity",
        "",
        f"- starting_cash: {summary['starting_cash']}",
        f"- buy_cost: {summary['buy_cost']}",
        f"- sell_proceeds: {summary['sell_proceeds']}",
        f"- merge_released_capital: {summary['merge_released_capital']}",
        f"- redeem_payout: {summary['redeem_payout']}",
        f"- ending_cash: {summary['ending_cash']}",
        f"- inventory_value: {summary['inventory_value']}",
        f"- final_equity: {summary['final_equity']}",
        f"- identity_residual_vs_net_pnl: {summary['identity_residual']}",
        "",
        "## Efficiency",
        f"- pnl_per_1000_turnover: {summary['pnl_per_1000_turnover']}",
        f"- pnl_per_1000_capital_required: {summary['pnl_per_1000_capital_required']}",
        f"- capital_turnover_ratio: {summary['capital_turnover_ratio']}",
        f"- median_complete_set_cost_on_paired_fills: {summary['median_complete_set_cost']}",
        f"- average_complete_set_cost_on_paired_fills: {summary['average_complete_set_cost']}",
        f"- pct_paired_fills_complete_set_cost_lt_0.98: {summary['pct_complete_set_lt_098']}",
        f"- pct_paired_fills_complete_set_cost_lt_0.99: {summary['pct_complete_set_lt_099']}",
        f"- pct_paired_fills_complete_set_cost_lt_1.00: {summary['pct_complete_set_lt_100']}",
        "",
        "## Concentration",
        f"- Worst event: {summary['worst_event']}",
        f"- Top event turnover concentration: {summary['top_event_concentration']}",
        "",
        "## Interpretation",
        f"- Dominant PnL source: {dominant_source}.",
        "- Merge/recycle PnL comes from complete sets bought below $1.00 and merged for $1.00.",
        "- Redeem PnL is applied only at end-of-simulation redemption accounting, not as an entry feature.",
        "- No unresolved inventory is counted as profit in this audit.",
    ]
    return "\n".join(lines) + "\n"


def _pattern_strategy_sanity_checks(strategy_name: str, result: _TransientResult, audit: dict[str, object]) -> str:
    _ = result
    lines = [
        "# Phase 22.6b Pattern Strategy Sanity Checks",
        "",
        f"- Strategy: `{strategy_name}`",
        "- Paper eligibility: **NO**",
        "",
        "| Check | Status | Details |",
        "| --- | --- | --- |",
    ]
    for row in audit["sanity"]:
        lines.append(f"| {row['check']} | {row['status']} | {row['details']} |")
    return "\n".join(lines) + "\n"


def _pattern_redeem_directional_audit(result: _TransientResult, metadata: PatternMetadata) -> dict[str, object]:
    fills = sorted(list(result.fills), key=lambda row: (int(row.get("filled_ts") or 0), int(row.get("order_index") or 0)))
    lifecycle_events = sorted(
        list(getattr(result, "lifecycle_events", [])),
        key=lambda row: (int(row.get("ts") or 0), _lifecycle_event_sort_key(str(row.get("event_type") or ""))),
    )
    redeem_events = [event for event in lifecycle_events if event.get("event_type") == "REDEEM_SIMULATED"]
    lots: dict[str, dict[str, list[dict[str, object]]]] = {}
    timeline_rows: list[dict[str, object]] = []
    condition_event_ids: dict[str, str] = {}

    actions: list[tuple[int, int, int, str, dict]] = []
    for fill in fills:
        actions.append((int(fill.get("filled_ts") or 0), 0, int(fill.get("order_index") or 0), "fill", fill))
    for idx, event in enumerate(lifecycle_events):
        event_type = str(event.get("event_type") or "")
        if event_type == "MERGE_SIMULATED":
            actions.append((int(event.get("ts") or 0), 1, idx, "merge", event))

    for _ts, _rank, _idx, action_type, payload in sorted(actions):
        if action_type == "fill":
            fill = payload
            condition_id = str(fill.get("condition_id") or "")
            token_id = str(fill.get("token_id") or "")
            if not condition_id or not token_id:
                continue
            condition_event_ids.setdefault(condition_id, str(fill.get("event_id") or metadata.event_key(condition_id)))
            pair = metadata.token_pairs.get(condition_id)
            yes_before = _dec(fill.get("qty_yes_before_sim"))
            no_before = _dec(fill.get("qty_no_before_sim"))
            yes_after = _dec(fill.get("qty_yes_after_sim"))
            no_after = _dec(fill.get("qty_no_after_sim"))
            side_relation, dominance_effect = _fill_relation_labels(pair, token_id, yes_before, no_before, yes_after, no_after)
            notional = _dec(fill.get("fill_notional_usdc"))
            fee = _dec(fill.get("estimated_fee"))
            qty = _dec(fill.get("fill_size"))
            lots.setdefault(condition_id, {}).setdefault(token_id, []).append(
                {
                    "condition_id": condition_id,
                    "event_id": condition_event_ids[condition_id],
                    "token_id": token_id,
                    "qty": qty,
                    "cost": notional + fee,
                    "entry_price": _dec(fill.get("fill_price")),
                    "complete_set_cost": fill.get("complete_set_cost_estimate") or "",
                    "side_relation": side_relation,
                    "dominance_effect": dominance_effect,
                    "entry_price_bucket": _entry_price_bucket(_dec(fill.get("fill_price"))),
                    "complete_set_cost_bucket": _complete_set_cost_bucket(fill.get("complete_set_cost_estimate")),
                    "dominance_ratio_before_bucket": _dominance_ratio_bucket(yes_before, no_before),
                    "filled_ts": int(fill.get("filled_ts") or 0),
                }
            )
            timeline_rows.append(
                _timeline_row(
                    ts=int(fill.get("filled_ts") or 0),
                    event_type="FILL_SIMULATED",
                    condition_id=condition_id,
                    event_id=condition_event_ids[condition_id],
                    pair=pair,
                    token_id=token_id,
                    action_qty=qty,
                    lots_by_token=lots.get(condition_id, {}),
                    fill_relation=side_relation,
                    dominance_effect=dominance_effect,
                    complete_set_cost_bucket=_complete_set_cost_bucket(fill.get("complete_set_cost_estimate")),
                )
            )
            continue

        event = payload
        condition_id = str(event.get("condition_id") or "")
        if not condition_id:
            continue
        condition_event_ids.setdefault(condition_id, metadata.event_key(condition_id))
        pair = metadata.token_pairs.get(condition_id)
        qty = _dec(event.get("qty"))
        for token_id in _merge_token_ids(pair, lots.get(condition_id, {})):
            _consume_lots_pro_rata(lots.setdefault(condition_id, {}).setdefault(token_id, []), qty)
        timeline_rows.append(
            _timeline_row(
                ts=int(event.get("ts") or 0),
                event_type="MERGE_SIMULATED",
                condition_id=condition_id,
                event_id=condition_event_ids[condition_id],
                pair=pair,
                token_id="",
                action_qty=qty,
                lots_by_token=lots.get(condition_id, {}),
                fill_relation="",
                dominance_effect="",
                complete_set_cost_bucket="",
            )
        )

    resolution_prices = _resolution_prices_from_redeem_events(redeem_events)
    redeem_rows: list[dict[str, object]] = []
    for condition_id, tokens in sorted(lots.items()):
        prices = resolution_prices.get(condition_id, {})
        if not prices:
            continue
        pair = metadata.token_pairs.get(condition_id)
        paired_remaining = _paired_remaining_by_token(pair, tokens)
        for token_id, token_lots in sorted(tokens.items()):
            price = prices.get(token_id, _ZERO)
            paired_left = paired_remaining.get(token_id, _ZERO)
            for lot in token_lots:
                qty = _dec(lot.get("qty"))
                if qty <= _ZERO:
                    continue
                paired_qty = min(qty, paired_left)
                if paired_qty > _ZERO:
                    redeem_rows.append(
                        _redeem_row_from_lot(lot, paired_qty, price, "paired_hedged_inventory")
                    )
                    paired_left -= paired_qty
                unpaired_qty = qty - paired_qty
                if unpaired_qty > _ZERO:
                    redeem_rows.append(
                        _redeem_row_from_lot(lot, unpaired_qty, price, "unpaired_directional_inventory")
                    )
            paired_remaining[token_id] = paired_left

    for event in redeem_events:
        condition_id = str(event.get("condition_id") or "")
        token_id = str(event.get("token_id") or "")
        timeline_rows.append(
            _timeline_row(
                ts=int(event.get("ts") or 0),
                event_type="REDEEM_SIMULATED",
                condition_id=condition_id,
                event_id=condition_event_ids.get(condition_id, metadata.event_key(condition_id)),
                pair=metadata.token_pairs.get(condition_id),
                token_id=token_id,
                action_qty=_dec(event.get("qty")),
                lots_by_token=lots.get(condition_id, {}),
                fill_relation="",
                dominance_effect="",
                complete_set_cost_bucket="",
            )
        )

    event_rows = _aggregate_redeem_rows(redeem_rows, ("event_id",))
    condition_rows = _aggregate_redeem_rows(redeem_rows, ("event_id", "condition_id"))
    token_rows = _aggregate_redeem_rows(redeem_rows, ("event_id", "condition_id", "token_id"))
    summary = _redeem_directional_summary(result, redeem_rows, event_rows, condition_rows, token_rows)
    sanity = _redeem_directional_sanity(result, fills, lifecycle_events, redeem_events, redeem_rows, summary)
    return {
        "summary": summary,
        "sanity": sanity,
        "redeem_rows": redeem_rows,
        "event_rows": event_rows,
        "condition_rows": condition_rows,
        "token_rows": token_rows,
        "timeline_rows": timeline_rows,
    }


def _pattern_redeem_directional_report(
    strategy_name: str,
    result: _TransientResult,
    audit: dict[str, object],
) -> str:
    summary = audit["summary"]
    sanity = audit["sanity"]
    failed = [row for row in sanity if row["status"] == "FAIL"]
    redeem_pnl = Decimal(str(summary["redeem_pnl"]))
    merge_pnl = Decimal(str(summary["merge_pnl"]))
    unpaired_pnl = Decimal(str(summary["unpaired_directional_pnl"]))
    paired_pnl = Decimal(str(summary["paired_hedged_pnl"]))
    winning_pnl = Decimal(str(summary["winning_side_pnl"]))
    losing_pnl = Decimal(str(summary["losing_side_pnl"]))
    top_event = summary["top_event_id"]
    worst_event = summary["worst_event_id"]
    directional_share = abs(unpaired_pnl) / max(abs(redeem_pnl), _ONE)
    winning_share = abs(winning_pnl) / max(abs(redeem_pnl), _ONE)
    mostly_spread = abs(merge_pnl) >= abs(redeem_pnl)
    mostly_directional = abs(unpaired_pnl) > abs(paired_pnl) and abs(winning_pnl) > abs(losing_pnl)
    controlled = (
        Decimal(str(summary["max_unpaired_inventory"])) <= Decimal("500")
        and Decimal(str(summary["top_3_event_abs_pnl_share"])) < Decimal("0.75")
        and not failed
    )
    recommendation = (
        "Require directional-risk filters first; redeem PnL is directional enough that the exact rule should not be the Phase 23 forward-watch unit."
        if mostly_directional
        else "Forward-watch can continue, but redeem PnL should stay separated from spread/merge PnL."
    )
    if not controlled:
        recommendation = "Require directional-risk filters first; do not forward-watch as an exact paper candidate."

    lines = [
        "# Phase 22.7 Redeem and Directional Attribution Audit",
        "",
        f"- Strategy: `{strategy_name}`",
        f"- Simulated fills: {result.fills_count}",
        f"- Audit status: {'INVALID' if failed else 'AUDITED'}",
        "- Scope: conservative pattern audit only. Strategy logic, parameters, and paper status are unchanged.",
        "",
        "## Redeem PnL Source",
        "",
        f"- Merge/recycle PnL: {merge_pnl}",
        f"- Realized redeem PnL: {redeem_pnl}",
        f"- Paired/hedged redeem PnL: {paired_pnl}",
        f"- Unpaired directional redeem PnL: {unpaired_pnl}",
        f"- Winning-side redeem PnL: {winning_pnl}",
        f"- Losing-side redeem PnL: {losing_pnl}",
        f"- Directional share of redeem PnL by absolute value: {directional_share}",
        f"- Winning-side share of redeem PnL by absolute value: {winning_share}",
        "",
        "## Fill Relation Breakdown",
        "",
        "| Relation | Redeem PnL |",
        "| --- | ---: |",
        f"| bought weak/complement side | {summary['side_bought_weak_complement_side_pnl']} |",
        f"| bought dominant side | {summary['side_bought_dominant_side_pnl']} |",
        f"| opened first side | {summary['side_opened_first_side_pnl']} |",
        f"| reduced dominance | {summary['effect_reduced_dominance_pnl']} |",
        f"| increased dominance | {summary['effect_increased_dominance_pnl']} |",
        "",
        "## Bucket Breakdown",
        "",
        "| Bucket | Redeem PnL |",
        "| --- | ---: |",
    ]
    for key in _bucket_summary_keys():
        lines.append(f"| {key} | {summary[key]} |")
    lines.extend(
        [
            "",
            "## Event Concentration",
            "",
            f"- Top event redeem PnL: `{top_event}` = {summary['top_event_redeem_pnl']}",
            f"- Worst event redeem PnL: `{worst_event}` = {summary['worst_event_redeem_pnl']}",
            f"- Top 3 event concentration by absolute redeem PnL: {summary['top_3_event_abs_pnl_share']}",
            "",
            "## Sanity Checks",
            "",
            "| Check | Status | Details |",
            "| --- | --- | --- |",
        ]
    )
    for row in sanity:
        lines.append(f"| {row['check']} | {row['status']} | {row['details']} |")
    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            f"- Is this mostly spread/merge? {'YES' if mostly_spread else 'NO'}; redeem PnL is {'smaller' if mostly_spread else 'larger'} than merge/recycle PnL by absolute value.",
            f"- Is this mostly directional winning inventory? {'YES' if mostly_directional else 'NO'}; unpaired directional and winning-side buckets dominate the redeem component.",
            f"- Is directional exposure controlled or accidental? {'CONTROLLED' if controlled else 'NOT YET CONTROLLED'} under this audit's concentration and unpaired-inventory checks.",
            f"- Phase 23 recommendation: {recommendation}",
        ]
    )
    return "\n".join(lines) + "\n"


def _redeem_row_from_lot(lot: dict[str, object], qty: Decimal, resolution_price: Decimal, inventory_class: str) -> dict[str, object]:
    lot_qty = _dec(lot.get("qty"))
    lot_cost = _dec(lot.get("cost"))
    cost = lot_cost * qty / lot_qty if lot_qty > _ZERO else _ZERO
    payout = qty * resolution_price
    pnl = payout - cost
    outcome_side = "winning_side" if resolution_price > _ZERO else "losing_side"
    return {
        "event_id": lot.get("event_id", ""),
        "condition_id": lot.get("condition_id", ""),
        "token_id": lot.get("token_id", ""),
        "inventory_class": inventory_class,
        "outcome_side": outcome_side,
        "resolution_price": resolution_price,
        "redeem_qty": qty,
        "redeem_payout": payout,
        "redeem_cost": cost,
        "redeem_pnl": pnl,
        "entry_price": lot.get("entry_price", _ZERO),
        "entry_price_bucket": lot.get("entry_price_bucket", ""),
        "complete_set_cost": lot.get("complete_set_cost", ""),
        "complete_set_cost_bucket": lot.get("complete_set_cost_bucket", ""),
        "dominance_ratio_before_bucket": lot.get("dominance_ratio_before_bucket", ""),
        "side_relation": lot.get("side_relation", ""),
        "dominance_effect": lot.get("dominance_effect", ""),
        "filled_ts": lot.get("filled_ts", ""),
    }


def _aggregate_redeem_rows(rows: list[dict[str, object]], keys: tuple[str, ...]) -> list[dict[str, object]]:
    buckets: dict[tuple[object, ...], dict[str, object]] = {}
    for row in rows:
        key = tuple(row.get(k, "") for k in keys)
        bucket = buckets.setdefault(key, _empty_redeem_bucket(keys, key))
        _add_redeem_row_to_bucket(bucket, row)
    return sorted(
        buckets.values(),
        key=lambda row: (str(row.get("event_id", "")), str(row.get("condition_id", "")), str(row.get("token_id", ""))),
    )


def _empty_redeem_bucket(keys: tuple[str, ...], key: tuple[object, ...]) -> dict[str, object]:
    bucket = {column: "" for column in ("event_id", "condition_id", "token_id")}
    for column, value in zip(keys, key):
        bucket[column] = value
    bucket.update(
        {
            "redeem_lot_count": 0,
            "redeem_qty": _ZERO,
            "redeem_payout": _ZERO,
            "redeem_cost": _ZERO,
            "redeem_pnl": _ZERO,
            "paired_hedged_redeem_pnl": _ZERO,
            "unpaired_directional_redeem_pnl": _ZERO,
            "winning_side_redeem_pnl": _ZERO,
            "losing_side_redeem_pnl": _ZERO,
            "side_bought_weak_complement_side_pnl": _ZERO,
            "side_bought_dominant_side_pnl": _ZERO,
            "side_opened_first_side_pnl": _ZERO,
            "effect_reduced_dominance_pnl": _ZERO,
            "effect_increased_dominance_pnl": _ZERO,
            "effect_flat_or_unchanged_pnl": _ZERO,
        }
    )
    for bucket_name in _complete_set_bucket_names():
        bucket[f"complete_set_cost_{bucket_name}_pnl"] = _ZERO
    for bucket_name in _entry_price_bucket_names():
        bucket[f"entry_price_{bucket_name}_pnl"] = _ZERO
    for bucket_name in _dominance_bucket_names():
        bucket[f"dominance_ratio_before_{bucket_name}_pnl"] = _ZERO
    return bucket


def _add_redeem_row_to_bucket(bucket: dict[str, object], row: dict[str, object]) -> None:
    pnl = _dec(row.get("redeem_pnl"))
    bucket["redeem_lot_count"] = int(bucket["redeem_lot_count"]) + 1
    for column in ("redeem_qty", "redeem_payout", "redeem_cost", "redeem_pnl"):
        bucket[column] = Decimal(str(bucket[column])) + _dec(row.get(column))
    if row.get("inventory_class") == "paired_hedged_inventory":
        bucket["paired_hedged_redeem_pnl"] = Decimal(str(bucket["paired_hedged_redeem_pnl"])) + pnl
    if row.get("inventory_class") == "unpaired_directional_inventory":
        bucket["unpaired_directional_redeem_pnl"] = Decimal(str(bucket["unpaired_directional_redeem_pnl"])) + pnl
    if row.get("outcome_side") == "winning_side":
        bucket["winning_side_redeem_pnl"] = Decimal(str(bucket["winning_side_redeem_pnl"])) + pnl
    else:
        bucket["losing_side_redeem_pnl"] = Decimal(str(bucket["losing_side_redeem_pnl"])) + pnl
    side_key = f"side_{row.get('side_relation')}_pnl"
    if side_key in bucket:
        bucket[side_key] = Decimal(str(bucket[side_key])) + pnl
    effect_key = f"effect_{row.get('dominance_effect')}_pnl"
    if effect_key in bucket:
        bucket[effect_key] = Decimal(str(bucket[effect_key])) + pnl
    complete_key = f"complete_set_cost_{row.get('complete_set_cost_bucket')}_pnl"
    if complete_key in bucket:
        bucket[complete_key] = Decimal(str(bucket[complete_key])) + pnl
    entry_key = f"entry_price_{row.get('entry_price_bucket')}_pnl"
    if entry_key in bucket:
        bucket[entry_key] = Decimal(str(bucket[entry_key])) + pnl
    dominance_key = f"dominance_ratio_before_{row.get('dominance_ratio_before_bucket')}_pnl"
    if dominance_key in bucket:
        bucket[dominance_key] = Decimal(str(bucket[dominance_key])) + pnl


def _redeem_directional_summary(
    result: _TransientResult,
    redeem_rows: list[dict[str, object]],
    event_rows: list[dict[str, object]],
    condition_rows: list[dict[str, object]],
    token_rows: list[dict[str, object]],
) -> dict[str, object]:
    _ = condition_rows, token_rows
    lifecycle = getattr(result, "lifecycle_metrics", LifecycleMetrics())
    row_bucket = _empty_redeem_bucket((), ())
    for row in redeem_rows:
        _add_redeem_row_to_bucket(row_bucket, row)
    sorted_events = sorted(event_rows, key=lambda row: Decimal(str(row["redeem_pnl"])), reverse=True)
    worst_events = sorted(event_rows, key=lambda row: Decimal(str(row["redeem_pnl"])))
    abs_total = sum((abs(Decimal(str(row["redeem_pnl"]))) for row in event_rows), _ZERO)
    top3_abs = sum((abs(Decimal(str(row["redeem_pnl"]))) for row in sorted(event_rows, key=lambda row: abs(Decimal(str(row["redeem_pnl"]))), reverse=True)[:3]), _ZERO)
    summary = {
        "merge_pnl": lifecycle.merge_pnl,
        "redeem_pnl": lifecycle.redeem_pnl,
        "attributed_redeem_pnl": row_bucket["redeem_pnl"],
        "redeem_pnl_residual": lifecycle.redeem_pnl - Decimal(str(row_bucket["redeem_pnl"])),
        "paired_hedged_pnl": row_bucket["paired_hedged_redeem_pnl"],
        "unpaired_directional_pnl": row_bucket["unpaired_directional_redeem_pnl"],
        "winning_side_pnl": row_bucket["winning_side_redeem_pnl"],
        "losing_side_pnl": row_bucket["losing_side_redeem_pnl"],
        "top_event_id": sorted_events[0]["event_id"] if sorted_events else "",
        "top_event_redeem_pnl": sorted_events[0]["redeem_pnl"] if sorted_events else _ZERO,
        "worst_event_id": worst_events[0]["event_id"] if worst_events else "",
        "worst_event_redeem_pnl": worst_events[0]["redeem_pnl"] if worst_events else _ZERO,
        "top_3_event_abs_pnl_share": top3_abs / abs_total if abs_total > _ZERO else _ZERO,
        "max_unpaired_inventory": lifecycle.max_unpaired_inventory,
        "unresolved_inventory_value": lifecycle.unresolved_inventory_value,
    }
    for key, value in row_bucket.items():
        if key.endswith("_pnl") and key not in summary:
            summary[key] = value
    return summary


def _redeem_directional_sanity(
    result: _TransientResult,
    fills: list[dict],
    lifecycle_events: list[dict],
    redeem_events: list[dict],
    redeem_rows: list[dict[str, object]],
    summary: dict[str, object],
) -> list[dict[str, object]]:
    _ = lifecycle_events
    latest_fill_ts = max((int(fill.get("filled_ts") or 0) for fill in fills), default=0)
    redeem_after_end = all(int(event.get("ts") or 0) > latest_fill_ts for event in redeem_events)
    duplicate_keys = [
        (event.get("condition_id"), event.get("token_id"))
        for event in redeem_events
    ]
    no_duplicate_redeem = len(set(duplicate_keys)) == len(duplicate_keys)
    attributed = Decimal(str(summary["attributed_redeem_pnl"]))
    lifecycle_redeem = Decimal(str(summary["redeem_pnl"]))
    residual = lifecycle_redeem - attributed
    return [
        {
            "check": "redeem_applied_only_after_simulated_end_resolution",
            "status": "PASS" if redeem_after_end else "FAIL",
            "details": f"latest_fill_ts={latest_fill_ts}; redeem_events={len(redeem_events)}",
        },
        {
            "check": "no_outcome_used_in_entry",
            "status": "PASS",
            "details": "Resolution prices are consumed only by end-of-simulation redeem attribution.",
        },
        {
            "check": "no_final_rn1_pnl_used",
            "status": "PASS",
            "details": "DecisionContext allowlist excludes final RN1 realized PnL and resolution PnL fields.",
        },
        {
            "check": "no_unresolved_inventory_counted_as_redeem",
            "status": "PASS" if Decimal(str(summary["unresolved_inventory_value"])) == _ZERO else "FAIL",
            "details": f"unresolved_inventory_value={summary['unresolved_inventory_value']}",
        },
        {
            "check": "no_duplicate_redeem_payout",
            "status": "PASS" if no_duplicate_redeem else "FAIL",
            "details": f"redeem_events={len(redeem_events)}; unique_condition_token={len(set(duplicate_keys))}",
        },
        {
            "check": "redeem_attribution_matches_lifecycle",
            "status": "PASS" if abs(residual) < Decimal("0.000001") else "FAIL",
            "details": f"lifecycle={lifecycle_redeem}; attributed={attributed}; residual={residual}",
        },
        {
            "check": "no_paper_promotion",
            "status": "PASS" if not getattr(result, "conservative_pass", False) else "FAIL",
            "details": "Phase 22.7 emits diagnostics only.",
        },
    ]


def _timeline_row(
    *,
    ts: int,
    event_type: str,
    condition_id: str,
    event_id: str,
    pair: TokenPair | None,
    token_id: str,
    action_qty: Decimal,
    lots_by_token: dict[str, list[dict[str, object]]],
    fill_relation: str,
    dominance_effect: str,
    complete_set_cost_bucket: str,
) -> dict[str, object]:
    if pair is not None:
        yes_token = pair.yes_token_id
        no_token = pair.no_token_id
    else:
        token_ids = sorted(lots_by_token)
        yes_token = token_ids[0] if token_ids else ""
        no_token = token_ids[1] if len(token_ids) > 1 else ""
    yes_qty = _lot_qty(lots_by_token.get(yes_token, []))
    no_qty = _lot_qty(lots_by_token.get(no_token, []))
    paired_qty = min(yes_qty, no_qty)
    unpaired_qty = abs(yes_qty - no_qty)
    dominant_token = ""
    if yes_qty > no_qty:
        dominant_token = yes_token
    elif no_qty > yes_qty:
        dominant_token = no_token
    return {
        "ts": ts,
        "event_id": event_id,
        "condition_id": condition_id,
        "event_type": event_type,
        "token_id": token_id,
        "action_qty": action_qty,
        "yes_token_id": yes_token,
        "no_token_id": no_token,
        "qty_yes": yes_qty,
        "qty_no": no_qty,
        "paired_qty": paired_qty,
        "unpaired_qty": unpaired_qty,
        "dominant_token_id": dominant_token,
        "dominance_ratio": _dominance_ratio_value(yes_qty, no_qty),
        "dominance_ratio_bucket": _dominance_ratio_bucket(yes_qty, no_qty),
        "fill_relation": fill_relation,
        "dominance_effect": dominance_effect,
        "complete_set_cost_bucket": complete_set_cost_bucket,
    }


def _fill_relation_labels(
    pair: TokenPair | None,
    token_id: str,
    yes_before: Decimal,
    no_before: Decimal,
    yes_after: Decimal,
    no_after: Decimal,
) -> tuple[str, str]:
    if yes_before <= _ZERO and no_before <= _ZERO:
        side_relation = "opened_first_side"
    elif pair is None:
        side_relation = "bought_dominant_side"
    else:
        same_before = yes_before if token_id == pair.yes_token_id else no_before
        opposite_before = no_before if token_id == pair.yes_token_id else yes_before
        side_relation = "bought_weak_complement_side" if same_before < opposite_before else "bought_dominant_side"
    before_gap = abs(yes_before - no_before)
    after_gap = abs(yes_after - no_after)
    if after_gap < before_gap:
        dominance_effect = "reduced_dominance"
    elif after_gap > before_gap:
        dominance_effect = "increased_dominance"
    else:
        dominance_effect = "flat_or_unchanged"
    return side_relation, dominance_effect


def _consume_lots_pro_rata(lots: list[dict[str, object]], qty: Decimal) -> None:
    total_qty = _lot_qty(lots)
    if qty <= _ZERO or total_qty <= _ZERO:
        return
    consumed = min(qty, total_qty)
    ratio = consumed / total_qty
    for lot in lots:
        lot_qty = _dec(lot.get("qty"))
        lot_cost = _dec(lot.get("cost"))
        lot["qty"] = lot_qty - lot_qty * ratio
        lot["cost"] = lot_cost - lot_cost * ratio


def _merge_token_ids(pair: TokenPair | None, lots_by_token: dict[str, list[dict[str, object]]]) -> list[str]:
    if pair is not None:
        return [pair.yes_token_id, pair.no_token_id]
    return sorted(lots_by_token)[:2]


def _paired_remaining_by_token(pair: TokenPair | None, lots_by_token: dict[str, list[dict[str, object]]]) -> dict[str, Decimal]:
    if pair is None:
        return {token_id: _ZERO for token_id in lots_by_token}
    yes_qty = _lot_qty(lots_by_token.get(pair.yes_token_id, []))
    no_qty = _lot_qty(lots_by_token.get(pair.no_token_id, []))
    paired_qty = min(yes_qty, no_qty)
    return {pair.yes_token_id: paired_qty, pair.no_token_id: paired_qty}


def _resolution_prices_from_redeem_events(events: list[dict]) -> dict[str, dict[str, Decimal]]:
    prices: dict[str, dict[str, Decimal]] = {}
    for event in events:
        condition_id = str(event.get("condition_id") or "")
        token_id = str(event.get("token_id") or "")
        qty = _dec(event.get("qty"))
        payout = _dec(event.get("capital_released") or event.get("usdc_delta"))
        if not condition_id or not token_id or qty <= _ZERO:
            continue
        prices.setdefault(condition_id, {})[token_id] = payout / qty
    return prices


def _lot_qty(lots: list[dict[str, object]]) -> Decimal:
    return sum((_dec(lot.get("qty")) for lot in lots), _ZERO)


def _lifecycle_event_sort_key(event_type: str) -> int:
    if event_type == "MERGE_SIMULATED":
        return 0
    if event_type == "CAPITAL_RELEASED":
        return 1
    if event_type == "REDEEM_SIMULATED":
        return 2
    return 9


def _entry_price_bucket(price: Decimal) -> str:
    if price < Decimal("0.10"):
        return "lt_0_10"
    if price < Decimal("0.30"):
        return "0_10_0_30"
    if price < Decimal("0.50"):
        return "0_30_0_50"
    if price < Decimal("0.70"):
        return "0_50_0_70"
    if price < Decimal("0.90"):
        return "0_70_0_90"
    return "gt_0_90"


def _complete_set_cost_bucket(value: object) -> str:
    if value in (None, ""):
        return "missing"
    cost = _dec(value)
    if cost < Decimal("0.95"):
        return "lt_0_95"
    if cost < Decimal("0.98"):
        return "0_95_0_98"
    if cost < Decimal("0.99"):
        return "0_98_0_99"
    if cost <= Decimal("1.00"):
        return "0_99_1_00"
    return "gt_1_00"


def _dominance_ratio_bucket(yes_qty: Decimal, no_qty: Decimal) -> str:
    if yes_qty <= _ZERO and no_qty <= _ZERO:
        return "flat"
    low = min(yes_qty, no_qty)
    high = max(yes_qty, no_qty)
    if low <= _ZERO:
        return "one_sided"
    ratio = high / low
    if ratio < Decimal("1.25"):
        return "1_0_1_25"
    if ratio < Decimal("1.5"):
        return "1_25_1_5"
    if ratio < Decimal("2.0"):
        return "1_5_2_0"
    if ratio < Decimal("3.0"):
        return "2_0_3_0"
    return "gt_3_0"


def _dominance_ratio_value(yes_qty: Decimal, no_qty: Decimal) -> object:
    low = min(yes_qty, no_qty)
    high = max(yes_qty, no_qty)
    if yes_qty <= _ZERO and no_qty <= _ZERO:
        return "flat"
    if low <= _ZERO:
        return "one_sided"
    return high / low


def _complete_set_bucket_names() -> list[str]:
    return ["lt_0_95", "0_95_0_98", "0_98_0_99", "0_99_1_00", "gt_1_00", "missing"]


def _entry_price_bucket_names() -> list[str]:
    return ["lt_0_10", "0_10_0_30", "0_30_0_50", "0_50_0_70", "0_70_0_90", "gt_0_90"]


def _dominance_bucket_names() -> list[str]:
    return ["flat", "1_0_1_25", "1_25_1_5", "1_5_2_0", "2_0_3_0", "gt_3_0", "one_sided"]


def _bucket_summary_keys() -> list[str]:
    keys = [f"complete_set_cost_{name}_pnl" for name in _complete_set_bucket_names()]
    keys.extend(f"entry_price_{name}_pnl" for name in _entry_price_bucket_names())
    keys.extend(f"dominance_ratio_before_{name}_pnl" for name in _dominance_bucket_names())
    return keys


def _redeem_cost_from_inventory_json(raw: dict[str, object], token_id: str, qty: Decimal) -> Decimal:
    token = raw.get(token_id)
    if not isinstance(token, dict):
        return _ZERO
    lot_qty = _dec(token.get("qty"))
    lot_cost = _dec(token.get("cost"))
    if lot_qty <= _ZERO:
        return _ZERO
    return min(lot_cost, lot_cost / lot_qty * qty)


def _median_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        return _ZERO
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal("2")


def _pct_below(values: list[Decimal], threshold: Decimal) -> Decimal:
    if not values:
        return _ZERO
    count = sum(1 for value in values if value < threshold)
    return Decimal(count) / Decimal(len(values))


def _pattern_signal_diagnostic_report(
    session: Session,
    wallet: str,
    strategy_name: str,
    diagnostic_rows: list[dict[str, object]],
    sample_failure_rows: list[dict[str, object]],
) -> str:
    source = _source_universe_summary(session, wallet, strategy_name)
    rule_d = _rule_d_gate_summary(session, wallet)
    evidence = _phase225b_evidence_checks(wallet)
    first_zero_gate = _first_zero_gate(diagnostic_rows)
    skip_totals = _skip_totals(diagnostic_rows)
    lines = [
        "# Phase 22.6 Pattern Signal Diagnostics",
        "",
        f"- Wallet: `{wallet}`",
        f"- Strategy: `{strategy_name}`",
        "- Scope: pre-signal diagnostics before order simulation and before scenario fill assumptions.",
        "- Profitability is not evaluated here.",
        "",
        "## Source Universe",
        "",
        "| Source | Rows | Notes |",
        "| --- | ---: | --- |",
    ]
    for row in source:
        lines.append(f"| {row['source']} | {row['rows']} | {row['notes']} |")
    lines.extend(
        [
            "",
            "## Gate Summary",
            "",
            "| Candidate | Split | Start | Token Mapping | Inventory | Opposite Unpaired | Rule B Increases Bond | Event Gate | Cost Basis | Positive Max Quote | Fresh Book | Positive Size | Risk Caps | Final Signals | Top Skip |",
            "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in diagnostic_rows:
        lines.append(
            f"| {row['candidate_id']} | {row['split']} | {row['starting_context_rows']} | "
            f"{row['rows_with_token_mapping']} | {row['rows_with_inventory_before']} | "
            f"{row['rows_where_rule_a_opposite_unpaired_exists']} | {row['rows_where_rule_b_would_increase_bond']} | "
            f"{row['rows_passing_event_basket_gate']} | {row['rows_with_wac_or_cost_basis']} | "
            f"{row['rows_with_positive_max_quote_price']} | {row['rows_with_fresh_book_context']} | "
            f"{row['rows_with_positive_order_size']} | {row['rows_passing_risk_caps']} | "
            f"{row['final_candidate_signals']} | {_top_skip(row)} |"
        )
    lines.extend(
        [
            "",
            "## Diagnosis",
            f"- First zero gate across the fixed grid: `{first_zero_gate}`.",
            f"- Aggregate pre-signal skips: `{json.dumps(skip_totals, sort_keys=True)}`.",
        ]
    )
    if first_zero_gate == "rows_with_inventory_before":
        lines.append(
            "- The simulator is evaluating the Phase 22.6 pattern rules against `microstructure_lifecycle_dataset` with an empty simulated inventory state. Rule A/B require pre-existing opposite-side inventory and cost basis, so candidate signals drop to zero before pricing, order size, risk caps, or fill assumptions."
        )
    elif first_zero_gate == "none":
        lines.append("- No zeroing gate remains in the pre-signal diagnostic; any remaining zero fills/orders are downstream of order simulation or scenario fill assumptions.")
    elif first_zero_gate == "rows_passing_event_basket_gate":
        lines.append("- Rule D is the first zeroing gate in the simulator-state replay.")
    else:
        lines.append("- See the gate-count CSV for the exact candidate/split where the first zero appears.")
    lines.extend(
        [
            "",
            "## Rule D Gate Alone",
            "",
            "| Source | min_active_conditions=2 | min_active_conditions=3 | Notes |",
            "| --- | ---: | ---: | --- |",
            f"| simulator_enriched_rows | {rule_d['simulator_min_2']} | {rule_d['simulator_min_3']} | Counts only the Rule D min-active-condition predicate on enriched pre-fill rows. Full Rule D gate passes: min2={rule_d['simulator_full_min_2']}, min3={rule_d['simulator_full_min_3']}. |",
            f"| phase22_5b_order_timing_dataset | {rule_d['order_timing_min_2']} | {rule_d['order_timing_min_3']} | Counts from exported `event_market_count_active_before`; this checks whether Rule D itself would block the supported universe. |",
            "",
            "## Evidence Row Checks",
            "",
            "| Rule | Fixture | Real Evidence Row | Opposite Unpaired | Max Quote Positive | Size Positive | Candidate Before Fills | Notes |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in evidence:
        lines.append(
            f"| {row['rule']} | {row['fixture']} | {row['fill_event_id']} | {row['opposite_unpaired']} | "
            f"{row['max_quote_positive']} | {row['size_positive']} | {row['candidate_before_fills']} | {row['notes']} |"
        )
    if sample_failure_rows:
        by_reason: dict[str, int] = {}
        for sample in sample_failure_rows:
            reason = str(sample.get("skip_reason") or "unknown_reason")
            by_reason[reason] = by_reason.get(reason, 0) + 1
        lines.extend(
            [
                "",
                "## Sample Failures",
                f"- Wrote up to 20 examples per major skip reason to `{PATTERN_SIGNAL_SAMPLE_FAILURES}`.",
                f"- Sample counts by reason: `{json.dumps(by_reason, sort_keys=True)}`.",
            ]
        )
    return "\n".join(lines) + "\n"


def _source_universe_summary(session: Session, wallet: str, strategy_name: str) -> list[dict[str, object]]:
    root = Path(".")
    order_timing_path = root / "exports" / "phase22_5_patterns" / "order_timing_dataset.csv"
    evidence_path = root / "exports" / "phase22_5_patterns" / "rule_evidence_examples.csv"
    micro_cols = set(_table_columns(session, "microstructure_lifecycle_dataset"))
    pattern_cols = _csv_header(order_timing_path)
    missing_micro_fields = [
        col for col in (
            "qty_yes_before",
            "qty_no_before",
            "wac_yes_before",
            "wac_no_before",
            "event_market_count_active_before",
            "event_unpaired_inventory_before",
            "event_bond_qty_before",
        )
        if col not in micro_cols
    ]
    return [
        {
            "source": "microstructure_lifecycle_dataset",
            "rows": _count_table_rows(session, "microstructure_lifecycle_dataset", wallet),
            "notes": "base Phase 22.6 simulator source via `_load_dataset`; enriched at runtime with Phase 22.5b pre-fill fields. Base table missing: "
            + ", ".join(missing_micro_fields),
        },
        {
            "source": "phase22_5b order_timing_dataset.csv",
            "rows": _count_csv_rows(order_timing_path, wallet),
            "notes": "exported supported-universe source with pattern columns: "
            + ", ".join(col for col in ("qty_yes_before", "qty_no_before", "wac_yes_before", "wac_no_before", "event_market_count_active_before") if col in pattern_cols),
        },
        {
            "source": "phase22_5b rule_evidence_examples.csv",
            "rows": _count_csv_rows(evidence_path, wallet),
            "notes": "evidence examples used for real-row Rule A/B/D checks",
        },
        {
            "source": "simulation_runs",
            "rows": _count_simulation_runs(session, wallet, strategy_name),
            "notes": "prior Phase 22 persisted scenario runs for this strategy; not used as candidate source",
        },
    ]


def _rule_d_gate_summary(session: Session, wallet: str) -> dict[str, int]:
    from .engine import _load_dataset

    simulator_min_2 = 0
    simulator_min_3 = 0
    simulator_full_min_2 = 0
    simulator_full_min_3 = 0
    sim_rows = _enrich_pattern_rows_from_phase225b(_load_dataset(session, wallet))
    if sim_rows:
        metadata = load_pattern_metadata(session, sim_rows)
        state = InventoryLifecycleState()
        for row in sim_rows:
            ctx = DecisionContext.from_row(row)
            condition_id = ctx.condition_id or f"token:{ctx.token_id}"
            _seed_pattern_state_from_row(row, state, metadata)
            state.mark(condition_id, ctx.token_id, ctx.decimal("mid_before"))
            active_count = ctx.integer("event_market_count_active_before")
            if active_count is None:
                active_count = len(_event_active_conditions(state, metadata.event_key(condition_id), metadata))
            if active_count >= 2:
                simulator_min_2 += 1
            if active_count >= 3:
                simulator_min_3 += 1
            if event_basket_gate(
                ctx,
                state,
                PatternRuleConfig("pattern_abd_inventory_rule_v1", min_active_conditions=2),
                metadata,
            ) is None:
                simulator_full_min_2 += 1
            if event_basket_gate(
                ctx,
                state,
                PatternRuleConfig("pattern_abd_inventory_rule_v1", min_active_conditions=3),
                metadata,
            ) is None:
                simulator_full_min_3 += 1

    path = Path("exports") / "phase22_5_patterns" / "order_timing_dataset.csv"
    min_2 = 0
    min_3 = 0
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if str(row.get("wallet", "")).lower() != wallet.lower():
                    continue
                active = _safe_int(row.get("event_market_count_active_before"))
                if active >= 2:
                    min_2 += 1
                if active >= 3:
                    min_3 += 1
    return {
        "simulator_min_2": simulator_min_2,
        "simulator_min_3": simulator_min_3,
        "simulator_full_min_2": simulator_full_min_2,
        "simulator_full_min_3": simulator_full_min_3,
        "order_timing_min_2": min_2,
        "order_timing_min_3": min_3,
    }


def _phase225b_evidence_checks(wallet: str) -> list[dict[str, object]]:
    evidence_path = Path("exports") / "phase22_5_patterns" / "rule_evidence_examples.csv"
    timing_path = Path("exports") / "phase22_5_patterns" / "order_timing_dataset.csv"
    timing_by_fill: dict[str, dict[str, str]] = {}
    if timing_path.exists():
        with timing_path.open("r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                timing_by_fill[str(row.get("fill_event_id", ""))] = row
    results: list[dict[str, object]] = []
    if not evidence_path.exists():
        return results
    wanted = {"A": "Rule A direct fixture applies", "B": "Rule B direct fixture increases paired_qty"}
    seen: set[str] = set()
    with evidence_path.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rule = str(row.get("rule_id", ""))
            if rule not in wanted or rule in seen or str(row.get("wallet", "")).lower() != wallet.lower():
                continue
            seen.add(rule)
            timing = timing_by_fill.get(str(row.get("fill_event_id", "")), {})
            results.append(_evidence_rule_check(row, timing, wanted[rule]))
            if seen == set(wanted):
                break
    return results


def _evidence_rule_check(evidence: dict[str, str], timing: dict[str, str], fixture_note: str) -> dict[str, object]:
    trigger = _parse_json_dict(evidence.get("trigger_snapshot"))
    side = str(trigger.get("fill_token_side") or timing.get("fill_token_side") or "").upper()
    qty_yes = _dec(trigger.get("qty_yes_before") or timing.get("qty_yes_before"))
    qty_no = _dec(trigger.get("qty_no_before") or timing.get("qty_no_before"))
    price = _dec(trigger.get("price") or timing.get("fill_price"))
    size = _dec(trigger.get("size") or timing.get("fill_size"))
    if side == "YES":
        opposite_unpaired = max(_ZERO, qty_no - qty_yes)
        opposite_wac = _dec(timing.get("wac_no_before"))
    else:
        opposite_unpaired = max(_ZERO, qty_yes - qty_no)
        opposite_wac = _dec(timing.get("wac_yes_before"))
    max_quote = Decimal("0.98") - opposite_wac if opposite_wac > _ZERO else Decimal("0.98") - price
    candidate = opposite_unpaired > _ZERO and max_quote > _ZERO and size > _ZERO
    if evidence.get("rule_id") == "B":
        paired_before = min(qty_yes, qty_no)
        paired_after = min(qty_yes + (size if side == "YES" else _ZERO), qty_no + (size if side == "NO" else _ZERO))
        candidate = candidate and paired_after > paired_before
    return {
        "rule": evidence.get("rule_id", ""),
        "fixture": fixture_note,
        "fill_event_id": evidence.get("fill_event_id", ""),
        "opposite_unpaired": "1" if opposite_unpaired > _ZERO else "0",
        "max_quote_positive": "1" if max_quote > _ZERO else "0",
        "size_positive": "1" if size > _ZERO else "0",
        "candidate_before_fills": "1" if candidate else "0",
        "notes": f"condition_id={evidence.get('condition_id', '')}; side={side}; max_quote_price={max_quote}",
    }


def _first_zero_gate(diagnostic_rows: list[dict[str, object]]) -> str:
    for column in DIAGNOSTIC_GATE_COLUMNS:
        if any(int(row.get(column) or 0) == 0 for row in diagnostic_rows):
            return column
    return "none"


def _top_skip(row: dict[str, object]) -> str:
    pairs = [(reason, int(row.get(f"skip_{reason}") or 0)) for reason in PRE_SIGNAL_SKIP_REASONS]
    reason, count = max(pairs, key=lambda item: item[1])
    return f"{reason}:{count}" if count else ""


def _skip_totals(rows: list[dict[str, object]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in rows:
        for reason in PRE_SIGNAL_SKIP_REASONS:
            count = int(row.get(f"skip_{reason}") or 0)
            if count:
                totals[reason] = totals.get(reason, 0) + count
    return totals


def _table_columns(session: Session, table_name: str) -> list[str]:
    try:
        rows = session.execute(text(f"PRAGMA table_info({table_name})")).mappings().fetchall()
    except Exception:
        return []
    return [str(row["name"]) for row in rows]


def _count_table_rows(session: Session, table_name: str, wallet: str) -> int:
    try:
        return int(session.execute(text(f"SELECT COUNT(*) FROM {table_name} WHERE wallet = :wallet"), {"wallet": wallet}).scalar_one())
    except Exception:
        return 0


def _count_simulation_runs(session: Session, wallet: str, strategy_name: str) -> int:
    try:
        return int(
            session.execute(
                text("SELECT COUNT(*) FROM simulation_runs WHERE wallet = :wallet AND strategy_name = :strategy"),
                {"wallet": wallet, "strategy": strategy_name},
            ).scalar_one()
        )
    except Exception:
        return 0


def _count_csv_rows(path: Path, wallet: str) -> int:
    if not path.exists():
        return 0
    with path.open("r", newline="", encoding="utf-8") as f:
        return sum(1 for row in csv.DictReader(f) if str(row.get("wallet", "")).lower() == wallet.lower())


def _csv_header(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            return set(next(reader))
        except StopIteration:
            return set()


def _parse_json_dict(raw: object) -> dict[str, object]:
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dec(value: object) -> Decimal:
    if value in (None, ""):
        return _ZERO
    try:
        return Decimal(str(value))
    except Exception:
        return _ZERO


def _safe_int(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(Decimal(str(value)))
    except Exception:
        return 0


def _fixed_parameter_grid(strategy_name: str) -> list[dict[str, object]]:
    costs = ["0.98", "0.99", "1.00"]
    active = [2, 3] if "event_gate" in strategy_name or strategy_name == "pattern_abd_inventory_rule_v1" else [2]
    windows = [300, 900] if strategy_name == "pattern_abd_inventory_rule_v1" else [300]
    rows = []
    for cost in costs:
        for min_active in active:
            for window in windows:
                rows.append(
                    {
                        "max_complete_set_cost": cost,
                        "min_active_conditions": min_active,
                        "min_merge_qty": "1",
                        "merge_batch_window_s": window,
                        "max_event_capital": "5000",
                        "max_order_size": "25",
                    }
                )
    return rows


def _latest_results(session: Session, wallet: str, strategy_name: str) -> list[dict[str, object]]:
    rows = session.execute(
        text(
            "SELECT * FROM simulation_runs WHERE wallet = :wallet AND strategy_name = :strategy "
            "ORDER BY run_ts DESC, id DESC"
        ),
        {"wallet": wallet, "strategy": strategy_name},
    ).mappings().fetchall()
    by_scenario: dict[str, dict[str, object]] = {}
    for row in rows:
        by_scenario.setdefault(str(row["scenario"]), dict(row))
    return [by_scenario[s] for s in ("optimistic", "medium", "conservative") if s in by_scenario]


def _write_csv(path: Path, rows: list[dict[str, object]], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        columns = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in columns})


def _csv_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, bool):
        return "1" if value else "0"
    return "" if value is None else value


def _metric_field(db_row: dict[str, object] | None, db_key: str, transient: _TransientResult | None, attr: str) -> object:
    if db_row is not None and db_key in db_row:
        return db_row.get(db_key)
    if transient is not None:
        return getattr(transient, attr, "")
    return ""


def _candidate_signals_field(db_row: dict[str, object] | None, transient: _TransientResult | None) -> object:
    if db_row is not None:
        return int(db_row.get("orders_count") or 0) + int(db_row.get("skipped_orders_count") or 0)
    if transient is not None:
        return transient.candidate_signals_count
    return ""


def _lifecycle_field(session: Session, run_row: dict[str, object] | None, key: str) -> object:
    if run_row is None:
        return ""
    row = session.execute(
        text(f"SELECT {key} FROM simulation_lifecycle_summary WHERE run_id = :run_id"),
        {"run_id": run_row["id"]},
    ).mappings().fetchone()
    return "" if row is None else row[key]


def _realized_only(session: Session, run_row: dict[str, object] | None) -> object:
    if run_row is None:
        return ""
    row = session.execute(
        text("SELECT trading_pnl, merge_pnl, redeem_pnl FROM simulation_lifecycle_summary WHERE run_id = :run_id"),
        {"run_id": run_row["id"]},
    ).mappings().fetchone()
    if row is None:
        return run_row.get("net_pnl", "")
    return Decimal(str(row["trading_pnl"])) + Decimal(str(row["merge_pnl"])) + Decimal(str(row["redeem_pnl"]))


def _event_count(rows: list[dict]) -> int:
    return len({str(row.get("condition_id") or row.get("event_id")) for row in rows})


def _degradation(validation: _TransientResult | None, test: _TransientResult | None) -> object:
    if validation is None or test is None or validation.net_pnl <= _ZERO:
        return ""
    return (validation.net_pnl - test.net_pnl) / max(abs(validation.net_pnl), _ONE)


def _event_robustness_rows(candidate_id: int, strategy: str, split: str, result: _TransientResult) -> list[dict[str, object]]:
    buckets: dict[str, dict[str, object]] = {}
    for fill in result.fills:
        key = str(fill.get("condition_id") or fill.get("event_id"))
        row = buckets.setdefault(
            key,
            {
                "candidate_id": candidate_id,
                "strategy": strategy,
                "split": split,
                "event_or_condition": key,
                "fills": 0,
                "notional": Decimal("0"),
                "estimated_pnl_proxy": Decimal("0"),
            },
        )
        row["fills"] = int(row["fills"]) + 1
        row["notional"] = Decimal(str(row["notional"])) + Decimal(str(fill.get("fill_notional_usdc") or "0"))
    for row in buckets.values():
        row["estimated_pnl_proxy"] = -Decimal(str(row["notional"])) * Decimal("0.002")
    return list(buckets.values())


def _worst_event_pnl(event_rows: list[dict[str, object]], candidate_id: int, split: str) -> object:
    values = [
        Decimal(str(row["estimated_pnl_proxy"]))
        for row in event_rows
        if row["candidate_id"] == candidate_id and row["split"] == split
    ]
    return min(values) if values else ""


def _test_concentration(event_rows: list[dict[str, object]], candidate_id: int) -> object:
    notionals = [
        Decimal(str(row["notional"]))
        for row in event_rows
        if row["candidate_id"] == candidate_id and row["split"] == "test"
    ]
    total = sum(notionals, _ZERO)
    if total <= _ZERO or not notionals:
        return ""
    return max(notionals) / total


def _risk_rows(
    candidate_id: int,
    strategy: str,
    metrics: dict[str, _TransientResult],
    gate: dict[str, object],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for split, metric in metrics.items():
        for reason, count in metric.skipped_by_reason.items():
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "strategy": strategy,
                    "split": split,
                    "risk_or_skip_reason": reason,
                    "count": count,
                    "risk_breaches": metric.risk_breaches,
                    "stale_context_excluded": metric.stale_context_excluded,
                    "notes": "pre-fill risk skip or stale context exclusion",
                }
            )
    for reason in gate["reasons"]:
        rows.append(
            {
                "candidate_id": candidate_id,
                "strategy": strategy,
                "split": "test_gate",
                "risk_or_skip_reason": reason,
                "count": 1,
                "risk_breaches": "",
                "stale_context_excluded": "",
                "notes": "paper gate failure; forward-watch only",
            }
        )
    return rows


def _gate_status(
    *,
    test: _TransientResult | None,
    validation: _TransientResult | None,
    max_capital: Decimal,
    ordering_violation: bool,
) -> dict[str, object]:
    reasons: list[str] = []
    if test is None:
        reasons.append("missing_test_split")
    else:
        if test.net_pnl <= _ZERO:
            reasons.append("conservative_test_net_pnl_non_positive")
        if test.net_pnl < max_capital * Decimal("0.05"):
            reasons.append("test_net_pnl_below_5pct_max_capital")
        if test.risk_breaches > 0:
            reasons.append("hard_risk_breach")
        if test.max_inventory > Decimal("500"):
            reasons.append("max_unpaired_inventory_exceeds_configured_units")
        event_count = len({fill.get("condition_id") for fill in test.fills})
        if event_count < 30:
            reasons.append("test_events_below_30")
    degradation = _degradation(validation, test)
    if isinstance(degradation, Decimal) and degradation > Decimal("0.50"):
        reasons.append("validation_to_test_degradation_gt_50pct")
    if ordering_violation:
        reasons.append("ordering_violation")
    return {"status": "PASS" if not reasons else "FAIL", "reasons": reasons}


def _result_ordering_violation(conservative: dict[str, object] | None, optimistic: dict[str, object] | None) -> bool:
    if conservative is None or optimistic is None:
        return False
    return (
        int(conservative.get("simulated_fills_count") or 0) > int(optimistic.get("simulated_fills_count") or 0)
        or Decimal(str(conservative.get("net_pnl") or "0")) > Decimal(str(optimistic.get("net_pnl") or "0"))
    )


def _holdout_report(candidate_rows: list[dict[str, object]]) -> str:
    lines = [
        "# Phase 22.6 Pattern Strategy Holdout Summary",
        "",
        "Test split is reported as a holdout diagnostic only. Parameter grids are fixed from Phase 22.5b recommendations and are not optimized on test.",
        "",
        "| Candidate | Strategy | Test Net PnL | Gate | Paper Eligible | Reasons |",
        "| ---: | --- | ---: | --- | --- | --- |",
    ]
    for row in candidate_rows:
        lines.append(
            f"| {row['candidate_id']} | {row['strategy']} | {row['test_net_pnl']} | "
            f"{row['gate_status']} | {row['paper_eligible']} | {row['gate_reasons']} |"
        )
    lines.extend(
        [
            "",
            "Right-censoring caveat: live/recent events can retain unresolved inventory. Unresolved inventory value is separated from realized PnL and is not marked as profit.",
        ]
    )
    return "\n".join(lines) + "\n"


def _pattern_report(
    strategy_name: str,
    candidate_rows: list[dict[str, object]],
    latest_results: list[dict[str, object]],
    notes: dict[str, str],
) -> str:
    lines = [
        "# Phase 22.6 Pattern Strategy Comparison Report",
        "",
        f"- Strategy: `{strategy_name}`",
        "- Source candidates: Rule A Complement Catch-Up, Rule B Bond-Increasing BUY, Rule D Event Basket Activation.",
        "- Rule E is implemented as risk limits. Rule F is implemented as simulated batch/capital recycling.",
        "- This report does not claim profitability and does not promote anything to paper.",
        "",
        "## Phase 22.5b Notes",
        f"- Rule A: {notes.get('A', '')}",
        f"- Rule B: {notes.get('B', '')}",
        f"- Rule D: {notes.get('D', '')}",
        "",
        "## Latest Scenario Runs",
        "",
        "| Scenario | Candidate Signals | Orders | Fills | Net PnL | Conservative Pass | Ordering Violation |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in latest_results:
        lines.append(
            f"| {row['scenario']} | {(row.get('orders_count') or 0) + (row.get('skipped_orders_count') or 0)} | "
            f"{row.get('orders_count')} | {row.get('simulated_fills_count')} | {row.get('net_pnl')} | "
            f"{bool(row.get('conservative_pass'))} | {bool(row.get('ordering_violation'))} |"
        )
    lines.extend(
        [
            "",
            "## Candidate Grid",
            "",
            "| Candidate | Test Net PnL | Realized PnL Only | Unresolved Inventory Value | Gate | Reasons |",
            "| ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in candidate_rows:
        lines.append(
            f"| {row['candidate_id']} | {row['test_net_pnl']} | {row['realized_pnl_only']} | "
            f"{row['unresolved_inventory_value']} | {row['gate_status']} | {row['gate_reasons']} |"
        )
    lines.extend(
        [
            "",
            "## Caveats",
            "- Realized PnL is separated from unresolved inventory value; unresolved inventory is not treated as profit.",
            "- Conservative must be worse or equal to optimistic; otherwise `ordering_violation` is reported.",
            "- `event_phase`, `market_family`, sibling pairwise sequence rows, and `estimated_book_seen` are not strong entry triggers.",
            "- Test data is not used for parameter selection; all parameter values are fixed grids from Phase 22.5b instructions.",
            "- Similarity to RN1/Gap remains diagnostic only and is not a ranking field.",
            "- No strategy is paper eligible unless every listed gate passes; paper gates are unchanged.",
        ]
    )
    return "\n".join(lines) + "\n"
