"""Terminal resolution-value marks."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from .base import Mark


class ResolutionMarkSource:
    name = "resolution"

    def __init__(self) -> None:
        self._cache: dict[str, tuple[Decimal, dict] | None] = {}

    def get_mark(self, session: Session, token_id: str, ts: int) -> Mark | None:
        cached = self._cache.get(token_id)
        if token_id in self._cache:
            if cached is None:
                return None
            price, meta = cached
            return Mark(
                token_id=token_id,
                ts=ts,
                price=price,
                source=self.name,
                mark_age_s=0,
                stale=False,
                meta=meta,
            )
        row = session.execute(
            text(
                "SELECT m.condition_id, m.closed, m.resolution_prices_json, m.closed_time "
                "FROM tokens t JOIN markets m ON m.condition_id = t.condition_id "
                "WHERE t.token_id = :token_id"
            ),
            {"token_id": token_id},
        ).fetchone()
        if row is None or not row.closed or not row.resolution_prices_json:
            self._cache[token_id] = None
            return None
        try:
            prices = json.loads(row.resolution_prices_json)
            price = Decimal(str(prices[token_id]))
        except (KeyError, TypeError, json.JSONDecodeError, InvalidOperation, ValueError):
            self._cache[token_id] = None
            return None
        meta = {"condition_id": row.condition_id, "closed_time": row.closed_time}
        self._cache[token_id] = (price, meta)
        return Mark(
            token_id=token_id,
            ts=ts,
            price=price,
            source=self.name,
            mark_age_s=0,
            stale=False,
            meta=meta,
        )
