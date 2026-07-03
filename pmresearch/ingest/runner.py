"""Iterates unprocessed raw_fetches (activity endpoint), parses each into
WalletEvent rows, and inserts them into wallet_events (insert-or-ignore on
dedupe_key — idempotent by construction: re-running finds nothing left to
process, and even a never-before-seen raw_fetch whose rows overlap an
already-ingested one contributes zero new rows for the overlapping part).

--reparse wipes a wallet's ledger rows and re-ingests from raw. Safe because
raw + the source API are the system of record for ingestion; ledger rows are
a deterministic parse of that, not independent data (ADR 0002).
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..ledger.model import WalletEvent
from .activity import parse_activity_row

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestStats:
    raw_fetches_processed: int
    events_seen: int
    events_inserted: int


_INSERT_SQL = text(
    "INSERT INTO wallet_events "
    "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
    "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, dedupe_key, ingested_at) "
    "VALUES (:wallet, :event_type, :ts, :tx_hash, :condition_id, :token_id, :side, "
    ":delta_shares, :delta_usdc, :price, :usdc_size, :source, 0, :raw_ref, :dedupe_key, :ingested_at) "
    "ON CONFLICT(dedupe_key) DO NOTHING"
)


def _load_payload(file_path: str) -> list[dict]:
    with gzip.open(file_path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _event_params(event: WalletEvent, ingested_at: str) -> dict:
    return {
        "wallet": event.wallet,
        "event_type": event.event_type,
        "ts": event.ts,
        "tx_hash": event.tx_hash,
        "condition_id": event.condition_id,
        "token_id": event.token_id,
        "side": event.side,
        "delta_shares": str(event.delta_shares),
        "delta_usdc": str(event.delta_usdc),
        "price": str(event.price),
        "usdc_size": str(event.usdc_size),
        "source": event.source,
        "raw_ref": event.raw_ref,
        "dedupe_key": event.dedupe_key,
        "ingested_at": ingested_at,
    }


def _parse_rows(rows: list[dict], raw_fetch_id: int) -> tuple[list[WalletEvent], bool]:
    seen: dict[str, int] = {}
    events: list[WalletEvent] = []
    has_duplicate_rows = False
    for row in rows:
        row_wallet = (row.get("proxyWallet") or "").lower()
        event = parse_activity_row(row, wallet=row_wallet, raw_fetch_id=raw_fetch_id)
        duplicate_index = seen.get(event.dedupe_key, 0)
        seen[event.dedupe_key] = duplicate_index + 1
        if duplicate_index:
            has_duplicate_rows = True
            event = parse_activity_row(
                row,
                wallet=row_wallet,
                raw_fetch_id=raw_fetch_id,
                duplicate_index=duplicate_index,
            )
        events.append(event)
    return events, has_duplicate_rows


def _insert_events(session: Session, events: list[WalletEvent], ingested_at: str) -> int:
    inserted = 0
    for event in events:
        result = session.execute(_INSERT_SQL, _event_params(event, ingested_at))
        if result.rowcount:
            inserted += 1
    return inserted


def run_ingest(session: Session, *, wallet: Optional[str] = None) -> IngestStats:
    query = (
        "SELECT id, file_path FROM raw_fetches "
        "WHERE source = 'dataapi' AND endpoint = 'activity' AND ingested_at IS NULL"
    )
    params: dict = {}
    if wallet is not None:
        query += " AND json_extract(params_json, '$.user') = :wallet"
        params["wallet"] = wallet.lower()
    query += " ORDER BY id"

    return _run_ingest(session, query=query, params=params, wallet=wallet)


def _run_ingest(
    session: Session,
    *,
    query: str,
    params: dict,
    wallet: Optional[str],
    on_progress: Callable[[int, int, int, int], None] | None = None,
) -> IngestStats:
    raw_fetches = session.execute(text(query), params).fetchall()
    raw_fetch_ids_processed = {raw_fetch.id for raw_fetch in raw_fetches}

    events_seen = 0
    events_inserted = 0
    now = datetime.now(timezone.utc).isoformat()

    total_fetches = len(raw_fetches)
    for index, raw_fetch in enumerate(raw_fetches, start=1):
        rows = _load_payload(raw_fetch.file_path)
        events, _ = _parse_rows(rows, raw_fetch.id)
        events_seen += len(events)
        raw_events_inserted = _insert_events(session, events, now)
        session.execute(
            text("UPDATE raw_fetches SET ingested_at = :t WHERE id = :id"),
            {"t": now, "id": raw_fetch.id},
        )
        session.commit()
        events_inserted += raw_events_inserted
        if on_progress is not None:
            on_progress(index, total_fetches, events_seen, events_inserted)

    duplicate_repair_fetches = 0
    if wallet is not None:
        repair_rows = session.execute(
            text(
                "SELECT id, file_path FROM raw_fetches "
                "WHERE source = 'dataapi' AND endpoint = 'activity' "
                "AND ingested_at IS NOT NULL "
                "AND json_extract(params_json, '$.user') = :wallet "
                "ORDER BY id"
            ),
            {"wallet": wallet.lower()},
        ).fetchall()
        for raw_fetch in repair_rows:
            if raw_fetch.id in raw_fetch_ids_processed:
                continue
            rows = _load_payload(raw_fetch.file_path)
            events, has_duplicate_rows = _parse_rows(rows, raw_fetch.id)
            if not has_duplicate_rows:
                continue
            duplicate_repair_fetches += 1
            events_seen += len(events)
            events_inserted += _insert_events(session, events, now)
            session.commit()

    return IngestStats(
        raw_fetches_processed=len(raw_fetches) + duplicate_repair_fetches,
        events_seen=events_seen,
        events_inserted=events_inserted,
    )


def run_ingest_with_progress(
    session: Session,
    *,
    wallet: Optional[str] = None,
    on_progress: Callable[[int, int, int, int], None] | None = None,
) -> IngestStats:
    query = (
        "SELECT id, file_path FROM raw_fetches "
        "WHERE source = 'dataapi' AND endpoint = 'activity' AND ingested_at IS NULL"
    )
    params: dict = {}
    if wallet is not None:
        query += " AND json_extract(params_json, '$.user') = :wallet"
        params["wallet"] = wallet.lower()
    query += " ORDER BY id"
    return _run_ingest(
        session, query=query, params=params, wallet=wallet, on_progress=on_progress
    )


def reparse_wallet(
    session: Session,
    wallet: str,
    *,
    on_progress: Callable[[int, int, int, int], None] | None = None,
) -> IngestStats:
    wallet = wallet.lower()
    session.execute(text("DELETE FROM wallet_events WHERE wallet = :w"), {"w": wallet})
    session.execute(
        text(
            "UPDATE raw_fetches SET ingested_at = NULL "
            "WHERE source = 'dataapi' AND endpoint = 'activity' "
            "AND json_extract(params_json, '$.user') = :w"
        ),
        {"w": wallet},
    )
    session.commit()
    return run_ingest_with_progress(session, wallet=wallet, on_progress=on_progress)
