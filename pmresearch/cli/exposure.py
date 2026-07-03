"""pmr exposure - directional+bond and event-level exposure projections."""

from __future__ import annotations

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ledger.replay import ledger_wallets
from ..projections.exposures import (
    ExposuresProgress,
    fetch_event_exposures,
    fetch_exposures,
    rebuild_exposures,
)
from ..reconcile.checks import decimal_string
from ..walletmanager.manager import list_wallets


@click.group("exposure")
def exposure_group() -> None:
    """Build and inspect market/event exposure snapshots."""


@exposure_group.command("build")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
def exposure_build(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        wallets = [wallet.lower()] if wallet else _active_or_ledger_wallets(session)
        if not wallets:
            click.echo("No wallets to build.")
            return
        for address in wallets:
            click.echo(f"{address}: starting exposure build")
            stats = rebuild_exposures(
                session,
                address,
                dust_epsilon=settings.dust_epsilon,
                progress_fn=_emit_build_progress,
            )
            click.echo(
                f"{stats.wallet}: condition_rows={stats.condition_rows} "
                f"event_rows={stats.event_rows} "
                f"dates={stats.first_date}..{stats.last_date} "
                f"unknown_structure_warnings={stats.unknown_structure_warnings}"
            )
    finally:
        session.close()


@exposure_group.command("show")
@click.option("--wallet", "wallet", required=True)
@click.option("--market", "market", default=None, help="Filter to one condition_id.")
@click.option("--event", "event", default=None, help="Filter to one event_id.")
@click.option("--limit", default=10, show_default=True)
def exposure_show(wallet: str, market: str | None, event: str | None, limit: int) -> None:
    if market and event:
        raise click.UsageError("--market and --event cannot be combined.")
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        if event:
            rows = fetch_event_exposures(session, wallet, event_id=event)
            if not rows:
                click.echo("No event exposure rows. Run `pmr exposure build` first.")
                return
            click.echo("date event_id net_after_exclusivity exposure_vector")
            for row in rows[-limit:]:
                click.echo(
                    f"{row.date} {row.event_id} "
                    f"{decimal_string(row.net_after_exclusivity)} {row.exposure_vector}"
                )
        else:
            rows = fetch_exposures(session, wallet, condition_id=market)
            if not rows:
                click.echo("No exposure rows. Run `pmr exposure build` first.")
                return
            click.echo("date condition_id structure_type directional bond event_id")
            for row in rows[-limit:]:
                directional = "-" if row.directional is None else decimal_string(row.directional)
                bond = "-" if row.bond is None else decimal_string(row.bond)
                click.echo(
                    f"{row.date} {row.condition_id} {row.structure_type} "
                    f"{directional} {bond} {row.event_id or '-'}"
                )
    finally:
        session.close()


def _active_or_ledger_wallets(session) -> list[str]:
    active = [row.address for row in list_wallets(session, active_only=True)]
    return active or ledger_wallets(session)


def _emit_build_progress(progress: ExposuresProgress) -> None:
    if progress.stage == "start":
        click.echo(
            f"  start: events_total={progress.events_total} "
            f"first_date={progress.current_date}"
        )
    elif progress.stage == "events":
        click.echo(
            f"  events: {progress.events_processed}/{progress.events_total} "
            f"date={progress.current_date} condition_rows={progress.condition_rows} "
            f"event_rows={progress.event_rows}"
        )
    elif progress.stage == "flush":
        click.echo(
            f"  flush: condition_rows={progress.condition_rows} "
            f"event_rows={progress.event_rows} "
            f"events={progress.events_processed}/{progress.events_total} "
            f"date={progress.current_date}"
        )
    elif progress.stage == "empty":
        click.echo("  no wallet_events found")
