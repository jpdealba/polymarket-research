"""Data-API `/activity` adapter — the canonical per-wallet fill feed (ADR 0001).

`offset` is capped at 3000 server-side. When a window has more rows than that,
we page in descending order until the cap would be exceeded, then continue
with a narrower window ending at the oldest timestamp seen so far (inclusive,
so any other same-second rows we might not have reached yet are re-fetched).
That boundary second is the only part that can be re-fetched — never the
whole window — so overlap is bounded to at most one second's worth of rows
per split; Phase 2's ledger ingest dedupes exact-duplicate rows by content.
Every page is persisted to the Raw Store before this module looks at its
contents; this module never writes to the ledger (that's Phase 2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from ..rawstore.store import RawStore
from .base import RetryConfig, SourceAdapter

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://data-api.polymarket.com"

# 2021-01-01T00:00:00Z — before Polymarket's CLOB launch; a safe lower bound
# for "fetch full history".
GENESIS_TS = 1609459200

PAGE_LIMIT = 500
MAX_OFFSET = 3000


@dataclass(frozen=True)
class FetchOutcome:
    raw_fetch_ids: tuple[int, ...]
    rows_fetched: int
    min_ts: Optional[int]
    max_ts: Optional[int]
    requests_made: int

    @staticmethod
    def empty() -> "FetchOutcome":
        return FetchOutcome((), 0, None, None, 0)

    def merge(self, other: "FetchOutcome") -> "FetchOutcome":
        mins = [v for v in (self.min_ts, other.min_ts) if v is not None]
        maxes = [v for v in (self.max_ts, other.max_ts) if v is not None]
        return FetchOutcome(
            raw_fetch_ids=self.raw_fetch_ids + other.raw_fetch_ids,
            rows_fetched=self.rows_fetched + other.rows_fetched,
            min_ts=min(mins) if mins else None,
            max_ts=max(maxes) if maxes else None,
            requests_made=self.requests_made + other.requests_made,
        )


def _is_offset_cap_error(response: httpx.Response, payload: object) -> bool:
    if response.status_code != 400:
        return False
    error = ""
    if isinstance(payload, dict):
        error = str(payload.get("error", ""))
    return "offset" in error.lower()


class DataApiSource:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        client: httpx.Client | None = None,
        retry: RetryConfig | None = None,
        sleep_fn=None,
    ) -> None:
        kwargs = {}
        if sleep_fn is not None:
            kwargs["sleep_fn"] = sleep_fn
        self._adapter = SourceAdapter(base_url, client=client, retry=retry, **kwargs)

    def close(self) -> None:
        self._adapter.close()

    def __enter__(self) -> "DataApiSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_activity_range(
        self, raw_store: RawStore, wallet: str, start_ts: int, end_ts: int
    ) -> FetchOutcome:
        """Fetch every `/activity` row for `wallet` within [start_ts, end_ts]
        (both inclusive), splitting on the offset cap as needed. Returns
        aggregate stats; every page fetched is persisted to the Raw Store."""
        return self._fetch_window(raw_store, wallet, start_ts, end_ts)

    def _fetch_window(
        self, raw_store: RawStore, wallet: str, start_ts: int, end_ts: int
    ) -> FetchOutcome:
        if start_ts > end_ts:
            return FetchOutcome.empty()

        outcome = FetchOutcome.empty()
        offset = 0
        while True:
            params = {
                "user": wallet,
                "limit": PAGE_LIMIT,
                "offset": offset,
                "start": start_ts,
                "end": end_ts,
                "sortDirection": "DESC",
            }
            response, payload = self._adapter.get_json("/activity", params)

            if _is_offset_cap_error(response, payload):
                return outcome.merge(
                    self._continue_from_cursor(raw_store, wallet, start_ts, end_ts, outcome.min_ts)
                )
            response.raise_for_status()

            rows = payload or []
            raw_result = raw_store.persist(
                source="dataapi",
                endpoint="activity",
                wallet=wallet,
                params=params,
                payload=rows,
                http_status=response.status_code,
            )
            ts_values = [row["timestamp"] for row in rows]
            page_outcome = FetchOutcome(
                raw_fetch_ids=() if raw_result.deduped else (raw_result.raw_fetch_id,),
                rows_fetched=len(rows),
                min_ts=min(ts_values) if ts_values else None,
                max_ts=max(ts_values) if ts_values else None,
                requests_made=1,
            )
            outcome = outcome.merge(page_outcome)

            if len(rows) < PAGE_LIMIT:
                return outcome  # last page of this window

            offset += PAGE_LIMIT
            if offset > MAX_OFFSET:
                # Reached the cap without exhausting the window: there may be
                # more rows than we can reach by paging further.
                return outcome.merge(
                    self._continue_from_cursor(raw_store, wallet, start_ts, end_ts, outcome.min_ts)
                )

    def _continue_from_cursor(
        self,
        raw_store: RawStore,
        wallet: str,
        start_ts: int,
        end_ts: int,
        cursor_ts: Optional[int],
    ) -> FetchOutcome:
        """Called when the offset cap was hit while paging [start_ts, end_ts].
        `cursor_ts` is the oldest timestamp seen so far in that window
        (min_ts across all pages fetched before the cap). Anything strictly
        newer than cursor_ts is fully covered; continue with [start_ts,
        cursor_ts] to pick up whatever else shares that boundary second."""
        if cursor_ts is None or cursor_ts >= end_ts:
            # No progress was made narrowing the window (either nothing was
            # fetched before the cap, or every row so far shares end_ts) —
            # recursing would repeat the same window forever.
            logger.warning(
                "Wallet %s: activity offset cap reached without being able to "
                "narrow the time window further (window=[%d, %d], cursor=%s); "
                "truncating — some events may be missing from the raw store.",
                wallet,
                start_ts,
                end_ts,
                cursor_ts,
            )
            return FetchOutcome.empty()
        return self._fetch_window(raw_store, wallet, start_ts, cursor_ts)
