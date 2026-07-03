"""pmr ingest / pmr ledger — parse Raw Store activity payloads into wallet_events."""

from __future__ import annotations

import click
from sqlalchemy import text

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ingest.runner import reparse_wallet, run_ingest
from ..logging_setup import setup_logging


@click.group("ingest")
def ingest_group() -> None:
    """Parse Raw Store activity payloads into the wallet_events ledger."""


@ingest_group.command("run")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
def ingest_run(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        stats = run_ingest(session, wallet=wallet)
    finally:
        session.close()
    click.echo(
        f"Processed {stats.raw_fetches_processed} raw fetches, "
        f"saw {stats.events_seen} events, inserted {stats.events_inserted} new rows."
    )


@ingest_group.command("reparse")
@click.option("--wallet", "wallet", required=True)
def ingest_reparse(wallet: str) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        stats = reparse_wallet(session, wallet)
    finally:
        session.close()
    click.echo(
        f"Reparsed {wallet.lower()}: {stats.raw_fetches_processed} raw fetches, "
        f"{stats.events_inserted} rows inserted."
    )


@click.group("ledger")
def ledger_group() -> None:
    """Ledger inspection."""


@ledger_group.command("stats")
@click.option("--wallet", "wallet", default=None)
def ledger_stats(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        query = (
            "SELECT event_type, COUNT(*) AS cnt, MIN(ts) AS min_ts, MAX(ts) AS max_ts "
            "FROM wallet_events"
        )
        params = {}
        if wallet:
            query += " WHERE wallet = :w"
            params["w"] = wallet.lower()
        query += " GROUP BY event_type ORDER BY cnt DESC"
        rows = session.execute(text(query), params).fetchall()
    finally:
        session.close()
    if not rows:
        click.echo("No ledger events.")
        return
    for row in rows:
        click.echo(f"{row.event_type:15s} count={row.cnt:8d} ts=[{row.min_ts}, {row.max_ts}]")
