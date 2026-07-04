"""Gross/base cashflow and fee-attribution summaries."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..fees.schedules import SPORTS_FEE_START_TS, normalize_category


@dataclass(frozen=True)
class FeeAttributionRow:
    period: str
    category: str
    trade_count: int
    enriched_fee_count: int
    buy_volume: Decimal
    gross_pnl: Decimal
    estimated_fee: Decimal
    worst_case_fee: Decimal
    actual_fee: Decimal | None
    estimated_fee_fallback: Decimal
    blended_fee: Decimal
    maker_trades: int
    taker_trades: int
    maker_volume: Decimal
    taker_volume: Decimal
    maker_fee: Decimal
    taker_fee: Decimal
    fee_source_counts: tuple[tuple[str, int], ...]

    @property
    def estimated_net_pnl(self) -> Decimal:
        return self.gross_pnl - self.estimated_fee

    @property
    def actual_net_pnl(self) -> Decimal | None:
        if self.actual_fee is None:
            return None
        return self.gross_pnl - self.actual_fee

    @property
    def blended_net_pnl(self) -> Decimal:
        return self.gross_pnl - self.blended_fee

    @property
    def actual_fee_coverage_pct(self) -> Decimal:
        if self.trade_count == 0:
            return Decimal(0)
        return Decimal(self.enriched_fee_count) / Decimal(self.trade_count) * Decimal(100)

    @property
    def fee_source_summary(self) -> str:
        if not self.fee_source_counts:
            return "missing"
        return ",".join(f"{source}:{count}" for source, count in self.fee_source_counts)

    @property
    def gross_roi(self) -> Decimal | None:
        if self.buy_volume == 0:
            return None
        return self.gross_pnl / self.buy_volume

    @property
    def estimated_net_roi(self) -> Decimal | None:
        if self.buy_volume == 0:
            return None
        return self.estimated_net_pnl / self.buy_volume


@dataclass(frozen=True)
class FeeAttributionCoverage:
    total_trades: int
    category_classified_trades: int
    fee_estimated_trades: int
    unknown_category_trades: int
    actual_enriched_trades: int
    actual_fee_total: Decimal
    estimated_fee_total: Decimal
    estimated_fee_fallback_total: Decimal
    blended_fee_total: Decimal

    @property
    def actual_fee_coverage_pct(self) -> Decimal:
        if self.total_trades == 0:
            return Decimal(0)
        return Decimal(self.actual_enriched_trades) / Decimal(self.total_trades) * Decimal(100)


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _period(ts: int, pre_post_sports_fee: bool) -> str:
    if not pre_post_sports_fee:
        return "all"
    return "pre_sports_fee" if int(ts) < SPORTS_FEE_START_TS else "post_sports_fee"


def fee_attribution_report(
    session: Session,
    *,
    wallet: str,
    by_category: bool = False,
    pre_post_sports_fee: bool = False,
) -> list[FeeAttributionRow]:
    rows = session.execute(
        text(
            "SELECT we.event_type, we.side, we.ts, we.delta_usdc, we.usdc_size, "
            "COALESCE(fe.category, m.category, 'unclassified') AS category, "
            "fe.estimated_fee, fe.worst_case_fee, fe.actual_fee, "
            "COALESCE(fe.fee_source, 'missing') AS fee_source, fen.role AS fill_role "
            "FROM wallet_events we "
            "LEFT JOIN markets m ON m.condition_id = we.condition_id "
            "LEFT JOIN fee_estimates fe ON fe.event_id = we.id "
            "LEFT JOIN fill_enrichment fen ON fen.event_id = we.id "
            "WHERE we.wallet = :wallet "
            "ORDER BY we.ts, we.id"
        ),
        {"wallet": wallet},
    ).fetchall()

    buckets: dict[tuple[str, str], dict[str, Decimal]] = {}
    for row in rows:
        period = _period(int(row.ts), pre_post_sports_fee)
        category = normalize_category(row.category) if by_category else "all"
        key = (period, category)
        bucket = buckets.setdefault(
            key,
            {
                "buy_volume": Decimal(0),
                "gross_pnl": Decimal(0),
                "estimated_fee": Decimal(0),
                "worst_case_fee": Decimal(0),
                "actual_fee": Decimal(0),
                "estimated_fee_fallback": Decimal(0),
                "blended_fee": Decimal(0),
                "trade_count": Decimal(0),
                "actual_count": Decimal(0),
                "maker_trades": Decimal(0),
                "taker_trades": Decimal(0),
                "maker_volume": Decimal(0),
                "taker_volume": Decimal(0),
                "maker_fee": Decimal(0),
                "taker_fee": Decimal(0),
                "sources": {},
            },
        )
        delta_usdc = _decimal(row.delta_usdc)
        bucket["gross_pnl"] += delta_usdc

        if row.event_type == "TRADE":
            bucket["trade_count"] += Decimal(1)
            if row.side == "BUY":
                bucket["buy_volume"] += abs(_decimal(row.usdc_size))
            estimated_fee = _decimal(row.estimated_fee)
            bucket["estimated_fee"] += estimated_fee
            bucket["worst_case_fee"] += _decimal(row.worst_case_fee)
            actual_fee = _decimal(row.actual_fee) if row.actual_fee is not None else None
            fee_for_blend = actual_fee if actual_fee is not None else estimated_fee
            if actual_fee is not None:
                bucket["actual_fee"] += actual_fee
                bucket["actual_count"] += Decimal(1)
            else:
                bucket["estimated_fee_fallback"] += estimated_fee
            bucket["blended_fee"] += fee_for_blend

            source = row.fee_source or "missing"
            sources = bucket["sources"]
            sources[source] = sources.get(source, 0) + 1

            role = row.fill_role
            if role == "maker":
                bucket["maker_trades"] += Decimal(1)
                bucket["maker_volume"] += abs(_decimal(row.usdc_size))
                bucket["maker_fee"] += fee_for_blend
            elif role == "taker":
                bucket["taker_trades"] += Decimal(1)
                bucket["taker_volume"] += abs(_decimal(row.usdc_size))
                bucket["taker_fee"] += fee_for_blend

    return [
        FeeAttributionRow(
            period=period,
            category=category,
            trade_count=int(values["trade_count"]),
            enriched_fee_count=int(values["actual_count"]),
            buy_volume=values["buy_volume"],
            gross_pnl=values["gross_pnl"],
            estimated_fee=values["estimated_fee"],
            worst_case_fee=values["worst_case_fee"],
            actual_fee=values["actual_fee"] if values["actual_count"] else None,
            estimated_fee_fallback=values["estimated_fee_fallback"],
            blended_fee=values["blended_fee"],
            maker_trades=int(values["maker_trades"]),
            taker_trades=int(values["taker_trades"]),
            maker_volume=values["maker_volume"],
            taker_volume=values["taker_volume"],
            maker_fee=values["maker_fee"],
            taker_fee=values["taker_fee"],
            fee_source_counts=tuple(sorted(values["sources"].items())),
        )
        for (period, category), values in sorted(buckets.items())
    ]


def fee_attribution_coverage(session: Session, *, wallet: str) -> FeeAttributionCoverage:
    row = session.execute(
        text(
            "SELECT "
            "COUNT(*) AS total_trades, "
            "SUM(CASE WHEN m.category IS NOT NULL THEN 1 ELSE 0 END) AS category_classified_trades, "
            "SUM(CASE WHEN fe.estimated_fee IS NOT NULL AND CAST(fe.estimated_fee AS REAL) > 0 "
            "THEN 1 ELSE 0 END) AS fee_estimated_trades, "
            "SUM(CASE WHEN m.category IS NULL THEN 1 ELSE 0 END) AS unknown_category_trades, "
            "SUM(CASE WHEN fe.actual_fee IS NOT NULL THEN 1 ELSE 0 END) AS actual_enriched_trades, "
            "SUM(CASE WHEN fe.actual_fee IS NOT NULL THEN CAST(fe.actual_fee AS REAL) ELSE 0 END) "
            "AS actual_fee_total, "
            "SUM(COALESCE(CAST(fe.estimated_fee AS REAL), 0)) AS estimated_fee_total, "
            "SUM(CASE WHEN fe.actual_fee IS NULL THEN COALESCE(CAST(fe.estimated_fee AS REAL), 0) "
            "ELSE 0 END) AS estimated_fee_fallback_total, "
            "SUM(CASE WHEN fe.actual_fee IS NOT NULL THEN CAST(fe.actual_fee AS REAL) "
            "ELSE COALESCE(CAST(fe.estimated_fee AS REAL), 0) END) AS blended_fee_total "
            "FROM wallet_events we "
            "LEFT JOIN markets m ON m.condition_id = we.condition_id "
            "LEFT JOIN fee_estimates fe ON fe.event_id = we.id "
            "WHERE we.wallet = :wallet AND we.event_type = 'TRADE'"
        ),
        {"wallet": wallet},
    ).fetchone()
    return FeeAttributionCoverage(
        total_trades=int(row.total_trades or 0),
        category_classified_trades=int(row.category_classified_trades or 0),
        fee_estimated_trades=int(row.fee_estimated_trades or 0),
        unknown_category_trades=int(row.unknown_category_trades or 0),
        actual_enriched_trades=int(row.actual_enriched_trades or 0),
        actual_fee_total=_decimal(row.actual_fee_total),
        estimated_fee_total=_decimal(row.estimated_fee_total),
        estimated_fee_fallback_total=_decimal(row.estimated_fee_fallback_total),
        blended_fee_total=_decimal(row.blended_fee_total),
    )
