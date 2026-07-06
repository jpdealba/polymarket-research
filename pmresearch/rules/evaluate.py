"""Phase 21 rule evaluation: apply a fitted rule to the dataset and compute
per-fill explanations.

This module provides:
- ``evaluate_rule``: apply a rule to the full dataset and return split metrics.
- ``explain_fill``: explain why a specific fill was or wasn't matched by a rule.
- ``persistence``: store/load rule results from ``strategy_candidates`` and
  ``rule_evaluations`` tables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db.retry import retry_locked
from .base import (
    FitResult,
    Rule,
    RuleDecision,
    SplitMetrics,
    apply_rule_no_future,
    compute_split_metrics,
    promotion_rejection_reason,
    temporal_split,
)

_ZERO = Decimal("0")


# ── evaluation ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvalResult:
    """Full evaluation of a rule over a wallet's dataset."""

    wallet: str
    rule_name: str
    rule_version: int
    parameters: dict
    features_used: list[str]
    train: SplitMetrics
    validation: SplitMetrics
    test: SplitMetrics
    explained_fills_pct: Decimal
    promoted: bool
    fill_details: list[FillDetail]
    promotion_rejection_reason: Optional[str] = None


@dataclass(frozen=True)
class FillDetail:
    """One fill's evaluation result (for explain-fill / export)."""

    event_id: int
    trade_ts: int
    trade_utc: str
    token_id: str
    side: Optional[str]
    fill_price: Optional[str]
    applies: bool
    explanation: str
    features_used: dict[str, Optional[str]]
    # evaluation-only columns (never used for rule matching)
    markout_5m: Optional[str] = None
    markout_1h: Optional[str] = None
    pnl_episode: Optional[str] = None


def evaluate_rule(
    session: Session,
    wallet: str,
    rule: Rule,
    *,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
) -> EvalResult:
    """Apply *rule* to a wallet's dataset and compute temporal metrics."""
    wallet = wallet.lower()
    rows = session.execute(
        text(
            "SELECT * FROM microstructure_lifecycle_dataset "
            "WHERE wallet = :w ORDER BY trade_ts, event_id"
        ),
        {"w": wallet},
    ).mappings().fetchall()
    rows = [dict(r) for r in rows]

    if not rows:
        empty_m = SplitMetrics(
            total_fills=0, explained_fills=0, fill_explained_rate=_ZERO,
            false_positives=0, false_positive_rate=_ZERO,
            precision=_ZERO, coverage=_ZERO,
            avg_markout_5m=None, avg_markout_1h=None,
            avg_pnl_episode=None, avg_bond_delta=None,
            avg_exposure_delta=None, max_inventory_required=None,
            out_of_sample_edge_bps=None, out_of_sample_pnl=None,
        )
        return EvalResult(
            wallet=wallet, rule_name=rule.name, rule_version=rule.version,
            parameters=rule.parameters, features_used=[],
            train=empty_m, validation=empty_m, test=empty_m,
            explained_fills_pct=_ZERO, promoted=False, fill_details=[],
        )

    split = temporal_split(rows, train_ratio=train_ratio, validation_ratio=validation_ratio)
    all_features: set[str] = set()

    decisions_per_window: dict[str, dict[int, RuleDecision]] = {}
    for name, window in [("train", split.train), ("validation", split.validation), ("test", split.test)]:
        decs: dict[int, RuleDecision] = {}
        for idx, row in enumerate(window):
            dec = apply_rule_no_future(rule, row)
            decs[idx] = dec
            all_features.update(dec.features_used.keys())
        decisions_per_window[name] = decs

    train_m = compute_split_metrics(split.train, decisions_per_window["train"], label_mode=True)
    val_m = compute_split_metrics(split.validation, decisions_per_window["validation"], label_mode=True)
    test_m = compute_split_metrics(split.test, decisions_per_window["test"], label_mode=True)

    total = len(rows)
    explained = sum(
        1
        for window_decs in decisions_per_window.values()
        for d in window_decs.values()
        if d.applies
    )
    explained_pct = Decimal(explained) / Decimal(total) if total > 0 else _ZERO

    rejection_reason = promotion_rejection_reason(rule, val_m, test_m)
    promoted = rejection_reason is None

    details: list[FillDetail] = []
    for idx, row in enumerate(rows):
        # Determine which window this row is in
        if idx < len(split.train):
            window = "train"
            local_idx = idx
        elif idx < len(split.train) + len(split.validation):
            window = "validation"
            local_idx = idx - len(split.train)
        else:
            window = "test"
            local_idx = idx - len(split.train) - len(split.validation)

        dec = decisions_per_window[window][local_idx]
        details.append(FillDetail(
            event_id=int(row.get("event_id", 0)),
            trade_ts=int(row.get("trade_ts", 0)),
            trade_utc=row.get("trade_utc", ""),
            token_id=row.get("token_id", ""),
            side=row.get("side"),
            fill_price=row.get("fill_price"),
            applies=dec.applies,
            explanation=dec.explanation,
            features_used=dec.features_used,
            markout_5m=row.get("markout_5m"),
            markout_1h=row.get("markout_1h"),
            pnl_episode=row.get("pnl_episode"),
        ))

    return EvalResult(
        wallet=wallet,
        rule_name=rule.name,
        rule_version=rule.version,
        parameters=rule.parameters,
        features_used=sorted(all_features),
        train=train_m,
        validation=val_m,
        test=test_m,
        explained_fills_pct=explained_pct,
        promoted=promoted,
        fill_details=details,
        promotion_rejection_reason=rejection_reason,
    )


# ── explain fill ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExplainResult:
    """Explanation of one fill's rule evaluation."""

    event_id: int
    wallet: str
    rule_name: str
    rule_version: int
    applies: bool
    explanation: str
    features_used: dict[str, Optional[str]]
    fill_context: dict[str, Optional[str]]


def explain_fill(
    session: Session,
    event_id: int,
    rule: Rule,
) -> Optional[ExplainResult]:
    """Explain why a specific fill was or wasn't matched by a rule.

    Returns None if the event_id is not found.
    """
    row = session.execute(
        text(
            "SELECT * FROM microstructure_lifecycle_dataset WHERE event_id = :eid"
        ),
        {"eid": event_id},
    ).mappings().fetchone()
    if row is None:
        return None
    row_dict = dict(row)

    decision = apply_rule_no_future(rule, row_dict)

    fill_context = {k: row_dict.get(k) for k in (
        "wallet", "token_id", "condition_id", "side", "fill_price",
        "fill_size", "delta_usdc", "trade_utc",
    )}

    return ExplainResult(
        event_id=event_id,
        wallet=row_dict.get("wallet", ""),
        rule_name=rule.name,
        rule_version=rule.version,
        applies=decision.applies,
        explanation=decision.explanation,
        features_used=decision.features_used,
        fill_context=fill_context,
    )


def fit_result_from_eval(result: EvalResult) -> FitResult:
    """Convert an EvalResult into the report/persistence shape."""
    expected_pnl = (
        result.test.avg_pnl_episode
        if result.test.avg_pnl_episode is not None
        else result.test.avg_markout_5m
    )
    inventory_impact = result.test.max_inventory_required
    risk_reqs = (
        f"max_inventory={result.test.max_inventory_required or 'N/A'}, "
        f"max_markout_1h={result.test.avg_markout_1h or 'N/A'}"
    )
    blind_spots = (
        f"Rule {result.rule_name} v{result.rule_version} uses only pre-fill "
        "features; markout/PnL metrics are evaluation-only."
    )
    return FitResult(
        rule_name=result.rule_name,
        rule_version=result.rule_version,
        parameters=result.parameters,
        features_used=result.features_used,
        train=result.train,
        validation=result.validation,
        test=result.test,
        explained_fills_pct=result.explained_fills_pct,
        expected_pnl_or_markout=expected_pnl,
        inventory_impact=inventory_impact,
        risk_requirements=risk_reqs,
        blind_spots=blind_spots,
        promoted=result.promoted,
        promotion_rejection_reason=result.promotion_rejection_reason,
    )


# ── persistence ──────────────────────────────────────────────────────────────


def store_fit_result(
    session: Session,
    wallet: str,
    fit_result: FitResult,
) -> None:
    """Persist a FitResult into strategy_candidates and rule_evaluations."""
    wallet = wallet.lower()
    now = datetime.now(timezone.utc).isoformat()

    def _write() -> None:
        session.execute(
            text(
                "DELETE FROM rule_evaluations "
                "WHERE wallet = :wallet AND rule_name = :rule_name "
                "AND rule_version = :rule_version"
            ),
            {
                "wallet": wallet,
                "rule_name": fit_result.rule_name,
                "rule_version": fit_result.rule_version,
            },
        )
        session.execute(
            text(
                "DELETE FROM strategy_candidates "
                "WHERE wallet = :wallet AND rule_name = :rule_name "
                "AND rule_version = :rule_version"
            ),
            {
                "wallet": wallet,
                "rule_name": fit_result.rule_name,
                "rule_version": fit_result.rule_version,
            },
        )
        session.execute(
            text(
                "INSERT INTO strategy_candidates "
                "(wallet, rule_name, rule_version, parameters_json, features_used_json, "
                "promoted, explained_fills_pct, expected_pnl_or_markout, "
                "inventory_impact, risk_requirements, blind_spots, fitted_at) "
                "VALUES (:wallet, :rule_name, :rule_version, :params, :features, "
                ":promoted, :explained_pct, :expected_pnl, :inv_impact, :risk, "
                ":blind, :fitted_at)"
            ),
            {
                "wallet": wallet,
                "rule_name": fit_result.rule_name,
                "rule_version": fit_result.rule_version,
                "params": json.dumps(fit_result.parameters, sort_keys=True, default=str),
                "features": json.dumps(fit_result.features_used),
                "promoted": 1 if fit_result.promoted else 0,
                "explained_pct": str(fit_result.explained_fills_pct),
                "expected_pnl": (
                    str(fit_result.expected_pnl_or_markout)
                    if fit_result.expected_pnl_or_markout is not None else None
                ),
                "inv_impact": (
                    str(fit_result.inventory_impact)
                    if fit_result.inventory_impact is not None else None
                ),
                "risk": fit_result.risk_requirements,
                "blind": fit_result.blind_spots,
                "fitted_at": now,
            },
        )

        for window_name, metrics in [
            ("train", fit_result.train),
            ("validation", fit_result.validation),
            ("test", fit_result.test),
        ]:
            session.execute(
                text(
                    "INSERT INTO rule_evaluations "
                    "(wallet, rule_name, rule_version, window, total_fills, "
                    "explained_fills, fill_explained_rate, precision, coverage, "
                    "avg_markout_5m, avg_markout_1h, avg_pnl_episode, "
                    "avg_bond_delta, avg_exposure_delta, max_inventory_required, "
                    "out_of_sample_edge_bps, out_of_sample_pnl, "
                    "promotion_rejection_reason, evaluated_at) "
                    "VALUES (:wallet, :rule_name, :rule_version, :window, :total, "
                    ":explained, :rate, :precision, :coverage, :m5, :m1h, :pnl, "
                    ":bd, :ed, :max_inv, :edge_bps, :oos_pnl, "
                    ":promotion_rejection_reason, :evaluated_at)"
                ),
                {
                    "wallet": wallet,
                    "rule_name": fit_result.rule_name,
                    "rule_version": fit_result.rule_version,
                    "window": window_name,
                    "total": metrics.total_fills,
                    "explained": metrics.explained_fills,
                    "rate": str(metrics.fill_explained_rate),
                    "precision": str(metrics.precision),
                    "coverage": str(metrics.coverage),
                    "m5": str(metrics.avg_markout_5m) if metrics.avg_markout_5m is not None else None,
                    "m1h": str(metrics.avg_markout_1h) if metrics.avg_markout_1h is not None else None,
                    "pnl": str(metrics.avg_pnl_episode) if metrics.avg_pnl_episode is not None else None,
                    "bd": str(metrics.avg_bond_delta) if metrics.avg_bond_delta is not None else None,
                    "ed": str(metrics.avg_exposure_delta) if metrics.avg_exposure_delta is not None else None,
                    "max_inv": str(metrics.max_inventory_required) if metrics.max_inventory_required is not None else None,
                    "edge_bps": str(metrics.out_of_sample_edge_bps) if metrics.out_of_sample_edge_bps is not None else None,
                    "oos_pnl": str(metrics.out_of_sample_pnl) if metrics.out_of_sample_pnl is not None else None,
                    "promotion_rejection_reason": fit_result.promotion_rejection_reason,
                    "evaluated_at": now,
                },
            )
        session.commit()

    retry_locked(session, _write)


@dataclass(frozen=True)
class StoredCandidate:
    """A row from strategy_candidates."""

    wallet: str
    rule_name: str
    rule_version: int
    parameters: dict
    features_used: list[str]
    promoted: bool
    explained_fills_pct: str
    expected_pnl_or_markout: Optional[str]
    inventory_impact: Optional[str]
    risk_requirements: str
    blind_spots: str
    fitted_at: str


def _stored_has_active_predicate(rule_name: str, parameters: dict) -> bool:
    if rule_name == "event_timing":
        return bool(
            parameters.get("allowed_hours_utc")
            or parameters.get("max_time_to_event_start_s") is not None
            or parameters.get("min_time_to_event_start_s") is not None
        )
    return True


def _decimal_or_none(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None
    return Decimal(value)


def _stored_split_metrics(row: Optional[dict]) -> SplitMetrics:
    if row is None:
        return SplitMetrics(
            total_fills=0,
            explained_fills=0,
            fill_explained_rate=_ZERO,
            false_positives=0,
            false_positive_rate=_ZERO,
            precision=_ZERO,
            coverage=_ZERO,
            avg_markout_5m=None,
            avg_markout_1h=None,
            avg_pnl_episode=None,
            avg_bond_delta=None,
            avg_exposure_delta=None,
            max_inventory_required=None,
            out_of_sample_edge_bps=None,
            out_of_sample_pnl=None,
        )
    return SplitMetrics(
        total_fills=int(row["total_fills"]),
        explained_fills=int(row["explained_fills"]),
        fill_explained_rate=Decimal(row["fill_explained_rate"]),
        false_positives=0,
        false_positive_rate=_ZERO,
        precision=Decimal(row["precision"]),
        coverage=Decimal(row["coverage"]),
        avg_markout_5m=_decimal_or_none(row["avg_markout_5m"]),
        avg_markout_1h=_decimal_or_none(row["avg_markout_1h"]),
        avg_pnl_episode=_decimal_or_none(row["avg_pnl_episode"]),
        avg_bond_delta=_decimal_or_none(row["avg_bond_delta"]),
        avg_exposure_delta=_decimal_or_none(row["avg_exposure_delta"]),
        max_inventory_required=_decimal_or_none(row["max_inventory_required"]),
        out_of_sample_edge_bps=_decimal_or_none(row["out_of_sample_edge_bps"]),
        out_of_sample_pnl=_decimal_or_none(row["out_of_sample_pnl"]),
    )


def fetch_candidates(
    session: Session,
    wallet: str,
    *,
    promoted_only: bool = False,
) -> list[StoredCandidate]:
    """Load strategy_candidates for a wallet."""
    where = ["wallet = :w"]
    params: dict = {"w": wallet.lower()}
    rows = session.execute(
        text(
            "SELECT * FROM strategy_candidates "
            f"WHERE {' AND '.join(where)} ORDER BY rule_name, rule_version"
        ),
        params,
    ).mappings().fetchall()
    candidates: list[StoredCandidate] = []
    for r in rows:
        parameters = json.loads(r["parameters_json"])
        candidates.append(
            StoredCandidate(
                wallet=r["wallet"],
                rule_name=r["rule_name"],
                rule_version=int(r["rule_version"]),
                parameters=parameters,
                features_used=json.loads(r["features_used_json"]),
                promoted=bool(r["promoted"])
                and _stored_has_active_predicate(r["rule_name"], parameters),
                explained_fills_pct=r["explained_fills_pct"],
                expected_pnl_or_markout=r["expected_pnl_or_markout"],
                inventory_impact=r["inventory_impact"],
                risk_requirements=r["risk_requirements"],
                blind_spots=r["blind_spots"],
                fitted_at=r["fitted_at"],
            )
        )
    if promoted_only:
        return [c for c in candidates if c.promoted]
    return candidates


def fetch_stored_fit_results(session: Session, wallet: str) -> list[FitResult]:
    """Load stored candidates and their persisted evaluation metrics."""
    candidates = fetch_candidates(session, wallet)
    if not candidates:
        return []

    results: list[FitResult] = []
    for c in candidates:
        metric_rows = session.execute(
            text(
                "SELECT * FROM rule_evaluations "
                "WHERE wallet = :wallet AND rule_name = :rule_name "
                "AND rule_version = :rule_version"
            ),
            {
                "wallet": c.wallet,
                "rule_name": c.rule_name,
                "rule_version": c.rule_version,
            },
        ).mappings().fetchall()
        by_window = {r["window"]: dict(r) for r in metric_rows}
        rejection_reason = next(
            (
                r.get("promotion_rejection_reason")
                for r in by_window.values()
                if r.get("promotion_rejection_reason")
            ),
            None,
        )
        if rejection_reason is None and not _stored_has_active_predicate(
            c.rule_name, c.parameters
        ):
            rejection_reason = "no_active_predicate"
        results.append(
            FitResult(
                rule_name=c.rule_name,
                rule_version=c.rule_version,
                parameters=c.parameters,
                features_used=c.features_used,
                train=_stored_split_metrics(by_window.get("train")),
                validation=_stored_split_metrics(by_window.get("validation")),
                test=_stored_split_metrics(by_window.get("test")),
                explained_fills_pct=Decimal(c.explained_fills_pct),
                expected_pnl_or_markout=_decimal_or_none(c.expected_pnl_or_markout),
                inventory_impact=_decimal_or_none(c.inventory_impact),
                risk_requirements=c.risk_requirements,
                blind_spots=c.blind_spots,
                promoted=c.promoted,
                promotion_rejection_reason=rejection_reason,
            )
        )
    return results
