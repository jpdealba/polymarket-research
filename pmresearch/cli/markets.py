"""pmr markets - sync and inspect Gamma market metadata."""

from __future__ import annotations

import click
from sqlalchemy import text

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ingest.markets import ledger_condition_ids, missing_market_count, upsert_market_payloads
from ..logging_setup import setup_logging
from ..rawstore.store import RawStore
from ..sources.gamma import GammaSource


@click.group("markets")
def markets_group() -> None:
    """Gamma market metadata."""


@markets_group.command("sync")
@click.option("--all", "sync_all", is_flag=True, help="Sync every condition_id in the ledger.")
@click.option("--condition", "conditions", multiple=True, help="Specific condition_id to sync.")
def markets_sync(sync_all: bool, conditions: tuple[str, ...]) -> None:
    if sync_all and conditions:
        raise click.UsageError("Use either --all or --condition, not both.")

    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    source = GammaSource()
    try:
        condition_ids = list(conditions)
        if not condition_ids:
            condition_ids = ledger_condition_ids(session, missing_only=not sync_all)
            if not condition_ids and not sync_all:
                condition_ids = ledger_condition_ids(session, missing_only=False)
        raw_store = RawStore(settings, session)
        fetch_result = source.fetch_markets_by_condition_ids(raw_store, condition_ids)
        stats = upsert_market_payloads(session, fetch_result.payloads, condition_ids)
    finally:
        source.close()
        session.close()

    click.echo(
        f"Requested {stats.requested_conditions} conditions; "
        f"upserted {stats.markets_upserted} markets, {stats.tokens_upserted} tokens, "
        f"{stats.events_upserted} events; Gamma missing {stats.missing_conditions}."
    )


@markets_group.command("stats")
def markets_stats() -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        row = session.execute(
            text(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN closed = 1 THEN 1 ELSE 0 END) AS resolved, "
                "SUM(CASE WHEN structure_type = 'unclassified' THEN 1 ELSE 0 END) AS unclassified "
                "FROM markets"
            )
        ).fetchone()
        missing = missing_market_count(session)
    finally:
        session.close()

    total = int(row.total or 0)
    resolved = int(row.resolved or 0)
    unclassified = int(row.unclassified or 0)
    click.echo(f"markets_total={total}")
    click.echo(f"resolved={resolved}")
    click.echo(f"unclassified_descriptors={unclassified}")
    click.echo(f"ledger_conditions_missing_market={missing}")

