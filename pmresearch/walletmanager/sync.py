"""Orchestrates backfill/incremental fetch calls: window-splitting lives in
the source adapter, sync_state bookkeeping lives in the manager, this module
just wires the two together and decides window boundaries."""

from __future__ import annotations

import logging
import time
from typing import Callable

from sqlalchemy.orm import Session

from ..config import Settings
from ..rawstore.store import RawStore
from ..sources.dataapi import GENESIS_TS, DataApiSource, FetchOutcome
from . import manager

logger = logging.getLogger(__name__)

INCREMENTAL_INDEXING_DELAY_SECONDS = 60
INCREMENTAL_OVERLAP_SECONDS = 300


def run_backfill(
    session: Session,
    settings: Settings,
    raw_store: RawStore,
    source: DataApiSource,
    address: str,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> FetchOutcome:
    """Fetch a wallet's full history. Resumable: if a previous attempt was
    interrupted partway (crash, process kill), this continues from its last
    checkpoint instead of re-walking already-covered history — see
    manager.checkpoint_backfill and DataApiSource.fetch_activity_range's
    on_progress callback."""
    address = address.lower()
    state = manager.get_sync_state(session, address)

    resuming = (
        state is not None and not state.backfill_complete and state.last_incremental_ts is not None
    )
    if resuming:
        high_bound = state.last_incremental_ts
        resume_end = state.backfill_cursor_ts if state.backfill_cursor_ts is not None else GENESIS_TS
    else:
        high_bound = int(time.time())
        resume_end = high_bound
        manager.start_backfill(session, address, high_bound=high_bound)

    def checkpoint(cursor_ts: int) -> None:
        manager.checkpoint_backfill(session, address, cursor_ts=cursor_ts)
        if on_progress is not None:
            on_progress(cursor_ts)

    try:
        outcome = source.fetch_activity_range(
            raw_store, address, GENESIS_TS, resume_end, on_progress=checkpoint
        )
    except Exception as exc:
        manager.record_failure(session, address, str(exc))
        raise
    manager.complete_backfill(session, address, cursor_ts=GENESIS_TS, up_to_ts=high_bound)
    return outcome


def run_incremental(
    session: Session,
    settings: Settings,
    raw_store: RawStore,
    source: DataApiSource,
    address: str,
    *,
    on_progress: Callable[[int], None] | None = None,
) -> FetchOutcome:
    address = address.lower()
    state = manager.get_sync_state(session, address)
    if state is None or not state.backfill_complete:
        return run_backfill(
            session, settings, raw_store, source, address, on_progress=on_progress
        )

    now_ts = int(time.time())
    effective_end_ts = now_ts - INCREMENTAL_INDEXING_DELAY_SECONDS
    last_watermark = state.last_incremental_ts or 0
    fetch_start_ts = max(0, last_watermark - INCREMENTAL_OVERLAP_SECONDS)
    next_watermark = max(last_watermark, effective_end_ts)

    if fetch_start_ts > effective_end_ts:
        logger.info(
            "Incremental sync skipped for %s: fetch_start=%d effective_end=%d "
            "last_watermark=%d now=%d",
            address,
            fetch_start_ts,
            effective_end_ts,
            last_watermark,
            now_ts,
        )
        return FetchOutcome.empty()

    logger.info(
        "Incremental sync for %s: fetching [%d, %d] with %ds overlap and %ds "
        "indexing delay (last_watermark=%d next_watermark=%d)",
        address,
        fetch_start_ts,
        effective_end_ts,
        INCREMENTAL_OVERLAP_SECONDS,
        INCREMENTAL_INDEXING_DELAY_SECONDS,
        last_watermark,
        next_watermark,
    )

    try:
        outcome = source.fetch_activity_range(
            raw_store,
            address,
            fetch_start_ts,
            effective_end_ts,
            on_progress=on_progress,
        )
    except Exception as exc:
        manager.record_failure(session, address, str(exc))
        raise
    manager.record_incremental_success(session, address, up_to_ts=next_watermark)
    logger.info(
        "Incremental sync complete for %s: rows=%d requests=%d watermark=%d",
        address,
        outcome.rows_fetched,
        outcome.requests_made,
        next_watermark,
    )
    return outcome
