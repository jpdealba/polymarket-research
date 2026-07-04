"""One-shot and foreground World Cup watch runners."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..booksampler.watchlist import WatchlistSampleStats, sample_watchlist_once
from ..config import Settings
from ..context.maker_fills import MakerFillContextStats, build_maker_fill_context
from ..db.engine import get_session_factory
from ..ingest.enrichment import run_enrichment
from ..ingest.runner import IngestStats, run_ingest
from ..projections.holdings import HoldingsRebuildStats, rebuild_holdings
from ..rawstore.store import RawStore
from ..sources.dataapi import DataApiSource, FetchOutcome
from ..walletmanager.sync import run_incremental
from ..watchlists.world_cup import WatchlistBuildStats, build_world_cup_watchlist

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorldCupTickStats:
    sync: FetchOutcome
    ingest: IngestStats
    holdings: HoldingsRebuildStats
    watchlist: WatchlistBuildStats
    sample: WatchlistSampleStats
    context: MakerFillContextStats
    enrichment_ran: bool


def tick_worldcup(settings: Settings, *, wallet: str) -> WorldCupTickStats:
    wallet = wallet.lower()
    session = get_session_factory(settings)()
    source = DataApiSource()
    raw_store = RawStore(settings, session)
    try:
        sync = run_incremental(session, settings, raw_store, source, wallet)
        ingest = run_ingest(session, wallet=wallet)
        holdings = rebuild_holdings(
            session,
            wallet,
            dust_epsilon=settings.dust_epsilon,
        )
        watchlist = build_world_cup_watchlist(
            session,
            wallet,
            name=settings.worldcup_watchlist_name,
            dust_epsilon=str(settings.dust_epsilon),
        )
        enrichment_ran = False
        if settings.subgraph_url:
            run_enrichment(session, settings, wallet, source="subgraph")
            enrichment_ran = True
    finally:
        source.close()
        session.close()

    sample = sample_watchlist_once(
        settings,
        name=settings.worldcup_watchlist_name,
        limit=settings.worldcup_sample_limit,
        wallet=wallet,
    )
    session = get_session_factory(settings)()
    try:
        context = build_maker_fill_context(
            session,
            wallet=wallet,
            watchlist=settings.worldcup_watchlist_name,
            max_age_s=settings.worldcup_context_max_age_s,
        )
    finally:
        session.close()
    return WorldCupTickStats(
        sync=sync,
        ingest=ingest,
        holdings=holdings,
        watchlist=watchlist,
        sample=sample,
        context=context,
        enrichment_ran=enrichment_ran,
    )


def watch_worldcup(settings: Settings, *, wallet: str) -> None:
    logger.info("Starting World Cup watch loop for %s", wallet.lower())
    while True:
        try:
            stats = tick_worldcup(settings, wallet=wallet)
            logger.info(
                "World Cup tick: sync_rows=%d ingest_inserted=%d watch_tokens=%d "
                "sampled=%d contexts=%d",
                stats.sync.rows_fetched,
                stats.ingest.events_inserted,
                stats.watchlist.active_tokens,
                stats.sample.tokens_sampled,
                stats.context.contexts_written,
            )
        except Exception:
            logger.exception("World Cup tick failed")
        time.sleep(max(1, settings.worldcup_book_interval_s))
