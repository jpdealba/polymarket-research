"""Gamma metadata adapter.

Gamma is the source for market/event dimensions. All responses are persisted
to the Raw Store before callers upsert mutable dimension tables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlencode

import httpx

from ..rawstore.store import RawFetchResult, RawStore
from .base import RetryConfig, SourceAdapter

DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_BATCH_SIZE = 100
DEFAULT_MAX_QUERY_CHARS = 1800

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class GammaFetchBatch:
    requested_ids: tuple[str, ...]
    raw_fetch: RawFetchResult
    payload: list[dict]
    closed: bool

    @property
    def rows_fetched(self) -> int:
        return len(self.payload)

    @property
    def returned_ids(self) -> tuple[str, ...]:
        return tuple(
            condition_id
            for market in self.payload
            if (condition_id := _market_condition_id(market)) is not None
        )

    @property
    def missing_ids(self) -> tuple[str, ...]:
        returned = set(self.returned_ids)
        return tuple(
            condition_id for condition_id in self.requested_ids if condition_id not in returned
        )


def _market_condition_id(market: dict) -> str | None:
    condition_id = market.get("conditionId") or market.get("condition_id")
    return str(condition_id).lower() if condition_id else None


def _market_params(condition_ids: list[str], *, closed: bool) -> list[tuple[str, str]]:
    params = [("condition_ids", condition_id) for condition_id in condition_ids]
    params.append(("closed", "true" if closed else "false"))
    params.append(("limit", str(len(condition_ids))))
    return params


def _query_length(param_name: str, values: list[str], *, closed: bool | None = None) -> int:
    if param_name == "condition_ids":
        return len(urlencode(_market_params(values, closed=bool(closed))))
    return len(
        urlencode([(param_name, value) for value in values] + [("limit", str(len(values)))])
    )


def _query_safe_chunks(
    values: list[str],
    *,
    param_name: str,
    batch_size: int,
    max_query_chars: int,
    closed: bool | None = None,
) -> Iterable[list[str]]:
    max_batch_size = max(1, batch_size)
    current: list[str] = []
    for value in values:
        candidate = current + [value]
        if current and (
            len(candidate) > max_batch_size
            or _query_length(param_name, candidate, closed=closed) > max_query_chars
        ):
            yield current
            current = [value]
        else:
            current = candidate
    if current:
        yield current


class GammaSource:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        client: httpx.Client | None = None,
        retry: RetryConfig | None = None,
        sleep_fn=None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_query_chars: int = DEFAULT_MAX_QUERY_CHARS,
    ) -> None:
        kwargs = {}
        if sleep_fn is not None:
            kwargs["sleep_fn"] = sleep_fn
        self._adapter = SourceAdapter(base_url, client=client, retry=retry, **kwargs)
        self.batch_size = batch_size
        self.max_query_chars = max_query_chars

    def close(self) -> None:
        self._adapter.close()

    def __enter__(self) -> "GammaSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_markets_by_condition_ids(
        self, raw_store: RawStore, condition_ids: list[str], *, closed: bool = False
    ) -> GammaFetchResult:
        raw_fetches: list[RawFetchResult] = []
        payloads: list[list[dict]] = []
        for batch_result in self.fetch_market_batches_by_condition_ids(
            raw_store, condition_ids, closed=closed
        ):
            raw_fetches.append(batch_result.raw_fetch)
            payloads.append(batch_result.payload)

        return GammaFetchResult(tuple(raw_fetches), tuple(payloads))

    def fetch_market_batches_by_condition_ids(
        self, raw_store: RawStore, condition_ids: list[str], *, closed: bool = False
    ) -> Iterable[GammaFetchBatch]:
        normalized = sorted(
            {condition_id.lower() for condition_id in condition_ids if condition_id}
        )

        for batch in _query_safe_chunks(
            normalized,
            param_name="condition_ids",
            batch_size=self.batch_size,
            max_query_chars=self.max_query_chars,
            closed=closed,
        ):
            params = _market_params(batch, closed=closed)
            response, payload = self._adapter.get_json("/markets", params)
            response.raise_for_status()
            rows = self._filter_exact_market_matches(
                payload if isinstance(payload, list) else [], batch
            )
            raw_result = raw_store.persist(
                source="gamma",
                endpoint="markets",
                wallet="_markets",
                params=params,
                payload=rows,
                http_status=response.status_code,
            )
            yield GammaFetchBatch(tuple(batch), raw_result, rows, closed)

    def _filter_exact_market_matches(
        self, rows: list[dict], requested_condition_ids: list[str]
    ) -> list[dict]:
        requested = set(requested_condition_ids)
        matches: list[dict] = []
        for market in rows:
            condition_id = _market_condition_id(market)
            if condition_id in requested:
                matches.append(market)
                continue
            logger.warning(
                "Ignoring Gamma market with conditionId=%s; not requested in batch of %d.",
                condition_id,
                len(requested),
            )
        return matches

    def fetch_events_by_ids(self, raw_store: RawStore, event_ids: list[str]) -> GammaFetchResult:
        raw_fetches: list[RawFetchResult] = []
        payloads: list[list[dict]] = []
        normalized = sorted({str(event_id) for event_id in event_ids if event_id})

        for batch in _query_safe_chunks(
            normalized,
            param_name="id",
            batch_size=self.batch_size,
            max_query_chars=self.max_query_chars,
        ):
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
