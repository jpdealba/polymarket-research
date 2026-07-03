"""Gamma metadata adapter.

Gamma is the source for market/event dimensions. All responses are persisted
to the Raw Store before callers upsert mutable dimension tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import httpx

from ..rawstore.store import RawFetchResult, RawStore
from .base import RetryConfig, SourceAdapter

DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_BATCH_SIZE = 100


@dataclass(frozen=True)
class GammaFetchResult:
    raw_fetches: tuple[RawFetchResult, ...]
    payloads: tuple[list[dict], ...]

    @property
    def rows_fetched(self) -> int:
        return sum(len(payload) for payload in self.payloads)

    @property
    def requests_made(self) -> int:
        return len(self.payloads)


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


class GammaSource:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        client: httpx.Client | None = None,
        retry: RetryConfig | None = None,
        sleep_fn=None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        kwargs = {}
        if sleep_fn is not None:
            kwargs["sleep_fn"] = sleep_fn
        self._adapter = SourceAdapter(base_url, client=client, retry=retry, **kwargs)
        self.batch_size = batch_size

    def close(self) -> None:
        self._adapter.close()

    def __enter__(self) -> "GammaSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_markets_by_condition_ids(
        self, raw_store: RawStore, condition_ids: list[str]
    ) -> GammaFetchResult:
        raw_fetches: list[RawFetchResult] = []
        payloads: list[list[dict]] = []
        normalized = sorted({condition_id.lower() for condition_id in condition_ids if condition_id})

        for batch in _chunks(normalized, self.batch_size):
            params = {"condition_ids": batch, "limit": len(batch)}
            response, payload = self._adapter.get_json("/markets", params)
            response.raise_for_status()
            rows = payload if isinstance(payload, list) else []
            raw_result = raw_store.persist(
                source="gamma",
                endpoint="markets",
                wallet="_markets",
                params=params,
                payload=rows,
                http_status=response.status_code,
            )
            raw_fetches.append(raw_result)
            payloads.append(rows)

        return GammaFetchResult(tuple(raw_fetches), tuple(payloads))

    def fetch_events_by_ids(self, raw_store: RawStore, event_ids: list[str]) -> GammaFetchResult:
        raw_fetches: list[RawFetchResult] = []
        payloads: list[list[dict]] = []
        normalized = sorted({str(event_id) for event_id in event_ids if event_id})

        for batch in _chunks(normalized, self.batch_size):
            params = {"id": batch, "limit": len(batch)}
            response, payload = self._adapter.get_json("/events", params)
            response.raise_for_status()
            rows = payload if isinstance(payload, list) else []
            raw_result = raw_store.persist(
                source="gamma",
                endpoint="events",
                wallet="_events",
                params=params,
                payload=rows,
                http_status=response.status_code,
            )
            raw_fetches.append(raw_result)
            payloads.append(rows)

        return GammaFetchResult(tuple(raw_fetches), tuple(payloads))

