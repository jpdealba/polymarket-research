"""Etherscan V2 (PolygonScan) `logs` API adapter.

Unlike JSON-RPC `eth_getLogs`, the Etherscan `logs` module paginates by results
(up to 1000 per page), so it avoids the tiny block-range caps many free RPC
tiers impose. A free API key allows 5 req/s and 100k calls/day, enough to
backfill the subgraph-lag window in a few minutes for the current MVP scale.

It returns the same `OrderFilledLog` shape as the RPC adapter, so
`decode_order_filled` and the enrichment join are reused verbatim.
"""

from __future__ import annotations

import time
from typing import Callable

import httpx

from ..rawstore.store import RawStore
from .base import RetryConfig, SourceAdapter
from .rpc import (
    EXCHANGE_CONTRACTS,
    OrderFilledLog,
    RpcError,
    RpcFetch,
    _wallet_topic,
    decode_order_filled,
)

ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN_ID = 137
PAGE_SIZE = 1000

_ROLE_TOPIC = {
    "maker": ("topic2", "topic0_2_opr"),
    "taker": ("topic3", "topic0_3_opr"),
}


def _result_rows(payload: object) -> list[dict]:
    """Return the Etherscan result list, treating "No records found" as empty.

    Bad keys, malformed queries, and other NOTOK responses raise. This keeps
    source failures from looking like genuine zero-fill windows.
    """
    if not isinstance(payload, dict):
        raise RpcError(f"PolygonScan returned a non-object payload: {payload!r}")
    result = payload.get("result")
    if str(payload.get("status")) == "1" and isinstance(result, list):
        return result
    if "no records" in f"{payload.get('message')} {result}".lower():
        return []
    raise RpcError(
        f"PolygonScan error: message={payload.get('message')!r} result={result!r}"
    )


class PolygonscanSource:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = ETHERSCAN_V2_URL,
        chain_id: int = POLYGON_CHAIN_ID,
        client: httpx.Client | None = None,
        retry: RetryConfig | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        page_size: int = PAGE_SIZE,
    ) -> None:
        if not api_key and client is None:
            raise ValueError(
                "PolygonscanSource requires an API key (PMR_POLYGONSCAN_API_KEY) or a client."
            )
        kwargs = {}
        if sleep_fn is not None:
            kwargs["sleep_fn"] = sleep_fn
        self._url = base_url
        self._key = api_key
        self._chain = chain_id
        self._sleep = sleep_fn or time.sleep
        self._adapter = SourceAdapter(base_url, client=client, retry=retry, **kwargs)
        self.page_size = page_size

    def close(self) -> None:
        self._adapter.close()

    def __enter__(self) -> "PolygonscanSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, params: dict) -> dict:
        """GET the Etherscan endpoint, retrying body-level rate-limit replies."""
        query = {"chainid": self._chain, "apikey": self._key, **params}
        payload = None
        for attempt in range(6):
            response, payload = self._adapter.get_json(self._url, query)
            response.raise_for_status()
            rate_limited = (
                isinstance(payload, dict)
                and "rate limit" in f"{payload.get('message')} {payload.get('result')}".lower()
            )
            if rate_limited:
                self._sleep(1.0 + attempt)
                continue
            if not isinstance(payload, dict):
                raise RpcError(f"PolygonScan returned a non-object payload: {payload!r}")
            return payload
        raise RpcError(f"PolygonScan rate limit did not clear: {payload!r}")

    def get_block_number(self) -> int:
        payload = self._get({"module": "proxy", "action": "eth_blockNumber"})
        return int(payload["result"], 16)

    def find_block_by_timestamp(self, ts: int, **_: object) -> int:
        payload = self._get(
            {
                "module": "block",
                "action": "getblocknobytime",
                "timestamp": ts,
                "closest": "after",
            }
        )
        return int(payload["result"])

    def fetch_order_filled_logs(
        self, raw_store: RawStore, *, wallet: str, from_block: int, to_block: int
    ) -> RpcFetch:
        """Fetch wallet-filtered OrderFilled logs in [from_block, to_block]."""
        wallet = wallet.lower()
        wallet_topic = _wallet_topic(wallet)
        logs: list[OrderFilledLog] = []
        seen: set[tuple[str, str]] = set()
        head_block = from_block
        requests_made = 0

        for address, topic0 in EXCHANGE_CONTRACTS:
            for role, (topic_slot, operator) in _ROLE_TOPIC.items():
                page = 1
                while True:
                    params = {
                        "module": "logs",
                        "action": "getLogs",
                        "address": address,
                        "fromBlock": from_block,
                        "toBlock": to_block,
                        "topic0": topic0,
                        topic_slot: wallet_topic,
                        operator: "and",
                        "page": page,
                        "offset": self.page_size,
                    }
                    payload = self._get(params)
                    raw_store.persist(
                        source="polygonscan",
                        endpoint="logs.getLogs",
                        wallet=wallet,
                        params={
                            "address": address,
                            "role": role,
                            "topic0": topic0,
                            "fromBlock": from_block,
                            "toBlock": to_block,
                            "page": page,
                            "offset": self.page_size,
                        },
                        payload=payload,
                        http_status=200,
                    )
                    requests_made += 1
                    rows = _result_rows(payload)

                    for raw_log in rows:
                        key = (
                            str(raw_log.get("transactionHash", "")).lower(),
                            str(raw_log.get("logIndex", "")),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        decoded = decode_order_filled(raw_log)
                        logs.append(decoded)
                        head_block = max(head_block, decoded.block_number)

                    if len(rows) < self.page_size:
                        break
                    page += 1

        return RpcFetch(tuple(logs), head_block, requests_made)
