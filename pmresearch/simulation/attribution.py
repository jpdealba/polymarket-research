"""PnL attribution helpers for Phase 22 simulations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class MarketAttribution:
    run_id: int
    condition_id: str
    question: Optional[str]
    event_id: Optional[str]
    fills_count: int
    fill_notional: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    max_inventory: Decimal
    turnover: Decimal


@dataclass(frozen=True)
class EventAttribution:
    run_id: int
    event_id: str
    event_title: Optional[str]
    markets_count: int
    fills_count: int
    fill_notional: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    max_event_exposure: Decimal
    turnover: Decimal


@dataclass(frozen=True)
class AttributionSummary:
    run_id: int
    run_net_pnl: Decimal
    market_total_pnl: Decimal
    residual: Decimal
    top_1_event_pnl_share: Decimal
    top_3_event_pnl_share: Decimal
    top_5_market_pnl_share: Decimal


def insert_run_attribution(
    session: Session,
    run_id: int,
    market_rows: list[dict[str, object]],
) -> None:
    """Persist market/event attribution rows after a simulation has completed."""
    if not market_rows:
        return

    enriched = _enrich_market_rows(session, market_rows)
    session.execute(
        text(
            "INSERT INTO simulation_pnl_by_market "
            "(run_id, condition_id, question, event_id, fills_count, fill_notional, "
            "realized_pnl, unrealized_pnl, total_pnl, max_inventory, turnover) "
            "VALUES "
            "(:run_id, :condition_id, :question, :event_id, :fills_count, :fill_notional, "
            ":realized_pnl, :unrealized_pnl, :total_pnl, :max_inventory, :turnover)"
        ),
        [{**row, "run_id": run_id} for row in enriched],
    )

    event_rows = _event_rows(session, run_id, enriched)
    if event_rows:
        session.execute(
            text(
                "INSERT INTO simulation_pnl_by_event "
                "(run_id, event_id, event_title, markets_count, fills_count, fill_notional, "
                "realized_pnl, unrealized_pnl, total_pnl, max_event_exposure, turnover) "
                "VALUES "
                "(:run_id, :event_id, :event_title, :markets_count, :fills_count, :fill_notional, "
                ":realized_pnl, :unrealized_pnl, :total_pnl, :max_event_exposure, :turnover)"
            ),
            event_rows,
        )


def fetch_market_attribution(session: Session, run_id: int) -> list[MarketAttribution]:
    rows = session.execute(
        text(
            "SELECT * FROM simulation_pnl_by_market "
            "WHERE run_id = :run_id ORDER BY CAST(total_pnl AS REAL) DESC, condition_id"
        ),
        {"run_id": run_id},
    ).mappings().fetchall()
    return [_market_from_row(dict(row)) for row in rows]


def fetch_event_attribution(session: Session, run_id: int) -> list[EventAttribution]:
    rows = session.execute(
        text(
            "SELECT * FROM simulation_pnl_by_event "
            "WHERE run_id = :run_id ORDER BY CAST(total_pnl AS REAL) DESC, event_id"
        ),
        {"run_id": run_id},
    ).mappings().fetchall()
    return [_event_from_row(dict(row)) for row in rows]


def fetch_attribution_summary(session: Session, run_id: int) -> AttributionSummary:
    run_net = session.execute(
        text("SELECT net_pnl FROM simulation_runs WHERE id = :run_id"),
        {"run_id": run_id},
    ).scalar_one_or_none()
    run_net_pnl = _decimal(run_net)
    markets = fetch_market_attribution(session, run_id)
    events = fetch_event_attribution(session, run_id)
    market_total = sum((row.total_pnl for row in markets), Decimal("0"))
    return AttributionSummary(
        run_id=run_id,
        run_net_pnl=run_net_pnl,
        market_total_pnl=market_total,
        residual=run_net_pnl - market_total,
        top_1_event_pnl_share=_pnl_share((row.total_pnl for row in events), 1),
        top_3_event_pnl_share=_pnl_share((row.total_pnl for row in events), 3),
        top_5_market_pnl_share=_pnl_share((row.total_pnl for row in markets), 5),
    )


def generate_attribution_report(session: Session, run_id: int, *, limit: int = 10) -> str:
    markets = fetch_market_attribution(session, run_id)
    events = fetch_event_attribution(session, run_id)
    summary = fetch_attribution_summary(session, run_id)
    lines = [
        "## PnL Attribution",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Run net PnL | {_fmt_usdc(summary.run_net_pnl)} |",
        f"| Attributed market PnL | {_fmt_usdc(summary.market_total_pnl)} |",
        f"| Attribution residual | {_fmt_usdc(summary.residual)} |",
        f"| top_1_event_pnl_share | {_fmt_pct(summary.top_1_event_pnl_share)} |",
        f"| top_3_event_pnl_share | {_fmt_pct(summary.top_3_event_pnl_share)} |",
        f"| top_5_market_pnl_share | {_fmt_pct(summary.top_5_market_pnl_share)} |",
        "",
        "### Top 10 Markets By PnL",
        "",
        "| Rank | Condition | Question | Event | Fills | Notional | Realized | Unrealized | Total | Max Inv |",
        "|------|-----------|----------|-------|-------|----------|----------|------------|-------|---------|",
    ]
    if markets:
        for rank, row in enumerate(markets[:limit], start=1):
            lines.append(
                f"| {rank} | `{row.condition_id}` | {_clip(row.question)} | `{row.event_id or ''}` | "
                f"{row.fills_count} | {_fmt_usdc(row.fill_notional)} | {_fmt_usdc(row.realized_pnl)} | "
                f"{_fmt_usdc(row.unrealized_pnl)} | {_fmt_usdc(row.total_pnl)} | {row.max_inventory:.2f} |"
            )
    else:
        lines.append("| - | - | No attribution rows found | - | - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "### Top 10 Events By PnL",
            "",
            "| Rank | Event | Title | Markets | Fills | Notional | Realized | Unrealized | Total | Max Exposure |",
            "|------|-------|-------|---------|-------|----------|----------|------------|-------|--------------|",
        ]
    )
    if events:
        for rank, row in enumerate(events[:limit], start=1):
            lines.append(
                f"| {rank} | `{row.event_id}` | {_clip(row.event_title)} | {row.markets_count} | "
                f"{row.fills_count} | {_fmt_usdc(row.fill_notional)} | {_fmt_usdc(row.realized_pnl)} | "
                f"{_fmt_usdc(row.unrealized_pnl)} | {_fmt_usdc(row.total_pnl)} | "
                f"{_fmt_usdc(row.max_event_exposure)} |"
            )
    else:
        lines.append("| - | - | No attribution rows found | - | - | - | - | - | - | - |")
    lines.append("")
    return "\n".join(lines)


def _enrich_market_rows(
    session: Session,
    market_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    condition_ids = [str(row["condition_id"]) for row in market_rows]
    metadata: dict[str, dict[str, object]] = {}
    if condition_ids:
        rows = session.execute(
            text(
                "SELECT condition_id, question, event_id FROM markets "
                "WHERE condition_id IN :condition_ids"
            ).bindparams(bindparam("condition_ids", expanding=True)),
            {"condition_ids": condition_ids},
        ).mappings().fetchall()
        metadata = {row["condition_id"]: dict(row) for row in rows}

    enriched = []
    for row in market_rows:
        condition_id = str(row["condition_id"])
        meta = metadata.get(condition_id, {})
        fallback_event_id = row.get("event_id")
        enriched.append(
            {
                "condition_id": condition_id,
                "question": meta.get("question"),
                "event_id": str(meta.get("event_id") or fallback_event_id or condition_id),
                "fills_count": int(row["fills_count"]),
                "fill_notional": str(row["fill_notional"]),
                "realized_pnl": str(row["realized_pnl"]),
                "unrealized_pnl": str(row["unrealized_pnl"]),
                "total_pnl": str(row["total_pnl"]),
                "max_inventory": str(row["max_inventory"]),
                "turnover": str(row["turnover"]),
                "max_exposure": str(row.get("max_exposure", "0")),
            }
        )
    return enriched


def _event_rows(
    session: Session,
    run_id: int,
    market_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    by_event: dict[str, dict[str, object]] = {}
    for row in market_rows:
        event_id = str(row["event_id"] or row["condition_id"])
        target = by_event.setdefault(
            event_id,
            {
                "run_id": run_id,
                "event_id": event_id,
                "event_title": None,
                "markets": set(),
                "fills_count": 0,
                "fill_notional": Decimal("0"),
                "realized_pnl": Decimal("0"),
                "unrealized_pnl": Decimal("0"),
                "total_pnl": Decimal("0"),
                "max_event_exposure": Decimal("0"),
                "turnover": Decimal("0"),
            },
        )
        target["markets"].add(row["condition_id"])
        target["fills_count"] += int(row["fills_count"])
        target["fill_notional"] += _decimal(row["fill_notional"])
        target["realized_pnl"] += _decimal(row["realized_pnl"])
        target["unrealized_pnl"] += _decimal(row["unrealized_pnl"])
        target["total_pnl"] += _decimal(row["total_pnl"])
        target["max_event_exposure"] += _decimal(row["max_exposure"])
        target["turnover"] += _decimal(row["turnover"])

    event_ids = tuple(by_event)
    titles: dict[str, Optional[str]] = {}
    if event_ids:
        rows = session.execute(
            text("SELECT event_id, title FROM pm_events WHERE event_id IN :event_ids").bindparams(
                bindparam("event_ids", expanding=True)
            ),
            {"event_ids": list(event_ids)},
        ).mappings().fetchall()
        titles = {str(row["event_id"]): row["title"] for row in rows}

    result = []
    for event_id, row in by_event.items():
        result.append(
            {
                "run_id": row["run_id"],
                "event_id": event_id,
                "event_title": titles.get(event_id),
                "markets_count": len(row["markets"]),
                "fills_count": row["fills_count"],
                "fill_notional": str(row["fill_notional"]),
                "realized_pnl": str(row["realized_pnl"]),
                "unrealized_pnl": str(row["unrealized_pnl"]),
                "total_pnl": str(row["total_pnl"]),
                "max_event_exposure": str(row["max_event_exposure"]),
                "turnover": str(row["turnover"]),
            }
        )
    return result


def _market_from_row(row: dict) -> MarketAttribution:
    return MarketAttribution(
        run_id=int(row["run_id"]),
        condition_id=row["condition_id"],
        question=row["question"],
        event_id=row["event_id"],
        fills_count=int(row["fills_count"]),
        fill_notional=_decimal(row["fill_notional"]),
        realized_pnl=_decimal(row["realized_pnl"]),
        unrealized_pnl=_decimal(row["unrealized_pnl"]),
        total_pnl=_decimal(row["total_pnl"]),
        max_inventory=_decimal(row["max_inventory"]),
        turnover=_decimal(row["turnover"]),
    )


def _event_from_row(row: dict) -> EventAttribution:
    return EventAttribution(
        run_id=int(row["run_id"]),
        event_id=row["event_id"],
        event_title=row["event_title"],
        markets_count=int(row["markets_count"]),
        fills_count=int(row["fills_count"]),
        fill_notional=_decimal(row["fill_notional"]),
        realized_pnl=_decimal(row["realized_pnl"]),
        unrealized_pnl=_decimal(row["unrealized_pnl"]),
        total_pnl=_decimal(row["total_pnl"]),
        max_event_exposure=_decimal(row["max_event_exposure"]),
        turnover=_decimal(row["turnover"]),
    )


def _pnl_share(values, n: int) -> Decimal:
    absolute = sorted((abs(_decimal(v)) for v in values), reverse=True)
    denominator = sum(absolute, Decimal("0"))
    if denominator == Decimal("0"):
        return Decimal("0")
    return sum(absolute[:n], Decimal("0")) / denominator


def _decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _fmt_usdc(value: Decimal) -> str:
    return f"${value:+.2f}"


def _fmt_pct(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def _clip(value: Optional[str], limit: int = 72) -> str:
    if not value:
        return ""
    value = value.replace("|", "\\|")
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
