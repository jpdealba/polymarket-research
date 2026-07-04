"""Relevant Tokens query for the book sampler.

A token is "relevant" if any watchlist wallet either:
1. Has a nonzero holding (from the holdings projection), OR
2. Traded it in the last 24 hours.

Tokens from resolved/closed markets are excluded — no need to sample a dead
orderbook.

The query is designed to be efficient against millions of ledger rows:
- holdings is a small projection table (one row per wallet×token)
- wallet_events has an index on (wallet, ts) for the recent-trade scan
- markets.closed is used to filter resolved markets
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def relevant_token_ids(
    session: Session,
    *,
    now_utc: datetime | None = None,
    dust_epsilon: str = "0.000001",
    recent_hours: int = 24,
) -> list[str]:
    """Return the deduplicated set of token IDs the book sampler should poll.

    Two efficient sub-queries, unioned in Python:

      1. Open-position tokens: from the holdings projection (tiny table).
         abs(qty) > dust_epsilon, market not closed.

      2. Recently-traded tokens: from wallet_events (large table, but the
         index on wallet+ts narrows the scan).  Trades in the last N hours,
         excluding closed markets.
    """
    now_ts = int((now_utc or datetime.now(timezone.utc)).timestamp())
    cutoff_ts = now_ts - recent_hours * 3600

    open_tokens = session.execute(
        text(
            "SELECT DISTINCT h.token_id "
            "FROM holdings h "
            "JOIN tokens t ON t.token_id = h.token_id "
            "JOIN markets m ON m.condition_id = t.condition_id "
            "WHERE m.closed = 0 "
            "AND (CAST(h.qty AS REAL) > :dust OR CAST(h.qty AS REAL) < -:dust)"
        ),
        {"dust": float(dust_epsilon)},
    ).fetchall()

    recent_tokens = session.execute(
        text(
            "SELECT DISTINCT e.token_id "
            "FROM wallet_events e "
            "JOIN tokens t ON t.token_id = e.token_id "
            "JOIN markets m ON m.condition_id = t.condition_id "
            "WHERE e.event_type = 'TRADE' "
            "AND e.ts >= :cutoff "
            "AND e.token_id IS NOT NULL "
            "AND m.closed = 0"
        ),
        {"cutoff": cutoff_ts},
    ).fetchall()

    seen: set[str] = set()
    result: list[str] = []
    for row in (*open_tokens, *recent_tokens):
        tid = row.token_id
        if tid not in seen:
            seen.add(tid)
            result.append(tid)

    result.sort()
    if result:
        logger.info(
            "Relevant tokens: %d total (%d open-position + %d recent-trade)",
            len(result),
            len(open_tokens),
            len(recent_tokens),
        )
    else:
        logger.info("Relevant tokens: none found.")

    return result
