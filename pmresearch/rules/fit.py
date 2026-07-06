"""Phase 21 rule fitting: parameter search + temporal validation.

The fitter loads the ``microstructure_lifecycle_dataset`` for a wallet,
splits it chronologically into train / validation / test, searches over a
parameter grid for each candidate rule (optimising on the training set),
and evaluates the best configuration on all three windows.  Only rules
that maintain positive signal in the out-of-sample test window are
*promoted*.

All rule decisions use only pre-fill features.  Future-information
columns (markout, PnL, close_path, etc.) are used *only* for the
evaluation metrics — never for rule matching.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .base import (
    FitResult,
    Rule,
    RuleDecision,
    SplitMetrics,
    TemporalSplit,
    apply_rule_no_future,
    compute_split_metrics,
    promotion_rejection_reason,
    temporal_split,
)
from .candidate_rules import (
    ALL_CANDIDATE_RULES,
    CompletionSetEdge,
    ClosedCycleEventTrading,
    CorrelatedSiblingMarkets,
    DepthImbalance,
    EventTiming,
    InventoryBalancing,
    SpreadCapture,
)

_ZERO = Decimal("0")


# ── parameter grids ──────────────────────────────────────────────────────────

_PARAM_GRIDS: dict[str, list[dict]] = {
    "spread_capture": [
        {"min_spread_bps": Decimal(v), "min_fill_improvement_bps": Decimal(f)}
        for v in ["20", "50", "100", "200"]
        for f in ["5", "10", "20"]
    ],
    "inventory_balancing": [
        {
            "require_directional_reduction": dr,
            "require_bond_increase": bi,
            "min_abs_directional_before": Decimal(m),
        }
        for dr in [True, False]
        for bi in [True, False]
        for m in ["0.5", "1", "5"]
        if dr or bi  # at least one condition active
    ],
    "completion_set_edge": [
        {"max_bond_cost": Decimal(v)}
        for v in ["0.95", "0.98", "0.99"]
    ],
    "depth_imbalance": [
        {"min_imbalance": Decimal(v), "require_favourable_side": fs}
        for v in ["0.2", "0.3", "0.5"]
        for fs in [True, False]
    ],
    "event_timing": [
        {"allowed_hours_utc": tuple(h for h in hours), "max_time_to_event_start_s": max_tts}
        for hours in [
            (),
            (12, 13, 14, 15, 16, 17, 18, 19, 20),
            (0, 1, 2, 3, 4, 5, 6, 7, 8),
        ]
        for max_tts in [None, 3600, 7200, 86400]
    ],
    "correlated_sibling_markets": [
        {"min_abs_event_exposure": Decimal(v)}
        for v in ["1", "5", "10"]
    ],
    "closed_cycle_event_trading": [
        {"min_spread_bps": Decimal(s), "min_abs_event_exposure": Decimal(e)}
        for s in ["20", "30", "50"]
        for e in ["3", "5", "10"]
    ],
}

_RULE_CLASSES: dict[str, type] = {
    "spread_capture": SpreadCapture,
    "inventory_balancing": InventoryBalancing,
    "completion_set_edge": CompletionSetEdge,
    "depth_imbalance": DepthImbalance,
    "event_timing": EventTiming,
    "correlated_sibling_markets": CorrelatedSiblingMarkets,
    "closed_cycle_event_trading": ClosedCycleEventTrading,
}


# ── dataset loading ──────────────────────────────────────────────────────────


def _load_dataset(session: Session, wallet: str) -> list[dict]:
    """Load the microstructure_lifecycle_dataset for a wallet, ordered by trade_ts."""
    rows = session.execute(
        text(
            "SELECT * FROM microstructure_lifecycle_dataset "
            "WHERE wallet = :w ORDER BY trade_ts, event_id"
        ),
        {"w": wallet.lower()},
    ).mappings().fetchall()
    return [dict(r) for r in rows]


# ── rule application ─────────────────────────────────────────────────────────


def _apply_rule(
    rule: Rule, rows: list[dict]
) -> tuple[dict[int, RuleDecision], list[str]]:
    """Apply *rule* to every row. Returns decisions keyed by row index and
    the list of features used across all decisions."""
    decisions: dict[int, RuleDecision] = {}
    all_features: set[str] = set()
    for idx, row in enumerate(rows):
        dec = rule.applies(row)
        decisions[idx] = dec
        all_features.update(dec.features_used.keys())
    return decisions, sorted(all_features)


def _apply_rule_no_future(
    rule: Rule, rows: list[dict]
) -> tuple[dict[int, RuleDecision], list[str]]:
    """Apply rule and verify no forbidden features were touched."""
    decisions: dict[int, RuleDecision] = {}
    all_features: set[str] = set()
    for idx, row in enumerate(rows):
        dec = apply_rule_no_future(rule, row)
        decisions[idx] = dec
        all_features.update(dec.features_used.keys())
    features = sorted(all_features)
    return decisions, features


# ── scoring for parameter search ─────────────────────────────────────────────


def _train_score(metrics: SplitMetrics) -> Decimal:
    """Combined score for parameter search: precision × fill_explained_rate.

    A rule that explains many fills with high precision scores highest.
    Zero fills explained or zero precision gives zero.
    """
    return metrics.precision * metrics.fill_explained_rate


# ── core fit loop ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CandidateResult:
    """One rule's fit result with parameter search."""

    rule: Rule
    best_params: dict
    fit_result: FitResult


def _fit_single_rule(
    rule_class: type,
    param_grid: list[dict],
    split: TemporalSplit,
    all_rows: list[dict],
) -> CandidateResult:
    """Search *param_grid* for the best configuration of *rule_class*.

    The best configuration is chosen by training score.  The returned
    ``CandidateResult`` carries the rule with the best parameters and
    its full FitResult across all three splits.
    """
    best_score = None
    best_rule: Optional[Rule] = None
    best_decisions: Optional[dict[int, RuleDecision]] = None
    best_features: list[str] = []

    for params in param_grid:
        try:
            rule = rule_class(**params)  # type: ignore[call-arg]
        except TypeError:
            continue

        decisions, features = _apply_rule_no_future(rule, split.train)
        train_metrics = compute_split_metrics(split.train, decisions, label_mode=True)
        score = _train_score(train_metrics)

        if best_score is None or score > best_score:
            best_score = score
            best_rule = rule
            best_decisions = decisions
            best_features = features

    if best_rule is None:
        rule = rule_class()  # type: ignore[call-arg]
        decisions, features = _apply_rule_no_future(rule, split.train)
        best_rule = rule
        best_decisions = decisions
        best_features = features

    train_decisions, _ = _apply_rule_no_future(best_rule, split.train)
    val_decisions, _ = _apply_rule_no_future(best_rule, split.validation)
    test_decisions, _ = _apply_rule_no_future(best_rule, split.test)

    train_m = compute_split_metrics(split.train, train_decisions, label_mode=True)
    val_m = compute_split_metrics(split.validation, val_decisions, label_mode=True)
    test_m = compute_split_metrics(split.test, test_decisions, label_mode=True)

    rejection_reason = promotion_rejection_reason(best_rule, val_m, test_m)
    promoted = rejection_reason is None

    explained_pct = (
        (train_m.explained_fills + val_m.explained_fills + test_m.explained_fills)
        / Decimal(train_m.total_fills + val_m.total_fills + test_m.total_fills)
        if (train_m.total_fills + val_m.total_fills + test_m.total_fills) > 0
        else _ZERO
    )

    expected_pnl: Optional[Decimal] = None
    if test_m.avg_pnl_episode is not None:
        expected_pnl = test_m.avg_pnl_episode
    elif test_m.avg_markout_5m is not None:
        expected_pnl = test_m.avg_markout_5m

    inventory_impact: Optional[Decimal] = None
    if test_m.max_inventory_required is not None:
        inventory_impact = test_m.max_inventory_required

    risk_reqs = (
        f"max_inventory={test_m.max_inventory_required or 'N/A'}, "
        f"max_markout_1h={test_m.avg_markout_1h or 'N/A'}"
    )

    blind_spots = (
        f"Rule {best_rule.name} v{best_rule.version} — "
        "uses only pre-fill features; does not account for execution quality, "
        "slippage, or competition for fills. "
        "Markout/PnL metrics are evaluation-only."
    )

    fit_result = FitResult(
        rule_name=best_rule.name,
        rule_version=best_rule.version,
        parameters=best_rule.parameters,
        features_used=best_features,
        train=train_m,
        validation=val_m,
        test=test_m,
        explained_fills_pct=explained_pct,
        expected_pnl_or_markout=expected_pnl,
        inventory_impact=inventory_impact,
        risk_requirements=risk_reqs,
        blind_spots=blind_spots,
        promoted=promoted,
        promotion_rejection_reason=rejection_reason,
    )

    return CandidateResult(rule=best_rule, best_params=best_rule.parameters, fit_result=fit_result)


# ── public API ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FitStats:
    """Summary of a full fit run."""

    wallet: str
    total_fills: int
    candidates_evaluated: int
    candidates_promoted: int
    results: list[FitResult]


def fit_rules(
    session: Session,
    wallet: str,
    *,
    rule_names: Optional[list[str]] = None,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
) -> FitStats:
    """Fit candidate rules to a wallet's dataset with temporal validation.

    Parameters
    ----------
    session :
        SQLAlchemy session.
    wallet :
        Wallet address.
    rule_names :
        Optional subset of rule names to evaluate.  None = all candidates.
    train_ratio / validation_ratio :
        Temporal split ratios (test gets the remainder).
    """
    wallet = wallet.lower()
    rows = _load_dataset(session, wallet)
    if not rows:
        return FitStats(
            wallet=wallet,
            total_fills=0,
            candidates_evaluated=0,
            candidates_promoted=0,
            results=[],
        )

    split = temporal_split(rows, train_ratio=train_ratio, validation_ratio=validation_ratio)

    targets = rule_names if rule_names else [cls().name for cls in ALL_CANDIDATE_RULES]
    results: list[FitResult] = []

    for rule_name in targets:
        rule_class = _RULE_CLASSES.get(rule_name)
        if rule_class is None:
            continue
        param_grid = _PARAM_GRIDS.get(rule_name, [{}])
        candidate = _fit_single_rule(rule_class, param_grid, split, rows)
        results.append(candidate.fit_result)

    promoted = sum(1 for r in results if r.promoted)

    return FitStats(
        wallet=wallet,
        total_fills=len(rows),
        candidates_evaluated=len(results),
        candidates_promoted=promoted,
        results=results,
    )


def load_dataset_rows(session: Session, wallet: str) -> list[dict]:
    """Public accessor for the raw dataset rows (used by evaluate/report)."""
    return _load_dataset(session, wallet.lower())
