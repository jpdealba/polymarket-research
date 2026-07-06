"""Phase 22.2 strategy parameter search."""

from __future__ import annotations

import itertools
import json
import random
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .engine import (
    GAP_WALLET,
    RN1_WALLET,
    StrategyConfig,
    _TransientResult,
    _has_ordering_violation,
    _load_dataset,
    _simulate,
)
from .risk import RiskLimits
from .scenarios import ALL_SCENARIOS, ScenarioConfig

SEARCH_SEED = 2202
SPLIT_NAMES = ("train", "validation", "test")
DEFAULT_MIN_TRAIN_FILLS = 30
DEFAULT_MIN_VALIDATION_FILLS = 30
DEFAULT_MIN_TEST_FILLS = 30

RN1_GRID: dict[str, list[object]] = {
    "max_bond_cost": ["0.96", "0.97", "0.98", "0.99"],
    "book_age_s_max": [5, 15, 30],
    "max_order_size": ["10", "25", "50", "100"],
    "max_position_per_token": ["100", "250", "500", "1000"],
    "max_event_exposure": ["250", "500", "1000", "2000"],
    "max_capital_deployed": ["1000", "2500", "5000", "10000"],
    "min_depth": ["0", "100", "250", "500", "1000"],
}

GAP_GRID: dict[str, list[object]] = {
    "min_spread_bps": ["20", "50", "100", "200", "500"],
    "min_fill_improvement_bps": ["5", "10", "25", "50"],
    "book_age_s_max": [5, 15, 30],
    "max_order_size": ["10", "25", "50", "100"],
    "max_position_per_token": ["100", "250", "500", "1000"],
    "max_event_exposure": ["250", "500", "1000", "2000"],
    "max_capital_deployed": ["1000", "2500", "5000", "10000"],
    "max_daily_loss": ["50", "100", "250", "500"],
    "min_depth": ["0", "100", "250", "500", "1000"],
}


@dataclass(frozen=True)
class SearchMetric:
    split_name: str
    candidate_signals_count: int
    accepted_orders_count: int
    skipped_orders_count: int
    skipped_by_reason: dict[str, int]
    simulated_fills_count: int
    fill_rate_on_candidates: Optional[Decimal]
    net_pnl: Decimal
    max_drawdown: Decimal
    max_inventory: Decimal
    capital_required: Decimal
    turnover: Decimal
    risk_breaches: int
    risk_prevented_count: int
    ordering_violation: bool
    conservative_pass: bool
    score: Optional[Decimal]

    @property
    def eligible(self) -> bool:
        return (
            self.conservative_pass
            and self.risk_breaches == 0
            and not self.ordering_violation
            and self.net_pnl > Decimal("0")
            and self.simulated_fills_count > 0
        )


@dataclass
class SearchCandidate:
    candidate_id: Optional[int]
    candidate_index: int
    strategy_name: str
    parameters: dict[str, object]
    metrics: dict[str, SearchMetric]
    rank_index: Optional[int] = None

    @property
    def validation_metric(self) -> SearchMetric:
        return self.metrics["validation"]

    @property
    def train_metric(self) -> SearchMetric:
        return self.metrics["train"]

    @property
    def test_metric(self) -> SearchMetric:
        return self.metrics["test"]

    @property
    def eligible(self) -> bool:
        return candidate_selected_pretest(self)


@dataclass
class SearchRunResult:
    run_id: int
    wallet: str
    rule_name: str
    strategy_family: str
    seed: int
    max_combos: int
    total_combos: int
    evaluated_combos: int
    candidates: list[SearchCandidate]
    selected_candidate_id: Optional[int]
    elapsed_ms: int

    @property
    def ranked_candidates(self) -> list[SearchCandidate]:
        return sorted(
            (c for c in self.candidates if c.rank_index is not None),
            key=lambda c: c.rank_index or 0,
        )


def run_strategy_search(
    session: Session,
    wallet: str,
    rule_name: str,
    *,
    max_combos: int,
    seed: int = SEARCH_SEED,
    min_train_fills: int = DEFAULT_MIN_TRAIN_FILLS,
    min_validation_fills: int = DEFAULT_MIN_VALIDATION_FILLS,
    progress_callback: Optional[Callable[[int, int, int, float], None]] = None,
) -> SearchRunResult:
    """Run deterministic train/validation/test parameter search and persist metrics."""
    wallet = wallet.lower()
    rule_name = rule_name.strip().lower()
    if max_combos <= 0:
        raise ValueError("--max-combos must be positive")
    _validate_search_pair(wallet, rule_name)

    rows = _load_dataset(session, wallet)
    if len(rows) < 3:
        raise ValueError(f"Need at least 3 dataset rows for train/validation/test split; found {len(rows)}")
    splits = split_rows_by_time(rows)
    combos = parameter_combinations(rule_name, max_combos=max_combos, seed=seed)
    total_combos = grid_size(rule_name)

    t0 = time.monotonic()
    search_run_id = _insert_search_run(
        session=session,
        wallet=wallet,
        rule_name=rule_name,
        strategy_family=_strategy_family(rule_name),
        seed=seed,
        max_combos=max_combos,
        total_combos=total_combos,
    )

    candidates: list[SearchCandidate] = []
    for idx, params in enumerate(combos):
        strategy = build_search_strategy(wallet, rule_name, params, idx)
        limits = risk_limits_from_parameters(params)
        scenario = scenario_from_parameters(ALL_SCENARIOS["conservative"], params)
        metrics = {
            split: evaluate_candidate_split(split_rows, strategy, scenario, limits, split)
            for split, split_rows in splits.items()
        }
        candidate_id = _insert_candidate(
            session=session,
            search_run_id=search_run_id,
            candidate_index=idx,
            strategy_name=strategy.strategy_name,
            parameters=params,
        )
        candidate = SearchCandidate(
            candidate_id=candidate_id,
            candidate_index=idx,
            strategy_name=strategy.strategy_name,
            parameters=params,
            metrics=metrics,
        )
        _insert_candidate_metrics(session, candidate_id, candidate.metrics)
        candidates.append(candidate)
        if progress_callback is not None:
            elapsed_s = time.monotonic() - t0
            eligible_so_far = sum(
                1
                for candidate in candidates
                if candidate_selected_pretest(
                    candidate,
                    min_train_fills=min_train_fills,
                    min_validation_fills=min_validation_fills,
                )
            )
            progress_callback(idx + 1, len(combos), eligible_so_far, elapsed_s)

    ranked = rank_candidates(
        candidates,
        min_train_fills=min_train_fills,
        min_validation_fills=min_validation_fills,
    )
    selected_candidate_id = ranked[0].candidate_id if ranked else None
    for rank, candidate in enumerate(ranked, start=1):
        candidate.rank_index = rank
        session.execute(
            text(
                "UPDATE simulation_strategy_candidates "
                "SET rank_index = :rank, eligible = 1 "
                "WHERE id = :id"
            ),
            {"rank": rank, "id": candidate.candidate_id},
        )
    if selected_candidate_id is not None:
        session.execute(
            text(
                "UPDATE simulation_strategy_candidates "
                "SET selected_for_test = 1 WHERE id = :id"
            ),
            {"id": selected_candidate_id},
        )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    session.execute(
        text(
            "UPDATE simulation_strategy_search_runs "
            "SET evaluated_combos = :evaluated, selected_candidate_id = :selected, "
            "elapsed_ms = :elapsed_ms, status = :status, notes = :notes "
            "WHERE id = :id"
        ),
        {
            "evaluated": len(candidates),
            "selected": selected_candidate_id,
            "elapsed_ms": elapsed_ms,
            "status": "complete",
            "notes": "" if selected_candidate_id is not None else "no eligible candidate",
            "id": search_run_id,
        },
    )
    session.commit()

    return SearchRunResult(
        run_id=search_run_id,
        wallet=wallet,
        rule_name=rule_name,
        strategy_family=_strategy_family(rule_name),
        seed=seed,
        max_combos=max_combos,
        total_combos=total_combos,
        evaluated_combos=len(candidates),
        candidates=candidates,
        selected_candidate_id=selected_candidate_id,
        elapsed_ms=elapsed_ms,
    )


def parameter_combinations(rule_name: str, *, max_combos: int, seed: int = SEARCH_SEED) -> list[dict[str, object]]:
    grid = _grid(rule_name)
    keys = list(grid)
    combos = [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]
    if max_combos >= len(combos):
        return combos
    rng = random.Random(seed)
    selected_indexes = sorted(rng.sample(range(len(combos)), max_combos))
    return [combos[i] for i in selected_indexes]


def grid_size(rule_name: str) -> int:
    size = 1
    for values in _grid(rule_name).values():
        size *= len(values)
    return size


def split_rows_by_time(rows: list[dict]) -> dict[str, list[dict]]:
    ordered = sorted(rows, key=lambda r: (int(r.get("trade_ts") or 0), int(r.get("event_id") or 0)))
    n = len(ordered)
    if n < 3:
        raise ValueError("Need at least 3 rows for train/validation/test split")
    train_end = max(1, int(n * Decimal("0.6")))
    validation_end = max(train_end + 1, int(n * Decimal("0.8")))
    validation_end = min(validation_end, n - 1)
    return {
        "train": ordered[:train_end],
        "validation": ordered[train_end:validation_end],
        "test": ordered[validation_end:],
    }


def build_search_strategy(
    wallet: str,
    rule_name: str,
    parameters: dict[str, object],
    candidate_index: int,
) -> StrategyConfig:
    base = {
        "context_quality": ["excellent", "good", "usable"],
        "max_book_age_s": int(parameters["book_age_s_max"]),
        "min_depth": str(parameters["min_depth"]),
    }
    if rule_name == "completion_set_edge":
        return StrategyConfig(
            strategy_name=f"rn1_completion_set_edge_search_{candidate_index:04d}",
            wallet=wallet,
            base_rule=rule_name,
            version=3,
            rule_parameters={"max_bond_cost": str(parameters["max_bond_cost"])},
            filters=base,
            execution_policy={"candidate_order_price": "book_before", "risk_gate": "pre_order"},
            pre_trade_risk_limits=(
                "max_position_per_token",
                "max_event_exposure",
                "max_capital_deployed",
                "max_order_size",
            ),
        )
    return StrategyConfig(
        strategy_name=f"gap_spread_capture_search_{candidate_index:04d}",
        wallet=wallet,
        base_rule=rule_name,
        version=3,
        rule_parameters={
            "min_spread_bps": str(parameters["min_spread_bps"]),
            "min_edge_bps": str(parameters["min_fill_improvement_bps"]),
            "min_fill_improvement_bps": str(parameters["min_fill_improvement_bps"]),
        },
        filters=base,
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
    )


def risk_limits_from_parameters(parameters: dict[str, object]) -> RiskLimits:
    return RiskLimits(
        max_position_per_token=Decimal(str(parameters["max_position_per_token"])),
        max_event_exposure=Decimal(str(parameters["max_event_exposure"])),
        max_daily_loss=Decimal(str(parameters.get("max_daily_loss", "100"))),
        max_capital_deployed=Decimal(str(parameters["max_capital_deployed"])),
        max_order_size=Decimal(str(parameters["max_order_size"])),
        max_stale_book_age_s=int(parameters["book_age_s_max"]),
    )


def scenario_from_parameters(scenario: ScenarioConfig, parameters: dict[str, object]) -> ScenarioConfig:
    return replace(
        scenario,
        max_order_size=Decimal(str(parameters["max_order_size"])),
        max_book_age_s=int(parameters["book_age_s_max"]),
        min_book_depth_for_fill=Decimal(str(parameters["min_depth"])),
    )


def evaluate_candidate_split(
    rows: list[dict],
    strategy: StrategyConfig,
    conservative_scenario: ScenarioConfig,
    risk_limits: RiskLimits,
    split_name: str,
) -> SearchMetric:
    conservative = _simulate(
        rows,
        strategy,
        conservative_scenario,
        risk_limits,
        collect_details=False,
    )
    optimistic = _simulate(
        rows,
        strategy,
        ALL_SCENARIOS["optimistic"],
        risk_limits,
        collect_details=False,
    )
    ordering_violation = _has_ordering_violation(conservative, optimistic)
    conservative_pass = (
        conservative.net_pnl > Decimal("0")
        and conservative.risk_breaches == 0
        and not ordering_violation
        and conservative.fills_count > 0
    )
    score = _score(conservative) if conservative_pass else None
    return _metric_from_transient(
        split_name=split_name,
        transient=conservative,
        conservative_pass=conservative_pass,
        ordering_violation=ordering_violation,
        score=score,
    )


def rank_candidates(
    candidates: list[SearchCandidate],
    *,
    min_train_fills: int = DEFAULT_MIN_TRAIN_FILLS,
    min_validation_fills: int = DEFAULT_MIN_VALIDATION_FILLS,
) -> list[SearchCandidate]:
    eligible = [
        c
        for c in candidates
        if candidate_selected_pretest(
            c,
            min_train_fills=min_train_fills,
            min_validation_fills=min_validation_fills,
        )
    ]
    return sorted(
        eligible,
        key=lambda c: (
            -selection_score(c),
            max(c.train_metric.max_drawdown, c.validation_metric.max_drawdown),
            -min(c.train_metric.simulated_fills_count, c.validation_metric.simulated_fills_count),
            c.train_metric.capital_required + c.validation_metric.capital_required,
            c.validation_metric.skipped_orders_count,
            c.candidate_index,
        ),
    )


def candidate_selected_pretest(
    candidate: SearchCandidate,
    *,
    min_train_fills: int = DEFAULT_MIN_TRAIN_FILLS,
    min_validation_fills: int = DEFAULT_MIN_VALIDATION_FILLS,
) -> bool:
    return _split_passes(candidate.train_metric, min_fills=min_train_fills) and _split_passes(
        candidate.validation_metric,
        min_fills=min_validation_fills,
    )


def candidate_validation_passes(
    candidate: SearchCandidate,
    *,
    min_validation_fills: int = DEFAULT_MIN_VALIDATION_FILLS,
) -> bool:
    return _split_passes(candidate.validation_metric, min_fills=min_validation_fills)


def candidate_test_passes(
    candidate: SearchCandidate,
    *,
    min_test_fills: int = DEFAULT_MIN_TEST_FILLS,
) -> bool:
    return _split_passes(candidate.test_metric, min_fills=min_test_fills)


def final_status(
    candidate: SearchCandidate,
    *,
    min_train_fills: int = DEFAULT_MIN_TRAIN_FILLS,
    min_validation_fills: int = DEFAULT_MIN_VALIDATION_FILLS,
    min_test_fills: int = DEFAULT_MIN_TEST_FILLS,
) -> str:
    if not candidate_selected_pretest(
        candidate,
        min_train_fills=min_train_fills,
        min_validation_fills=min_validation_fills,
    ):
        return "NOT_SELECTED"
    if candidate_test_passes(candidate, min_test_fills=min_test_fills):
        return "TEST_PASS"
    return "TEST_FAIL"


def selection_score(candidate: SearchCandidate) -> Decimal:
    train_roi = _metric_roi(candidate.train_metric)
    validation_roi = _metric_roi(candidate.validation_metric)
    drawdown_penalty = max(
        _drawdown_ratio(candidate.train_metric),
        _drawdown_ratio(candidate.validation_metric),
    )
    instability_penalty = abs(train_roi - validation_roi)
    return min(train_roi, validation_roi) - drawdown_penalty - instability_penalty


def fetch_latest_search(session: Session, wallet: str, rule_name: str) -> Optional[SearchRunResult]:
    row = session.execute(
        text(
            "SELECT * FROM simulation_strategy_search_runs "
            "WHERE wallet = :wallet AND rule_name = :rule "
            "ORDER BY run_ts DESC, id DESC LIMIT 1"
        ),
        {"wallet": wallet.lower(), "rule": rule_name.lower()},
    ).mappings().fetchone()
    if row is None:
        return None
    return fetch_search_run(session, int(row["id"]))


def fetch_search_run(session: Session, run_id: int) -> Optional[SearchRunResult]:
    run_row = session.execute(
        text("SELECT * FROM simulation_strategy_search_runs WHERE id = :id"),
        {"id": run_id},
    ).mappings().fetchone()
    if run_row is None:
        return None
    candidates = _fetch_candidates(session, run_id)
    return SearchRunResult(
        run_id=int(run_row["id"]),
        wallet=run_row["wallet"],
        rule_name=run_row["rule_name"],
        strategy_family=run_row["strategy_family"],
        seed=int(run_row["seed"]),
        max_combos=int(run_row["max_combos"]),
        total_combos=int(run_row["total_combos"]),
        evaluated_combos=int(run_row["evaluated_combos"]),
        candidates=candidates,
        selected_candidate_id=run_row["selected_candidate_id"],
        elapsed_ms=int(run_row["elapsed_ms"] or 0),
    )


def generate_search_report(result: SearchRunResult, *, limit: int = 10) -> str:
    lines = [
        f"# Strategy Search: {result.rule_name}",
        "",
        f"- **Wallet:** `{result.wallet}`",
        f"- **Search run:** {result.run_id}",
        f"- **Seed:** {result.seed}",
        f"- **Evaluated combos:** {result.evaluated_combos} / {result.total_combos}",
        f"- **Selected candidate:** {result.selected_candidate_id or 'none'}",
        "",
    ]
    ranked = result.ranked_candidates[:limit]
    if not ranked:
        lines.extend(
            [
                "## Top Candidates",
                "",
                "No eligible candidates found. Eligibility requires train and validation conservative pass, "
                "zero risk breaches, no ordering violation, positive net PnL, and enough simulated fills.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "## Top Candidates",
            "",
            "| Rank | Candidate | Score | Train net PnL | Validation net PnL | Test net PnL | Train fills | Validation fills | Test fills | Validation pass | Test pass | Final status |",
            "|------|-----------|-------|---------------|--------------------|--------------|-------------|------------------|------------|-----------------|-----------|--------------|",
        ]
    )
    for candidate in ranked:
        train = candidate.metrics["train"]
        validation = candidate.metrics["validation"]
        test = candidate.metrics["test"]
        lines.append(
            f"| {candidate.rank_index} | {candidate.candidate_id} | {_fmt_decimal(selection_score(candidate))} | "
            f"{_fmt_usdc(train.net_pnl)} | {_fmt_usdc(validation.net_pnl)} | {_fmt_usdc(test.net_pnl)} | "
            f"{train.simulated_fills_count} | {validation.simulated_fills_count} | {test.simulated_fills_count} | "
            f"{_fmt_bool(_split_passes(validation, min_fills=DEFAULT_MIN_VALIDATION_FILLS))} | "
            f"{_fmt_bool(candidate_test_passes(candidate))} | {final_status(candidate)} |"
        )

    lines.extend(["", "## Metrics", ""])
    for candidate in ranked:
        lines.extend(_candidate_lines(candidate))
    return "\n".join(lines)


def write_search_report(result: SearchRunResult, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(generate_search_report(result), encoding="utf-8")


def top_candidates(
    session: Session,
    wallet: str,
    rule_name: str,
    *,
    limit: int,
    eligible_only: bool = False,
    min_test_fills: int = DEFAULT_MIN_TEST_FILLS,
) -> list[SearchCandidate]:
    latest = fetch_latest_search(session, wallet, rule_name)
    if latest is None:
        return []
    candidates = latest.ranked_candidates
    if eligible_only:
        candidates = [
            candidate
            for candidate in candidates
            if candidate_selected_pretest(candidate) and candidate_test_passes(candidate, min_test_fills=min_test_fills)
        ]
    return candidates[:limit]


def _metric_from_transient(
    *,
    split_name: str,
    transient: _TransientResult,
    conservative_pass: bool,
    ordering_violation: bool,
    score: Optional[Decimal],
) -> SearchMetric:
    return SearchMetric(
        split_name=split_name,
        candidate_signals_count=transient.candidate_signals_count,
        accepted_orders_count=transient.orders_count,
        skipped_orders_count=transient.skipped_orders_count,
        skipped_by_reason=dict(transient.skipped_by_reason),
        simulated_fills_count=transient.fills_count,
        fill_rate_on_candidates=transient.fill_rate,
        net_pnl=transient.net_pnl,
        max_drawdown=transient.max_drawdown,
        max_inventory=transient.max_inventory,
        capital_required=transient.capital_required,
        turnover=transient.turnover,
        risk_breaches=transient.risk_breaches,
        risk_prevented_count=transient.risk_prevented_count,
        ordering_violation=ordering_violation,
        conservative_pass=conservative_pass,
        score=score,
    )


def _score(transient: _TransientResult) -> Decimal:
    return transient.net_pnl / max(transient.capital_required, Decimal("1"))


def _split_passes(metric: SearchMetric, *, min_fills: int) -> bool:
    return (
        metric.conservative_pass
        and metric.net_pnl > Decimal("0")
        and metric.risk_breaches == 0
        and not metric.ordering_violation
        and metric.simulated_fills_count >= min_fills
    )


def _metric_roi(metric: SearchMetric) -> Decimal:
    return metric.net_pnl / max(metric.capital_required, Decimal("1"))


def _drawdown_ratio(metric: SearchMetric) -> Decimal:
    return metric.max_drawdown / max(metric.capital_required, Decimal("1"))


def _grid(rule_name: str) -> dict[str, list[object]]:
    if rule_name == "completion_set_edge":
        return RN1_GRID
    if rule_name == "spread_capture":
        return GAP_GRID
    raise ValueError("Supported searches are completion_set_edge and spread_capture")


def _validate_search_pair(wallet: str, rule_name: str) -> None:
    if rule_name == "completion_set_edge" and wallet == RN1_WALLET:
        return
    if rule_name == "spread_capture" and wallet == GAP_WALLET:
        return
    raise ValueError(
        f"Supported searches are {RN1_WALLET}: completion_set_edge and "
        f"{GAP_WALLET}: spread_capture"
    )


def _strategy_family(rule_name: str) -> str:
    return "rn1_completion_set_edge" if rule_name == "completion_set_edge" else "gap_spread_capture"


def _insert_search_run(
    *,
    session: Session,
    wallet: str,
    rule_name: str,
    strategy_family: str,
    seed: int,
    max_combos: int,
    total_combos: int,
) -> int:
    result = session.execute(
        text(
            "INSERT INTO simulation_strategy_search_runs "
            "(wallet, rule_name, strategy_family, seed, max_combos, total_combos, "
            "evaluated_combos, selected_candidate_id, run_ts, elapsed_ms, status, notes) "
            "VALUES "
            "(:wallet, :rule_name, :strategy_family, :seed, :max_combos, :total_combos, "
            "0, NULL, :run_ts, 0, 'running', '')"
        ),
        {
            "wallet": wallet,
            "rule_name": rule_name,
            "strategy_family": strategy_family,
            "seed": seed,
            "max_combos": max_combos,
            "total_combos": total_combos,
            "run_ts": int(time.time()),
        },
    )
    return int(result.lastrowid)


def _insert_candidate(
    *,
    session: Session,
    search_run_id: int,
    candidate_index: int,
    strategy_name: str,
    parameters: dict[str, object],
) -> int:
    result = session.execute(
        text(
            "INSERT INTO simulation_strategy_candidates "
            "(search_run_id, candidate_index, strategy_name, parameter_json, "
            "rank_index, eligible, selected_for_test) "
            "VALUES "
            "(:search_run_id, :candidate_index, :strategy_name, :parameter_json, "
            "NULL, 0, 0)"
        ),
        {
            "search_run_id": search_run_id,
            "candidate_index": candidate_index,
            "strategy_name": strategy_name,
            "parameter_json": json.dumps(parameters, sort_keys=True),
        },
    )
    return int(result.lastrowid)


def _insert_candidate_metrics(
    session: Session,
    candidate_id: int,
    metrics: dict[str, SearchMetric],
) -> None:
    rows = []
    for metric in metrics.values():
        rows.append(
            {
                "candidate_id": candidate_id,
                "split_name": metric.split_name,
                "candidate_signals_count": metric.candidate_signals_count,
                "accepted_orders_count": metric.accepted_orders_count,
                "skipped_orders_count": metric.skipped_orders_count,
                "skipped_by_reason_json": json.dumps(metric.skipped_by_reason, sort_keys=True),
                "simulated_fills_count": metric.simulated_fills_count,
                "fill_rate_on_candidates": (
                    str(metric.fill_rate_on_candidates)
                    if metric.fill_rate_on_candidates is not None
                    else None
                ),
                "net_pnl": str(metric.net_pnl),
                "max_drawdown": str(metric.max_drawdown),
                "max_inventory": str(metric.max_inventory),
                "capital_required": str(metric.capital_required),
                "turnover": str(metric.turnover),
                "risk_breaches": metric.risk_breaches,
                "risk_prevented_count": metric.risk_prevented_count,
                "ordering_violation": 1 if metric.ordering_violation else 0,
                "conservative_pass": 1 if metric.conservative_pass else 0,
                "score": str(metric.score) if metric.score is not None else None,
            }
        )
    session.execute(
        text(
            "INSERT INTO simulation_strategy_candidate_metrics "
            "(candidate_id, split_name, candidate_signals_count, accepted_orders_count, "
            "skipped_orders_count, skipped_by_reason_json, simulated_fills_count, "
            "fill_rate_on_candidates, net_pnl, max_drawdown, max_inventory, "
            "capital_required, turnover, risk_breaches, risk_prevented_count, "
            "ordering_violation, conservative_pass, score) "
            "VALUES "
            "(:candidate_id, :split_name, :candidate_signals_count, :accepted_orders_count, "
            ":skipped_orders_count, :skipped_by_reason_json, :simulated_fills_count, "
            ":fill_rate_on_candidates, :net_pnl, :max_drawdown, :max_inventory, "
            ":capital_required, :turnover, :risk_breaches, :risk_prevented_count, "
            ":ordering_violation, :conservative_pass, :score)"
        ),
        rows,
    )


def _fetch_candidates(session: Session, search_run_id: int) -> list[SearchCandidate]:
    rows = session.execute(
        text(
            "SELECT * FROM simulation_strategy_candidates "
            "WHERE search_run_id = :id ORDER BY candidate_index"
        ),
        {"id": search_run_id},
    ).mappings().fetchall()
    candidates = []
    for row in rows:
        candidate = SearchCandidate(
            candidate_id=int(row["id"]),
            candidate_index=int(row["candidate_index"]),
            strategy_name=row["strategy_name"],
            parameters=_json_dict(row["parameter_json"]),
            metrics=_fetch_metrics(session, int(row["id"])),
            rank_index=row["rank_index"],
        )
        candidates.append(candidate)
    return candidates


def _fetch_metrics(session: Session, candidate_id: int) -> dict[str, SearchMetric]:
    rows = session.execute(
        text(
            "SELECT * FROM simulation_strategy_candidate_metrics "
            "WHERE candidate_id = :id"
        ),
        {"id": candidate_id},
    ).mappings().fetchall()
    return {row["split_name"]: _metric_from_row(dict(row)) for row in rows}


def _metric_from_row(row: dict) -> SearchMetric:
    return SearchMetric(
        split_name=row["split_name"],
        candidate_signals_count=int(row["candidate_signals_count"]),
        accepted_orders_count=int(row["accepted_orders_count"]),
        skipped_orders_count=int(row["skipped_orders_count"]),
        skipped_by_reason=_json_int_dict(row["skipped_by_reason_json"]),
        simulated_fills_count=int(row["simulated_fills_count"]),
        fill_rate_on_candidates=(
            Decimal(row["fill_rate_on_candidates"])
            if row["fill_rate_on_candidates"] is not None
            else None
        ),
        net_pnl=Decimal(str(row["net_pnl"])),
        max_drawdown=Decimal(str(row["max_drawdown"])),
        max_inventory=Decimal(str(row["max_inventory"])),
        capital_required=Decimal(str(row["capital_required"])),
        turnover=Decimal(str(row["turnover"])),
        risk_breaches=int(row["risk_breaches"]),
        risk_prevented_count=int(row["risk_prevented_count"]),
        ordering_violation=bool(row["ordering_violation"]),
        conservative_pass=bool(row["conservative_pass"]),
        score=Decimal(str(row["score"])) if row["score"] is not None else None,
    )


def _candidate_lines(candidate: SearchCandidate) -> list[str]:
    lines = [
        f"### Candidate {candidate.candidate_id} (rank {candidate.rank_index})",
        "",
        f"`{json.dumps(candidate.parameters, sort_keys=True)}`",
        "",
        "| Split | Signals | Orders | Skipped | Fills | Fill Rate | Net PnL | Max DD | Capital | Breaches | Ordering | Pass | Score |",
        "|-------|---------|--------|---------|-------|-----------|---------|--------|---------|----------|----------|------|-------|",
    ]
    for split in SPLIT_NAMES:
        metric = candidate.metrics[split]
        lines.append(
            f"| {split} | {metric.candidate_signals_count} | {metric.accepted_orders_count} | "
            f"{metric.skipped_orders_count} | {metric.simulated_fills_count} | "
            f"{_fmt_pct(metric.fill_rate_on_candidates)} | {_fmt_usdc(metric.net_pnl)} | "
            f"{_fmt_usdc(metric.max_drawdown)} | {_fmt_usdc(metric.capital_required)} | "
            f"{metric.risk_breaches} | {'yes' if metric.ordering_violation else 'no'} | "
            f"{'yes' if metric.conservative_pass else 'no'} | {_fmt_decimal(metric.score)} |"
        )
    lines.append("")
    return lines


def _json_dict(raw: Optional[str]) -> dict[str, object]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_int_dict(raw: Optional[str]) -> dict[str, int]:
    parsed = _json_dict(raw)
    result = {}
    for key, value in parsed.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def _fmt_usdc(value: Decimal) -> str:
    return f"${value:+.2f}"


def _fmt_pct(value: Optional[Decimal]) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def _fmt_decimal(value: Optional[Decimal]) -> str:
    if value is None:
        return "-"
    return f"{value:.6f}"


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"
