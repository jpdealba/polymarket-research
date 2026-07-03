"""Priority and persistence for marks."""

from __future__ import annotations

from decimal import Decimal
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from .base import Mark, MarkSource
from .resolution import ResolutionMarkSource

_ZERO = Decimal("0")


class MarkService:
    """Resolve marks with resolution precedence, cache, lazy sources, fallback."""

    def __init__(
        self,
        sources: list[MarkSource],
        *,
        staleness_window_s: int = 24 * 60 * 60,
        use_persistent_cache: bool = True,
    ) -> None:
        self.resolution_source = ResolutionMarkSource()
        self.sources = sources
        self.staleness_window_s = staleness_window_s
        self.use_persistent_cache = use_persistent_cache

    def close(self) -> None:
        for source in self.sources:
            close = getattr(source, "close", None)
            if close is not None:
                close()

    def get_mark(
        self, session: Session, token_id: str, ts: int, *, persist: bool = True
    ) -> Mark | None:
        token_id = str(token_id)
        resolution = self.resolution_source.get_mark(session, token_id, ts)
        if resolution is not None:
            if persist:
                self.persist_mark(session, resolution)
            return resolution

        if self.use_persistent_cache:
            cached = self._cached_exact(session, token_id, ts, fresh_only=True)
            if cached is not None:
                return cached

        for source in self.sources:
            mark = source.get_mark(session, token_id, ts)
            if mark is not None:
                if persist:
                    self.persist_mark(session, mark)
                return mark

        fallback = self._last_trade_mark(session, token_id, ts)
        if fallback is not None:
            if persist:
                self.persist_mark(session, fallback)
            return fallback
        if self.use_persistent_cache:
            return self._cached_exact(session, token_id, ts, fresh_only=False)
        return None

    def persist_mark(self, session: Session, mark: Mark) -> None:
        self.persist_marks(session, [mark])

    def persist_marks(self, session: Session, marks: list[Mark]) -> None:
        if not marks:
            return
        session.execute(
            text(
                "INSERT INTO price_points "
                "(token_id, ts, price, source, mark_age_s, stale, meta_json) "
                "VALUES (:token_id, :ts, :price, :source, :mark_age_s, :stale, :meta_json) "
                "ON CONFLICT(token_id, ts, source) DO UPDATE SET "
                "price = excluded.price, mark_age_s = excluded.mark_age_s, "
                "stale = excluded.stale, meta_json = excluded.meta_json"
            ),
            [
                {
                    "token_id": mark.token_id,
                    "ts": mark.ts,
                    "price": str(mark.price),
                    "source": mark.source,
                    "mark_age_s": int(mark.mark_age_s),
                    "stale": 1 if mark.stale else 0,
                    "meta_json": json.dumps(mark.meta, sort_keys=True, separators=(",", ":")),
                }
                for mark in marks
            ],
        )

    def _cached_exact(
        self, session: Session, token_id: str, ts: int, *, fresh_only: bool
    ) -> Mark | None:
        stale_clause = "AND stale = 0 AND mark_age_s <= :window" if fresh_only else ""
        row = session.execute(
            text(
                "SELECT token_id, ts, price, source, mark_age_s, stale, meta_json "
                "FROM price_points WHERE token_id = :token_id AND ts = :ts "
                "AND source != 'resolution' "
                f"{stale_clause} "
                "ORDER BY stale ASC, mark_age_s ASC LIMIT 1"
            ),
            {"token_id": token_id, "ts": ts, "window": self.staleness_window_s},
        ).fetchone()
        return _row_to_mark(row) if row is not None else None

    def _last_trade_mark(self, session: Session, token_id: str, ts: int) -> Mark | None:
        row = session.execute(
            text(
                "SELECT ts, price FROM wallet_events "
                "WHERE token_id = :token_id AND event_type = 'TRADE' AND ts <= :ts "
                "AND price IS NOT NULL AND CAST(price AS REAL) > 0 "
                "ORDER BY ts DESC, id DESC LIMIT 1"
            ),
            {"token_id": token_id, "ts": ts},
        ).fetchone()
        if row is None:
            return None
        age = max(0, ts - int(row.ts))
        return Mark(
            token_id=token_id,
            ts=ts,
            price=Decimal(str(row.price)),
            source="ledger_trade",
            mark_age_s=age,
            stale=True,
            meta={"underlying_ts": int(row.ts), "fallback": "last_ledger_trade"},
        )


def _row_to_mark(row) -> Mark:
    try:
        meta = json.loads(row.meta_json or "{}")
    except json.JSONDecodeError:
        meta = {}
    return Mark(
        token_id=row.token_id,
        ts=int(row.ts),
        price=Decimal(str(row.price)),
        source=row.source,
        mark_age_s=int(row.mark_age_s),
        stale=bool(row.stale),
        meta=meta,
    )
