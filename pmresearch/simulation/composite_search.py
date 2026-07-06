"""Phase 22.4 progressive composite strategy search."""

from __future__ import annotations

import csv
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from .engine import (
    GAP_WALLET,
    RN1_WALLET,
    DecisionContext,
    PortfolioState,
    RuleDecision,
    _has_ordering_violation,
    _scenario_adjusted_order_price,
)
from .risk import RiskLimits, check_all_risks
from .scenarios import ALL_SCENARIOS, ScenarioConfig, SimOrder, decide_fill
from .search import split_rows_by_time

COMPOSITE_REPORT_FILENAME = "composite_search_report.md"
COMPOSITE_CANDIDATES_FILENAME = "composite_candidates.csv"
COMPOSITE_TOP_FILENAME = "composite_top.csv"
FORWARD_WATCH_FILENAME = "forward_watch_candidates.csv"
COMPONENT_EFFECTIVENESS_REPORT_FILENAME = "component_effectiveness_report.md"
COMPONENT_EFFECTIVENESS_FILENAME = "component_effectiveness.csv"
PER_CANDIDATE_EVENT_PNL_FILENAME = "per_candidate_event_pnl.csv"
SKIPPED_BY_COMPONENT_FILENAME = "skipped_by_component.csv"
COMPONENT_CONTRIBUTION_FILENAME = "component_contribution_report.md"
EVENT_ROBUSTNESS_FILENAME = "event_robustness_report.md"

_ZERO = Decimal("0")
_ONE = Decimal("1")
_FEE_RATE = Decimal("0.002")
_SPLITS = ("train", "validation", "test")

@dataclass(frozen=True)
class ComponentSpec:
    name: str
    family: str
    kind: str
    parameters: dict[str, object] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.family}:{self.name}:{json.dumps(self.parameters, sort_keys=True)}"

    @property
    def label(self) -> str:
        if not self.parameters:
            return self.name
        params = ",".join(f"{k}={v}" for k, v in sorted(self.parameters.items()))
        return f"{self.name}({params})"


@dataclass
class CompositeCandidate:
    candidate_id: int
    stage: int
    components: tuple[ComponentSpec, ...]
    parent_id: Optional[int] = None
    promoted: bool = False
    rank_index: Optional[int] = None
    metrics: dict[str, "CompositeMetric"] = field(default_factory=dict)
    validation_score: Decimal = _ZERO
    final_status: str = "REJECTED_LOSS"
    rn1_similarity_score: Decimal = _ZERO
    gap_similarity_score: Decimal = _ZERO
    complete_set_mechanism_score: Decimal = _ZERO
    execution_similarity_score: Decimal = _ZERO

    @property
    def component_count(self) -> int:
        return len(self.components)

    @property
    def component_keys(self) -> tuple[str, ...]:
        return tuple(component.key for component in self.components)

    @property
    def component_labels(self) -> list[str]:
        return [component.label for component in self.components]


@dataclass(frozen=True)
class CompositeMetric:
    split_name: str
    candidate_signals_count: int
    accepted_orders_count: int
    skipped_orders_count: int
    simulated_fills_count: int
    events_count: int
    fill_rate_on_candidates: Optional[Decimal]
    net_pnl: Decimal
    roi_on_capital: Decimal
    max_drawdown: Decimal
    max_event_loss: Decimal
    capital_required: Decimal
    turnover: Decimal
    capital_recycling: Decimal
    risk_breaches: int
    risk_prevented_count: int
    concentration: Decimal
    score: Decimal
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    event_rows: tuple[dict[str, object], ...] = ()
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


@dataclass
class CompositeSearchResult:
    candidates: list[CompositeCandidate]
    ranked_candidates: list[CompositeCandidate]
    selected_candidate: Optional[CompositeCandidate]
    elapsed_ms: int
    evaluated_candidates: int
    max_candidates: int
    max_components: int
    seed: int
    capital_mode: str
    max_capital: Decimal
    max_order_size: Decimal
    min_events: int
    min_fills: int
    wallet_scope: str
    strategy_family: str = "composite"


@dataclass
class _CompositeState:
    prior_event_rows: dict[str, int] = field(default_factory=dict)
    prior_event_conditions: dict[str, set[str]] = field(default_factory=dict)

    def observe(self, ctx: DecisionContext) -> None:
        event_key = _event_key(ctx)
        self.prior_event_rows[event_key] = self.prior_event_rows.get(event_key, 0) + 1
        if ctx.condition_id:
            self.prior_event_conditions.setdefault(event_key, set()).add(ctx.condition_id)


@dataclass
class _TransientCompositeResult:
    metric: CompositeMetric
    ordering_violation: bool


def run_progressive_composite_search(
    session: Session,
    *,
    max_components: int,
    max_candidates: int,
    seed: int,
    capital_mode: str,
    max_capital,
    max_order_size,
    min_events: int,
    min_fills: int,
    wallet: str = "all",
    strategy_family: str = "composite",
    progress_callback: Optional[Callable[[str, int, int, int, int, float], None]] = None,
) -> CompositeSearchResult:
    """Run Phase 22.4 progressive search and keep test as final holdout only."""
    if max_components <= 0:
        raise ValueError("--max-components must be positive")
    if max_candidates <= 0:
        raise ValueError("--max-candidates must be positive")
    if min_events <= 0:
        raise ValueError("--min-events must be positive")
    if min_fills <= 0:
        raise ValueError("--min-fills must be positive")
    capital_mode = capital_mode.lower()
    if capital_mode not in {"small", "scaled"}:
        raise ValueError("--capital-mode must be small or scaled")
    strategy_family = strategy_family.lower()
    if strategy_family not in {"composite", "event_inventory_cycling"}:
        raise ValueError("--strategy-family must be composite or event_inventory_cycling")

    max_capital_d = Decimal(str(max_capital))
    max_order_size_d = Decimal(str(max_order_size))
    rows = _load_rows(session, wallet)
    if len(rows) < 3:
        raise ValueError(f"Need at least 3 dataset rows for train/validation/test split; found {len(rows)}")
    splits = split_rows_by_time(rows)

    rng = random.Random(seed)
    filter_components = _filter_components()
    rng.shuffle(filter_components)
    base_components = _base_components(strategy_family=strategy_family)
    rng.shuffle(base_components)

    started = time.monotonic()
    evaluated: list[CompositeCandidate] = []
    promoted: list[CompositeCandidate] = []
    candidates_by_id: dict[int, CompositeCandidate] = {}
    seen: set[tuple[str, ...]] = set()
    next_id = 1

    stage_one = [
        CompositeCandidate(candidate_id=idx + 1, stage=1, components=(component,))
        for idx, component in enumerate(base_components)
    ]
    next_id = len(stage_one) + 1

    current_stage = stage_one
    for stage in range(1, max_components + 1):
        if not current_stage or len(evaluated) >= max_candidates:
            break
        stage_results: list[CompositeCandidate] = []
        for candidate in current_stage:
            if len(evaluated) >= max_candidates:
                break
            key = candidate.component_keys
            if key in seen:
                continue
            seen.add(key)
            _evaluate_candidate(
                candidate,
                splits=splits,
                max_capital=max_capital_d,
                max_order_size=max_order_size_d,
                capital_mode=capital_mode,
                min_events=min_events,
                min_fills=min_fills,
            )
            parent = candidates_by_id.get(candidate.parent_id or -1)
            if parent is not None:
                _apply_complexity_gate(candidate, parent)
            evaluated.append(candidate)
            candidates_by_id[candidate.candidate_id] = candidate
            stage_results.append(candidate)
            if progress_callback is not None:
                progress_callback(
                    "evaluate",
                    len(evaluated),
                    max_candidates,
                    stage,
                    len(promoted),
                    time.monotonic() - started,
                )

        stage_promoted = _promote_stage(stage_results, min_events=min_events, min_fills=min_fills)
        promoted.extend(stage_promoted)
        if progress_callback is not None:
            progress_callback(
                "promote",
                len(evaluated),
                max_candidates,
                stage,
                len(promoted),
                time.monotonic() - started,
            )

        if stage >= max_components or len(evaluated) >= max_candidates:
            break
        current_stage = []
        for parent in stage_promoted[: max(4, min(24, max_candidates // 4 or 1))]:
            for component in filter_components:
                if component.family in {c.family for c in parent.components}:
                    continue
                child_components = parent.components + (component,)
                if len(child_components) > max_components:
                    continue
                current_stage.append(
                    CompositeCandidate(
                        candidate_id=next_id,
                        stage=stage + 1,
                        components=child_components,
                        parent_id=parent.candidate_id,
                    )
                )
                next_id += 1
        rng.shuffle(current_stage)

    ranked = _rank_candidates(evaluated, min_events=min_events, min_fills=min_fills)
    for rank, candidate in enumerate(ranked, start=1):
        candidate.rank_index = rank
    selected = ranked[0] if ranked else None
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return CompositeSearchResult(
        candidates=evaluated,
        ranked_candidates=ranked,
        selected_candidate=selected,
        elapsed_ms=elapsed_ms,
        evaluated_candidates=len(evaluated),
        max_candidates=max_candidates,
        max_components=max_components,
        seed=seed,
        capital_mode=capital_mode,
        max_capital=max_capital_d,
        max_order_size=max_order_size_d,
        min_events=min_events,
        min_fills=min_fills,
        wallet_scope=wallet,
        strategy_family=strategy_family,
    )


def write_composite_outputs(result: CompositeSearchResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_candidates_csv(result.candidates, out_dir / COMPOSITE_CANDIDATES_FILENAME, max_capital=result.max_capital)
    _write_candidates_csv(result.ranked_candidates[:25], out_dir / COMPOSITE_TOP_FILENAME, max_capital=result.max_capital)
    _write_forward_watch_csv(result, out_dir / FORWARD_WATCH_FILENAME)
    diagnostic_candidates = _component_effectiveness_candidates(result)
    _write_component_effectiveness_csv(result, diagnostic_candidates, out_dir / COMPONENT_EFFECTIVENESS_FILENAME)
    _write_per_candidate_event_pnl_csv(diagnostic_candidates, out_dir / PER_CANDIDATE_EVENT_PNL_FILENAME)
    _write_skipped_by_component_csv(diagnostic_candidates, out_dir / SKIPPED_BY_COMPONENT_FILENAME)
    (out_dir / COMPONENT_EFFECTIVENESS_REPORT_FILENAME).write_text(
        _render_component_effectiveness_report(result, diagnostic_candidates),
        encoding="utf-8",
    )
    (out_dir / COMPOSITE_REPORT_FILENAME).write_text(_render_search_report(result), encoding="utf-8")
    (out_dir / COMPONENT_CONTRIBUTION_FILENAME).write_text(
        _render_component_contribution_report(result),
        encoding="utf-8",
    )
    (out_dir / EVENT_ROBUSTNESS_FILENAME).write_text(
        _render_event_robustness_report(result),
        encoding="utf-8",
    )


def _evaluate_candidate(
    candidate: CompositeCandidate,
    *,
    splits: dict[str, list[dict]],
    max_capital: Decimal,
    max_order_size: Decimal,
    capital_mode: str,
    min_events: int,
    min_fills: int,
) -> None:
    risk_limits = _risk_limits(max_capital=max_capital, max_order_size=max_order_size, mode=capital_mode)
    scenario = _scenario(max_order_size=max_order_size, mode=capital_mode)
    metrics: dict[str, CompositeMetric] = {}
    ordering_violations = False
    for split_name in _SPLITS:
        conservative = _simulate_composite(
            splits[split_name],
            candidate.components,
            scenario,
            risk_limits,
            split_name=split_name,
        )
        optimistic = _simulate_composite(
            splits[split_name],
            candidate.components,
            ALL_SCENARIOS["optimistic"],
            risk_limits,
            split_name=split_name,
            collect_events=False,
        )
        ordering_violation = _has_ordering_violation_metric(conservative.metric, optimistic.metric)
        ordering_violations = ordering_violations or ordering_violation
        metrics[split_name] = conservative.metric
    candidate.metrics = metrics
    candidate.rn1_similarity_score = _rn1_similarity(candidate.components)
    candidate.gap_similarity_score = _gap_similarity(candidate.components)
    candidate.complete_set_mechanism_score = _complete_set_score(candidate.components)
    candidate.execution_similarity_score = _execution_similarity(candidate.components, max_order_size)
    candidate.validation_score = _selection_score(candidate, min_events=min_events, min_fills=min_fills)
    candidate.final_status = _final_status(
        candidate,
        ordering_violation=ordering_violations,
        max_capital=max_capital,
        capital_mode=capital_mode,
        min_events=min_events,
        min_fills=min_fills,
    )


def _simulate_composite(
    rows: list[dict],
    components: tuple[ComponentSpec, ...],
    scenario: ScenarioConfig,
    risk_limits: RiskLimits,
    *,
    split_name: str,
    collect_events: bool = True,
) -> _TransientCompositeResult:
    base = next((component for component in components if component.kind == "base"), None)
    if base is not None and base.name == "event_inventory_cycling_v1":
        return _simulate_inventory_composite(
            rows,
            components,
            scenario,
            risk_limits,
            split_name=split_name,
            collect_events=collect_events,
        )
    portfolio = PortfolioState()
    history = _CompositeState()
    event_pnl: dict[str, Decimal] = {}
    event_fills: dict[str, int] = {}
    event_turnover: dict[str, Decimal] = {}
    event_max_exposure: dict[str, Decimal] = {}
    day_start_values: dict[str, Decimal] = {}
    stopped_days: set[str] = set()
    rule_fires = 0
    orders_count = 0
    fills_count = 0
    skipped_orders_count = 0
    risk_prevented_count = 0
    risk_breaches = 0
    fill_seq = 0
    skipped_by_reason: dict[str, int] = {}

    for row in rows:
        ctx = DecisionContext.from_row(row)
        mid = ctx.decimal("mid_before")
        portfolio.mark(ctx.token_id, ctx.condition_id, mid)
        day = _ts_to_date(ctx.trade_ts)
        day_start_values.setdefault(day, portfolio.value())

        decision = _composite_decision(components, ctx, portfolio, history, scenario)
        history.observe(ctx)
        if not decision.applies or decision.order is None:
            if "filter rejected" in decision.explanation:
                reason = f"component:{decision.explanation.split(' filter rejected', 1)[0]}"
                skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
                skipped_orders_count += 1
            continue

        rule_fires += 1
        if day in stopped_days:
            skipped_orders_count += 1
            risk_prevented_count += 1
            continue

        risk_reason = _risk_skip_reason(
            portfolio=portfolio,
            ctx=ctx,
            order=decision.order,
            scenario=scenario,
            limits=risk_limits,
            day_start_value=day_start_values[day],
        )
        if risk_reason is not None:
            skipped_by_reason[risk_reason] = skipped_by_reason.get(risk_reason, 0) + 1
            skipped_orders_count += 1
            risk_prevented_count += 1
            if risk_reason == "max_daily_loss":
                stopped_days.add(day)
            continue

        orders_count += 1
        if _is_stale(ctx, risk_limits):
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

        fee = portfolio.apply_fill(
            token_id=ctx.token_id,
            condition_id=ctx.condition_id,
            side=decision.order.side,
            price=assumption.fill_price,
            size=assumption.fill_size,
            mark_price=mid,
        )
        notional = assumption.fill_price * assumption.fill_size
        event_key = _event_key(ctx)
        event_pnl[event_key] = event_pnl.get(event_key, _ZERO)
        if decision.order.side == "BUY":
            event_pnl[event_key] -= notional + fee
        else:
            event_pnl[event_key] += notional - fee
        event_turnover[event_key] = event_turnover.get(event_key, _ZERO) + notional
        event_fills[event_key] = event_fills.get(event_key, 0) + 1
        event_exposure = portfolio.event_exposure(ctx.condition_id)
        event_max_exposure[event_key] = max(event_max_exposure.get(event_key, _ZERO), event_exposure)
        fills_count += 1

        pos = portfolio.position(ctx.token_id, ctx.condition_id)
        daily_pnl = portfolio.value() - day_start_values[day]
        breaches = check_all_risks(
            token_id=ctx.token_id,
            qty_token=pos.qty,
            daily_pnl=daily_pnl,
            capital_used=portfolio.exposure(),
            event_exposure=event_exposure,
            condition_id=ctx.condition_id,
            limits=risk_limits,
            ts=ctx.trade_ts,
        )
        risk_breaches += len(breaches)

    for pos in portfolio.positions.values():
        key = pos.condition_id or f"token:{pos.token_id}"
        event_pnl[key] = event_pnl.get(key, _ZERO) + pos.value()

    net_pnl = portfolio.value()
    capital_required = portfolio.max_capital_seen
    roi = net_pnl / max(capital_required, _ONE)
    max_event_loss = abs(min(event_pnl.values(), default=_ZERO))
    concentration = _concentration(event_pnl.values())
    capital_recycling = portfolio.turnover / max(capital_required, _ONE)
    complexity_penalty = Decimal("0.020") * Decimal(max(0, len(components) - 1))
    score = (
        roi
        - (portfolio.max_drawdown_seen / max(capital_required, _ONE))
        - (max_event_loss / max(capital_required, _ONE) * Decimal("0.60"))
        - (concentration * Decimal("0.25"))
        + min(capital_recycling, Decimal("5")) * Decimal("0.015")
        - complexity_penalty
    )
    event_rows: tuple[dict[str, object], ...] = ()
    if collect_events:
        event_rows = tuple(
            {
                "split_name": split_name,
                "event_id": event_id,
                "total_pnl": str(total_pnl),
                "fills_count": event_fills.get(event_id, 0),
                "turnover": str(event_turnover.get(event_id, _ZERO)),
                "max_event_exposure": str(event_max_exposure.get(event_id, _ZERO)),
            }
            for event_id, total_pnl in sorted(event_pnl.items())
            if event_fills.get(event_id, 0) > 0
        )
    metric = CompositeMetric(
        split_name=split_name,
        candidate_signals_count=rule_fires,
        accepted_orders_count=orders_count,
        skipped_orders_count=skipped_orders_count,
        simulated_fills_count=fills_count,
        events_count=len([event_id for event_id, count in event_fills.items() if count > 0]),
        fill_rate_on_candidates=(Decimal(fills_count) / Decimal(rule_fires) if rule_fires else None),
        net_pnl=net_pnl,
        roi_on_capital=roi,
        max_drawdown=portfolio.max_drawdown_seen,
        max_event_loss=max_event_loss,
        capital_required=capital_required,
        turnover=portfolio.turnover,
        capital_recycling=capital_recycling,
        risk_breaches=risk_breaches,
        risk_prevented_count=risk_prevented_count,
        concentration=concentration,
        score=score,
        skipped_by_reason=skipped_by_reason,
        event_rows=event_rows,
    )
    return _TransientCompositeResult(metric=metric, ordering_violation=False)


def _composite_decision(
    components: tuple[ComponentSpec, ...],
    ctx: DecisionContext,
    portfolio: PortfolioState,
    history: _CompositeState,
    scenario: ScenarioConfig,
) -> RuleDecision:
    base = next((component for component in components if component.kind == "base"), None)
    if base is None:
        return RuleDecision(False, None, "no base mechanic", {})
    decision = _base_decision(base, ctx, portfolio, scenario)
    if not decision.applies or decision.order is None:
        return decision
    for component in components:
        if component.kind == "base":
            continue
        if not _component_allows(component, ctx, portfolio, history, decision.order):
            return RuleDecision(False, None, f"{component.name} filter rejected", decision.features_used)
    return decision


def _base_decision(
    component: ComponentSpec,
    ctx: DecisionContext,
    portfolio: PortfolioState,
    scenario: ScenarioConfig,
) -> RuleDecision:
    bid = ctx.decimal("best_bid_before")
    ask = ctx.decimal("best_ask_before")
    mid = ctx.decimal("mid_before")
    features = {
        "best_bid_before": ctx.get("best_bid_before"),
        "best_ask_before": ctx.get("best_ask_before"),
        "mid_before": ctx.get("mid_before"),
        "spread_bps": ctx.get("spread_bps"),
    }
    if bid is None or ask is None:
        return RuleDecision(False, None, "missing book-before bid/ask", features)
    if component.name == "completion_set_edge":
        max_bond_cost = Decimal(str(component.parameters["max_bond_cost"]))
        total_cost = bid + ask
        if total_cost > max_bond_cost:
            return RuleDecision(False, None, f"bond cost {total_cost} > {max_bond_cost}", features)
        size = _order_size(ctx.decimal("ask_depth_top1"), scenario)
        return RuleDecision(
            True,
            SimOrder(side="BUY", order_price=bid, order_size=size, reason="completion_set_edge"),
            f"prospective bond cost {total_cost:.4f} <= {max_bond_cost}",
            features,
        )
    if component.name == "spread_capture":
        if mid in (None, _ZERO):
            return RuleDecision(False, None, "missing mid", features)
        spread = ctx.decimal("spread_bps")
        min_spread = Decimal(str(component.parameters["min_spread_bps"]))
        min_edge = Decimal(str(component.parameters["min_edge_bps"]))
        if spread is None or spread < min_spread:
            return RuleDecision(False, None, f"spread {spread} < {min_spread}", features)
        pos = portfolio.position(ctx.token_id, ctx.condition_id)
        if pos.qty > _ZERO:
            side = "SELL"
            price = ask
            depth = ctx.decimal("bid_depth_top1")
            edge_bps = (price - mid) / mid * Decimal("10000")
        else:
            side = "BUY"
            price = bid
            depth = ctx.decimal("ask_depth_top1")
            edge_bps = (mid - price) / mid * Decimal("10000")
        if edge_bps < min_edge:
            return RuleDecision(False, None, f"edge {edge_bps:.1f} bps < {min_edge}", features)
        return RuleDecision(
            True,
            SimOrder(side=side, order_price=price, order_size=_order_size(depth, scenario), reason="spread_capture"),
            f"spread {spread:.1f} bps, simulated inventory {pos.qty}",
            features,
        )
    return RuleDecision(False, None, "unsupported base mechanic", features)


def _component_allows(
    component: ComponentSpec,
    ctx: DecisionContext,
    portfolio: PortfolioState,
    history: _CompositeState,
    order: SimOrder,
) -> bool:
    if component.name == "depth_imbalance":
        min_depth = Decimal(str(component.parameters["min_depth"]))
        relevant_depth = ctx.decimal("ask_depth_top1") if order.side == "BUY" else ctx.decimal("bid_depth_top1")
        if relevant_depth is None or relevant_depth < min_depth:
            return False
        imbalance = ctx.decimal("book_imbalance_top1")
        max_adverse = Decimal(str(component.parameters["max_adverse_imbalance"]))
        if imbalance is None:
            return True
        if order.side == "BUY":
            return imbalance <= max_adverse
        return imbalance >= -max_adverse
    if component.name == "inventory_balancing":
        pos = portfolio.position(ctx.token_id, ctx.condition_id)
        max_qty = Decimal(str(component.parameters["max_abs_qty_before"]))
        return abs(pos.qty) <= max_qty
    if component.name == "sibling_market_confirmation":
        event_key = _event_key(ctx)
        min_prior_rows = int(component.parameters["min_prior_event_rows"])
        return history.prior_event_rows.get(event_key, 0) >= min_prior_rows
    if component.name == "event_phase_conditions":
        tte = ctx.decimal("time_to_event_start_s")
        if tte is None:
            return bool(component.parameters.get("allow_unknown", True))
        return tte <= Decimal(str(component.parameters["max_time_to_start_s"]))
    if component.name == "market_family_conditions":
        category = str(ctx.get("market_category", "") or "").lower()
        allowed = {str(v).lower() for v in component.parameters["allowed_categories"]}
        return category in allowed
    if component.name == "price_bucket_filters":
        mid = ctx.decimal("mid_before")
        if mid is None:
            return False
        return Decimal(str(component.parameters["min_mid"])) <= mid <= Decimal(str(component.parameters["max_mid"]))
    if component.name == "book_age_context_quality_filters":
        age = ctx.integer("book_before_age_s")
        allowed_statuses = {str(v) for v in component.parameters["statuses"]}
        if ctx.context_status not in allowed_statuses:
            return False
        return age is None or age <= int(component.parameters["max_book_age_s"])
    return True


def _simulate_inventory_composite(
    rows: list[dict],
    components: tuple[ComponentSpec, ...],
    scenario: ScenarioConfig,
    risk_limits: RiskLimits,
    *,
    split_name: str,
    collect_events: bool,
) -> _TransientCompositeResult:
    from .inventory_cycling import InventoryCyclingConfig, simulate_inventory_cycling

    base = next(component for component in components if component.kind == "base")
    params = dict(base.parameters)
    config = InventoryCyclingConfig(
        max_bond_cost=Decimal(str(params.get("max_bond_cost", "0.98"))),
        min_bond_delta=Decimal(str(params.get("min_bond_delta", "0"))),
        max_unpaired_inventory=Decimal(str(params.get("max_unpaired_inventory", risk_limits.max_position_per_token))),
        max_event_exposure=risk_limits.max_event_exposure,
        max_position_per_token=risk_limits.max_position_per_token,
        max_capital=risk_limits.max_capital_deployed,
        max_order_size=risk_limits.max_order_size,
        min_merge_qty=Decimal(str(params.get("min_merge_qty", "1"))),
        auto_merge_enabled=bool(params.get("auto_merge_enabled", True)),
        recycle_capital_enabled=bool(params.get("recycle_capital_enabled", True)),
    )
    component_filter = _inventory_component_filter(components)
    transient = simulate_inventory_cycling(
        rows,
        scenario,
        risk_limits,
        config=config,
        resolution_prices={},
        collect_details=collect_events,
        pre_order_filter=component_filter,
    )
    lifecycle = getattr(transient, "lifecycle_metrics", None)
    event_pnl: dict[str, Decimal] = {}
    event_fills: dict[str, int] = {}
    event_turnover: dict[str, Decimal] = {}
    for fill in transient.fills:
        key = str(fill.get("condition_id") or fill.get("event_id"))
        notional = _decimal(fill.get("fill_notional_usdc"))
        fee = _decimal(fill.get("estimated_fee"))
        event_pnl[key] = event_pnl.get(key, _ZERO) - notional - fee
        event_turnover[key] = event_turnover.get(key, _ZERO) + notional
        event_fills[key] = event_fills.get(key, 0) + 1
    for event in getattr(transient, "lifecycle_events", []):
        key = str(event.get("condition_id") or "")
        event_pnl[key] = event_pnl.get(key, _ZERO) + _decimal(event.get("usdc_delta"))
    capital_required = transient.capital_required
    roi = transient.net_pnl / max(capital_required, _ONE)
    max_event_loss = abs(min(event_pnl.values(), default=_ZERO))
    concentration = _concentration(event_pnl.values())
    capital_recycling = transient.turnover / max(capital_required, _ONE)
    complexity_penalty = Decimal("0.020") * Decimal(max(0, len(components) - 1))
    score = (
        roi
        - (transient.max_drawdown / max(capital_required, _ONE))
        - (max_event_loss / max(capital_required, _ONE) * Decimal("0.60"))
        - (concentration * Decimal("0.25"))
        + min(capital_recycling, Decimal("5")) * Decimal("0.015")
        - complexity_penalty
    )
    event_rows = tuple(
        {
            "split_name": split_name,
            "event_id": event_id,
            "total_pnl": str(total_pnl),
            "fills_count": event_fills.get(event_id, 0),
            "turnover": str(event_turnover.get(event_id, _ZERO)),
            "max_event_exposure": str(getattr(transient, "max_event_exposure_seen", _ZERO)),
        }
        for event_id, total_pnl in sorted(event_pnl.items())
        if event_fills.get(event_id, 0) > 0
    )
    metric = CompositeMetric(
        split_name=split_name,
        candidate_signals_count=transient.candidate_signals_count,
        accepted_orders_count=transient.orders_count,
        skipped_orders_count=transient.skipped_orders_count,
        simulated_fills_count=transient.fills_count,
        events_count=len([event_id for event_id, count in event_fills.items() if count > 0]),
        fill_rate_on_candidates=transient.fill_rate,
        net_pnl=transient.net_pnl,
        roi_on_capital=roi,
        max_drawdown=transient.max_drawdown,
        max_event_loss=max_event_loss,
        capital_required=capital_required,
        turnover=transient.turnover,
        capital_recycling=capital_recycling,
        risk_breaches=transient.risk_breaches,
        risk_prevented_count=transient.risk_prevented_count,
        concentration=concentration,
        score=score,
        skipped_by_reason=dict(transient.skipped_by_reason),
        event_rows=event_rows,
        merge_count=lifecycle.merge_count if lifecycle else 0,
        merged_qty=lifecycle.merged_qty if lifecycle else _ZERO,
        released_capital_total=lifecycle.released_capital_total if lifecycle else _ZERO,
        capital_recycled_total=lifecycle.capital_recycled_total if lifecycle else _ZERO,
        redeem_count=lifecycle.redeem_count if lifecycle else 0,
        redeem_pnl=lifecycle.redeem_pnl if lifecycle else _ZERO,
        trading_pnl=lifecycle.trading_pnl if lifecycle else transient.net_pnl,
        merge_pnl=lifecycle.merge_pnl if lifecycle else _ZERO,
        unresolved_inventory_value=lifecycle.unresolved_inventory_value if lifecycle else _ZERO,
        max_unpaired_inventory=lifecycle.max_unpaired_inventory if lifecycle else transient.max_inventory,
        avg_unpaired_inventory=lifecycle.avg_unpaired_inventory if lifecycle else _ZERO,
        bond_inventory_created=lifecycle.bond_inventory_created if lifecycle else _ZERO,
        bond_inventory_merged=lifecycle.bond_inventory_merged if lifecycle else _ZERO,
        capital_turnover_ratio=lifecycle.capital_turnover_ratio if lifecycle else _ZERO,
    )
    return _TransientCompositeResult(metric=metric, ordering_violation=False)


def _inventory_component_filter(components: tuple[ComponentSpec, ...]):
    extra_components = [component for component in components if component.kind != "base"]
    if not extra_components:
        return None
    prior_event_rows: dict[str, int] = {}

    def check(ctx, state, order) -> Optional[str]:
        event_key = _event_key(ctx)
        try:
            for component in extra_components:
                if not _inventory_component_allows(component, ctx, state, order, prior_event_rows):
                    return f"component:{component.name}"
            return None
        finally:
            prior_event_rows[event_key] = prior_event_rows.get(event_key, 0) + 1

    return check


def _inventory_component_allows(
    component: ComponentSpec,
    ctx: DecisionContext,
    state,
    order: SimOrder,
    prior_event_rows: dict[str, int],
) -> bool:
    if component.name == "depth_imbalance":
        min_depth = Decimal(str(component.parameters["min_depth"]))
        relevant_depth = ctx.decimal("ask_depth_top1") if order.side == "BUY" else ctx.decimal("bid_depth_top1")
        if relevant_depth is None or relevant_depth < min_depth:
            return False
        imbalance = ctx.decimal("book_imbalance_top1")
        max_adverse = Decimal(str(component.parameters["max_adverse_imbalance"]))
        if imbalance is None:
            return True
        return imbalance <= max_adverse if order.side == "BUY" else imbalance >= -max_adverse
    if component.name == "inventory_balancing":
        condition_id = ctx.condition_id or f"token:{ctx.token_id}"
        max_qty = Decimal(str(component.parameters["max_abs_qty_before"]))
        return abs(state.lot(condition_id, ctx.token_id).qty) <= max_qty
    if component.name == "sibling_market_confirmation":
        min_prior_rows = int(component.parameters["min_prior_event_rows"])
        return prior_event_rows.get(_event_key(ctx), 0) >= min_prior_rows
    if component.name == "event_phase_conditions":
        tte = ctx.decimal("time_to_event_start_s")
        if tte is None:
            return bool(component.parameters.get("allow_unknown", True))
        return tte <= Decimal(str(component.parameters["max_time_to_start_s"]))
    if component.name == "market_family_conditions":
        category = str(ctx.get("market_category", "") or "").lower()
        allowed = {str(v).lower() for v in component.parameters["allowed_categories"]}
        return category in allowed
    if component.name == "price_bucket_filters":
        mid = ctx.decimal("mid_before")
        if mid is None:
            return False
        return Decimal(str(component.parameters["min_mid"])) <= mid <= Decimal(str(component.parameters["max_mid"]))
    if component.name == "book_age_context_quality_filters":
        age = ctx.integer("book_before_age_s")
        allowed_statuses = {str(v) for v in component.parameters["statuses"]}
        if ctx.context_status not in allowed_statuses:
            return False
        return age is None or age <= int(component.parameters["max_book_age_s"])
    return True


def _base_components(*, strategy_family: str = "composite") -> list[ComponentSpec]:
    if strategy_family == "event_inventory_cycling":
        return [
            ComponentSpec(
                "event_inventory_cycling_v1",
                "event_inventory_cycling",
                "base",
                {
                    "max_bond_cost": "0.98",
                    "min_bond_delta": "0",
                    "max_unpaired_inventory": "100",
                    "min_merge_qty": "1",
                    "auto_merge_enabled": True,
                    "recycle_capital_enabled": True,
                },
            )
        ]
    return [
        ComponentSpec("completion_set_edge", "completion_set_edge", "base", {"max_bond_cost": value})
        for value in ("0.96", "0.98", "0.99")
    ] + [
        ComponentSpec(
            "spread_capture",
            "spread_capture",
            "base",
            {"min_spread_bps": spread, "min_edge_bps": edge},
        )
        for spread in ("50", "100", "200")
        for edge in ("10", "25")
    ]


def _filter_components() -> list[ComponentSpec]:
    return [
        ComponentSpec("depth_imbalance", "depth_imbalance", "filter", {"min_depth": "10", "max_adverse_imbalance": "0.75"}),
        ComponentSpec("depth_imbalance", "depth_imbalance", "filter", {"min_depth": "25", "max_adverse_imbalance": "0.50"}),
        ComponentSpec("inventory_balancing", "inventory_balancing", "filter", {"max_abs_qty_before": "50"}),
        ComponentSpec("inventory_balancing", "inventory_balancing", "filter", {"max_abs_qty_before": "150"}),
        ComponentSpec("sibling_market_confirmation", "sibling_market_confirmation", "filter", {"min_prior_event_rows": 1}),
        ComponentSpec("sibling_market_confirmation", "sibling_market_confirmation", "filter", {"min_prior_event_rows": 2}),
        ComponentSpec("event_phase_conditions", "event_phase_conditions", "filter", {"max_time_to_start_s": "86400", "allow_unknown": True}),
        ComponentSpec("event_phase_conditions", "event_phase_conditions", "filter", {"max_time_to_start_s": "7200", "allow_unknown": True}),
        ComponentSpec("market_family_conditions", "market_family_conditions", "filter", {"allowed_categories": ["sports"]}),
        ComponentSpec("price_bucket_filters", "price_bucket_filters", "filter", {"min_mid": "0.10", "max_mid": "0.90"}),
        ComponentSpec("price_bucket_filters", "price_bucket_filters", "filter", {"min_mid": "0.20", "max_mid": "0.80"}),
        ComponentSpec("book_age_context_quality_filters", "book_age_context_quality_filters", "filter", {"max_book_age_s": 30, "statuses": ["excellent", "good", "usable"]}),
        ComponentSpec("book_age_context_quality_filters", "book_age_context_quality_filters", "filter", {"max_book_age_s": 15, "statuses": ["excellent", "good"]}),
    ]


def _risk_limits(*, max_capital: Decimal, max_order_size: Decimal, mode: str) -> RiskLimits:
    if mode == "small":
        return RiskLimits(
            max_position_per_token=max(max_order_size * Decimal("5"), Decimal("5")),
            max_event_exposure=max_capital * Decimal("0.35"),
            max_daily_loss=max_capital * Decimal("0.15"),
            max_capital_deployed=max_capital,
            max_order_size=max_order_size,
            max_stale_book_age_s=30,
        )
    return RiskLimits(
        max_position_per_token=max(max_order_size * Decimal("12"), Decimal("10")),
        max_event_exposure=max_capital * Decimal("0.60"),
        max_daily_loss=max_capital * Decimal("0.25"),
        max_capital_deployed=max_capital,
        max_order_size=max_order_size,
        max_stale_book_age_s=45,
    )


def _scenario(*, max_order_size: Decimal, mode: str) -> ScenarioConfig:
    base = ALL_SCENARIOS["conservative"]
    depth_fraction = Decimal("0.25") if mode == "small" else Decimal("0.35")
    return ScenarioConfig(
        name=base.name,
        description=base.description,
        fill_rate_multiplier=base.fill_rate_multiplier,
        slippage_bps=base.slippage_bps,
        max_order_size=min(base.max_order_size, max_order_size),
        depth_fraction=depth_fraction,
        min_spread_bps_for_fill=base.min_spread_bps_for_fill,
        min_book_depth_for_fill=base.min_book_depth_for_fill,
        max_book_age_s=base.max_book_age_s,
        min_context_status=base.min_context_status,
        risk_limit_multiplier=base.risk_limit_multiplier,
    )


def _risk_skip_reason(
    *,
    portfolio: PortfolioState,
    ctx: DecisionContext,
    order: SimOrder,
    scenario: ScenarioConfig,
    limits: RiskLimits,
    day_start_value: Decimal,
) -> Optional[str]:
    if order.order_size > limits.max_order_size:
        return "max_order_size"
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
    if abs(projected.qty_token) > limits.max_position_per_token:
        return "max_position_per_token"
    if abs(projected.event_exposure) > limits.max_event_exposure:
        return "max_event_exposure"
    if projected.capital_used > limits.max_capital_deployed:
        return "max_capital_deployed"
    projected_daily_pnl = projected.value - day_start_value
    if projected_daily_pnl < _ZERO and abs(projected_daily_pnl) > limits.max_daily_loss:
        return "max_daily_loss"
    return None


def _order_size(depth: Optional[Decimal], scenario: ScenarioConfig) -> Decimal:
    if depth is None or depth <= _ZERO:
        return scenario.max_order_size
    return max(Decimal("1"), min(scenario.max_order_size, depth * scenario.depth_fraction))


def _is_stale(ctx: DecisionContext, limits: RiskLimits) -> bool:
    if ctx.context_status in {"missing", "stale"}:
        return True
    age = ctx.integer("book_before_age_s")
    return bool(age is not None and age > limits.max_stale_book_age_s)


def _selection_score(candidate: CompositeCandidate, *, min_events: int, min_fills: int) -> Decimal:
    train = candidate.metrics["train"]
    validation = candidate.metrics["validation"]
    if not _split_gate(train, min_events=min_events, min_fills=min_fills):
        return Decimal("-999")
    if not _split_gate(validation, min_events=min_events, min_fills=min_fills):
        return Decimal("-999")
    instability = abs(train.roi_on_capital - validation.roi_on_capital)
    robustness = min(train.score, validation.score)
    return robustness - instability


def _split_gate(metric: CompositeMetric, *, min_events: int, min_fills: int) -> bool:
    return (
        metric.net_pnl > _ZERO
        and metric.risk_breaches == 0
        and metric.simulated_fills_count >= min_fills
        and metric.events_count >= min_events
        and metric.capital_required > _ZERO
    )


def _promote_stage(
    candidates: list[CompositeCandidate],
    *,
    min_events: int,
    min_fills: int,
) -> list[CompositeCandidate]:
    eligible = [
        candidate
        for candidate in candidates
        if _pretest_gate(candidate, min_events=min_events, min_fills=min_fills)
    ]
    promoted = sorted(eligible, key=lambda c: (-c.validation_score, c.component_count, c.candidate_id))
    for candidate in promoted[:24]:
        candidate.promoted = True
    return promoted[:24]


def _apply_complexity_gate(candidate: CompositeCandidate, parent: CompositeCandidate) -> None:
    """Reject added complexity unless validation improves after all penalties."""
    material_improvement = Decimal("0.015")
    if candidate.validation_score < parent.validation_score + material_improvement:
        candidate.final_status = "REJECTED_TOO_COMPLEX"


def _rank_candidates(candidates: list[CompositeCandidate], *, min_events: int, min_fills: int) -> list[CompositeCandidate]:
    eligible = [
        candidate
        for candidate in candidates
        if _pretest_gate(candidate, min_events=min_events, min_fills=min_fills)
        and candidate.final_status != "REJECTED_TOO_COMPLEX"
    ]
    return sorted(
        eligible,
        key=lambda c: (
            -c.validation_score,
            c.metrics["validation"].max_drawdown,
            c.metrics["validation"].max_event_loss,
            c.component_count,
            c.candidate_id,
        ),
    )


def _pretest_gate(candidate: CompositeCandidate, *, min_events: int, min_fills: int) -> bool:
    if not candidate.metrics:
        return False
    train = candidate.metrics["train"]
    validation = candidate.metrics["validation"]
    if not _split_gate(train, min_events=min_events, min_fills=min_fills):
        return False
    if not _split_gate(validation, min_events=min_events, min_fills=min_fills):
        return False
    if train.concentration > Decimal("0.80") or validation.concentration > Decimal("0.80"):
        return False
    return True


def _final_status(
    candidate: CompositeCandidate,
    *,
    ordering_violation: bool,
    max_capital: Decimal,
    capital_mode: str,
    min_events: int,
    min_fills: int,
) -> str:
    train = candidate.metrics["train"]
    validation = candidate.metrics["validation"]
    test = candidate.metrics["test"]
    if ordering_violation:
        return "TEST_FAIL_HARD"
    for metric in (train, validation):
        if metric.events_count < min_events:
            return "REJECTED_TOO_FEW_EVENTS"
        if metric.simulated_fills_count < min_fills:
            return "REJECTED_TOO_FEW_FILLS"
        if metric.risk_breaches or metric.capital_required > max_capital:
            return "REJECTED_RISK"
        if metric.concentration > Decimal("0.80"):
            return "REJECTED_TOO_CONCENTRATED"
    if train.net_pnl <= _ZERO or validation.net_pnl <= _ZERO:
        return "REJECTED_LOSS"

    hard_loss_floor = -_edge_bands(max_capital)["1pct"]
    test_loss_material = test.net_pnl < hard_loss_floor or test.roi_on_capital < Decimal("-0.010")
    test_risk_bad = (
        test.risk_breaches > 0
        or test.max_event_loss > max_capital * Decimal("0.20")
        or test.max_drawdown > max_capital * Decimal("0.25")
        or test.concentration > Decimal("0.85")
    )
    if test_loss_material or test_risk_bad:
        return "TEST_FAIL_HARD"
    if test.events_count < min_events:
        return "REJECTED_TOO_FEW_EVENTS"
    if test.simulated_fills_count < min_fills:
        return "REJECTED_TOO_FEW_FILLS"

    if test.net_pnl < _ZERO:
        return "WEAK_RESEARCH_CANDIDATE"

    test_roi_floor_paper = Decimal("0.030") if capital_mode == "small" else Decimal("0.020")
    drawdown_ok = test.max_drawdown <= max(test.capital_required, _ONE) * Decimal("0.20")
    event_loss_ok = test.max_event_loss <= max_capital * Decimal("0.10")
    concentration_ok = test.concentration <= Decimal("0.60")
    degradation_ok = _degradation_pct(validation.net_pnl, test.net_pnl) <= Decimal("0.75")
    if (
        test.net_pnl > _ZERO
        and test.roi_on_capital >= test_roi_floor_paper
        and drawdown_ok
        and event_loss_ok
        and concentration_ok
        and degradation_ok
    ):
        return "ELIGIBLE_PAPER"

    weak_floor = max_capital * Decimal("0.005")
    if test.net_pnl <= weak_floor or test.roi_on_capital < Decimal("0.010"):
        return "WEAK_RESEARCH_CANDIDATE"
    return "FORWARD_WATCH_CANDIDATE"


def _reason_not_paper(candidate: CompositeCandidate, *, max_capital: Decimal, min_events: int, min_fills: int) -> str:
    if candidate.final_status == "ELIGIBLE_PAPER":
        return ""
    test = candidate.metrics["test"]
    validation = candidate.metrics["validation"]
    reasons = []
    if candidate.final_status == "REJECTED_TOO_FEW_EVENTS" or test.events_count < min_events:
        reasons.append("sample: too few test events")
    if candidate.final_status == "REJECTED_TOO_FEW_FILLS" or test.simulated_fills_count < min_fills:
        reasons.append("sample: too few test fills")
    if candidate.final_status in {"REJECTED_RISK", "TEST_FAIL_HARD"} and test.risk_breaches > 0:
        reasons.append("risk: test risk breach")
    if test.net_pnl < -_edge_bands(max_capital)["1pct"]:
        reasons.append("economic: material negative test PnL")
    if test.max_event_loss > max_capital * Decimal("0.20"):
        reasons.append("risk: max event loss too high")
    if test.max_drawdown > max_capital * Decimal("0.25"):
        reasons.append("risk: drawdown too high")
    if test.concentration > Decimal("0.85"):
        reasons.append("risk: concentration too high")
    if _degradation_pct(validation.net_pnl, test.net_pnl) > Decimal("0.75"):
        reasons.append("economic/statistical: high validation-to-test degradation")
    if test.net_pnl >= _ZERO and test.net_pnl < _edge_bands(max_capital)["3pct"]:
        reasons.append("economic: below 3% max-capital reference band")
    if candidate.final_status == "WEAK_RESEARCH_CANDIDATE" and not reasons:
        reasons.append("statistical: near breakeven holdout")
    if candidate.final_status == "FORWARD_WATCH_CANDIDATE" and not reasons:
        reasons.append("statistical: not robust enough for paper")
    return "; ".join(reasons) if reasons else "not paper eligible"


def _rn1_similarity(components: tuple[ComponentSpec, ...]) -> Decimal:
    families = {component.family for component in components}
    score = _ZERO
    if "completion_set_edge" in families:
        score += Decimal("0.50")
    if "inventory_balancing" in families:
        score += Decimal("0.15")
    if "sibling_market_confirmation" in families:
        score += Decimal("0.10")
    if "book_age_context_quality_filters" in families:
        score += Decimal("0.10")
    if "event_phase_conditions" in families:
        score += Decimal("0.05")
    if "depth_imbalance" in families:
        score += Decimal("0.05")
    return min(score, _ONE)


def _gap_similarity(components: tuple[ComponentSpec, ...]) -> Decimal:
    families = {component.family for component in components}
    score = _ZERO
    if "spread_capture" in families:
        score += Decimal("0.55")
    if "depth_imbalance" in families:
        score += Decimal("0.15")
    if "inventory_balancing" in families:
        score += Decimal("0.10")
    if "book_age_context_quality_filters" in families:
        score += Decimal("0.10")
    if "price_bucket_filters" in families:
        score += Decimal("0.05")
    return min(score, _ONE)


def _complete_set_score(components: tuple[ComponentSpec, ...]) -> Decimal:
    families = {component.family for component in components}
    if "completion_set_edge" not in families:
        return _ZERO
    score = Decimal("0.70")
    if "inventory_balancing" in families:
        score += Decimal("0.15")
    if "sibling_market_confirmation" in families:
        score += Decimal("0.10")
    return min(score, _ONE)


def _execution_similarity(components: tuple[ComponentSpec, ...], max_order_size: Decimal) -> Decimal:
    score = Decimal("0.55")
    families = {component.family for component in components}
    if "book_age_context_quality_filters" in families:
        score += Decimal("0.20")
    if "depth_imbalance" in families:
        score += Decimal("0.10")
    if max_order_size <= Decimal("10"):
        score += Decimal("0.10")
    return min(score, _ONE)


def _has_ordering_violation_metric(conservative: CompositeMetric, optimistic: CompositeMetric) -> bool:
    return conservative.simulated_fills_count > optimistic.simulated_fills_count or conservative.net_pnl > optimistic.net_pnl


def _edge_bands(max_capital: Decimal) -> dict[str, Decimal]:
    return {
        "1pct": max_capital * Decimal("0.01"),
        "2pct": max_capital * Decimal("0.02"),
        "3pct": max_capital * Decimal("0.03"),
        "5pct": max_capital * Decimal("0.05"),
    }


def _degradation_pct(reference: Decimal, observed: Decimal) -> Decimal:
    if reference <= _ZERO:
        return _ZERO
    return max(_ZERO, (reference - observed) / reference)


def _worst_event_pnl(metric: CompositeMetric) -> Decimal:
    return min((_decimal(row["total_pnl"]) for row in metric.event_rows), default=_ZERO)


def _event_count_by_split(candidate: CompositeCandidate) -> dict[str, int]:
    return {split: candidate.metrics[split].events_count for split in _SPLITS}


def _fills_count_by_split(candidate: CompositeCandidate) -> dict[str, int]:
    return {split: candidate.metrics[split].simulated_fills_count for split in _SPLITS}


def _max_event_loss_pct_of_capital(metric: CompositeMetric, max_capital: Decimal) -> Decimal:
    return metric.max_event_loss / max(max_capital, _ONE)


def _is_forward_watch_export(candidate: CompositeCandidate, *, min_events: int, min_fills: int) -> bool:
    train = candidate.metrics["train"]
    validation = candidate.metrics["validation"]
    test = candidate.metrics["test"]
    return (
        train.net_pnl > _ZERO
        and validation.net_pnl > _ZERO
        and test.net_pnl >= _ZERO
        and train.risk_breaches == 0
        and validation.risk_breaches == 0
        and test.risk_breaches == 0
        and test.events_count >= min_events
        and test.simulated_fills_count >= min_fills
        and candidate.final_status != "ELIGIBLE_PAPER"
    )


def _component_param(candidate: CompositeCandidate, key: str) -> str:
    for component in candidate.components:
        if key in component.parameters:
            return str(component.parameters[key])
    return ""


def _max_event_exposure_from_rows(metric: CompositeMetric) -> Decimal:
    return max((_decimal(row.get("max_event_exposure")) for row in metric.event_rows), default=_ZERO)


def _test_band_label(test_net_pnl: Decimal, bands: dict[str, Decimal]) -> str:
    if test_net_pnl < _ZERO:
        return "below breakeven"
    if test_net_pnl < bands["1pct"]:
        return "between breakeven and 1% of max_capital"
    if test_net_pnl < bands["2pct"]:
        return "between 1% and 2% of max_capital"
    if test_net_pnl < bands["3pct"]:
        return "between 2% and 3% of max_capital"
    if test_net_pnl < bands["5pct"]:
        return "between 3% and 5% of max_capital"
    return "at or above 5% of max_capital"


def _failure_type(candidate: CompositeCandidate, *, max_capital: Decimal, min_events: int, min_fills: int) -> str:
    test = candidate.metrics["test"]
    validation = candidate.metrics["validation"]
    if candidate.final_status == "ELIGIBLE_PAPER":
        return "none"
    if test.risk_breaches or test.max_event_loss > max_capital * Decimal("0.20"):
        return "risk"
    if test.events_count < min_events or test.simulated_fills_count < min_fills:
        return "sample_insufficient"
    if test.net_pnl < -_edge_bands(max_capital)["1pct"] or test.roi_on_capital < Decimal("-0.010"):
        return "economic"
    if _degradation_pct(validation.net_pnl, test.net_pnl) > Decimal("0.75"):
        return "statistical/economic"
    return "statistical"


def _inventory_improvement_sentence(candidate: CompositeCandidate) -> str:
    test = candidate.metrics["test"]
    if "event_inventory_cycling" not in {component.family for component in candidate.components}:
        return "not an inventory-cycle candidate"
    if test.merge_count <= 0:
        return "no simulated MERGE occurred; inventory lifecycle did not improve the base signal"
    if test.net_pnl <= _ZERO:
        return "simulated MERGE occurred, but holdout economics did not improve enough"
    return "simulated MERGE/recycle lifecycle active; evaluate against forward-watch and paper gates"


def _concentration(values: Iterable[Decimal]) -> Decimal:
    absolute = sorted((abs(v) for v in values), reverse=True)
    total = sum(absolute, _ZERO)
    if total <= _ZERO:
        return _ZERO
    return absolute[0] / total


def _event_key(ctx: DecisionContext) -> str:
    return ctx.condition_id or str(ctx.event_id) or f"token:{ctx.token_id}"


def _ts_to_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _load_rows(session: Session, wallet: str) -> list[dict]:
    wallet = wallet.lower()
    if wallet in {"all", "*"}:
        rows = session.execute(
            text(
                "SELECT * FROM microstructure_lifecycle_dataset "
                "WHERE wallet IN :wallets ORDER BY trade_ts, event_id"
            ).bindparams(bindparam("wallets", expanding=True)),
            {"wallets": [RN1_WALLET, GAP_WALLET]},
        ).mappings().fetchall()
    else:
        rows = session.execute(
            text(
                "SELECT * FROM microstructure_lifecycle_dataset "
                "WHERE wallet = :wallet ORDER BY trade_ts, event_id"
            ),
            {"wallet": wallet},
        ).mappings().fetchall()
    if wallet in {"all", "*"} and not rows:
        rows = session.execute(
            text("SELECT * FROM microstructure_lifecycle_dataset ORDER BY trade_ts, event_id")
        ).mappings().fetchall()
    return [dict(row) for row in rows]


def _write_candidates_csv(candidates: list[CompositeCandidate], path: Path, *, max_capital: Decimal) -> None:
    fields = [
        "rank_index",
        "candidate_id",
        "parent_id",
        "stage",
        "selected_components",
        "number_components",
        "complexity_penalty",
        "train_pnl",
        "validation_pnl",
        "test_pnl",
        "test_roi_on_capital",
        "validation_to_test_pnl_degradation_pct",
        "validation_to_test_roi_degradation_pct",
        "max_event_loss_pct_of_capital",
        "worst_event_pnl",
        "event_count_by_split",
        "fills_count_by_split",
        "validation_roi_on_capital",
        "validation_max_drawdown",
        "validation_max_event_loss",
        "validation_fills",
        "validation_events",
        "auto_merge_enabled",
        "max_bond_cost",
        "min_merge_qty",
        "merge_count",
        "released_capital_total",
        "capital_recycled_total",
        "trading_pnl",
        "merge_pnl",
        "redeem_pnl",
        "unresolved_inventory_value",
        "capital_turnover_ratio",
        "max_unpaired_inventory",
        "max_event_exposure",
        "rn1_similarity_score",
        "gap_similarity_score",
        "complete_set_mechanism_score",
        "execution_similarity_score",
        "risk_adjusted_economic_score",
        "final_status",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for candidate in candidates:
            train = candidate.metrics.get("train")
            validation = candidate.metrics.get("validation")
            test = candidate.metrics.get("test")
            pnl_degradation = _degradation_pct(validation.net_pnl, test.net_pnl) if validation and test else _ZERO
            roi_degradation = _degradation_pct(validation.roi_on_capital, test.roi_on_capital) if validation and test else _ZERO
            writer.writerow(
                {
                    "rank_index": candidate.rank_index or "",
                    "candidate_id": candidate.candidate_id,
                    "parent_id": candidate.parent_id or "",
                    "stage": candidate.stage,
                    "selected_components": ";".join(candidate.component_labels),
                    "number_components": candidate.component_count,
                    "complexity_penalty": _fmt_decimal(Decimal("0.020") * Decimal(max(0, candidate.component_count - 1))),
                    "train_pnl": _fmt_decimal(train.net_pnl if train else _ZERO),
                    "validation_pnl": _fmt_decimal(validation.net_pnl if validation else _ZERO),
                    "test_pnl": _fmt_decimal(test.net_pnl if test else _ZERO),
                    "test_roi_on_capital": _fmt_decimal(test.roi_on_capital if test else _ZERO),
                    "validation_to_test_pnl_degradation_pct": _fmt_decimal(pnl_degradation),
                    "validation_to_test_roi_degradation_pct": _fmt_decimal(roi_degradation),
                    "max_event_loss_pct_of_capital": _fmt_decimal(
                        _max_event_loss_pct_of_capital(test, max_capital) if test else _ZERO
                    ),
                    "worst_event_pnl": _fmt_decimal(_worst_event_pnl(test) if test else _ZERO),
                    "event_count_by_split": json.dumps(_event_count_by_split(candidate), sort_keys=True),
                    "fills_count_by_split": json.dumps(_fills_count_by_split(candidate), sort_keys=True),
                    "validation_roi_on_capital": _fmt_decimal(validation.roi_on_capital if validation else _ZERO),
                    "validation_max_drawdown": _fmt_decimal(validation.max_drawdown if validation else _ZERO),
                    "validation_max_event_loss": _fmt_decimal(validation.max_event_loss if validation else _ZERO),
                    "validation_fills": validation.simulated_fills_count if validation else 0,
                    "validation_events": validation.events_count if validation else 0,
                    "auto_merge_enabled": _component_param(candidate, "auto_merge_enabled"),
                    "max_bond_cost": _component_param(candidate, "max_bond_cost"),
                    "min_merge_qty": _component_param(candidate, "min_merge_qty"),
                    "merge_count": test.merge_count if test else 0,
                    "released_capital_total": _fmt_decimal(test.released_capital_total if test else _ZERO),
                    "capital_recycled_total": _fmt_decimal(test.capital_recycled_total if test else _ZERO),
                    "trading_pnl": _fmt_decimal(test.trading_pnl if test else _ZERO),
                    "merge_pnl": _fmt_decimal(test.merge_pnl if test else _ZERO),
                    "redeem_pnl": _fmt_decimal(test.redeem_pnl if test else _ZERO),
                    "unresolved_inventory_value": _fmt_decimal(test.unresolved_inventory_value if test else _ZERO),
                    "capital_turnover_ratio": _fmt_decimal(test.capital_turnover_ratio if test else _ZERO),
                    "max_unpaired_inventory": _fmt_decimal(test.max_unpaired_inventory if test else _ZERO),
                    "max_event_exposure": _fmt_decimal(_max_event_exposure_from_rows(test) if test else _ZERO),
                    "rn1_similarity_score": _fmt_decimal(candidate.rn1_similarity_score),
                    "gap_similarity_score": _fmt_decimal(candidate.gap_similarity_score),
                    "complete_set_mechanism_score": _fmt_decimal(candidate.complete_set_mechanism_score),
                    "execution_similarity_score": _fmt_decimal(candidate.execution_similarity_score),
                    "risk_adjusted_economic_score": _fmt_decimal(candidate.validation_score),
                    "final_status": candidate.final_status,
                }
            )


def _write_forward_watch_csv(result: CompositeSearchResult, path: Path) -> None:
    fields = [
        "candidate_id",
        "rank",
        "components_json",
        "train_net_pnl",
        "validation_net_pnl",
        "test_net_pnl",
        "test_roi_on_capital",
        "max_capital",
        "max_drawdown",
        "max_event_loss",
        "events",
        "fills",
        "rn1_similarity",
        "gap_similarity",
        "final_status",
        "reason_not_paper",
    ]
    candidates = [
        candidate
        for candidate in result.ranked_candidates
        if _is_forward_watch_export(candidate, min_events=result.min_events, min_fills=result.min_fills)
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for candidate in candidates:
            train = candidate.metrics["train"]
            validation = candidate.metrics["validation"]
            test = candidate.metrics["test"]
            writer.writerow(
                {
                    "candidate_id": candidate.candidate_id,
                    "rank": candidate.rank_index or "",
                    "components_json": json.dumps(candidate.component_labels),
                    "train_net_pnl": _fmt_decimal(train.net_pnl),
                    "validation_net_pnl": _fmt_decimal(validation.net_pnl),
                    "test_net_pnl": _fmt_decimal(test.net_pnl),
                    "test_roi_on_capital": _fmt_decimal(test.roi_on_capital),
                    "max_capital": _fmt_decimal(result.max_capital),
                    "max_drawdown": _fmt_decimal(test.max_drawdown),
                    "max_event_loss": _fmt_decimal(test.max_event_loss),
                    "events": test.events_count,
                    "fills": test.simulated_fills_count,
                    "rn1_similarity": _fmt_decimal(candidate.rn1_similarity_score),
                    "gap_similarity": _fmt_decimal(candidate.gap_similarity_score),
                    "final_status": candidate.final_status,
                    "reason_not_paper": _reason_not_paper(
                        candidate,
                        max_capital=result.max_capital,
                        min_events=result.min_events,
                        min_fills=result.min_fills,
                    ),
                }
            )


def _component_effectiveness_candidates(result: CompositeSearchResult) -> list[CompositeCandidate]:
    if not result.candidates:
        return []
    base = next((candidate for candidate in result.candidates if candidate.component_count == 1), result.candidates[0])
    targets = [
        "book_age_context_quality_filters",
        "depth_imbalance",
        "inventory_balancing",
        "price_bucket_filters",
        "sibling_market_confirmation",
        "event_phase_conditions",
    ]
    selected = [base]
    for family in targets:
        matches = [
            candidate
            for candidate in result.candidates
            if candidate.candidate_id != base.candidate_id
            and any(component.family == family for component in candidate.components)
        ]
        if matches:
            selected.append(sorted(matches, key=lambda c: (c.component_count, c.candidate_id))[0])
    seen = set()
    unique = []
    for candidate in selected:
        if candidate.candidate_id in seen:
            continue
        seen.add(candidate.candidate_id)
        unique.append(candidate)
    return unique


def _write_component_effectiveness_csv(
    result: CompositeSearchResult,
    candidates: list[CompositeCandidate],
    path: Path,
) -> None:
    fields = [
        "candidate_id",
        "variant",
        "components",
        "split",
        "candidate_signals_count",
        "accepted_orders_count",
        "simulated_fills_count",
        "skipped_orders_count",
        "skipped_by_reason",
        "events_count",
        "net_pnl",
        "max_event_exposure",
        "max_unpaired_inventory",
        "delta_signals_vs_base",
        "delta_orders_vs_base",
        "delta_fills_vs_base",
        "delta_pnl_vs_base",
        "same_orders_fills_pnl_as_base",
    ]
    base = candidates[0] if candidates else None
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for candidate in candidates:
            for split in _SPLITS:
                metric = candidate.metrics[split]
                base_metric = base.metrics[split] if base is not None else metric
                same = (
                    metric.accepted_orders_count == base_metric.accepted_orders_count
                    and metric.simulated_fills_count == base_metric.simulated_fills_count
                    and metric.net_pnl == base_metric.net_pnl
                )
                writer.writerow(
                    {
                        "candidate_id": candidate.candidate_id,
                        "variant": _variant_label(candidate),
                        "components": ";".join(candidate.component_labels),
                        "split": split,
                        "candidate_signals_count": metric.candidate_signals_count,
                        "accepted_orders_count": metric.accepted_orders_count,
                        "simulated_fills_count": metric.simulated_fills_count,
                        "skipped_orders_count": metric.skipped_orders_count,
                        "skipped_by_reason": json.dumps(metric.skipped_by_reason, sort_keys=True),
                        "events_count": metric.events_count,
                        "net_pnl": _fmt_decimal(metric.net_pnl),
                        "max_event_exposure": _fmt_decimal(_max_event_exposure_from_rows(metric)),
                        "max_unpaired_inventory": _fmt_decimal(metric.max_unpaired_inventory),
                        "delta_signals_vs_base": metric.candidate_signals_count - base_metric.candidate_signals_count,
                        "delta_orders_vs_base": metric.accepted_orders_count - base_metric.accepted_orders_count,
                        "delta_fills_vs_base": metric.simulated_fills_count - base_metric.simulated_fills_count,
                        "delta_pnl_vs_base": _fmt_decimal(metric.net_pnl - base_metric.net_pnl),
                        "same_orders_fills_pnl_as_base": int(same),
                    }
                )


def _write_per_candidate_event_pnl_csv(candidates: list[CompositeCandidate], path: Path) -> None:
    fields = [
        "candidate_id",
        "variant",
        "split",
        "event_id",
        "fills_count",
        "total_pnl",
        "turnover",
        "max_event_exposure",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for candidate in candidates:
            for split in _SPLITS:
                for row in candidate.metrics[split].event_rows:
                    writer.writerow(
                        {
                            "candidate_id": candidate.candidate_id,
                            "variant": _variant_label(candidate),
                            "split": split,
                            "event_id": row.get("event_id"),
                            "fills_count": row.get("fills_count"),
                            "total_pnl": row.get("total_pnl"),
                            "turnover": row.get("turnover"),
                            "max_event_exposure": row.get("max_event_exposure"),
                        }
                    )


def _write_skipped_by_component_csv(candidates: list[CompositeCandidate], path: Path) -> None:
    fields = ["candidate_id", "variant", "split", "component_or_reason", "skipped_count"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for candidate in candidates:
            for split in _SPLITS:
                skipped = candidate.metrics[split].skipped_by_reason
                if not skipped:
                    writer.writerow(
                        {
                            "candidate_id": candidate.candidate_id,
                            "variant": _variant_label(candidate),
                            "split": split,
                            "component_or_reason": "",
                            "skipped_count": 0,
                        }
                    )
                    continue
                for reason, count in sorted(skipped.items()):
                    writer.writerow(
                        {
                            "candidate_id": candidate.candidate_id,
                            "variant": _variant_label(candidate),
                            "split": split,
                            "component_or_reason": reason,
                            "skipped_count": count,
                        }
                    )


def _render_component_effectiveness_report(
    result: CompositeSearchResult,
    candidates: list[CompositeCandidate],
) -> str:
    lines = [
        "# Component Effectiveness Report",
        "",
        "Diagnostic only. Selection still uses train + validation; test is not used to choose parameters.",
        "",
    ]
    if not candidates:
        lines.append("No candidates were available for component effectiveness diagnostics.")
        return "\n".join(lines)
    base = candidates[0]
    lines.extend(
        [
            f"- **Base candidate:** {base.candidate_id}",
            f"- **Base components:** {', '.join(base.component_labels)}",
            f"- **Selected candidate remains:** {result.selected_candidate.candidate_id if result.selected_candidate else 'none'}",
            "",
            "## Summary",
            "",
            "| Variant | Candidate | Split | Orders | Fills | PnL | Skipped | Max Event Exposure | Max Unpaired | Same As Base |",
            "|---------|-----------|-------|--------|-------|-----|---------|--------------------|--------------|--------------|",
        ]
    )
    all_same = True
    for candidate in candidates:
        for split in _SPLITS:
            metric = candidate.metrics[split]
            base_metric = base.metrics[split]
            same = (
                metric.accepted_orders_count == base_metric.accepted_orders_count
                and metric.simulated_fills_count == base_metric.simulated_fills_count
                and metric.net_pnl == base_metric.net_pnl
            )
            all_same = all_same and (same or candidate.candidate_id == base.candidate_id)
            lines.append(
                f"| {_variant_label(candidate)} | {candidate.candidate_id} | {split} | "
                f"{metric.accepted_orders_count} | {metric.simulated_fills_count} | {_fmt_usdc(metric.net_pnl)} | "
                f"{metric.skipped_orders_count} | {_fmt_usdc(_max_event_exposure_from_rows(metric))} | "
                f"{_fmt_decimal(metric.max_unpaired_inventory)} | {'yes' if same else 'no'} |"
            )
    lines.extend(["", "## Diagnosis", ""])
    if all_same and len(candidates) > 1:
        lines.extend(
            [
                "All compared variants produced identical order/fill/PnL metrics versus the base candidate.",
                "Likely causes to inspect: filters are non-binding on this dataset, feature thresholds are permissive, or candidate variants were not evaluated in this run.",
            ]
        )
    else:
        lines.append("At least one compared variant changed orders, fills, PnL, skipped counts, or event-level metrics.")
    missing_targets = _missing_effectiveness_targets(candidates)
    if missing_targets:
        lines.append("")
        lines.append(f"Not evaluated in this run: {', '.join(missing_targets)}.")
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "A deliberately impossible filter is covered by unit tests; it must reduce accepted orders/fills to zero or near-zero.",
            "Candidate 1 remains forward-watch only and not paper eligible until final_status is PASS, test events >= 30, test PnL >= 5% of max_capital, validation-to-test degradation <= 50%, no ordering violation, max_unpaired_inventory is within risk units, and unresolved inventory is reported separately.",
            "",
        ]
    )
    return "\n".join(lines)


def _variant_label(candidate: CompositeCandidate) -> str:
    if candidate.component_count == 1:
        return "base"
    for component in candidate.components:
        if component.kind != "base":
            return "+" + component.name
    return "base"


def _missing_effectiveness_targets(candidates: list[CompositeCandidate]) -> list[str]:
    found = {
        component.family
        for candidate in candidates
        for component in candidate.components
        if component.kind != "base"
    }
    targets = {
        "book_age_context_quality_filters",
        "depth_imbalance",
        "inventory_balancing",
        "price_bucket_filters",
        "sibling_market_confirmation",
        "event_phase_conditions",
    }
    return sorted(targets - found)


def _render_search_report(result: CompositeSearchResult) -> str:
    selected = result.selected_candidate
    bands = _edge_bands(result.max_capital)
    lines = [
        "# Progressive Composite Strategy Search",
        "",
        f"- **Wallet scope:** `{result.wallet_scope}`",
        f"- **Strategy family:** `{result.strategy_family}`",
        f"- **Seed:** {result.seed}",
        f"- **Capital mode:** {result.capital_mode}",
        f"- **Max capital:** {_fmt_usdc(result.max_capital)}",
        f"- **Max order size:** {result.max_order_size}",
        f"- **Min events / fills:** {result.min_events} / {result.min_fills}",
        f"- **Evaluated candidates:** {result.evaluated_candidates} / {result.max_candidates}",
        f"- **Elapsed:** {result.elapsed_ms} ms",
        "",
        "Similarity to RN1 or Gap is diagnostic only; ranking uses train and validation risk-adjusted economics.",
        "",
        "## Small-Capital Edge Bands",
        "",
        "| Band | PnL reference |",
        "|------|---------------|",
        f"| 1% of max_capital | {_fmt_usdc(bands['1pct'])} |",
        f"| 2% of max_capital | {_fmt_usdc(bands['2pct'])} |",
        f"| 3% of max_capital | {_fmt_usdc(bands['3pct'])} |",
        f"| 5% of max_capital | {_fmt_usdc(bands['5pct'])} |",
        "",
    ]
    if selected is None:
        lines.extend(["## Selected", "", "No candidate passed train/validation selection gates.", ""])
    else:
        validation = selected.metrics["validation"]
        test = selected.metrics["test"]
        test_band = _test_band_label(test.net_pnl, bands)
        reason_not_paper = _reason_not_paper(
            selected,
            max_capital=result.max_capital,
            min_events=result.min_events,
            min_fills=result.min_fills,
        )
        forward_watch = (
            "yes"
            if _is_forward_watch_export(selected, min_events=result.min_events, min_fills=result.min_fills)
            else "no"
        )
        lines.extend(
            [
                "## Selected",
                "",
                f"- **Candidate:** {selected.candidate_id}",
                f"- **Selected components:** {', '.join(selected.component_labels)}",
                f"- **Why selected:** highest train/validation risk-adjusted economic score among candidates passing pre-test gates.",
                f"- **Number of components:** {selected.component_count}",
                f"- **Complexity penalty:** {_fmt_decimal(Decimal('0.020') * Decimal(max(0, selected.component_count - 1)))}",
                f"- **Train / validation / test PnL:** {_fmt_usdc(selected.metrics['train'].net_pnl)} / {_fmt_usdc(validation.net_pnl)} / {_fmt_usdc(test.net_pnl)}",
                f"- **Validation ROI on capital:** {_fmt_pct(validation.roi_on_capital)}",
                f"- **Test ROI on capital:** {_fmt_pct(test.roi_on_capital)}",
                f"- **Validation to test PnL degradation:** {_fmt_pct(_degradation_pct(validation.net_pnl, test.net_pnl))}",
                f"- **Validation to test ROI degradation:** {_fmt_pct(_degradation_pct(validation.roi_on_capital, test.roi_on_capital))}",
                f"- **Validation max drawdown:** {_fmt_usdc(validation.max_drawdown)}",
                f"- **Validation max event loss:** {_fmt_usdc(validation.max_event_loss)}",
                f"- **Test max drawdown:** {_fmt_usdc(test.max_drawdown)}",
                f"- **Test max event loss:** {_fmt_usdc(test.max_event_loss)} ({_fmt_pct(_max_event_loss_pct_of_capital(test, result.max_capital))} of max_capital)",
                f"- **Worst test event PnL:** {_fmt_usdc(_worst_event_pnl(test))}",
                f"- **Auto merge enabled:** {_component_param(selected, 'auto_merge_enabled') or 'n/a'}",
                f"- **max_bond_cost / min_merge_qty:** {_component_param(selected, 'max_bond_cost') or 'n/a'} / {_component_param(selected, 'min_merge_qty') or 'n/a'}",
                f"- **Merge count / released capital / recycled capital:** {test.merge_count} / {_fmt_usdc(test.released_capital_total)} / {_fmt_usdc(test.capital_recycled_total)}",
                f"- **Trading / merge / redeem PnL:** {_fmt_usdc(test.trading_pnl)} / {_fmt_usdc(test.merge_pnl)} / {_fmt_usdc(test.redeem_pnl)}",
                f"- **Unresolved inventory value:** {_fmt_usdc(test.unresolved_inventory_value)}",
                f"- **Capital turnover ratio:** {_fmt_decimal(test.capital_turnover_ratio)}",
                f"- **Max unpaired inventory:** {_fmt_decimal(test.max_unpaired_inventory)}",
                f"- **Max event exposure:** {_fmt_usdc(_max_event_exposure_from_rows(test))}",
                f"- **Event count by split:** `{json.dumps(_event_count_by_split(selected), sort_keys=True)}`",
                f"- **Fills count by split:** `{json.dumps(_fills_count_by_split(selected), sort_keys=True)}`",
                f"- **RN1 similarity score:** {_fmt_decimal(selected.rn1_similarity_score)}",
                f"- **Gap similarity score:** {_fmt_decimal(selected.gap_similarity_score)}",
                f"- **Final status:** {selected.final_status}",
                f"- **Forward-watch candidate:** {forward_watch}",
                f"- **Why not paper:** {reason_not_paper or 'paper eligible'}",
                f"- **Test PnL vs edge bands:** {_fmt_usdc(test.net_pnl)} is {test_band}",
                f"- **Failure type:** {_failure_type(selected, max_capital=result.max_capital, min_events=result.min_events, min_fills=result.min_fills)}",
                f"- **Inventory-cycle improvement:** {_inventory_improvement_sentence(selected)}",
                "",
            ]
        )
    lines.extend(
        [
            "## Top Candidates",
            "",
            "| Rank | Candidate | Components | Score | Train PnL | Validation PnL | Test PnL | Test ROI | Merges | Released | Trading PnL | Merge PnL | Redeem PnL | Turnover | Final status |",
            "|------|-----------|------------|-------|-----------|----------------|----------|----------|--------|----------|-------------|-----------|------------|----------|--------------|",
        ]
    )
    for candidate in result.ranked_candidates[:10]:
        train = candidate.metrics["train"]
        validation = candidate.metrics["validation"]
        test = candidate.metrics["test"]
        lines.append(
            f"| {candidate.rank_index} | {candidate.candidate_id} | {_clip(', '.join(candidate.component_labels), 56)} | "
            f"{_fmt_decimal(candidate.validation_score)} | {_fmt_usdc(train.net_pnl)} | {_fmt_usdc(validation.net_pnl)} | "
            f"{_fmt_usdc(test.net_pnl)} | {_fmt_pct(test.roi_on_capital)} | {test.merge_count} | "
            f"{_fmt_usdc(test.released_capital_total)} | {_fmt_usdc(test.trading_pnl)} | {_fmt_usdc(test.merge_pnl)} | "
            f"{_fmt_usdc(test.redeem_pnl)} | {_fmt_decimal(test.capital_turnover_ratio)} | {candidate.final_status} |"
        )
    if not result.ranked_candidates:
        lines.append("| - | - | No ranked candidates | - | - | - | - | - | - | - | - | - | - | - | - |")
    lines.append("")
    return "\n".join(lines)


def _render_component_contribution_report(result: CompositeSearchResult) -> str:
    rows: dict[str, list[CompositeCandidate]] = {}
    for candidate in result.candidates:
        for component in candidate.components:
            rows.setdefault(component.name, []).append(candidate)
    lines = [
        "# Component Contribution Report",
        "",
        "| Component | Candidates | Avg validation score | Best validation score | Best candidate | Best status |",
        "|-----------|------------|----------------------|-----------------------|----------------|-------------|",
    ]
    for component_name, candidates in sorted(rows.items()):
        avg = sum((candidate.validation_score for candidate in candidates), _ZERO) / Decimal(len(candidates))
        best = max(candidates, key=lambda c: c.validation_score)
        lines.append(
            f"| {component_name} | {len(candidates)} | {_fmt_decimal(avg)} | {_fmt_decimal(best.validation_score)} | "
            f"{best.candidate_id} | {best.final_status} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_event_robustness_report(result: CompositeSearchResult) -> str:
    selected = result.selected_candidate or (result.ranked_candidates[0] if result.ranked_candidates else None)
    lines = ["# Event Robustness Report", ""]
    if selected is None:
        lines.append("No selected candidate; event robustness unavailable.")
        return "\n".join(lines)
    lines.extend(
        [
            f"- **Candidate:** {selected.candidate_id}",
            f"- **Components:** {', '.join(selected.component_labels)}",
            "",
            "| Split | Events | Worst event PnL | Max event loss | Concentration | Fills | Merges | Released | Trading PnL | Merge PnL | Redeem PnL | Unresolved | Max Unpaired |",
            "|-------|--------|-----------------|----------------|---------------|-------|--------|----------|-------------|-----------|------------|------------|--------------|",
        ]
    )
    for split in _SPLITS:
        metric = selected.metrics[split]
        worst = min((_decimal(row["total_pnl"]) for row in metric.event_rows), default=_ZERO)
        lines.append(
            f"| {split} | {metric.events_count} | {_fmt_usdc(worst)} | {_fmt_usdc(metric.max_event_loss)} | "
            f"{_fmt_pct(metric.concentration)} | {metric.simulated_fills_count} | {metric.merge_count} | "
            f"{_fmt_usdc(metric.released_capital_total)} | {_fmt_usdc(metric.trading_pnl)} | "
            f"{_fmt_usdc(metric.merge_pnl)} | {_fmt_usdc(metric.redeem_pnl)} | "
            f"{_fmt_usdc(metric.unresolved_inventory_value)} | {_fmt_decimal(metric.max_unpaired_inventory)} |"
        )
    lines.extend(["", "## Event Rows", ""])
    lines.extend(["| Split | Event | PnL | Fills | Turnover | Max Exposure |", "|-------|-------|-----|-------|----------|--------------|"])
    for split in _SPLITS:
        metric = selected.metrics[split]
        ordered = sorted(metric.event_rows, key=lambda row: _decimal(row["total_pnl"]))
        for row in ordered[:15]:
            lines.append(
                f"| {split} | `{row['event_id']}` | {_fmt_usdc(_decimal(row['total_pnl']))} | "
                f"{row['fills_count']} | {_fmt_usdc(_decimal(row['turnover']))} | {_fmt_usdc(_decimal(row['max_event_exposure']))} |"
            )
    lines.append("")
    return "\n".join(lines)


def _decimal(value) -> Decimal:
    if value is None:
        return _ZERO
    return Decimal(str(value))


def _fmt_decimal(value: Decimal) -> str:
    return f"{value:.6f}"


def _fmt_usdc(value: Decimal) -> str:
    return f"${value:+.2f}"


def _fmt_pct(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value.replace("|", "\\|")
    return value[: limit - 3].replace("|", "\\|") + "..."
