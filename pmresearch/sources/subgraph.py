"""Goldsky Polymarket orderbook-subgraph adapter (Phase 11).

Queries `orderFilledEvents` for a wallet, paginated by a **(timestamp, id)
cursor** — NOT a numeric offset. Goldsky/The-Graph throttle deep `skip`/offset
paging, so we order by timestamp ascending and advance the cursor to the last
(timestamp, id) seen, re-fetching only the boundary timestamp and de-duplicating
by id (the same bounded-overlap trick the Data API adapter uses on its
one-second window boundary).

The wallet can be either the `maker` or the `taker` of a fill, so we run TWO
separate paginated queries (one per role) and merge/dedupe by id. A single
top-level `or:[{maker},{taker}]` filter is rejected/timed-out server-side by
this subgraph (the `or` across two indexed columns is too expensive, and mixing
`timestamp_gte` with `or` at the same level is a hard error), whereas each
single-column `where:{role, timestamp_gte}` query is fast and index-backed.

Every page (a GraphQL POST) is persisted to the Raw Store BEFORE it is parsed
(source="subgraph", endpoint="orderFilledEvents").

On-chain amounts arrive as 6-decimal integer strings. `to_shares()` converts
them to Decimal shares so they can be compared to ledger `delta_shares`.

Address-space note: the subgraph's `maker`/`taker` are the on-chain order
signer/filler addresses on the exchange contract. RN1's known maker fills are
attributed to its trading address here; enrichment lower-cases and matches on
that address (verify against a known fill when live data is available).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

import httpx

from ..rawstore.store import RawStore
from .base import RetryConfig, SourceAdapter

logger = logging.getLogger(__name__)

# The 6-decimal fixed-point scale Polymarket uses for on-chain amounts.
AMOUNT_SCALE = Decimal(10) ** 6

DEFAULT_PAGE_SIZE = 500

# The wallet's two possible roles on a fill; each is queried separately.
_ROLES = ("maker", "taker")


class SubgraphError(RuntimeError):
    """A GraphQL response carried an `errors` payload (never silently zeroed)."""


def _order_filled_query(role: str) -> str:
    """Per-role query. `role` is a trusted internal constant ('maker'/'taker'),
    never user input — interpolated because GraphQL field names can't be
    variables."""
    return (
        "query OrderFills($first: Int!, $wallet: Bytes!, $sinceTs: BigInt!) {"
        "  orderFilledEvents("
        "    first: $first"
        "    orderBy: timestamp"
        "    orderDirection: asc"
        f"    where: {{ {role}: $wallet, timestamp_gte: $sinceTs }}"
        "  ) {"
        "    id transactionHash timestamp orderHash maker taker"
        "    makerAssetId takerAssetId makerAmountFilled takerAmountFilled fee"
        "  }"
        "}"
    )


def to_shares(amount_6dec: str | int) -> Decimal:
    """Convert a 6-decimal integer amount (string or int) to Decimal shares."""
    return Decimal(int(amount_6dec)) / AMOUNT_SCALE


def resolve_traded(
    maker_asset_id: int, taker_asset_id: int, maker_amount: int, taker_amount: int
) -> tuple[str, int]:
    """Verified convention: `makerAssetId == 0` ⇒ the maker paid USDC
    (collateral asset id 0), so the traded outcome token is the takerAssetId
    and the outcome-token quantity is takerAmountFilled. Otherwise the maker
    paid shares, so the traded token is makerAssetId with makerAmountFilled.

    Returns (traded_token_id_as_decimal_string, traded_raw_amount_6dec)."""
    if maker_asset_id == 0:
        return str(taker_asset_id), taker_amount
    return str(maker_asset_id), maker_amount


def resolve_collateral_amount(
    maker_asset_id: int, taker_asset_id: int, maker_amount: int, taker_amount: int
) -> int:
    """Return the raw 6-decimal USDC side of the fill."""
    if maker_asset_id == 0:
        return maker_amount
    if taker_asset_id == 0:
        return taker_amount
    return 0


@dataclass(frozen=True)
class OrderFill:
    order_hash: str
    maker: str
    taker: str
    maker_asset_id: int
    taker_asset_id: int
    maker_amount_filled: int  # raw 6-decimal integer
    taker_amount_filled: int  # raw 6-decimal integer
    fee: int  # raw 6-decimal integer
    timestamp: int
    transaction_hash: str
    subgraph_id: str = ""

    @property
    def traded_token_id(self) -> str:
        token_id, _ = resolve_traded(
            self.maker_asset_id,
            self.taker_asset_id,
            self.maker_amount_filled,
            self.taker_amount_filled,
        )
        return token_id

    @property
    def traded_shares(self) -> Decimal:
        _, raw = resolve_traded(
            self.maker_asset_id,
            self.taker_asset_id,
            self.maker_amount_filled,
            self.taker_amount_filled,
        )
        return to_shares(raw)

    @property
    def fee_decimal(self) -> Decimal:
        return to_shares(self.fee)

    @property
    def collateral_usdc(self) -> Decimal:
        raw = resolve_collateral_amount(
            self.maker_asset_id,
            self.taker_asset_id,
            self.maker_amount_filled,
            self.taker_amount_filled,
        )
        return to_shares(raw)


@dataclass(frozen=True)
class SubgraphFetch:
    fills: tuple[OrderFill, ...]
    head_ts: int  # max fill timestamp seen — how far subgraph data reaches
    requests_made: int


def _parse_fill(row: dict) -> OrderFill:
    return OrderFill(
        order_hash=str(row["orderHash"]),
        maker=str(row["maker"]).lower(),
        taker=str(row["taker"]).lower(),
        maker_asset_id=int(row["makerAssetId"]),
        taker_asset_id=int(row["takerAssetId"]),
        maker_amount_filled=int(row["makerAmountFilled"]),
        taker_amount_filled=int(row["takerAmountFilled"]),
        fee=int(row.get("fee", 0)),
        timestamp=int(row["timestamp"]),
        transaction_hash=str(row["transactionHash"]).lower(),
        subgraph_id=str(row.get("id", "")),
    )


class SubgraphSource:
    def __init__(
        self,
        url: str,
        *,
        client: httpx.Client | None = None,
        retry: RetryConfig | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        if not url and client is None:
            raise ValueError("SubgraphSource requires a url (PMR_SUBGRAPH_URL) or a client.")
        kwargs = {}
        if sleep_fn is not None:
            kwargs["sleep_fn"] = sleep_fn
        # POST to the ABSOLUTE endpoint URL, not a relative "" path: httpx joins
        # base_url + "" into ".../gn/" (trailing slash), and that route does NOT
        # reach the GraphQL resolver — it returns {"message": ...} with no data,
        # which would look like "zero fills". An absolute request URL bypasses
        # base_url joining and hits the endpoint exactly.
        self._url = url
        self._adapter = SourceAdapter(url, client=client, retry=retry, **kwargs)
        self.page_size = page_size

    def close(self) -> None:
        self._adapter.close()

    def __enter__(self) -> "SubgraphSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_order_fills(
        self, raw_store: RawStore, wallet: str, *, since_ts: int = 0
    ) -> SubgraphFetch:
        """Fetch every orderFilledEvent for `wallet` at or after `since_ts`,
        as maker and as taker (two paginated queries), merged and deduped by id.
        Each page is raw-stored before parsing."""
        wallet = wallet.lower()
        fills: list[OrderFill] = []
        seen_ids: set[str] = set()
        requests_made = 0
        head_ts = 0

        for role in _ROLES:
            requests, role_head = self._fetch_role(
                raw_store, wallet, role, since_ts, fills, seen_ids
            )
            requests_made += requests
            head_ts = max(head_ts, role_head)

        return SubgraphFetch(tuple(fills), head_ts, requests_made)

    def _fetch_role(
        self,
        raw_store: RawStore,
        wallet: str,
        role: str,
        since_ts: int,
        fills: list["OrderFill"],
        seen_ids: set[str],
    ) -> tuple[int, int]:
        """Paginate one role's query, appending new fills into `fills`/`seen_ids`.
        Returns (requests_made, head_ts) for this role."""
        query = _order_filled_query(role)
        cursor_ts = since_ts
        requests_made = 0
        head_ts = 0

        while True:
            variables = {"first": self.page_size, "wallet": wallet, "sinceTs": cursor_ts}
            body = {"query": query, "variables": variables}
            response, payload = self._adapter.post_json(self._url, body)
            response.raise_for_status()
            raw_store.persist(
                source="subgraph",
                endpoint="orderFilledEvents",
                wallet=wallet,
                params={**variables, "role": role},
                payload=payload if payload is not None else {},
                http_status=response.status_code,
            )
            requests_made += 1

            rows = _rows(payload)  # raises SubgraphError on a GraphQL error payload
            new_rows = [row for row in rows if str(row.get("id", "")) not in seen_ids]
            for row in new_rows:
                fill = _parse_fill(row)
                fills.append(fill)
                seen_ids.add(fill.subgraph_id)
                head_ts = max(head_ts, fill.timestamp)

            if len(rows) < self.page_size:
                break  # last page

            page_max_ts = max(int(row["timestamp"]) for row in rows)
            if page_max_ts <= cursor_ts and not new_rows:
                logger.warning(
                    "Subgraph %s pagination for %s stalled at ts=%d (page full but "
                    "no forward progress); stopping to avoid an infinite loop.",
                    role,
                    wallet,
                    cursor_ts,
                )
                break
            cursor_ts = page_max_ts

        return requests_made, head_ts


def _rows(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    # A GraphQL error (bad filter, subgraph timeout, schema drift) comes back as
    # HTTP 200 with an `errors` array and a null `data`. Never silently treat
    # that as "zero fills" (ADR 0006) — raise so the caller/operator sees it.
    errors = payload.get("errors")
    if errors:
        raise SubgraphError(f"Subgraph returned GraphQL errors: {errors}")
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("orderFilledEvents")
    return rows if isinstance(rows, list) else []
