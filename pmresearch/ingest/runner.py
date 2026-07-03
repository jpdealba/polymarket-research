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
from typing import Optional

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

    raw_fetches = session.execute(text(query), params).fetchall()

    events_seen = 0
    events_inserted = 0
    now = datetime.now(timezone.utc).isoformat()

    for raw_fetch in raw_fetches:
        rows = _load_payload(raw_fetch.file_path)
        for row in rows:
            row_wallet = (row.get("proxyWallet") or "").lower()
            event = parse_activity_row(row, wallet=row_wallet, raw_fetch_id=raw_fetch.id)
            events_seen += 1
            result = session.execute(_INSERT_SQL, _event_params(event, now))
            if result.rowcount:
                events_inserted += 1
        session.execute(
            text("UPDATE raw_fetches SET ingested_at = :t WHERE id = :id"),
            {"t": now, "id": raw_fetch.id},
        )

    session.commit()

    return IngestStats(
        raw_fetches_processed=len(raw_fetches),
        events_seen=events_seen,
        events_inserted=events_inserted,
    )


def reparse_wallet(session: Session, wallet: str) -> IngestStats:
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
    return run_ingest(session, wallet=wallet)
