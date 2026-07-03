"""pmr — CLI entrypoint for the pmresearch package."""

from __future__ import annotations

from pathlib import Path

import click

from .. import __version__
from ..backup import create_backup, restore_backup
from ..config import ensure_data_dirs, get_settings
from ..db.migrations import current_revision, upgrade_to_head
from ..logging_setup import setup_logging
from .ingest import ingest_group, ledger_group
from .markets import markets_group
from .sync import sync_group
from .wallets import wallet_group


@click.group()
def main() -> None:
    """pmr — Polymarket Wallet Research Platform CLI."""


main.add_command(wallet_group)
main.add_command(sync_group)
main.add_command(ingest_group)
main.add_command(ledger_group)
main.add_command(markets_group)


@main.command()
def run() -> None:
    """Run the collector scheduler in the foreground (what the container runs)."""
    from ..walletmanager.scheduler import run_forever

    run_forever()


@main.command()
def version() -> None:
    """Print the installed pmresearch version."""
    click.echo(__version__)


@main.group()
def db() -> None:
    """Database schema commands."""


@db.command("upgrade")
def db_upgrade() -> None:
    """Apply all pending Alembic migrations."""
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    upgrade_to_head(settings)
    click.echo("Database upgraded to head.")


@db.command("current")
def db_current() -> None:
    """Show the current Alembic revision."""
    settings = get_settings()
    current_revision(settings, verbose=True)


@main.command()
def backup() -> None:
    """VACUUM INTO a timestamped backup under {data_dir}/backups/."""
    settings = get_settings()
    ensure_data_dirs(settings)
    path = create_backup(settings)
    click.echo(str(path))


@main.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def restore(file: Path) -> None:
    """Restore the database from a backup file, replacing the live DB."""
    settings = get_settings()
    ensure_data_dirs(settings)
    restore_backup(settings, file)
    click.echo("Restore complete.")
