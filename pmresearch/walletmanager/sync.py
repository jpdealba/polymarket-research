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
    address = address.lower()
    manager.start_backfill(session, address)
    now_ts = int(time.time())
    try:
        outcome = source.fetch_activity_range(raw_store, address, GENESIS_TS, now_ts)
    except Exception as exc:
        manager.record_failure(session, address, str(exc))
        raise
    manager.complete_backfill(session, address, cursor_ts=GENESIS_TS, up_to_ts=now_ts)
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
