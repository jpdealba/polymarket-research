"""Ordered ledger event stream for projections (Phase 4).

Projections are folds over `wallet_events` in deterministic order: `ts` first,
insert `id` as the tie-breaker (same-timestamp events replay in ingestion
order, so two rebuilds always see the same sequence — ADR 0002).
"""

from __future__ import annotations

from typing import Iterator, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

_STREAM_SQL = (
    "SELECT id, wallet, event_type, ts, condition_id, token_id, "
    "delta_shares, delta_usdc "
    "FROM wallet_events {where} ORDER BY ts, id"
)


def stream_events(session: Session, *, wallet: Optional[str] = None) -> Iterator:
    """Yield ledger event rows for one wallet (or all) ordered by (ts, id)."""
    where = ""
    params: dict = {}
    if wallet is not None:
        where = "WHERE wallet = :wallet"
        params["wallet"] = wallet.lower()
    result = session.execute(
        text(_STREAM_SQL.format(where=where)).execution_options(stream_results=True),
        params,
    )
    yield from result


def ledger_wallets(session: Session) -> list[str]:
    """All wallets present in the ledger (for --wallet-less replays)."""
    rows = session.execute(text("SELECT DISTINCT wallet FROM wallet_events ORDER BY wallet"))
    return [row.wallet for row in rows]
