"""pmr markets - sync and inspect Gamma market metadata."""

from __future__ import annotations

import click
from sqlalchemy import text

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ingest.markets import (
    MarketSyncStats,
    event_ids_for_conditions,
    incremental_market_condition_ids,
    ledger_condition_ids,
    missing_market_count,
    upsert_event_category,
    upsert_market_payloads,
)
from ..logging_setup import setup_logging
from ..rawstore.store import RawStore
from ..sources.gamma import GammaSource


def _sync_condition_ids(
    session, source: GammaSource, raw_store: RawStore, condition_ids: list[str]
) -> MarketSyncStats:
    requested = sorted(
        {condition_id.lower() for condition_id in condition_ids if condition_id}
    )
    markets_upserted = 0
    tokens_upserted = 0
    events_upserted = 0
    missing: list[str] = []
    batches = 0

    categorized_markets = 0

    for open_batch in source.fetch_market_batches_by_condition_ids(
        raw_store, requested, closed=False
    ):
        batches += 1
        open_stats = upsert_market_payloads(
            session, (open_batch.payload,), list(open_batch.requested_ids)
        )
        markets_upserted += open_stats.markets_upserted
        tokens_upserted += open_stats.tokens_upserted
        events_upserted += open_stats.events_upserted
        categorized_markets += _sync_categories(
            session, source, raw_store, list(open_batch.requested_ids)
        )
        session.commit()
        if open_batch.payload or batches == 1 or batches % 50 == 0:
            click.echo(
                f"Batch {batches} closed=false: "
                f"requested +{open_stats.requested_conditions}, "
                f"Gamma rows {len(open_batch.payload)}, "
                f"upserted +{open_stats.markets_upserted}; "
                f"totals requested {len(requested)}, "
                f"markets {markets_upserted}, categorized {categorized_markets}."
            )

        if not open_batch.missing_ids:
            continue

        for closed_batch in source.fetch_market_batches_by_condition_ids(
            raw_store, list(open_batch.missing_ids), closed=True
        ):
            batches += 1
            closed_stats = upsert_market_payloads(
                session, (closed_batch.payload,), list(closed_batch.requested_ids)
            )
            markets_upserted += closed_stats.markets_upserted
            tokens_upserted += closed_stats.tokens_upserted
            events_upserted += closed_stats.events_upserted
            categorized_markets += _sync_categories(
                session, source, raw_store, list(closed_batch.requested_ids)
            )
            session.commit()
            missing.extend(closed_batch.missing_ids)
            if closed_batch.payload or batches % 50 == 0:
                click.echo(
                    f"Batch {batches} closed=true: "
                    f"requested +{closed_stats.requested_conditions}, "
                    f"Gamma rows {len(closed_batch.payload)}, "
                    f"upserted +{closed_stats.markets_upserted}; "
                    f"totals requested {len(requested)}, "
                    f"markets {markets_upserted}, missing {len(missing)}."
                )

    click.echo(f"Categorized {categorized_markets} markets total.")

    return MarketSyncStats(
        requested_conditions=len(requested),
        markets_upserted=markets_upserted,
        tokens_upserted=tokens_upserted,
        events_upserted=events_upserted,
        missing_conditions=len(set(missing)),
    )


def _sync_categories(
    session, source: GammaSource, raw_store: RawStore, condition_ids: list[str]
) -> int:
    event_ids = event_ids_for_conditions(session, condition_ids)
    if not event_ids:
        return 0

    categorized = 0
    for payload in source.fetch_events_by_ids(raw_store, event_ids).payloads:
        for event in payload:
            categorized += upsert_event_category(session, event)
    return categorized


@click.group("markets")
def markets_group() -> None:
    """Gamma market metadata."""


@markets_group.command("sync")
@click.option(
    "--all",
    "sync_all",
    is_flag=True,
    help="Full refresh: sync every condition_id in the ledger (slow).",
)
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
            condition_ids = (
                ledger_condition_ids(session, missing_only=False)
                if sync_all
                else incremental_market_condition_ids(session)
            )
        raw_store = RawStore(settings, session)
        stats = _sync_condition_ids(session, source, raw_store, condition_ids)
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
