"""Align maker fills with valid forward-collected book context."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class MakerFillContextStats:
    fills_seen: int
    contexts_written: int
    excellent: int
    good: int
    usable: int
    weak: int
    stale: int
    missing: int


def _trade_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def context_status(age_s: int | None, *, max_age_s: int = 30) -> tuple[str, str | None]:
    if age_s is None:
        return "missing", "no_book_before_fill"
    if age_s > max_age_s:
        return "stale", "book_before_too_old"
    if age_s <= 2:
        return "excellent", None
    if age_s <= 5:
        return "good", None
    if age_s <= 10:
        return "usable", None
    return "weak", None


def _watchlist_id(session: Session, watchlist: str) -> int | None:
    row = session.execute(
        text("SELECT id FROM watchlists WHERE name = :name"), {"name": watchlist}
    ).fetchone()
    return int(row.id) if row is not None else None


def _fill_rows(session: Session, *, wallet: str, watchlist_id: int):
    """Both maker and taker fills — `role` (from fill_enrichment) tells them
    apart. Book-before/after context is equally meaningful for both: it's the
    state of the book at the moment of the trade either way."""
    return session.execute(
        text(
            "SELECT we.id AS event_id, we.wallet, we.token_id, we.condition_id, "
            "we.ts AS trade_ts, we.side, we.price AS fill_price, "
            "we.usdc_size AS fill_size, we.delta_usdc, fe.role "
            "FROM wallet_events we "
            "JOIN fill_enrichment fe ON fe.event_id = we.id "
            "JOIN watchlist_tokens wt ON wt.token_id = we.token_id "
            "WHERE wt.watchlist_id = :watchlist_id AND wt.is_active = 1 "
            "AND we.wallet = :wallet AND we.event_type = 'TRADE' "
            "AND fe.role IN ('maker', 'taker') AND we.token_id IS NOT NULL "
            "ORDER BY we.ts, we.id"
        ),
        {"watchlist_id": watchlist_id, "wallet": wallet.lower()},
    ).fetchall()


def _book_before(session: Session, token_id: str, trade_ts: int):
    return session.execute(
        text(
            "SELECT * FROM book_snapshots "
            "WHERE token_id = :token_id AND ts <= :trade_ts "
            "ORDER BY ts DESC LIMIT 1"
        ),
        {"token_id": token_id, "trade_ts": trade_ts},
    ).fetchone()


def _book_after(session: Session, token_id: str, trade_ts: int, *, max_age_s: int):
    return session.execute(
        text(
            "SELECT * FROM book_snapshots "
            "WHERE token_id = :token_id AND ts >= :trade_ts AND ts <= :max_ts "
            "ORDER BY ts ASC LIMIT 1"
        ),
        {"token_id": token_id, "trade_ts": trade_ts, "max_ts": trade_ts + max_age_s},
    ).fetchone()


def _params(fill, before, after, *, max_age_s: int) -> dict:
    before_age = None if before is None else int(fill.trade_ts) - int(before.ts)
    status, null_reason = context_status(before_age, max_age_s=max_age_s)
    after_age = None if after is None else int(after.ts) - int(fill.trade_ts)
    now = int(time.time())
    return {
        "event_id": fill.event_id,
        "wallet": fill.wallet,
        "token_id": fill.token_id,
        "condition_id": fill.condition_id,
        "trade_ts": fill.trade_ts,
        "trade_utc": _trade_utc(int(fill.trade_ts)),
        "side": fill.side,
        "fill_price": fill.fill_price,
        "fill_size": fill.fill_size,
        "delta_usdc": fill.delta_usdc,
        "role": fill.role,
        "book_before_ts": None if before is None else before.ts,
        "book_before_age_s": before_age,
        "best_bid_before": None if before is None else before.best_bid,
        "best_ask_before": None if before is None else before.best_ask,
        "spread_before": None if before is None else before.spread,
        "mid_before": None if before is None else before.mid,
        "depth_top_before_json": None if before is None else before.depth_top_json,
        "book_after_ts": None if after is None else after.ts,
        "book_after_age_s": after_age,
        "best_bid_after": None if after is None else after.best_bid,
        "best_ask_after": None if after is None else after.best_ask,
        "spread_after": None if after is None else after.spread,
        "mid_after": None if after is None else after.mid,
        "depth_top_after_json": None if after is None else after.depth_top_json,
        "context_status": status,
        "null_reason": null_reason,
        "created_at": now,
        "updated_at": now,
    }


def build_maker_fill_context(
    session: Session,
    *,
    wallet: str,
    watchlist: str = "world_cup_2026",
    max_age_s: int = 30,
) -> MakerFillContextStats:
    watchlist_id = _watchlist_id(session, watchlist)
    if watchlist_id is None:
        return MakerFillContextStats(0, 0, 0, 0, 0, 0, 0, 0)

    fills = _fill_rows(session, wallet=wallet, watchlist_id=watchlist_id)
    counts = {name: 0 for name in ("excellent", "good", "usable", "weak", "stale", "missing")}
    written = 0
    for fill in fills:
        before = _book_before(session, fill.token_id, int(fill.trade_ts))
        after = _book_after(session, fill.token_id, int(fill.trade_ts), max_age_s=max_age_s)
        params = _params(fill, before, after, max_age_s=max_age_s)
        counts[params["context_status"]] += 1
        session.execute(
            text(
                "INSERT INTO maker_fill_context "
                "(event_id, wallet, token_id, condition_id, trade_ts, trade_utc, side, "
                "fill_price, fill_size, delta_usdc, role, book_before_ts, "
                "book_before_age_s, best_bid_before, best_ask_before, spread_before, "
                "mid_before, depth_top_before_json, book_after_ts, book_after_age_s, "
                "best_bid_after, best_ask_after, spread_after, mid_after, "
                "depth_top_after_json, context_status, null_reason, created_at, updated_at) "
                "VALUES (:event_id, :wallet, :token_id, :condition_id, :trade_ts, "
                ":trade_utc, :side, :fill_price, :fill_size, :delta_usdc, :role, "
                ":book_before_ts, :book_before_age_s, :best_bid_before, "
                ":best_ask_before, :spread_before, :mid_before, "
                ":depth_top_before_json, :book_after_ts, :book_after_age_s, "
                ":best_bid_after, :best_ask_after, :spread_after, :mid_after, "
                ":depth_top_after_json, :context_status, :null_reason, "
                ":created_at, :updated_at) "
                "ON CONFLICT(event_id) DO UPDATE SET "
                "book_before_ts = excluded.book_before_ts, "
                "book_before_age_s = excluded.book_before_age_s, "
                "best_bid_before = excluded.best_bid_before, "
                "best_ask_before = excluded.best_ask_before, "
                "spread_before = excluded.spread_before, "
                "mid_before = excluded.mid_before, "
                "depth_top_before_json = excluded.depth_top_before_json, "
                "book_after_ts = excluded.book_after_ts, "
                "book_after_age_s = excluded.book_after_age_s, "
                "best_bid_after = excluded.best_bid_after, "
                "best_ask_after = excluded.best_ask_after, "
                "spread_after = excluded.spread_after, "
                "mid_after = excluded.mid_after, "
                "depth_top_after_json = excluded.depth_top_after_json, "
                "context_status = excluded.context_status, "
                "null_reason = excluded.null_reason, "
                "updated_at = excluded.updated_at"
            ),
            params,
        )
        written += 1
    session.commit()
    return MakerFillContextStats(
        fills_seen=len(fills),
        contexts_written=written,
        excellent=counts["excellent"],
        good=counts["good"],
        usable=counts["usable"],
        weak=counts["weak"],
        stale=counts["stale"],
        missing=counts["missing"],
    )
