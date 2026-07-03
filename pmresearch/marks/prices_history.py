"""Lazy CLOB prices-history mark source."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy.orm import Session

from ..rawstore.store import RawStore
from ..sources.base import RetryConfig, SourceAdapter
from .base import Mark

CLOB_BASE_URL = "https://clob.polymarket.com"


@dataclass(frozen=True)
class HistoryPoint:
    ts: int
    price: Decimal


class PricesHistoryMarkSource:
    name = "prices_history"

    def __init__(
        self,
        raw_store: RawStore,
        *,
        base_url: str = CLOB_BASE_URL,
        client: httpx.Client | None = None,
        retry: RetryConfig | None = None,
        staleness_window_s: int = 24 * 60 * 60,
        fidelity_minutes: int = 60,
        sleep_fn=None,
    ) -> None:
        kwargs = {}
        if sleep_fn is not None:
            kwargs["sleep_fn"] = sleep_fn
        self.raw_store = raw_store
        self.staleness_window_s = staleness_window_s
        self.fidelity_minutes = fidelity_minutes
        self._adapter = SourceAdapter(base_url, client=client, retry=retry, **kwargs)
        self._history_cache: dict[str, list[HistoryPoint]] = {}

    def close(self) -> None:
        self._adapter.close()

    def get_mark(self, session: Session, token_id: str, ts: int) -> Mark | None:
        points = self._history_for_token(token_id)
        before = [point for point in points if point.ts <= ts]
        if not before:
            return None
        point = max(before, key=lambda item: item.ts)
        age = max(0, ts - point.ts)
        return Mark(
            token_id=token_id,
            ts=ts,
            price=point.price,
            source=self.name,
            mark_age_s=age,
            stale=age > self.staleness_window_s,
            meta={"underlying_ts": point.ts, "fidelity_minutes": self.fidelity_minutes},
        )

    def _history_for_token(self, token_id: str) -> list[HistoryPoint]:
        if token_id in self._history_cache:
            return self._history_cache[token_id]
        params = {"market": token_id, "interval": "max", "fidelity": self.fidelity_minutes}
        response, payload = self._adapter.get_json("/prices-history", params)
        response.raise_for_status()
        self.raw_store.persist(
            source="clob",
            endpoint="prices-history",
            wallet=token_id,
            params=params,
            payload=payload or {},
            http_status=response.status_code,
        )
        points = _parse_points(payload)
        self._history_cache[token_id] = points
        return points


def _parse_points(payload: object) -> list[HistoryPoint]:
    if isinstance(payload, dict):
        rows = payload.get("history", [])
    else:
        rows = payload or []
    points: list[HistoryPoint] = []
    if not isinstance(rows, list):
        return points
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ts = int(row.get("t") or row.get("ts") or row.get("timestamp"))
            price = Decimal(str(row.get("p") or row.get("price")))
        except (TypeError, ValueError, InvalidOperation):
            continue
        points.append(HistoryPoint(ts=ts, price=price))
    return points
