"""World Cup token watchlist construction.

This module is deliberately conservative: it selects tokens from local market
metadata and RN1-local ledger/holdings facts only. It does not infer hidden
orders or attribute public liquidity to the wallet.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

WORLD_CUP_KEYWORDS = (
    "world cup",
    "fifa",
    "canada",
    "morocco",
    "paraguay",
    "france",
    "brazil",
    "norway",
    "mexico",
    "england",
    "portugal",
    "spain",
    "united states",
    "belgium",
    "argentina",
    "egypt",
    "switzerland",
    "colombia",
    "team to advance",
)

DERIVATIVE_KEYWORDS = (
    "o/u",
    "over",
    "under",
    "spread",
    "exact score",
    "team to advance",
    "quarterfinal",
    "semifinal",
    "final",
)


@dataclass(frozen=True)
class WatchlistBuildStats:
    watchlist_id: int
    tokens_seen: int
    tokens_upserted: int
    active_tokens: int


@dataclass(frozen=True)
class WatchlistToken:
    watchlist_id: int
    token_id: str
    condition_id: str | None
    market_id: str | None
    question: str | None
    outcome_label: str | None
    market_category: str | None
    market_slug: str | None
    source: str
    priority: int
    reason: str | None
    first_seen_ts: int
    last_seen_ts: int
    is_active: int
    latest_best_bid: str | None = None
    latest_best_ask: str | None = None
    latest_spread: str | None = None
    latest_mid: str | None = None
    latest_book_ts: int | None = None


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower())


def _is_world_cup_text(*values: object) -> bool:
    haystack = " ".join(_norm(v) for v in values)
    return any(keyword in haystack for keyword in WORLD_CUP_KEYWORDS)


def _is_derivative(question: object, slug: object) -> bool:
    haystack = f"{_norm(question)} {_norm(slug)}"
    return any(keyword in haystack for keyword in DERIVATIVE_KEYWORDS)


def ensure_watchlist(
    session: Session,
    name: str = "world_cup_2026",
    *,
    description: str | None = None,
) -> int:
    now = int(time.time())
    row = session.execute(
        text("SELECT id FROM watchlists WHERE name = :name"), {"name": name}
    ).fetchone()
    if row is not None:
        session.execute(
            text("UPDATE watchlists SET updated_at = :now, is_active = 1 WHERE id = :id"),
            {"now": now, "id": row.id},
        )
        session.commit()
        return int(row.id)

    watchlist_id = session.execute(
        text(
            "INSERT INTO watchlists (name, description, created_at, updated_at, is_active) "
            "VALUES (:name, :description, :now, :now, 1) RETURNING id"
        ),
        {"name": name, "description": description, "now": now},
    ).scalar()
    session.commit()
    return int(watchlist_id)


def _market_token_rows(session: Session):
    return session.execute(
        text(
            "SELECT t.token_id, t.condition_id, t.outcome_label, "
            "m.question, m.slug AS market_slug, m.category AS market_category, "
            "m.event_id, m.closed, pe.title AS event_title, pe.slug AS event_slug "
            "FROM tokens t "
            "JOIN markets m ON m.condition_id = t.condition_id "
            "LEFT JOIN pm_events pe ON pe.event_id = m.event_id "
            "WHERE m.closed = 0"
        )
    ).fetchall()


def _row_payload(row, *, source: str, priority: int, reason: str) -> dict:
    return {
        "token_id": row.token_id,
        "condition_id": row.condition_id,
        "market_id": row.condition_id,
        "question": row.question,
        "outcome_label": row.outcome_label,
        "market_category": row.market_category,
        "market_slug": row.market_slug,
        "source": source,
        "priority": priority,
        "reason": reason,
    }


def _upsert_watchlist_token(
    session: Session,
    *,
    watchlist_id: int,
    token: dict,
    now: int,
) -> bool:
    existing = session.execute(
        text(
            "SELECT priority FROM watchlist_tokens "
            "WHERE watchlist_id = :watchlist_id AND token_id = :token_id"
        ),
        {"watchlist_id": watchlist_id, "token_id": token["token_id"]},
    ).fetchone()
    inserted = existing is None
    keep_existing = existing is not None and int(existing.priority) <= int(token["priority"])
    params = {
        "watchlist_id": watchlist_id,
        "now": now,
        **token,
        "update_source": token["source"],
        "update_priority": token["priority"],
        "update_reason": token["reason"],
    }
    if keep_existing:
        params["update_source"] = None
        params["update_priority"] = existing.priority
        params["update_reason"] = None

    session.execute(
        text(
            "INSERT INTO watchlist_tokens "
            "(watchlist_id, token_id, condition_id, market_id, question, outcome_label, "
            "market_category, market_slug, source, priority, reason, first_seen_ts, "
            "last_seen_ts, is_active) "
            "VALUES (:watchlist_id, :token_id, :condition_id, :market_id, :question, "
            ":outcome_label, :market_category, :market_slug, :source, :priority, "
            ":reason, :now, :now, 1) "
            "ON CONFLICT(watchlist_id, token_id) DO UPDATE SET "
            "condition_id = COALESCE(excluded.condition_id, watchlist_tokens.condition_id), "
            "market_id = COALESCE(excluded.market_id, watchlist_tokens.market_id), "
            "question = COALESCE(excluded.question, watchlist_tokens.question), "
            "outcome_label = COALESCE(excluded.outcome_label, watchlist_tokens.outcome_label), "
            "market_category = COALESCE(excluded.market_category, watchlist_tokens.market_category), "
            "market_slug = COALESCE(excluded.market_slug, watchlist_tokens.market_slug), "
            "source = COALESCE(:update_source, watchlist_tokens.source), "
            "priority = :update_priority, "
            "reason = COALESCE(:update_reason, watchlist_tokens.reason), "
            "last_seen_ts = :now, "
            "is_active = 1"
        ),
        params,
    )
    return inserted


def _recent_trade_tokens(session: Session, wallet: str, *, recent_hours: int = 168) -> set[str]:
    cutoff = int(time.time()) - recent_hours * 3600
    rows = session.execute(
        text(
            "SELECT DISTINCT token_id FROM wallet_events "
            "WHERE wallet = :wallet AND event_type = 'TRADE' "
            "AND token_id IS NOT NULL AND ts >= :cutoff"
        ),
        {"wallet": wallet.lower(), "cutoff": cutoff},
    ).fetchall()
    return {row.token_id for row in rows}


def _open_holding_tokens(session: Session, wallet: str, *, dust_epsilon: str = "0.000001") -> set[str]:
    rows = session.execute(
        text(
            "SELECT DISTINCT token_id FROM holdings "
            "WHERE wallet = :wallet "
            "AND (CAST(qty AS REAL) > :dust OR CAST(qty AS REAL) < -:dust)"
        ),
        {"wallet": wallet.lower(), "dust": float(dust_epsilon)},
    ).fetchall()
    return {row.token_id for row in rows}


def build_world_cup_watchlist(
    session: Session,
    wallet: str,
    *,
    name: str = "world_cup_2026",
    dust_epsilon: str = "0.000001",
) -> WatchlistBuildStats:
    watchlist_id = ensure_watchlist(
        session,
        name,
        description="Forward-only World Cup 2026 microstructure watch",
    )
    now = int(time.time())
    recent = _recent_trade_tokens(session, wallet)
    holdings = _open_holding_tokens(session, wallet, dust_epsilon=dust_epsilon)

    candidates: dict[str, dict] = {}
    for row in _market_token_rows(session):
        # Wallet-local facts (recent trade / open holding) are tracked regardless
        # of the World Cup keyword filter: once RN1 leaves the World Cup for MLB,
        # tennis, etc., we still follow its live positions. The keyword filter only
        # gates the *speculative* branches below (markets RN1 hasn't touched yet).
        if row.token_id in recent:
            token = _row_payload(
                row,
                source="rn1_recent_trade",
                priority=10,
                reason="rn1 traded this token recently",
            )
        elif row.token_id in holdings:
            token = _row_payload(
                row,
                source="rn1_open_holding",
                priority=20,
                reason="rn1 has nonzero local holding",
            )
        elif not _is_world_cup_text(
            row.question,
            row.market_slug,
            row.market_category,
            row.event_title,
            row.event_slug,
        ):
            continue
        elif _is_derivative(row.question, row.market_slug):
            token = _row_payload(
                row,
                source="world_cup_market_type",
                priority=40,
                reason="question contains world cup derivative market type",
            )
        else:
            token = _row_payload(
                row,
                source="world_cup_keyword",
                priority=30,
                reason="question contains world cup keyword",
            )

        existing = candidates.get(row.token_id)
        if existing is None or token["priority"] < existing["priority"]:
            candidates[row.token_id] = token

    # Directly add recently traded tokens that weren't captured by
    # _market_token_rows (which only returns open markets).  This covers
    # tokens whose market resolved but the wallet still traded on them
    # within the lookback window.
    for token_id in recent:
        if token_id not in candidates:
            candidates[token_id] = {
                "token_id": token_id,
                "condition_id": None,
                "market_id": None,
                "question": None,
                "outcome_label": None,
                "market_category": None,
                "market_slug": None,
                "source": "rn1_recent_trade_closed",
                "priority": 10,
                "reason": "rn1 traded recently on a resolved market",
            }

    upserted = 0
    for token in candidates.values():
        if _upsert_watchlist_token(session, watchlist_id=watchlist_id, token=token, now=now):
            upserted += 1

    # Retire tokens whose market has since resolved AND were not recently
    # traded.  Recently traded tokens on resolved markets are kept active
    # so the book sampler can still collect snapshots for them.
    session.execute(
        text(
            "UPDATE watchlist_tokens SET is_active = 0, last_seen_ts = :now "
            "WHERE watchlist_id = :watchlist_id AND is_active = 1 "
            "AND token_id IN ("
            "  SELECT t.token_id FROM tokens t "
            "  JOIN markets m ON m.condition_id = t.condition_id "
            "  WHERE m.closed = 1"
            ") "
            "AND token_id NOT IN ("
            "  SELECT DISTINCT we.token_id FROM wallet_events we "
            "  WHERE we.wallet = :wallet AND we.event_type = 'TRADE' "
            "  AND we.token_id IS NOT NULL AND we.ts >= :recent_cutoff"
            ")"
        ),
        {"watchlist_id": watchlist_id, "now": now, "wallet": wallet.lower(), "recent_cutoff": now - 168 * 3600},
    )
    session.commit()
    active = session.execute(
        text(
            "SELECT COUNT(*) FROM watchlist_tokens "
            "WHERE watchlist_id = :watchlist_id AND is_active = 1"
        ),
        {"watchlist_id": watchlist_id},
    ).scalar() or 0
    return WatchlistBuildStats(
        watchlist_id=watchlist_id,
        tokens_seen=len(candidates),
        tokens_upserted=upserted,
        active_tokens=int(active),
    )


def add_manual_token(
    session: Session,
    *,
    name: str,
    token_id: str,
    reason: str = "manual add",
) -> bool:
    watchlist_id = ensure_watchlist(session, name)
    now = int(time.time())
    row = session.execute(
        text(
            "SELECT t.token_id, t.condition_id, t.outcome_label, "
            "m.question, m.slug AS market_slug, m.category AS market_category "
            "FROM tokens t LEFT JOIN markets m ON m.condition_id = t.condition_id "
            "WHERE t.token_id = :token_id"
        ),
        {"token_id": token_id},
    ).fetchone()
    if row is None:
        class MinimalRow:
            pass

        row = MinimalRow()
        row.token_id = token_id
        row.condition_id = None
        row.outcome_label = None
        row.question = None
        row.market_slug = None
        row.market_category = None
    token = _row_payload(row, source="manual", priority=90, reason=reason)
    inserted = _upsert_watchlist_token(session, watchlist_id=watchlist_id, token=token, now=now)
    session.commit()
    return inserted


def deactivate_token(session: Session, *, name: str, token_id: str) -> bool:
    row = session.execute(
        text("SELECT id FROM watchlists WHERE name = :name"), {"name": name}
    ).fetchone()
    if row is None:
        return False
    result = session.execute(
        text(
            "UPDATE watchlist_tokens SET is_active = 0, last_seen_ts = :now "
            "WHERE watchlist_id = :watchlist_id AND token_id = :token_id"
        ),
        {"watchlist_id": row.id, "token_id": token_id, "now": int(time.time())},
    )
    session.commit()
    return bool(result.rowcount)


def list_watchlist_tokens(
    session: Session,
    *,
    name: str,
    active_only: bool = False,
) -> list[WatchlistToken]:
    query = (
        "SELECT wt.*, bs.best_bid AS latest_best_bid, bs.best_ask AS latest_best_ask, "
        "bs.spread AS latest_spread, bs.mid AS latest_mid, bs.ts AS latest_book_ts "
        "FROM watchlist_tokens wt "
        "JOIN watchlists w ON w.id = wt.watchlist_id "
        "LEFT JOIN book_snapshots bs ON bs.token_id = wt.token_id "
        "AND bs.ts = (SELECT MAX(ts) FROM book_snapshots WHERE token_id = wt.token_id) "
        "WHERE w.name = :name"
    )
    if active_only:
        query += " AND wt.is_active = 1"
    query += " ORDER BY wt.priority, wt.token_id"
    rows = session.execute(text(query), {"name": name}).fetchall()
    return [WatchlistToken(**dict(row._mapping)) for row in rows]


def active_token_rows_for_sampling(
    session: Session,
    *,
    name: str,
    limit: int,
    max_priority: int | None = None,
    min_priority: int | None = None,
) -> tuple[int | None, list]:
    watchlist = session.execute(
        text("SELECT id FROM watchlists WHERE name = :name AND is_active = 1"),
        {"name": name},
    ).fetchone()
    if watchlist is None:
        return None, []
    query = (
        "SELECT * FROM watchlist_tokens "
        "WHERE watchlist_id = :watchlist_id AND is_active = 1"
    )
    params: dict[str, object] = {"watchlist_id": watchlist.id, "limit": limit}
    if max_priority is not None:
        query += " AND priority <= :max_priority"
        params["max_priority"] = max_priority
    if min_priority is not None:
        query += " AND priority >= :min_priority"
        params["min_priority"] = min_priority
    query += " ORDER BY priority, last_seen_ts DESC, token_id LIMIT :limit"
    return int(watchlist.id), session.execute(text(query), params).fetchall()


def token_ids(rows: Iterable) -> list[str]:
    return [row.token_id for row in rows]
