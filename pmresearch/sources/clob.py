"""CLOB orderbook source adapter.

Fetches the orderbook for a given token from the Polymarket CLOB REST API
(`/book`). Every response is persisted to the Raw Store before parsing.

The CLOB base URL is https://clob.polymarket.com.  No authentication is
required for read-only /book calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..rawstore.store import RawFetchResult, RawStore
from .base import SourceAdapter

logger = logging.getLogger(__name__)

DEFAULT_CLOB_BASE_URL = "https://clob.polymarket.com"

_ZERO = Decimal("0")


@dataclass(frozen=True)
class BookSnapshot:
    """Parsed orderbook summary for one token at one point in time."""
    token_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread: Decimal | None
    mid: Decimal | None
    depth_top: dict[str, list[dict[str, str]]] | None
    raw_fetch: RawFetchResult

    @property
    def has_book(self) -> bool:
        return self.best_bid is not None or self.best_ask is not None


def _safe_decimal(value: str | float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d


def _parse_book(token_id: str, payload: dict[str, Any], raw_fetch: RawFetchResult) -> BookSnapshot:
    """Parse a CLOB /book response into a BookSnapshot.

    The CLOB response format is:
        {
          "market": "...",
          "asset_id": "...",
          "bids": [{"price": "0.50", "size": "100.0"}, ...],
          "asks": [{"price": "0.52", "size": "50.0"}, ...],
          "hash": "...",
          "timestamp": "..."
        }

    Bids are sorted descending by price; asks ascending.  We only keep the
    top-10 levels for each side.
    """
    raw_bids = payload.get("bids") or []
    raw_asks = payload.get("asks") or []

    bids_sorted = sorted(raw_bids, key=lambda x: Decimal(str(x.get("price", "0"))), reverse=True)
    asks_sorted = sorted(raw_asks, key=lambda x: Decimal(str(x.get("price", "0"))))

    best_bid = _safe_decimal(bids_sorted[0]["price"]) if bids_sorted else None
    best_ask = _safe_decimal(asks_sorted[0]["price"]) if asks_sorted else None

    spread = None
    mid = None
    if best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2

    depth_top = None
    if bids_sorted or asks_sorted:
        depth_top = {
            "bids": [{"price": str(b.get("price", "")), "size": str(b.get("size", ""))} for b in bids_sorted[:10]],
            "asks": [{"price": str(a.get("price", "")), "size": str(a.get("size", ""))} for a in asks_sorted[:10]],
        }

    return BookSnapshot(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        mid=mid,
        depth_top=depth_top,
        raw_fetch=raw_fetch,
    )


class ClobSource:
    """Thin wrapper around the Polymarket CLOB REST API for /book calls."""

    def __init__(
        self,
        base_url: str = DEFAULT_CLOB_BASE_URL,
        *,
        adapter: SourceAdapter | None = None,
    ) -> None:
        self._adapter = adapter or SourceAdapter(base_url)

    def close(self) -> None:
        self._adapter.close()

    def __enter__(self) -> "ClobSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_book(
        self,
        raw_store: RawStore,
        token_id: str,
    ) -> BookSnapshot:
        """Fetch the orderbook for a single token.

        The response is persisted to the Raw Store (source="clob", endpoint="book")
        before parsing.  Deduplication is handled by the Raw Store — identical
        book content for the same token within the same second is a near-no-op.
        """
        params = {"token_id": token_id}
        response, payload = self._adapter.get_json("/book", params)

        raw_fetch = raw_store.persist(
            source="clob",
            endpoint="book",
            wallet=token_id,  # token_id as the "wallet" partition key
            params=params,
            payload=payload if payload is not None else {},
            http_status=response.status_code,
        )

        if payload is None:
            # Empty response body or error — return an empty snapshot
            return BookSnapshot(
                token_id=token_id,
                best_bid=None,
                best_ask=None,
                spread=None,
                mid=None,
                depth_top=None,
                raw_fetch=raw_fetch,
            )

        return _parse_book(token_id, payload, raw_fetch)

    def fetch_book_batch(
        self,
        raw_store: RawStore,
        token_ids: list[str],
        *,
        per_token_delay_s: float = 0.0,
        max_concurrency: int = 20,
    ) -> list[BookSnapshot]:
        """Fetch books for multiple tokens, HTTP requests in flight concurrently.

        The GET calls (thread-safe on the shared httpx.Client) run in a bounded
        thread pool; every Raw Store write happens back on the caller's thread
        afterward, in the original token order, since RawStore's SQLAlchemy
        session is not thread-safe. Errors on individual tokens are logged and
        produce an empty snapshot (no crash, no abort).
        """
        from concurrent.futures import ThreadPoolExecutor

        def _get(token_id: str):
            try:
                return token_id, self._adapter.get_json("/book", {"token_id": token_id}), None
            except Exception as exc:  # noqa: BLE001 - reported per-token below
                return token_id, None, exc

        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            fetched = dict(
                (token_id, (response_payload, exc))
                for token_id, response_payload, exc in executor.map(_get, token_ids)
            )

        results: list[BookSnapshot] = []
        for token_id in token_ids:
            response_payload, exc = fetched[token_id]
            if exc is not None:
                logger.exception("Failed to fetch book for token %s", token_id, exc_info=exc)
                results.append(BookSnapshot(
                    token_id=token_id,
                    best_bid=None,
                    best_ask=None,
                    spread=None,
                    mid=None,
                    depth_top=None,
                    raw_fetch=RawFetchResult(
                        raw_fetch_id=0, file_path=raw_store.settings.raw_dir,
                        content_hash="", row_count=0, deduped=False,
                    ),
                ))
                continue

            response, payload = response_payload
            raw_fetch = raw_store.persist(
                source="clob",
                endpoint="book",
                wallet=token_id,
                params={"token_id": token_id},
                payload=payload if payload is not None else {},
                http_status=response.status_code,
            )
            if payload is None:
                results.append(BookSnapshot(
                    token_id=token_id,
                    best_bid=None,
                    best_ask=None,
                    spread=None,
                    mid=None,
                    depth_top=None,
                    raw_fetch=raw_fetch,
                ))
            else:
                results.append(_parse_book(token_id, payload, raw_fetch))
        return results
