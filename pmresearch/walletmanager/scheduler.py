"""APScheduler wiring: incremental sync every 5 min across all active
watchlist wallets, serialized (global concurrency 1) to avoid rate-limit
bans. Backfill is on-demand only (CLI), never scheduled."""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import text

from ..config import Settings, ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..db.migrations import upgrade_to_head
from ..logging_setup import setup_logging
from ..rawstore.store import RawStore
from ..sources.dataapi import DataApiSource
from ..sources.gamma import GammaSource
from ..ingest.markets import (
    MarketSyncStats,
    ledger_condition_ids,
    upsert_market_payloads,
)
from . import manager
from . import sync as sync_runner

logger = logging.getLogger(__name__)

INCREMENTAL_INTERVAL_MINUTES = 5
MARKETS_REFRESH_INTERVAL_MINUTES = 60


def _fetch_and_upsert_markets(
    session, source: GammaSource, raw_store: RawStore, condition_ids: list[str]
) -> MarketSyncStats:
    requested = sorted(
        {condition_id.lower() for condition_id in condition_ids if condition_id}
    )
    markets_upserted = 0
    tokens_upserted = 0
    events_upserted = 0
    missing: list[str] = []

    for open_batch in source.fetch_market_batches_by_condition_ids(
        raw_store, requested, closed=False
    ):
        open_stats = upsert_market_payloads(
            session, (open_batch.payload,), list(open_batch.requested_ids)
        )
        markets_upserted += open_stats.markets_upserted
        tokens_upserted += open_stats.tokens_upserted
        events_upserted += open_stats.events_upserted

        if not open_batch.missing_ids:
            continue

        for closed_batch in source.fetch_market_batches_by_condition_ids(
            raw_store, list(open_batch.missing_ids), closed=True
        ):
            closed_stats = upsert_market_payloads(
                session, (closed_batch.payload,), list(closed_batch.requested_ids)
            )
            markets_upserted += closed_stats.markets_upserted
            tokens_upserted += closed_stats.tokens_upserted
            events_upserted += closed_stats.events_upserted
            missing.extend(closed_batch.missing_ids)

    return MarketSyncStats(
        requested_conditions=len(requested),
        markets_upserted=markets_upserted,
        tokens_upserted=tokens_upserted,
        events_upserted=events_upserted,
        missing_conditions=len(set(missing)),
    )


def _fetch_and_upsert_closed_markets(
    session, source: GammaSource, raw_store: RawStore, condition_ids: list[str]
) -> MarketSyncStats:
    requested = sorted(
        {condition_id.lower() for condition_id in condition_ids if condition_id}
    )
    stats = MarketSyncStats.empty()
    missing: list[str] = []
    for batch in source.fetch_market_batches_by_condition_ids(
        raw_store, requested, closed=True
    ):
        batch_stats = upsert_market_payloads(
            session, (batch.payload,), list(batch.requested_ids)
        )
        stats = stats.merge(batch_stats)
        missing.extend(batch.missing_ids)
    return MarketSyncStats(
        requested_conditions=len(requested),
        markets_upserted=stats.markets_upserted,
        tokens_upserted=stats.tokens_upserted,
        events_upserted=stats.events_upserted,
        missing_conditions=len(set(missing)),
    )


def run_incremental_cycle(settings: Settings) -> None:
    session = get_session_factory(settings)()
    source = DataApiSource()
    raw_store = RawStore(settings, session)
    try:
        wallets = [row.address for row in manager.list_wallets(session)]
        for address in wallets:
            try:
                outcome = sync_runner.run_incremental(session, settings, raw_store, source, address)
                logger.info(
                    "Incremental sync %s: %d rows, %d requests",
                    address,
                    outcome.rows_fetched,
                    outcome.requests_made,
                )
            except Exception:
                logger.exception("Incremental sync failed for %s", address)
    finally:
        source.close()
        session.close()


def run_markets_refresh_cycle(settings: Settings, *, missing_only: bool = False) -> None:
    session = get_session_factory(settings)()
    source = GammaSource()
    raw_store = RawStore(settings, session)
    try:
        condition_ids = ledger_condition_ids(session, missing_only=missing_only)
        if not condition_ids:
            return
        stats = _fetch_and_upsert_markets(session, source, raw_store, condition_ids)
        logger.info(
            "Markets refresh: requested=%d markets=%d tokens=%d events=%d missing=%d",
            stats.requested_conditions,
            stats.markets_upserted,
            stats.tokens_upserted,
            stats.events_upserted,
            stats.missing_conditions,
        )
    except Exception:
        logger.exception("Markets refresh failed")
    finally:
        source.close()
        session.close()


def run_resolution_sweep_cycle(settings: Settings) -> None:
    # Same Gamma upsert path as refresh, narrowed to unresolved markets already
    # past their end date. Gamma exposes resolution by updating closed/prices.
    session = get_session_factory(settings)()
    try:
        rows = session.execute(
            text(
                "SELECT condition_id FROM markets "
                "WHERE closed = 0 AND end_date IS NOT NULL AND end_date <= datetime('now') "
                "ORDER BY end_date LIMIT 500"
            )
        ).fetchall()
    finally:
        session.close()

    condition_ids = [row.condition_id for row in rows]
    if not condition_ids:
        return

    session = get_session_factory(settings)()
    source = GammaSource()
    raw_store = RawStore(settings, session)
    try:
        stats = _fetch_and_upsert_closed_markets(session, source, raw_store, condition_ids)
        logger.info(
            "Resolution sweep: requested=%d markets=%d missing=%d",
            stats.requested_conditions,
            stats.markets_upserted,
            stats.missing_conditions,
        )
    except Exception:
        logger.exception("Resolution sweep failed")
    finally:
        source.close()
        session.close()


def build_scheduler(settings: Settings) -> BlockingScheduler:
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_incremental_cycle,
        "interval",
        minutes=INCREMENTAL_INTERVAL_MINUTES,
        args=[settings],
        id="incremental_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_markets_refresh_cycle,
        "interval",
        minutes=MARKETS_REFRESH_INTERVAL_MINUTES,
        args=[settings],
        kwargs={"missing_only": False},
        id="markets_refresh",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_resolution_sweep_cycle,
        "interval",
        minutes=MARKETS_REFRESH_INTERVAL_MINUTES,
        args=[settings],
        id="resolution_sweep",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def run_forever(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    logger.info("Starting collector; data_dir=%s", settings.data_dir)

    upgrade_to_head(settings)
    logger.info("Migrations applied.")

    scheduler = build_scheduler(settings)
    logger.info(
        "Collector scheduler started; incremental sync every %d min.", INCREMENTAL_INTERVAL_MINUTES
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Collector shutting down.")
