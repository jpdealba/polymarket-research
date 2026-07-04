"""pmr sync — backfill / incremental sync of raw wallet activity."""

from __future__ import annotations

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..logging_setup import setup_logging
from ..rawstore.store import RawStore
from ..sources.dataapi import DataApiSource
from ..walletmanager import manager
from ..walletmanager import sync as sync_runner


@click.group("sync")
def sync_group() -> None:
    """Backfill / incremental sync of raw wallet activity."""


@sync_group.command("backfill")
@click.argument("address")
def sync_backfill(address: str) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    raw_store = RawStore(settings, session)
    source = DataApiSource()
    try:
        def progress(cursor_ts: int) -> None:
            click.echo(f"{address.lower()}: backfill_checkpoint cursor_ts={cursor_ts}")

        outcome = sync_runner.run_backfill(
            session, settings, raw_store, source, address, on_progress=progress
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        source.close()
        session.close()
    click.echo(
        f"Backfill complete: {outcome.rows_fetched} rows across {outcome.requests_made} requests "
        f"(ts range {outcome.min_ts}..{outcome.max_ts})"
    )


@sync_group.command("incremental")
@click.argument("address", required=False)
def sync_incremental(address: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    source = DataApiSource()
    raw_store = RawStore(settings, session)
    try:
        addresses = [address.lower()] if address else [row.address for row in manager.list_wallets(session)]
        for addr in addresses:
            def progress(cursor_ts: int, *, wallet: str = addr) -> None:
                click.echo(f"{wallet}: sync_checkpoint cursor_ts={cursor_ts}")

            outcome = sync_runner.run_incremental(
                session, settings, raw_store, source, addr, on_progress=progress
            )
            click.echo(f"{addr}: {outcome.rows_fetched} new rows ({outcome.requests_made} requests)")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        source.close()
        session.close()


@sync_group.command("status")
@click.option("--alerts", is_flag=True, help="Show staleness and failure alerts.")
def sync_status(alerts: bool) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = manager.list_sync_states(session)
    finally:
        session.close()
    if not rows:
        click.echo("No wallets tracked.")
        return
    for row in rows:
        stale = manager.is_stale(session, row.wallet, cadence_s=900) if row.last_success_at else False
        flag = ""
        if stale:
            flag = " [STALE]"
        elif row.consecutive_failures >= 3:
            flag = " [FAILING]"
        elif row.status == "new":
            flag = " [NEW]"
        click.echo(
            f"{row.wallet}: status={row.status}{flag} backfill_complete={row.backfill_complete} "
            f"last_success_at={row.last_success_at} consecutive_failures={row.consecutive_failures} "
            f"last_error={row.last_error or '-'}"
        )
    if alerts:
        from ..alerts import check_wallet_alerts

        alert_list = check_wallet_alerts(session, settings)
        if alert_list:
            click.echo("\nAlerts:")
            for a in alert_list:
                click.echo(f"  [{a.severity.value.upper()}] {a.alert_type}: {a.message}")
        else:
            click.echo("\nNo active alerts.")
