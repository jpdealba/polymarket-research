"""APScheduler wiring: incremental sync every 5 min across all active
watchlist wallets, serialized (global concurrency 1) to avoid rate-limit
bans. Backfill is on-demand only (CLI), never scheduled."""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from ..config import Settings, ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..db.migrations import upgrade_to_head
from ..logging_setup import setup_logging
from ..rawstore.store import RawStore
from ..sources.dataapi import DataApiSource
from . import manager
from . import sync as sync_runner

logger = logging.getLogger(__name__)

INCREMENTAL_INTERVAL_MINUTES = 5


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
