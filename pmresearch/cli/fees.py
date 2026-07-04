"""pmr fees - fee estimates and attribution reports."""

from __future__ import annotations

import click
from decimal import Decimal
from sqlalchemy.exc import OperationalError

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..fees.estimate import DEFAULT_BATCH_SIZE, compute_fee_estimates
from ..fees.schedules import FeeRule, list_fee_schedules
from ..logging_setup import setup_logging
from ..reports.fee_attribution import (
    FeeAttributionCoverage,
    FeeAttributionRow,
    fee_attribution_coverage,
    fee_attribution_report,
)


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _echo_row(row: FeeAttributionRow) -> None:
    click.echo(
        " ".join(
            [
                f"period={row.period}",
                f"category={row.category}",
                f"trade_count={row.trade_count}",
                f"enriched_fee_count={row.enriched_fee_count}",
                f"actual_fee_coverage_pct={row.actual_fee_coverage_pct.quantize(Decimal('0.01'))}",
                f"buy_volume={row.buy_volume}",
                f"gross_pnl={row.gross_pnl}",
                f"estimated_fee={row.estimated_fee}",
                f"worst_case_fee={row.worst_case_fee}",
                f"actual_fee={_fmt(row.actual_fee)}",
                f"estimated_fee_fallback={row.estimated_fee_fallback}",
                f"blended_fee={row.blended_fee}",
                f"estimated_net_pnl={row.estimated_net_pnl}",
                f"actual_net_pnl={_fmt(row.actual_net_pnl)}",
                f"net_pnl_after_blended_fees={row.blended_net_pnl}",
                f"gross_roi={_fmt(row.gross_roi)}",
                f"estimated_net_roi={_fmt(row.estimated_net_roi)}",
                f"maker_trades={row.maker_trades}",
                f"taker_trades={row.taker_trades}",
                f"maker_volume={row.maker_volume}",
                f"taker_volume={row.taker_volume}",
                f"maker_fees={row.maker_fee}",
                f"taker_fees={row.taker_fee}",
                f"fee_sources={row.fee_source_summary}",
            ]
        )
    )


def _echo_coverage(coverage: FeeAttributionCoverage) -> None:
    click.echo(f"total_trades={coverage.total_trades}")
    click.echo(f"category_classified_trades={coverage.category_classified_trades}")
    click.echo(f"fee_estimated_trades={coverage.fee_estimated_trades}")
    click.echo(f"unknown_category_trades={coverage.unknown_category_trades}")
    click.echo(f"actual_enriched_trades={coverage.actual_enriched_trades}")
    click.echo(f"actual_fee_coverage_pct={coverage.actual_fee_coverage_pct.quantize(Decimal('0.01'))}")
    click.echo(f"actual_fee_total={coverage.actual_fee_total}")
    click.echo(f"estimated_fee_total={coverage.estimated_fee_total}")
    click.echo(f"estimated_fee_fallback_total={coverage.estimated_fee_fallback_total}")
    click.echo(f"blended_fee_total={coverage.blended_fee_total}")


def _utc(ts: int | None) -> str:
    if ts is None:
        return "open"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _echo_schedule(rule: FeeRule) -> None:
    click.echo(
        " ".join(
            [
                f"category={rule.category}",
                f"from_utc={_utc(rule.effective_from_ts)}",
                f"to_utc={_utc(rule.effective_to_ts)}",
                f"rule_name={rule.rule_name}",
                f"params={rule.params}",
                f"source={rule.source}",
            ]
        )
    )


def _missing_fees_table_error(exc: OperationalError, *, db_path: str) -> click.ClickException:
    if "no such table: fee_schedules" in str(exc):
        return click.ClickException(
            "fee_schedules table is missing. Run `pmr db upgrade` first "
            f"against the same PMR_DATA_DIR/database. current_db={db_path}"
        )
    return click.ClickException(str(exc))


@click.group("fees")
def fees_group() -> None:
    """Fee estimate attribution reports."""


@fees_group.command("schedules")
def fees_schedules() -> None:
    """Show configured fee schedules, seeding defaults if needed."""
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        try:
            rules = list_fee_schedules(session)
            session.commit()
        except OperationalError as exc:
            session.rollback()
            raise _missing_fees_table_error(exc, db_path=str(settings.db_path)) from exc
    finally:
        session.close()

    for rule in rules:
        _echo_schedule(rule)


@fees_group.command("compute")
@click.option("--wallet", help="Only compute fee estimates for one wallet.")
@click.option(
    "--batch-size",
    default=DEFAULT_BATCH_SIZE,
    show_default=True,
    type=click.IntRange(min=1),
    help="Trades to process per commit/progress update.",
)
def fees_compute(wallet: str | None, batch_size: int) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        try:
            def progress(processed: int, total: int) -> None:
                click.echo(f"fee_estimates_progress={processed}/{total}", err=True)

            stats = compute_fee_estimates(
                session,
                wallet=wallet,
                batch_size=batch_size,
                on_progress=progress,
            )
        except OperationalError as exc:
            session.rollback()
            raise _missing_fees_table_error(exc, db_path=str(settings.db_path)) from exc
    finally:
        session.close()

    click.echo(f"total_trades={stats.total_trades}")
    click.echo(f"category_classified_trades={stats.category_classified_trades}")
    click.echo(f"fee_estimated_trades={stats.fee_estimated_trades}")
    click.echo(f"unknown_category_trades={stats.unknown_category_trades}")
    click.echo(f"actual_enriched_trades={stats.actual_enriched_trades}")
    click.echo(f"fee_estimates_upserted={stats.estimates_upserted}")
    click.echo(f"estimated_fee_total={stats.estimated_fee_total}")
    click.echo(f"worst_case_fee_total={stats.worst_case_fee_total}")
    click.echo(f"actual_fee_total={stats.actual_fee_total}")
    click.echo(f"estimated_fee_fallback_total={stats.estimated_fee_fallback_total}")
    click.echo(f"blended_fee_total={stats.blended_fee_total}")


@fees_group.command("report")
@click.option("--wallet", required=True, help="Wallet address to report.")
@click.option("--by-category", is_flag=True, help="Group rows by market category.")
@click.option(
    "--pre-post-sports-fee",
    is_flag=True,
    help="Split rows before/after the 2026-03-30 sports fee regime.",
)
def fees_report(wallet: str, by_category: bool, pre_post_sports_fee: bool) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        try:
            def progress(processed: int, total: int) -> None:
                click.echo(f"fee_estimates_progress={processed}/{total}", err=True)

            compute_fee_estimates(session, wallet=wallet, on_progress=progress)
            rows = fee_attribution_report(
                session,
                wallet=wallet,
                by_category=by_category,
                pre_post_sports_fee=pre_post_sports_fee,
            )
            coverage = fee_attribution_coverage(session, wallet=wallet)
        except OperationalError as exc:
            session.rollback()
            raise _missing_fees_table_error(exc, db_path=str(settings.db_path)) from exc
    finally:
        session.close()

    click.echo(
        "wallet_events remains gross/base Data-API cashflow. "
        "Fee reports preserve estimated fees, use observed fill_enrichment.fee where available, "
        "and use schedule estimates as fallback for the blended net view."
    )
    _echo_coverage(coverage)
    for row in rows:
        _echo_row(row)
