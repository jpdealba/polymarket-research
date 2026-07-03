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
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

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
POSITIONS_MAX_OFFSET = 10000


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


@dataclass(frozen=True)
class PositionRow:
    token_id: str
    size: Decimal
    avg_price: Decimal
    cur_price: Decimal
    current_value: Decimal
    condition_id: Optional[str]
    title: Optional[str]
    outcome: Optional[str]
    raw: dict
    realized_pnl: Decimal = Decimal(0)


@dataclass(frozen=True)
class PositionsFetchOutcome:
    raw_fetch_ids: tuple[int, ...]
    positions: tuple[PositionRow, ...]
    requests_made: int
    complete: bool = True

    @property
    def rows_fetched(self) -> int:
        return len(self.positions)


class PositionsFetchIncomplete(RuntimeError):
    """Raised when `/positions` pagination reaches the documented offset cap."""


@dataclass(frozen=True)
class ValueFetchOutcome:
    raw_fetch_ids: tuple[int, ...]
    value: Decimal
    requests_made: int
    raw: object


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _parse_position(row: object) -> PositionRow:
    if not isinstance(row, dict):
        raise ValueError(f"Unexpected /positions row type: {type(row).__name__}")
    asset = row.get("asset")
    if not asset:
        raise ValueError("/positions row missing required 'asset' token id")
    condition_id = row.get("conditionId")
    return PositionRow(
        token_id=str(asset),
        size=_decimal(row.get("size")),
        avg_price=_decimal(row.get("avgPrice")),
        cur_price=_decimal(row.get("curPrice")),
        current_value=_decimal(row.get("currentValue")),
        condition_id=str(condition_id).lower() if condition_id else None,
        title=str(row["title"]) if row.get("title") is not None else None,
        outcome=str(row["outcome"]) if row.get("outcome") is not None else None,
        raw=row,
        realized_pnl=_decimal(row.get("realizedPnl")),
    )


def _parse_value(payload: object) -> Decimal:
    if isinstance(payload, dict):
        for key in ("value", "totalValue", "portfolioValue", "currentValue"):
            if key in payload:
                return _decimal(payload.get(key))
    if isinstance(payload, list) and payload:
        return sum((_parse_value(item) for item in payload), Decimal(0))
    return _decimal(payload)


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
        self,
        raw_store: RawStore,
        wallet: str,
        start_ts: int,
        end_ts: int,
        *,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> FetchOutcome:
        """Fetch every `/activity` row for `wallet` within [start_ts, end_ts]
        (both inclusive), narrowing the window on the offset cap as needed.
        Returns aggregate stats; every page fetched is persisted to the Raw
        Store.

        Implemented as an outer loop, not recursion: a wallet's full history
        can require narrowing the window hundreds of times (observed against
        a very high-frequency real wallet — recursion blew Python's stack
        after ~60 narrowings on ~30 min of continuous activity), so the
        number of narrowing steps must not grow the call stack.

        If given, `on_progress(cursor_ts)` is called after each narrowing
        with "everything from cursor_ts to end_ts is now covered" — callers
        doing a long-running backfill use this to checkpoint resumable
        progress (see pmresearch.walletmanager.sync.run_backfill), so an
        interrupted run doesn't have to re-walk history it already covered.
        """
        if start_ts > end_ts:
            return FetchOutcome.empty()

        total = FetchOutcome.empty()
        window_end = end_ts

        while True:
            window_outcome, capped = self._page_window(raw_store, wallet, start_ts, window_end)
            total = total.merge(window_outcome)

            if not capped:
                if on_progress is not None:
                    on_progress(start_ts)
                return total  # [start_ts, window_end] fully paged — and by
                # induction everything newer than window_end was already
                # covered by prior iterations, so [start_ts, end_ts] is done.

            cursor_ts = window_outcome.min_ts
            if cursor_ts is None or cursor_ts >= window_end:
                # No progress narrowing the window (either nothing was
                # fetched before the cap, or every row so far shares
                # window_end) — looping again would repeat forever.
                logger.warning(
                    "Wallet %s: activity offset cap reached without being able "
                    "to narrow the time window further (window=[%d, %d], "
                    "cursor=%s); truncating — some events may be missing from "
                    "the raw store.",
                    wallet,
                    start_ts,
                    window_end,
                    cursor_ts,
                )
                return total

            # Anything strictly newer than cursor_ts is fully covered;
            # continue with a window ending at cursor_ts (inclusive, so any
            # other same-second rows we might not have reached yet are
            # re-fetched — the only bounded overlap this algorithm produces).
            window_end = cursor_ts
            if on_progress is not None:
                on_progress(window_end)

    def _page_window(
        self, raw_store: RawStore, wallet: str, start_ts: int, end_ts: int
    ) -> tuple[FetchOutcome, bool]:
        """Page [start_ts, end_ts] until exhausted or the offset cap is hit.
        Returns (outcome, capped) — capped=True means the caller should
        narrow the window and call this again."""
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
                return outcome, True
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
                return outcome, False  # last page of this window

            offset += PAGE_LIMIT
            if offset > MAX_OFFSET:
                # Reached the cap without exhausting the window: there may be
                # more rows than we can reach by paging further.
                return outcome, True

    def fetch_positions(
        self,
        raw_store: RawStore,
        wallet: str,
        *,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> PositionsFetchOutcome:
        """Fetch all current `/positions` rows for `wallet`.

        The Data API's default sizeThreshold is 1 share, which would hide dust
        and make reconciliation asymmetric, so this always requests
        sizeThreshold=0. Every page is raw-stored before any parsed row is
        returned. If the documented offset cap is reached while pages are still
        full, the result would be partial, so fail instead of comparing it.
        """
        wallet = wallet.lower()
        raw_fetch_ids: list[int] = []
        positions: list[PositionRow] = []
        requests_made = 0
        offset = 0

        while True:
            params = {
                "user": wallet,
                "sizeThreshold": 0,
                "limit": PAGE_LIMIT,
                "offset": offset,
            }
            response, payload = self._adapter.get_json("/positions", params)
            response.raise_for_status()
            if payload is None:
                rows = []
            elif isinstance(payload, list):
                rows = payload
            else:
                raise ValueError(
                    f"Unexpected /positions payload type: {type(payload).__name__}"
                )

            raw_result = raw_store.persist(
                source="dataapi",
                endpoint="positions",
                wallet=wallet,
                params=params,
                payload=rows,
                http_status=response.status_code,
            )
            if not raw_result.deduped:
                raw_fetch_ids.append(raw_result.raw_fetch_id)
            requests_made += 1
            positions.extend(_parse_position(row) for row in rows)
            if on_progress is not None:
                on_progress(offset + len(rows), len(positions))

            if len(rows) < PAGE_LIMIT:
                return PositionsFetchOutcome(
                    raw_fetch_ids=tuple(raw_fetch_ids),
                    positions=tuple(positions),
                    requests_made=requests_made,
                    complete=True,
                )

            offset += PAGE_LIMIT
            if offset > POSITIONS_MAX_OFFSET:
                raise PositionsFetchIncomplete(
                    "Data API /positions reached offset cap with a full page; "
                    "refusing to reconcile partial oracle data."
                )

    def fetch_value(self, raw_store: RawStore, wallet: str) -> ValueFetchOutcome:
        """Fetch the live Data API `/value` portfolio value for `wallet`."""
        wallet = wallet.lower()
        params = {"user": wallet}
        response, payload = self._adapter.get_json("/value", params)
        response.raise_for_status()
        raw_result = raw_store.persist(
            source="dataapi",
            endpoint="value",
            wallet=wallet,
            params=params,
            payload=payload or {},
            http_status=response.status_code,
        )
        return ValueFetchOutcome(
            raw_fetch_ids=() if raw_result.deduped else (raw_result.raw_fetch_id,),
            value=_parse_value(payload),
            requests_made=1,
            raw=payload,
        )
