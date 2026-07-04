"""Raw ledger inspection queries: event-type counts and paginated event
listing. Extracted from `pmresearch/cli/ingest.py`'s `ledger_stats` command
so the CLI and `pmresearch/api.py` share one implementation (no duplicated
SQL) — see Phase 16 (docs/plan/IMPLEMENTATION_PLAN.md)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


def ledger_event_counts(session: Session, wallet: Optional[str] = None) -> list:
    """event_type -> count/min_ts/max_ts, exactly as `pmr ledger stats` prints."""
    query = (
        "SELECT event_type, COUNT(*) AS cnt, MIN(ts) AS min_ts, MAX(ts) AS max_ts "
        "FROM wallet_events"
    )
    params: dict = {}
    if wallet:
        query += " WHERE wallet = :w"
        params["w"] = wallet.lower()
    query += " GROUP BY event_type ORDER BY cnt DESC"
    return session.execute(text(query), params).fetchall()


def list_wallet_events(
    session: Session,
    wallet: str,
    *,
    limit: int = 100,
    offset: int = 0,
    event_type: Optional[str] = None,
) -> list:
    """Paginated, unaggregated raw listing of a wallet's ledger events."""
    where = "wallet = :wallet"
    params: dict = {"wallet": wallet.lower(), "limit": limit, "offset": offset}
    if event_type is not None:
        where += " AND event_type = :event_type"
        params["event_type"] = event_type
    query = (
        "SELECT id, event_type, ts, tx_hash, condition_id, token_id, side, "
        "delta_shares, delta_usdc, price, usdc_size, is_derived "
        f"FROM wallet_events WHERE {where} "
        "ORDER BY ts DESC, id DESC LIMIT :limit OFFSET :offset"
    )
    return session.execute(text(query), params).fetchall()


def fetch_events_by_ids(session: Session, ids: list[int]) -> list:
    """Full rows for a set of event ids, ordered for replay (ts, id)."""
    if not ids:
        return []
    _CHUNK = 500
    all_rows: list = []
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        placeholders = ", ".join(f":id{i}" for i in range(len(chunk)))
        params = {f"id{i}": event_id for i, event_id in enumerate(chunk)}
        query = f"SELECT * FROM wallet_events WHERE id IN ({placeholders}) ORDER BY ts, id"
        all_rows.extend(session.execute(text(query), params).fetchall())
    return all_rows
