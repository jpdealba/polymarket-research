"""Phase 22.3 holdout failure attribution diagnostics."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from .engine import DecisionContext, StrategyConfig, _load_dataset, _simulate
from .scenarios import ALL_SCENARIOS, SimOrder, decide_fill
from .search import (
    SearchCandidate,
    SearchRunResult,
    build_search_strategy,
    fetch_latest_search,
    fetch_search_run,
    final_status,
    risk_limits_from_parameters,
    scenario_from_parameters,
    split_rows_by_time,
)

_ZERO = Decimal("0")
_FEE_RATE = Decimal("0.002")

REPORT_FILENAME = "holdout_failure_report.md"
CONDITION_FILENAME = "holdout_failure_by_condition.csv"
PRICE_BUCKET_FILENAME = "holdout_failure_by_price_bucket.csv"
BOOK_AGE_FILENAME = "holdout_failure_by_book_age.csv"
SIDE_FILENAME = "holdout_failure_by_side.csv"
TIME_BUCKET_FILENAME = "holdout_failure_by_time_bucket.csv"


@dataclass(frozen=True)
class DiagnosticRecord:
    split_name: str
    execution_status: str
    wallet_event_id: int
    event_id: str
    event_title: Optional[str]
    condition_id: str
    question: Optional[str]
    token_id: str
    side: Optional[str]
    price: Optional[Decimal]
    price_bucket: str
    spread_bps_bucket: str
    book_age_bucket: str
    depth_bucket: str
    time_to_event_bucket: str
    hour_utc: str
    context_quality: str
    skipped_reason: str
    fill_notional: Decimal
    net_pnl: Decimal
    skipped_opportunity_pnl: Decimal


@dataclass(frozen=True)
class BreakdownRow:
    bucket: str
    candidate_signals: int
    accepted_orders: int
    simulated_fills: int
    accepted_not_filled: int
    skipped_orders: int
    fill_notional: Decimal
    net_pnl: Decimal
    skipped_opportunity_pnl: Decimal


@dataclass(frozen=True)
class ConditionBreakdownRow:
    event_id: str
    event_title: Optional[str]
    condition_id: str
    question: Optional[str]
    candidate_signals: int
    accepted_orders: int
    simulated_fills: int
    accepted_not_filled: int
    skipped_orders: int
    fill_notional: Decimal
    net_pnl: Decimal
    skipped_opportunity_pnl: Decimal


@dataclass(frozen=True)
class HoldoutFailureDiagnostics:
    search_run: SearchRunResult
    candidate: SearchCandidate
    strategy: StrategyConfig
    records: list[DiagnosticRecord]
    train_validation_condition_rows: list[ConditionBreakdownRow]

    @property
    def test_records(self) -> list[DiagnosticRecord]:
        return [row for row in self.records if row.split_name == "test"]

    @property
    def train_validation_records(self) -> list[DiagnosticRecord]:
        return [row for row in self.records if row.split_name in {"train", "validation"}]

    @property
    def test_net_pnl(self) -> Decimal:
        return sum((row.net_pnl for row in self.test_records), _ZERO)

    @property
    def test_skipped_opportunity_pnl(self) -> Decimal:
        return sum((row.skipped_opportunity_pnl for row in self.test_records), _ZERO)


def generate_holdout_failure_diagnostics(
    session: Session,
    wallet: str,
    rule_name: str,
    *,
    search_run_id: Optional[int] = None,
) -> HoldoutFailureDiagnostics:
    """Replay the selected search candidate and attribute holdout failures.

    This is intentionally diagnostic-only. It reads the selected candidate from
    a completed search run and does not write to the database or alter ranking.
    """
    wallet = wallet.lower()
    rule_name = rule_name.strip().lower()
    search_run = (
        fetch_search_run(session, search_run_id)
        if search_run_id is not None
        else fetch_latest_search(session, wallet, rule_name)
    )
    if search_run is None:
        raise ValueError("No search run found for wallet/rule.")
    if search_run.wallet != wallet or search_run.rule_name != rule_name:
        raise ValueError("Search run does not match wallet/rule.")
    if search_run.selected_candidate_id is None:
        raise ValueError("Search run has no selected candidate.")

    candidate = _selected_candidate(search_run)
    strategy = build_search_strategy(wallet, rule_name, candidate.parameters, candidate.candidate_index)
    risk_limits = risk_limits_from_parameters(candidate.parameters)
    scenario = scenario_from_parameters(ALL_SCENARIOS["conservative"], candidate.parameters)
    rows = _load_dataset(session, wallet)
    splits = split_rows_by_time(rows)
    metadata = _load_market_metadata(session, rows)

    records: list[DiagnosticRecord] = []
    for split_name, split_rows in splits.items():
        transient = _simulate(split_rows, strategy, scenario, risk_limits, collect_details=True)
        records.extend(
            _records_for_split(
                split_name=split_name,
                rows=split_rows,
                transient_orders=transient.orders,
                transient_fills=transient.fills,
                transient_skips=transient.skipped_orders,
                scenario_name=scenario.name,
                metadata=metadata,
            )
        )

    train_validation_condition_rows = condition_breakdown(
        [row for row in records if row.split_name in {"train", "validation"}]
    )
    return HoldoutFailureDiagnostics(
        search_run=search_run,
        candidate=candidate,
        strategy=strategy,
        records=records,
        train_validation_condition_rows=train_validation_condition_rows,
    )


def write_holdout_failure_outputs(
    session: Session,
    wallet: str,
    rule_name: str,
    out_dir: Path,
    *,
    search_run_id: Optional[int] = None,
) -> HoldoutFailureDiagnostics:
    diagnostics = generate_holdout_failure_diagnostics(
        session,
        wallet,
        rule_name,
        search_run_id=search_run_id,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / REPORT_FILENAME).write_text(
        generate_holdout_failure_report(diagnostics),
        encoding="utf-8",
    )
    _write_condition_csv(out_dir / CONDITION_FILENAME, condition_breakdown(diagnostics.test_records))
    _write_breakdown_csv(out_dir / PRICE_BUCKET_FILENAME, bucket_breakdown(diagnostics.test_records, "price_bucket"))
    _write_breakdown_csv(out_dir / BOOK_AGE_FILENAME, bucket_breakdown(diagnostics.test_records, "book_age_bucket"))
    _write_breakdown_csv(out_dir / SIDE_FILENAME, bucket_breakdown(diagnostics.test_records, "side"))
    _write_breakdown_csv(out_dir / TIME_BUCKET_FILENAME, bucket_breakdown(diagnostics.test_records, "time_to_event_bucket"))
    return diagnostics


def generate_holdout_failure_report(diagnostics: HoldoutFailureDiagnostics) -> str:
    candidate = diagnostics.candidate
    test = candidate.metrics["test"]
    train = candidate.metrics["train"]
    validation = candidate.metrics["validation"]
    status = final_status(candidate)
    condition_rows = condition_breakdown(diagnostics.test_records)
    event_rows = bucket_breakdown(diagnostics.test_records, "event")
    side_rows = bucket_breakdown(diagnostics.test_records, "side")
    price_rows = bucket_breakdown(diagnostics.test_records, "price_bucket")
    spread_rows = bucket_breakdown(diagnostics.test_records, "spread_bps_bucket")
    book_age_rows = bucket_breakdown(diagnostics.test_records, "book_age_bucket")
    depth_rows = bucket_breakdown(diagnostics.test_records, "depth_bucket")
    time_rows = bucket_breakdown(diagnostics.test_records, "time_to_event_bucket")
    hour_rows = bucket_breakdown(diagnostics.test_records, "hour_utc")
    quality_rows = bucket_breakdown(diagnostics.test_records, "context_quality")
    skipped_rows = bucket_breakdown(diagnostics.test_records, "skipped_reason")
    execution_rows = bucket_breakdown(diagnostics.test_records, "execution_status")

    lines = [
        "# Holdout Failure Attribution",
        "",
        "Diagnostic only: this report replays the already-selected candidate and does not select or tune parameters from test results.",
        "",
        "## Candidate",
        "",
        f"- **Search run:** {diagnostics.search_run.run_id}",
        f"- **Selected candidate:** {candidate.candidate_id}",
        f"- **Rank:** {candidate.rank_index}",
        f"- **Rule:** `{diagnostics.search_run.rule_name}`",
        f"- **Strategy:** `{diagnostics.strategy.strategy_name}`",
        f"- **Final status:** {status}",
        f"- **Parameters:** `{json.dumps(candidate.parameters, sort_keys=True)}`",
        "",
        "| Split | Net PnL | Fills | Signals | Skipped | Conservative pass |",
        "|-------|---------|-------|---------|---------|-------------------|",
        f"| train | {_fmt_usdc(train.net_pnl)} | {train.simulated_fills_count} | {train.candidate_signals_count} | {train.skipped_orders_count} | {_yes_no(train.conservative_pass)} |",
        f"| validation | {_fmt_usdc(validation.net_pnl)} | {validation.simulated_fills_count} | {validation.candidate_signals_count} | {validation.skipped_orders_count} | {_yes_no(validation.conservative_pass)} |",
        f"| test | {_fmt_usdc(test.net_pnl)} | {test.simulated_fills_count} | {test.candidate_signals_count} | {test.skipped_orders_count} | {_yes_no(test.conservative_pass)} |",
        "",
        "## Questions Answered",
        "",
        f"1. **Is the test loss concentrated in a few markets?** {_market_concentration_answer(condition_rows)}",
        f"2. **Is the test loss concentrated in one side, price range, or stale context?** {_side_price_context_answer(side_rows, price_rows, book_age_rows, quality_rows)}",
        f"3. **Did risk gating skip profitable opportunities or prevent losses?** {_risk_gate_answer(diagnostics.test_records)}",
        f"4. **Are train/validation profits coming from a small number of conditions?** {_train_validation_concentration_answer(diagnostics.train_validation_condition_rows)}",
        f"5. **Does the strategy look event-specific rather than general?** {_event_specific_answer(event_rows, diagnostics.train_validation_condition_rows)}",
        f"6. **What filters should be tested next, without using test to select final parameters?** {_next_filters_answer(side_rows, price_rows, spread_rows, book_age_rows, depth_rows, time_rows, quality_rows)}",
        "",
        "## Test Breakdowns",
        "",
        "### By Condition",
        "",
    ]
    lines.extend(_condition_table(condition_rows[:10]))
    lines.extend(["", "### By Event", ""])
    lines.extend(_bucket_table(event_rows[:10]))
    lines.extend(["", "### By Side", ""])
    lines.extend(_bucket_table(side_rows))
    lines.extend(["", "### By Price Bucket", ""])
    lines.extend(_bucket_table(price_rows))
    lines.extend(["", "### By Spread Bps Bucket", ""])
    lines.extend(_bucket_table(spread_rows))
    lines.extend(["", "### By Book Age Bucket", ""])
    lines.extend(_bucket_table(book_age_rows))
    lines.extend(["", "### By Depth Bucket", ""])
    lines.extend(_bucket_table(depth_rows))
    lines.extend(["", "### By Time To Event Bucket", ""])
    lines.extend(_bucket_table(time_rows))
    lines.extend(["", "### By Hour UTC", ""])
    lines.extend(_bucket_table(hour_rows))
    lines.extend(["", "### By Context Quality", ""])
    lines.extend(_bucket_table(quality_rows))
    lines.extend(["", "### By Skipped Reason", ""])
    lines.extend(_bucket_table(skipped_rows))
    lines.extend(["", "### Simulated Fill vs Skipped", ""])
    lines.extend(_bucket_table(execution_rows))
    lines.extend(
        [
            "",
            "## Output Files",
            "",
            f"- `{CONDITION_FILENAME}`",
            f"- `{PRICE_BUCKET_FILENAME}`",
            f"- `{BOOK_AGE_FILENAME}`",
            f"- `{SIDE_FILENAME}`",
            f"- `{TIME_BUCKET_FILENAME}`",
            "",
        ]
    )
    return "\n".join(lines)


def condition_breakdown(records: Iterable[DiagnosticRecord]) -> list[ConditionBreakdownRow]:
    buckets: dict[tuple[str, str], dict[str, object]] = {}
    for row in records:
        key = (row.event_id, row.condition_id)
        bucket = buckets.setdefault(
            key,
            {
                "event_id": row.event_id,
                "event_title": row.event_title,
                "condition_id": row.condition_id,
                "question": row.question,
                "candidate_signals": 0,
                "accepted_orders": 0,
                "simulated_fills": 0,
                "accepted_not_filled": 0,
                "skipped_orders": 0,
                "fill_notional": _ZERO,
                "net_pnl": _ZERO,
                "skipped_opportunity_pnl": _ZERO,
            },
        )
        _accumulate(bucket, row)
    return [
        ConditionBreakdownRow(
            event_id=str(bucket["event_id"]),
            event_title=_optional_str(bucket["event_title"]),
            condition_id=str(bucket["condition_id"]),
            question=_optional_str(bucket["question"]),
            candidate_signals=int(bucket["candidate_signals"]),
            accepted_orders=int(bucket["accepted_orders"]),
            simulated_fills=int(bucket["simulated_fills"]),
            accepted_not_filled=int(bucket["accepted_not_filled"]),
            skipped_orders=int(bucket["skipped_orders"]),
            fill_notional=bucket["fill_notional"],
            net_pnl=bucket["net_pnl"],
            skipped_opportunity_pnl=bucket["skipped_opportunity_pnl"],
        )
        for bucket in sorted(
            buckets.values(),
            key=lambda b: (b["net_pnl"], -abs(b["net_pnl"]), str(b["condition_id"])),
        )
    ]


def bucket_breakdown(records: Iterable[DiagnosticRecord], field: str) -> list[BreakdownRow]:
    buckets: dict[str, dict[str, object]] = {}
    for row in records:
        bucket_name = _bucket_value(row, field)
        bucket = buckets.setdefault(
            bucket_name,
            {
                "bucket": bucket_name,
                "candidate_signals": 0,
                "accepted_orders": 0,
                "simulated_fills": 0,
                "accepted_not_filled": 0,
                "skipped_orders": 0,
                "fill_notional": _ZERO,
                "net_pnl": _ZERO,
                "skipped_opportunity_pnl": _ZERO,
            },
        )
        _accumulate(bucket, row)
    return [
        BreakdownRow(
            bucket=str(bucket["bucket"]),
            candidate_signals=int(bucket["candidate_signals"]),
            accepted_orders=int(bucket["accepted_orders"]),
            simulated_fills=int(bucket["simulated_fills"]),
            accepted_not_filled=int(bucket["accepted_not_filled"]),
            skipped_orders=int(bucket["skipped_orders"]),
            fill_notional=bucket["fill_notional"],
            net_pnl=bucket["net_pnl"],
            skipped_opportunity_pnl=bucket["skipped_opportunity_pnl"],
        )
        for bucket in sorted(
            buckets.values(),
            key=lambda b: (b["net_pnl"], -abs(b["net_pnl"]), str(b["bucket"])),
        )
    ]


def _records_for_split(
    *,
    split_name: str,
    rows: list[dict],
    transient_orders: list[dict],
    transient_fills: list[dict],
    transient_skips: list[dict],
    scenario_name: str,
    metadata: dict[str, dict[str, Optional[str]]],
) -> list[DiagnosticRecord]:
    rows_by_event_id = {int(row["event_id"]): row for row in rows}
    final_marks = _final_marks(rows)
    fill_by_order_index = {int(fill["order_index"]): fill for fill in transient_fills}
    result: list[DiagnosticRecord] = []

    for order_index, order in enumerate(transient_orders):
        row = rows_by_event_id.get(int(order["event_id"]), {})
        fill = fill_by_order_index.get(order_index)
        status = "simulated_fill" if fill is not None else "accepted_not_filled"
        price = _decimal(fill["fill_price"] if fill else order.get("order_price"))
        fill_notional = _decimal_or_zero(fill.get("fill_notional_usdc") if fill else None)
        fee = _decimal_or_zero(fill.get("estimated_fee") if fill else None)
        fill_size = _decimal(fill.get("fill_size") if fill else None)
        pnl = (
            _fill_pnl(
                side=str(order.get("side") or ""),
                price=price,
                size=fill_size,
                fee=fee,
                final_mark=final_marks.get(str(order.get("token_id") or "")),
            )
            if fill is not None
            else _ZERO
        )
        result.append(
            _diagnostic_record(
                split_name=split_name,
                execution_status=status,
                row=row,
                event_id=int(order["event_id"]),
                token_id=str(order.get("token_id") or ""),
                condition_id=str(order.get("condition_id") or ""),
                side=_optional_str(order.get("side")),
                price=price,
                skipped_reason="",
                fill_notional=fill_notional,
                net_pnl=pnl,
                skipped_opportunity_pnl=_ZERO,
                metadata=metadata,
            )
        )

    scenario = ALL_SCENARIOS[scenario_name]
    for skipped in transient_skips:
        row = rows_by_event_id.get(int(skipped["event_id"]), {})
        price = _decimal(skipped.get("order_price"))
        size = _decimal(skipped.get("order_size"))
        side = _optional_str(skipped.get("side"))
        opportunity_pnl = _ZERO
        if side and price is not None and size is not None:
            order = SimOrder(side=side, order_price=price, order_size=size, reason="skipped_diagnostic")
            ctx = DecisionContext.from_row(row)
            assumption = decide_fill(
                order=order,
                scenario=scenario,
                context_status=ctx.context_status,
                book_age_s=ctx.integer("book_before_age_s"),
                spread_bps=ctx.decimal("spread_bps"),
                bid_depth_top1=ctx.decimal("bid_depth_top1"),
                ask_depth_top1=ctx.decimal("ask_depth_top1"),
                deterministic_seq=0,
            )
            if assumption.would_fill and assumption.fill_price is not None and assumption.fill_size is not None:
                opportunity_pnl = _fill_pnl(
                    side=side,
                    price=assumption.fill_price,
                    size=assumption.fill_size,
                    fee=assumption.fill_price * assumption.fill_size * _FEE_RATE,
                    final_mark=final_marks.get(str(skipped.get("token_id") or "")),
                )
        result.append(
            _diagnostic_record(
                split_name=split_name,
                execution_status="skipped",
                row=row,
                event_id=int(skipped["event_id"]),
                token_id=str(skipped.get("token_id") or ""),
                condition_id=str(skipped.get("condition_id") or ""),
                side=side,
                price=price,
                skipped_reason=str(skipped.get("skipped_reason") or "unknown"),
                fill_notional=_ZERO,
                net_pnl=_ZERO,
                skipped_opportunity_pnl=opportunity_pnl,
                metadata=metadata,
            )
        )
    return result


def _diagnostic_record(
    *,
    split_name: str,
    execution_status: str,
    row: dict,
    event_id: int,
    token_id: str,
    condition_id: str,
    side: Optional[str],
    price: Optional[Decimal],
    skipped_reason: str,
    fill_notional: Decimal,
    net_pnl: Decimal,
    skipped_opportunity_pnl: Decimal,
    metadata: dict[str, dict[str, Optional[str]]],
) -> DiagnosticRecord:
    meta = metadata.get(condition_id, {})
    depth = _relevant_depth(row, side)
    market_event_id = str(meta.get("event_id") or event_id)
    return DiagnosticRecord(
        split_name=split_name,
        execution_status=execution_status,
        wallet_event_id=event_id,
        event_id=market_event_id,
        event_title=meta.get("event_title"),
        condition_id=condition_id,
        question=meta.get("question"),
        token_id=token_id,
        side=side,
        price=price,
        price_bucket=_price_bucket(price),
        spread_bps_bucket=_spread_bucket(_decimal(row.get("spread_bps"))),
        book_age_bucket=_book_age_bucket(_int_or_none(row.get("book_before_age_s"))),
        depth_bucket=_depth_bucket(depth),
        time_to_event_bucket=_time_to_event_bucket(_int_or_none(row.get("time_to_event_start_s"))),
        hour_utc=_hour_bucket(_int_or_none(row.get("trade_hour_utc"))),
        context_quality=str(row.get("context_status") or "unknown"),
        skipped_reason=skipped_reason,
        fill_notional=fill_notional,
        net_pnl=net_pnl,
        skipped_opportunity_pnl=skipped_opportunity_pnl,
    )


def _selected_candidate(search_run: SearchRunResult) -> SearchCandidate:
    for candidate in search_run.candidates:
        if candidate.candidate_id == search_run.selected_candidate_id:
            return candidate
    raise ValueError("Selected candidate row not found.")


def _load_market_metadata(session: Session, rows: list[dict]) -> dict[str, dict[str, Optional[str]]]:
    condition_ids = sorted({str(row.get("condition_id")) for row in rows if row.get("condition_id")})
    if not condition_ids:
        return {}
    market_rows = session.execute(
        text(
            "SELECT m.condition_id, m.question, m.event_id, e.title AS event_title "
            "FROM markets m LEFT JOIN pm_events e ON e.event_id = m.event_id "
            "WHERE m.condition_id IN :condition_ids"
        ).bindparams(bindparam("condition_ids", expanding=True)),
        {"condition_ids": condition_ids},
    ).mappings().fetchall()
    return {str(row["condition_id"]): dict(row) for row in market_rows}


def _final_marks(rows: list[dict]) -> dict[str, Decimal]:
    marks: dict[str, Decimal] = {}
    for row in sorted(rows, key=lambda r: (int(r.get("trade_ts") or 0), int(r.get("event_id") or 0))):
        token_id = str(row.get("token_id") or "")
        mark = _decimal(row.get("mid_before"))
        if token_id and mark is not None:
            marks[token_id] = mark
    return marks


def _fill_pnl(
    *,
    side: str,
    price: Optional[Decimal],
    size: Optional[Decimal],
    fee: Decimal,
    final_mark: Optional[Decimal],
) -> Decimal:
    if price is None or size is None or final_mark is None:
        return _ZERO
    if side == "BUY":
        return (final_mark - price) * size - fee
    if side == "SELL":
        return (price - final_mark) * size - fee
    return _ZERO


def _accumulate(bucket: dict[str, object], row: DiagnosticRecord) -> None:
    bucket["candidate_signals"] += 1
    if row.execution_status in {"simulated_fill", "accepted_not_filled"}:
        bucket["accepted_orders"] += 1
    if row.execution_status == "simulated_fill":
        bucket["simulated_fills"] += 1
    elif row.execution_status == "accepted_not_filled":
        bucket["accepted_not_filled"] += 1
    elif row.execution_status == "skipped":
        bucket["skipped_orders"] += 1
    bucket["fill_notional"] += row.fill_notional
    bucket["net_pnl"] += row.net_pnl
    bucket["skipped_opportunity_pnl"] += row.skipped_opportunity_pnl


def _bucket_value(row: DiagnosticRecord, field: str) -> str:
    if field == "side":
        return row.side or "unknown"
    if field == "event":
        title = f" - {row.event_title}" if row.event_title else ""
        return f"{row.event_id}{title}"
    value = getattr(row, field)
    return str(value or "unknown")


def _write_condition_csv(path: Path, rows: list[ConditionBreakdownRow]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "event_id",
                "event_title",
                "condition_id",
                "question",
                "candidate_signals",
                "accepted_orders",
                "simulated_fills",
                "accepted_not_filled",
                "skipped_orders",
                "fill_notional",
                "net_pnl",
                "skipped_opportunity_pnl",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.event_id,
                    row.event_title or "",
                    row.condition_id,
                    row.question or "",
                    row.candidate_signals,
                    row.accepted_orders,
                    row.simulated_fills,
                    row.accepted_not_filled,
                    row.skipped_orders,
                    _csv_decimal(row.fill_notional),
                    _csv_decimal(row.net_pnl),
                    _csv_decimal(row.skipped_opportunity_pnl),
                ]
            )


def _write_breakdown_csv(path: Path, rows: list[BreakdownRow]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "bucket",
                "candidate_signals",
                "accepted_orders",
                "simulated_fills",
                "accepted_not_filled",
                "skipped_orders",
                "fill_notional",
                "net_pnl",
                "skipped_opportunity_pnl",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.bucket,
                    row.candidate_signals,
                    row.accepted_orders,
                    row.simulated_fills,
                    row.accepted_not_filled,
                    row.skipped_orders,
                    _csv_decimal(row.fill_notional),
                    _csv_decimal(row.net_pnl),
                    _csv_decimal(row.skipped_opportunity_pnl),
                ]
            )


def _condition_table(rows: list[ConditionBreakdownRow]) -> list[str]:
    lines = [
        "| Event | Title | Condition | Question | Fills | Skipped | Net PnL | Skipped opportunity |",
        "|-------|-------|-----------|----------|-------|---------|---------|---------------------|",
    ]
    if not rows:
        lines.append("| - | - | - | No records | - | - | - | - |")
        return lines
    for row in rows:
        lines.append(
            f"| `{_clip(row.event_id, 24)}` | {_clip(row.event_title)} | `{_clip(row.condition_id, 24)}` | "
            f"{_clip(row.question)} | {row.simulated_fills} | {row.skipped_orders} | "
            f"{_fmt_usdc(row.net_pnl)} | {_fmt_usdc(row.skipped_opportunity_pnl)} |"
        )
    return lines


def _bucket_table(rows: list[BreakdownRow]) -> list[str]:
    lines = [
        "| Bucket | Signals | Orders | Fills | Not filled | Skipped | Notional | Net PnL | Skipped opportunity |",
        "|--------|---------|--------|-------|------------|---------|----------|---------|---------------------|",
    ]
    if not rows:
        lines.append("| - | - | - | - | - | - | - | - | - |")
        return lines
    for row in rows:
        lines.append(
            f"| {_clip(row.bucket)} | {row.candidate_signals} | {row.accepted_orders} | "
            f"{row.simulated_fills} | {row.accepted_not_filled} | {row.skipped_orders} | "
            f"{_fmt_usdc(row.fill_notional)} | {_fmt_usdc(row.net_pnl)} | "
            f"{_fmt_usdc(row.skipped_opportunity_pnl)} |"
        )
    return lines


def _market_concentration_answer(rows: list[ConditionBreakdownRow]) -> str:
    negative = [row for row in rows if row.net_pnl < _ZERO]
    total_loss = sum((abs(row.net_pnl) for row in negative), _ZERO)
    if total_loss == _ZERO:
        return "No negative test PnL was attributed to simulated fills."
    top = sum((abs(row.net_pnl) for row in negative[:3]), _ZERO)
    return f"Top 3 losing conditions explain {_fmt_pct(top / total_loss)} of attributed test losses."


def _side_price_context_answer(
    side_rows: list[BreakdownRow],
    price_rows: list[BreakdownRow],
    book_age_rows: list[BreakdownRow],
    quality_rows: list[BreakdownRow],
) -> str:
    parts = []
    for label, rows in (
        ("side", side_rows),
        ("price bucket", price_rows),
        ("book-age bucket", book_age_rows),
        ("context bucket", quality_rows),
    ):
        row = _worst_row(rows)
        if row is not None and row.net_pnl < _ZERO:
            parts.append(f"{label} `{row.bucket}` had {_fmt_usdc(row.net_pnl)}")
    return "; ".join(parts) + "." if parts else "No single side, price, age, or context bucket had negative attributed test PnL."


def _risk_gate_answer(records: list[DiagnosticRecord]) -> str:
    skipped = [row for row in records if row.execution_status == "skipped"]
    if not skipped:
        return "No selected-candidate test signals were skipped by filters or risk gates."
    opportunity = sum((row.skipped_opportunity_pnl for row in skipped), _ZERO)
    positives = sum((row.skipped_opportunity_pnl for row in skipped if row.skipped_opportunity_pnl > _ZERO), _ZERO)
    negatives = sum((row.skipped_opportunity_pnl for row in skipped if row.skipped_opportunity_pnl < _ZERO), _ZERO)
    if opportunity > _ZERO:
        verdict = "the skipped set looks net profitable in the diagnostic replay"
    elif opportunity < _ZERO:
        verdict = "the skipped set looks net loss-preventing in the diagnostic replay"
    else:
        verdict = "the skipped set is roughly flat or not fillable in the diagnostic replay"
    return (
        f"{len(skipped)} skipped signals; skipped opportunity estimate is {_fmt_usdc(opportunity)} "
        f"({_fmt_usdc(positives)} positive, {_fmt_usdc(negatives)} negative), so {verdict}."
    )


def _train_validation_concentration_answer(rows: list[ConditionBreakdownRow]) -> str:
    positive = [row for row in rows if row.net_pnl > _ZERO]
    total_profit = sum((row.net_pnl for row in positive), _ZERO)
    if total_profit == _ZERO:
        return "Train/validation has no positive attributed fill PnL."
    ordered = sorted(positive, key=lambda row: row.net_pnl, reverse=True)
    top3 = sum((row.net_pnl for row in ordered[:3]), _ZERO)
    return f"Top 3 profitable train/validation conditions explain {_fmt_pct(top3 / total_profit)} of positive attributed PnL."


def _event_specific_answer(
    event_rows: list[BreakdownRow],
    train_validation_condition_rows: list[ConditionBreakdownRow],
) -> str:
    negative_event = _worst_row(event_rows)
    tv_events = {row.event_id for row in train_validation_condition_rows if row.net_pnl > _ZERO}
    if negative_event is None:
        return "No test event attribution was available."
    concentration = abs(negative_event.net_pnl) / max(
        sum((abs(row.net_pnl) for row in event_rows), _ZERO),
        Decimal("1"),
    )
    if concentration >= Decimal("0.5") or len(tv_events) <= 2:
        return (
            f"Potentially event-specific: worst test event `{_clip(negative_event.bucket, 40)}` "
            f"contributes {_fmt_pct(concentration)} of absolute event PnL, and train/validation profits span {len(tv_events)} events."
        )
    return f"Less obviously event-specific: train/validation profits span {len(tv_events)} events and no single test event dominates absolute PnL."


def _next_filters_answer(*groups: list[BreakdownRow]) -> str:
    worst: list[BreakdownRow] = []
    for rows in groups:
        row = _worst_row(rows)
        if row is not None and row.net_pnl < _ZERO:
            worst.append(row)
    if not worst:
        return "No loss bucket stands out; next tests should be pre-registered on train/validation only."
    labels = ", ".join(f"`{row.bucket}`" for row in worst[:5])
    return (
        f"Pre-register train/validation-only filter tests around these diagnostic loss buckets: {labels}. "
        "Do not choose final thresholds from this test report."
    )


def _worst_row(rows: list[BreakdownRow]) -> Optional[BreakdownRow]:
    return min(rows, key=lambda row: row.net_pnl, default=None)


def _relevant_depth(row: dict, side: Optional[str]) -> Optional[Decimal]:
    if side == "SELL":
        return _decimal(row.get("bid_depth_top1"))
    return _decimal(row.get("ask_depth_top1"))


def _price_bucket(value: Optional[Decimal]) -> str:
    if value is None:
        return "unknown"
    if value < Decimal("0.10"):
        return "<0.10"
    if value < Decimal("0.25"):
        return "0.10-0.25"
    if value < Decimal("0.50"):
        return "0.25-0.50"
    if value < Decimal("0.75"):
        return "0.50-0.75"
    if value < Decimal("0.90"):
        return "0.75-0.90"
    return ">=0.90"


def _spread_bucket(value: Optional[Decimal]) -> str:
    if value is None:
        return "unknown"
    if value < Decimal("50"):
        return "<50"
    if value < Decimal("100"):
        return "50-100"
    if value < Decimal("250"):
        return "100-250"
    if value < Decimal("500"):
        return "250-500"
    if value < Decimal("1000"):
        return "500-1000"
    return ">=1000"


def _book_age_bucket(value: Optional[int]) -> str:
    if value is None:
        return "unknown"
    if value <= 5:
        return "0-5s"
    if value <= 15:
        return "6-15s"
    if value <= 30:
        return "16-30s"
    if value <= 60:
        return "31-60s"
    return ">60s"


def _depth_bucket(value: Optional[Decimal]) -> str:
    if value is None:
        return "unknown"
    if value < Decimal("10"):
        return "<10"
    if value < Decimal("50"):
        return "10-50"
    if value < Decimal("100"):
        return "50-100"
    if value < Decimal("250"):
        return "100-250"
    if value < Decimal("500"):
        return "250-500"
    return ">=500"


def _time_to_event_bucket(value: Optional[int]) -> str:
    if value is None:
        return "unknown"
    if value < 0:
        return "past_start"
    if value < 3600:
        return "0-1h"
    if value < 6 * 3600:
        return "1-6h"
    if value < 24 * 3600:
        return "6-24h"
    if value < 3 * 24 * 3600:
        return "1-3d"
    if value < 7 * 24 * 3600:
        return "3-7d"
    return ">7d"


def _hour_bucket(value: Optional[int]) -> str:
    if value is None or value < 0 or value > 23:
        return "unknown"
    return f"{value:02d}"


def _decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal_or_zero(value) -> Decimal:
    return _decimal(value) or _ZERO


def _int_or_none(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _fmt_usdc(value: Decimal) -> str:
    return f"${value:+.2f}"


def _fmt_pct(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _csv_decimal(value: Decimal) -> str:
    return format(value, "f")


def _clip(value: Optional[str], limit: int = 72) -> str:
    if not value:
        return ""
    value = str(value).replace("|", "\\|")
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
