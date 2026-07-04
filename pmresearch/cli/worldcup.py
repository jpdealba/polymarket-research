"""pmr worldcup - World Cup forward watch."""

from __future__ import annotations

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..logging_setup import setup_logging
from ..walletmanager.manager import add_wallet
from ..worldcup.runner import tick_worldcup, watch_worldcup
from ..worldcup.status import (
    set_worldcup_tracked_wallets,
    worldcup_tracked_wallet_rows,
)


@click.group("worldcup")
def worldcup_group() -> None:
    """World Cup forward microstructure watch."""


@worldcup_group.group("wallets")
def worldcup_wallets_group() -> None:
    """Select up to 2 wallets for the permanent World Cup collector."""


@worldcup_wallets_group.command("list")
def worldcup_wallets_list() -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = worldcup_tracked_wallet_rows(session)
    finally:
        session.close()
    if not rows:
        click.echo("No World Cup tracked wallets selected.")
        return
    for row in rows:
        name = f" ({row.display_name})" if row.display_name else ""
        click.echo(
            f"priority={row.priority} wallet={row.wallet}{name} "
            f"source={row.source} selected_at={row.selected_at}"
        )


@worldcup_wallets_group.command("set")
@click.argument("wallets", nargs=-1, required=True)
def worldcup_wallets_set(wallets: tuple[str, ...]) -> None:
    """Replace the World Cup tracked-wallet selection. Max 2 wallets."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        if len(wallets) > 2:
            raise click.ClickException("World Cup watch supports at most 2 wallets.")
        for wallet in wallets:
            add_wallet(session, wallet)
        saved = set_worldcup_tracked_wallets(session, list(wallets), source="cli")
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        session.close()
    click.echo("tracking=" + ",".join(saved))


@worldcup_wallets_group.command("clear")
def worldcup_wallets_clear() -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        set_worldcup_tracked_wallets(session, [], source="cli")
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        session.close()
    click.echo("tracking=")


@worldcup_group.command("tick")
@click.option("--wallet", "wallet", required=True)
def worldcup_tick(wallet: str) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    stats = tick_worldcup(settings, wallet=wallet)
    click.echo(f"sync_rows={stats.sync.rows_fetched}")
    click.echo(f"ingest_inserted={stats.ingest.events_inserted}")
    click.echo(f"holdings_tokens={stats.holdings.tokens_written}")
    click.echo(f"watchlist_active_tokens={stats.watchlist.active_tokens}")
    click.echo(
        f"sample_run_id={stats.sample.run_id} sampled={stats.sample.tokens_sampled} "
        f"found={stats.sample.books_found} empty={stats.sample.empty_books} "
        f"errors={stats.sample.errors}"
    )
    click.echo(
        f"contexts_written={stats.context.contexts_written} "
        f"excellent={stats.context.excellent} good={stats.context.good} "
        f"usable={stats.context.usable} weak={stats.context.weak} "
        f"stale={stats.context.stale} missing={stats.context.missing}"
    )
    click.echo(f"enrichment_ran={stats.enrichment_ran}")


@worldcup_group.command("watch")
@click.option("--wallet", "wallet", required=True)
def worldcup_watch(wallet: str) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    watch_worldcup(settings, wallet=wallet)
