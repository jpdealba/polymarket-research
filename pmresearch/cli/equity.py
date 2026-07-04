"""pmr equity - daily valuation projection."""

from __future__ import annotations

from decimal import Decimal

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ledger.replay import ledger_wallets
from ..projections.daily_equity import (
    DailyEquityProgress,
    fetch_daily_equity,
    rebuild_daily_equity,
)
from ..reconcile.checks import decimal_string
from ..walletmanager.manager import list_wallets


@click.group("equity")
def equity_group() -> None:
    """Build and inspect daily equity curves."""


@equity_group.command("build")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
def equity_build(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        wallets = [wallet.lower()] if wallet else _active_or_ledger_wallets(session)
        if not wallets:
            click.echo("No wallets to build.")
            return
        for address in wallets:
            click.echo(f"{address}: starting daily equity build")
            stats = rebuild_daily_equity(
                session,
                address,
                settings=settings,
                dust_epsilon=settings.dust_epsilon,
                progress_fn=_emit_build_progress,
            )
            click.echo(
                f"{stats.wallet}: rows={stats.rows_written} dates={stats.first_date}..{stats.last_date} "
                f"latest_value={decimal_string(stats.latest_portfolio_value)} "
                f"latest_stale_share={_pct(stats.latest_stale_equity_share)} "
                f"max_drawdown={decimal_string(stats.max_drawdown)}"
            )
    finally:
        session.close()


@equity_group.command("show")
@click.option("--wallet", "wallet", required=True)
@click.option("--limit", default=10, show_default=True)
def equity_show(wallet: str, limit: int) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = fetch_daily_equity(session, wallet)
    finally:
        session.close()
    if not rows:
        click.echo("No daily equity rows. Run `pmr equity build --wallet <addr>` first.")
        return
    click.echo("Caveat: drawdown is daily-close based; intraday drawdown is approximate.")
    click.echo(
        f"rows={len(rows)} date_range={rows[0].date}..{rows[-1].date} "
        f"latest_value={decimal_string(rows[-1].portfolio_value)} "
        f"latest_stale_share={_pct(rows[-1].stale_equity_share)} "
        f"max_drawdown={decimal_string(max(row.drawdown for row in rows))}"
    )
    click.echo(
        "date portfolio_value realized_pnl_cum unrealized_pnl reward_income_cum "
        "marked_pnl drawdown drawdown_basis stale_equity_share"
    )
    for row in rows[-limit:]:
        click.echo(
            f"{row.date} {decimal_string(row.portfolio_value)} "
            f"{decimal_string(row.realized_pnl_cum)} {decimal_string(row.unrealized_pnl)} "
            f"{decimal_string(row.reward_income_cum)} {decimal_string(row.marked_pnl)} "
            f"{decimal_string(row.drawdown)} {row.drawdown_basis} "
            f"{_pct(row.stale_equity_share)}"
        )


def _active_or_ledger_wallets(session) -> list[str]:
    active = [row.address for row in list_wallets(session, active_only=True)]
    return active or ledger_wallets(session)


def _pct(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"


def _emit_build_progress(progress: DailyEquityProgress) -> None:
    if progress.stage == "start":
        click.echo(
            f"  start: events_total={progress.events_total} "
            f"first_date={progress.current_date}"
        )
    elif progress.stage == "events":
        click.echo(
            f"  events: {progress.events_processed}/{progress.events_total} "
            f"date={progress.current_date} rows={progress.rows_written} "
            f"marks={progress.marks_written}"
        )
    elif progress.stage == "marks_flush":
        click.echo(
            f"  flush price_points: marks={progress.marks_written} "
            f"events={progress.events_processed}/{progress.events_total} "
            f"date={progress.current_date}"
        )
    elif progress.stage == "equity_flush":
        click.echo(
            f"  flush daily_equity: rows={progress.rows_written} "
            f"events={progress.events_processed}/{progress.events_total} "
            f"date={progress.current_date} marks={progress.marks_written}"
        )
    elif progress.stage == "empty":
        click.echo("  no wallet_events found")
