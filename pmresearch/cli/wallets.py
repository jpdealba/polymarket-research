"""pmr wallet — watchlist CRUD."""

from __future__ import annotations

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..logging_setup import setup_logging
from ..walletmanager import manager


@click.group("wallet")
def wallet_group() -> None:
    """Watchlist wallet CRUD."""


@wallet_group.command("add")
@click.argument("address")
@click.option("--name", "display_name", default=None, help="Optional display name.")
def wallet_add(address: str, display_name: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        added = manager.add_wallet(session, address, display_name=display_name)
    finally:
        session.close()
    click.echo(f"{'Added' if added else 'Already on watchlist'}: {address.lower()}")


@wallet_group.command("remove")
@click.argument("address")
def wallet_remove(address: str) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        removed = manager.remove_wallet(session, address)
    finally:
        session.close()
    click.echo(f"{'Removed' if removed else 'Not active'}: {address.lower()}")


@wallet_group.command("list")
def wallet_list() -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = manager.list_wallets(session)
    finally:
        session.close()
    if not rows:
        click.echo("No active watchlist wallets.")
        return
    for row in rows:
        name = f" ({row.display_name})" if row.display_name else ""
        click.echo(f"{row.address}{name}")
