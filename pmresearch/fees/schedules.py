"""Versioned fee rules for explanatory fee attribution.

The rules here estimate protocol trading fees from public fee schedules. They
are not written into `wallet_events`, and they remain estimates until a later
enrichment source can provide actual per-fill fees.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

SPORTS_FEE_START_TS = 1774828800  # 2026-03-30T00:00:00Z


@dataclass(frozen=True)
class FeeRule:
    category: str
    effective_from_ts: int
    effective_to_ts: int | None
    rule_name: str
    params: dict[str, str]
    source: str
    notes: str

    @property
    def fee_rate(self) -> Decimal:
        return Decimal(str(self.params.get("fee_rate", "0")))

    @property
    def exponent(self) -> int:
        return int(self.params.get("exponent", "1"))


NO_FEE_RULE = FeeRule(
    category="__default__",
    effective_from_ts=0,
    effective_to_ts=None,
    rule_name="no_fee",
    params={"fee_rate": "0", "exponent": "1"},
    source="pmresearch",
    notes="Default zero-fee rule for categories without a configured fee schedule.",
)

DEFAULT_RULES = (
    NO_FEE_RULE,
    FeeRule(
        category="sports",
        effective_from_ts=SPORTS_FEE_START_TS,
        effective_to_ts=None,
        rule_name="polymarket_sports_taker_fee_v1",
        params={"fee_rate": "0.03", "exponent": "1"},
        source="https://help.polymarket.com/en/articles/13364478-trading-fees",
        notes=(
            "Sports taker fee schedule effective 2026-03-30; "
            "fee = shares * price * feeRate * (price * (1 - price))^exponent."
        ),
    ),
)


def normalize_category(category: str | None) -> str:
    if not category:
        return "unclassified"
    return str(category).strip().lower()


def seed_fee_schedules(session: Session) -> int:
    inserted = 0
    for rule in DEFAULT_RULES:
        result = session.execute(
            text(
                "INSERT INTO fee_schedules "
                "(category, effective_from_ts, effective_to_ts, rule_name, params_json, source, notes) "
                "VALUES (:category, :effective_from_ts, :effective_to_ts, :rule_name, "
                ":params_json, :source, :notes) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "category": rule.category,
                "effective_from_ts": rule.effective_from_ts,
                "effective_to_ts": rule.effective_to_ts,
                "rule_name": rule.rule_name,
                "params_json": json.dumps(rule.params, sort_keys=True),
                "source": rule.source,
                "notes": rule.notes,
            },
        )
        inserted += int(result.rowcount or 0)
    return inserted


def list_fee_schedules(session: Session) -> list[FeeRule]:
    seed_fee_schedules(session)
    rows = session.execute(
        text(
            "SELECT category, effective_from_ts, effective_to_ts, rule_name, "
            "params_json, source, notes "
            "FROM fee_schedules "
            "ORDER BY category, effective_from_ts, rule_name"
        )
    ).fetchall()
    return [_row_to_rule(row) for row in rows]


def _row_to_rule(row) -> FeeRule:
    return FeeRule(
        category=row.category,
        effective_from_ts=int(row.effective_from_ts),
        effective_to_ts=int(row.effective_to_ts) if row.effective_to_ts is not None else None,
        rule_name=row.rule_name,
        params=json.loads(row.params_json),
        source=row.source,
        notes=row.notes or "",
    )


def rule_for(session: Session, category: str | None, ts: int) -> FeeRule:
    seed_fee_schedules(session)
    normalized = normalize_category(category)
    row = session.execute(
        text(
            "SELECT * FROM fee_schedules "
            "WHERE category = :category "
            "AND effective_from_ts <= :ts "
            "AND (effective_to_ts IS NULL OR effective_to_ts > :ts) "
            "ORDER BY effective_from_ts DESC, id DESC "
            "LIMIT 1"
        ),
        {"category": normalized, "ts": int(ts)},
    ).fetchone()
    if row is not None:
        return _row_to_rule(row)
    return NO_FEE_RULE
