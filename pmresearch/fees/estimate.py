"""Compute per-fill fee estimates without mutating the ledger."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from sqlalchemy import text
from sqlalchemy.orm import Session

from .schedules import FeeRule, NO_FEE_RULE, list_fee_schedules, normalize_category, rule_for

FEE_QUANT = Decimal("0.0001")
DEFAULT_BATCH_SIZE = 25_000


@dataclass(frozen=True)
class FeeEstimate:
    event_id: int
    wallet: str
    condition_id: str | None
    token_id: str | None
    category: str
    ts: int
    estimated_fee: Decimal
    worst_case_fee: Decimal
    actual_fee: Decimal | None
    fee_source: str
    fee_currency: str
    rule_name: str
    confidence: str


@dataclass(frozen=True)
class FeeEstimateStats:
    total_trades: int
    category_classified_trades: int
    fee_estimated_trades: int
    unknown_category_trades: int
    actual_enriched_trades: int
    estimates_upserted: int
    estimated_fee_total: Decimal
    worst_case_fee_total: Decimal
    actual_fee_total: Decimal
    estimated_fee_fallback_total: Decimal
    blended_fee_total: Decimal


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _actual_fee(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fee_formula(shares: Decimal, price: Decimal, fee_rate: Decimal, exponent: int) -> Decimal:
    if shares <= 0 or price <= 0 or price >= 1 or fee_rate <= 0:
        return Decimal(0)
    uncertainty = price * (Decimal(1) - price)
    fee = shares * price * fee_rate * (uncertainty ** exponent)
    return fee.quantize(FEE_QUANT, rounding=ROUND_HALF_EVEN)


def estimate_trade_fee(
    session: Session,
    *,
    category: str | None,
    ts: int,
    side: str | None,
    price: Decimal,
    size: Decimal,
) -> tuple[Decimal, str, str]:
    rule = rule_for(session, category, ts)
    fee = _fee_formula(abs(size), price, rule.fee_rate, rule.exponent)
    if rule.rule_name == "no_fee":
        return fee, rule.rule_name, "no_fee_rule"
    if side not in {"BUY", "SELL"}:
        return fee, rule.rule_name, "low_unknown_side"
    return fee, rule.rule_name, "estimate_taker_assumption_no_maker_taker"


def _rule_from_cache(rules: list[FeeRule], category: str | None, ts: int) -> FeeRule:
    normalized = normalize_category(category)
    matching = [
        rule
        for rule in rules
        if rule.category == normalized
        and rule.effective_from_ts <= ts
        and (rule.effective_to_ts is None or rule.effective_to_ts > ts)
    ]
    if not matching:
        return NO_FEE_RULE
    return max(matching, key=lambda rule: rule.effective_from_ts)


def _estimate_trade_fee_with_rules(
    rules: list[FeeRule],
    *,
    category: str | None,
    ts: int,
    side: str | None,
    price: Decimal,
    size: Decimal,
) -> tuple[Decimal, str, str]:
    rule = _rule_from_cache(rules, category, ts)
    fee = _fee_formula(abs(size), price, rule.fee_rate, rule.exponent)
    if rule.rule_name == "no_fee":
        return fee, rule.rule_name, "no_fee_rule"
    if side not in {"BUY", "SELL"}:
        return fee, rule.rule_name, "low_unknown_side"
    return fee, rule.rule_name, "estimate_taker_assumption_no_maker_taker"


def _trade_count(session: Session, wallet: str | None) -> int:
    query = "SELECT COUNT(*) FROM wallet_events WHERE event_type = 'TRADE' "
    params = {}
    if wallet:
        query += "AND lower(wallet) = lower(:wallet) "
        params["wallet"] = wallet
    return int(session.execute(text(query), params).scalar() or 0)


def _trade_rows(session: Session, wallet: str | None, *, after_id: int, limit: int):
    query = (
        "SELECT we.id AS event_id, we.wallet, we.condition_id, we.token_id, we.side, "
        "we.ts, we.delta_shares, we.price, m.category AS market_category, "
        "fen.fee AS observed_fee, fen.source AS observed_source "
        "FROM wallet_events we "
        "LEFT JOIN markets m ON m.condition_id = lower(we.condition_id) "
        "LEFT JOIN fill_enrichment fen ON fen.event_id = we.id "
        "WHERE we.event_type = 'TRADE' AND we.id > :after_id "
    )
    params = {"after_id": after_id, "limit": limit}
    if wallet:
        query += "AND lower(we.wallet) = lower(:wallet) "
        params["wallet"] = wallet
    query += "ORDER BY we.id LIMIT :limit"
    return session.execute(text(query), params).fetchall()


def compute_fee_estimates(
    session: Session,
    wallet: str | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_progress: Callable[[int, int], None] | None = None,
) -> FeeEstimateStats:
    rules = list_fee_schedules(session)
    total_trades = _trade_count(session, wallet)
    if on_progress is not None:
        on_progress(0, total_trades)

    now = datetime.now(timezone.utc).isoformat()
    estimates_upserted = 0
    estimated_total = Decimal(0)
    worst_case_total = Decimal(0)
    actual_total = Decimal(0)
    fallback_total = Decimal(0)
    blended_total = Decimal(0)
    category_classified = 0
    fee_estimated = 0
    actual_enriched = 0
    unknown_category = 0
    last_id = 0
    batch_size = max(1, int(batch_size))
    upsert = text(
        "INSERT INTO fee_estimates "
        "(event_id, wallet, condition_id, token_id, category, ts, estimated_fee, worst_case_fee, "
        "actual_fee, fee_source, fee_currency, rule_name, confidence, computed_at) "
        "VALUES (:event_id, :wallet, :condition_id, :token_id, :category, :ts, "
        ":estimated_fee, :worst_case_fee, :actual_fee, :fee_source, 'USDC', "
        ":rule_name, :confidence, :computed_at) "
        "ON CONFLICT(event_id) DO UPDATE SET "
        "wallet = excluded.wallet, condition_id = excluded.condition_id, "
        "token_id = excluded.token_id, category = excluded.category, ts = excluded.ts, "
        "estimated_fee = excluded.estimated_fee, worst_case_fee = excluded.worst_case_fee, "
        "actual_fee = excluded.actual_fee, fee_source = excluded.fee_source, "
        "fee_currency = excluded.fee_currency, rule_name = excluded.rule_name, "
        "confidence = excluded.confidence, computed_at = excluded.computed_at"
    )

    while True:
        rows = _trade_rows(session, wallet, after_id=last_id, limit=batch_size)
        if not rows:
            break

        batch_params = []
        for row in rows:
            last_id = int(row.event_id)
            if row.market_category is None:
                unknown_category += 1
            else:
                category_classified += 1
            category = normalize_category(row.market_category)
            size = abs(_decimal(row.delta_shares))
            fee, rule_name, confidence = _estimate_trade_fee_with_rules(
                rules,
                category=category,
                ts=int(row.ts),
                side=row.side,
                price=_decimal(row.price),
                size=size,
            )
            if fee > 0:
                fee_estimated += 1
            estimated_total += fee
            worst_case_total += fee
            actual = _actual_fee(row.observed_fee)
            if actual is not None:
                actual_enriched += 1
                actual_total += actual
                blended_total += actual
                fee_source = f"actual_{row.observed_source or 'unknown'}"
            else:
                fallback_total += fee
                blended_total += fee
                fee_source = "estimated_schedule"
            batch_params.append(
                {
                    "event_id": int(row.event_id),
                    "wallet": row.wallet,
                    "condition_id": row.condition_id,
                    "token_id": row.token_id,
                    "category": category,
                    "ts": int(row.ts),
                    "estimated_fee": str(fee),
                    "worst_case_fee": str(fee),
                    "actual_fee": str(actual) if actual is not None else None,
                    "fee_source": fee_source,
                    "rule_name": rule_name,
                    "confidence": confidence,
                    "computed_at": now,
                }
            )

        session.execute(upsert, batch_params)
        session.commit()
        estimates_upserted += len(batch_params)
        if on_progress is not None:
            on_progress(estimates_upserted, total_trades)

    session.commit()
    return FeeEstimateStats(
        total_trades=total_trades,
        category_classified_trades=category_classified,
        fee_estimated_trades=fee_estimated,
        unknown_category_trades=unknown_category,
        actual_enriched_trades=actual_enriched,
        estimates_upserted=estimates_upserted,
        estimated_fee_total=estimated_total,
        worst_case_fee_total=worst_case_total,
        actual_fee_total=actual_total,
        estimated_fee_fallback_total=fallback_total,
        blended_fee_total=blended_total,
    )
