"""Orchestrates backfill/incremental fetch calls: window-splitting lives in
the source adapter, sync_state bookkeeping lives in the manager, this module
just wires the two together and decides window boundaries."""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from ..config import Settings
from ..rawstore.store import RawStore
from ..sources.dataapi import GENESIS_TS, DataApiSource, FetchOutcome
from . import manager


def run_backfill(
    session: Session, settings: Settings, raw_store: RawStore, source: DataApiSource, address: str
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
    session: Session, settings: Settings, raw_store: RawStore, source: DataApiSource, address: str
) -> FetchOutcome:
    address = address.lower()
    state = manager.get_sync_state(session, address)
    if state is None or not state.backfill_complete:
        return run_backfill(session, settings, raw_store, source, address)

    start_ts = (state.last_incremental_ts or 0) + 1
    now_ts = int(time.time())
    if start_ts > now_ts:
        return FetchOutcome.empty()

    try:
        outcome = source.fetch_activity_range(raw_store, address, start_ts, now_ts)
    except Exception as exc:
        manager.record_failure(session, address, str(exc))
        raise
    manager.record_incremental_success(session, address, up_to_ts=now_ts)
    return outcome
