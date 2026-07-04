"""Optional JSON-RPC adapter for Polymarket OrderFilled logs (Phase 11).

The adapter reads wallet-filtered OrderFilled logs from all known Polymarket
CTF exchange contracts. V1 and V2 use different event signatures, so each
contract is queried with its matching topic0 and decoded into the same
OrderFilledLog shape consumed by the enrichment join.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

import httpx

from ..rawstore.store import RawStore
from .base import RetryConfig, SourceAdapter
from .subgraph import resolve_collateral_amount, resolve_traded, to_shares

CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E".lower()
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a".lower()
CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B".lower()
NEG_RISK_CTF_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59".lower()

EXCHANGE_ADDRESSES = (
    CTF_EXCHANGE,
    NEG_RISK_CTF_EXCHANGE,
    CTF_EXCHANGE_V2,
    NEG_RISK_CTF_EXCHANGE_V2,
)

ORDER_FILLED_V1_SIGNATURE = (
    "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
)
ORDER_FILLED_V2_SIGNATURE = (
    "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)"
)


class RpcError(RuntimeError):
    """A JSON-RPC response carried an error object."""


# --- minimal keccak-256 (Ethereum's hash; NOT hashlib's NIST SHA3) ---------
_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_KECCAK_ROT = [
    [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56], [27, 20, 39, 8, 14],
]
_MASK = (1 << 64) - 1


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK


def _keccak_f(state: list[list[int]]) -> None:
    for _ in range(24):
        c = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(state[x][y], _KECCAK_ROT[x][y])
        for x in range(5):
            for y in range(5):
                state[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) & _MASK
        state[0][0] ^= _KECCAK_RC[_]


def keccak256(data: bytes) -> bytes:
    rate = 136
    state = [[0] * 5 for _ in range(5)]
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)
    out = bytearray()
    for i in range(4):
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out[:32])


ORDER_FILLED_TOPIC0 = "0x" + keccak256(ORDER_FILLED_V1_SIGNATURE.encode()).hex()
ORDER_FILLED_V1_TOPIC0 = ORDER_FILLED_TOPIC0
ORDER_FILLED_V2_TOPIC0 = "0x" + keccak256(ORDER_FILLED_V2_SIGNATURE.encode()).hex()

EXCHANGE_CONTRACTS = (
    (CTF_EXCHANGE, ORDER_FILLED_V1_TOPIC0),
    (NEG_RISK_CTF_EXCHANGE, ORDER_FILLED_V1_TOPIC0),
    (CTF_EXCHANGE_V2, ORDER_FILLED_V2_TOPIC0),
    (NEG_RISK_CTF_EXCHANGE_V2, ORDER_FILLED_V2_TOPIC0),
)


@dataclass(frozen=True)
class OrderFilledLog:
    order_hash: str
    maker: str
    taker: str
    maker_asset_id: int
    taker_asset_id: int
    maker_amount_filled: int
    taker_amount_filled: int
    fee: int
    transaction_hash: str
    block_number: int

    @property
    def traded_token_id(self) -> str:
        token_id, _ = resolve_traded(
            self.maker_asset_id, self.taker_asset_id,
            self.maker_amount_filled, self.taker_amount_filled,
        )
        return token_id

    @property
    def traded_shares(self) -> Decimal:
        _, raw = resolve_traded(
            self.maker_asset_id, self.taker_asset_id,
            self.maker_amount_filled, self.taker_amount_filled,
        )
        return to_shares(raw)

    @property
    def fee_decimal(self) -> Decimal:
        return to_shares(self.fee)

    @property
    def collateral_usdc(self) -> Decimal:
        raw = resolve_collateral_amount(
            self.maker_asset_id, self.taker_asset_id,
            self.maker_amount_filled, self.taker_amount_filled,
        )
        return to_shares(raw)

    timestamp = 0


def _addr_from_topic(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _wallet_topic(wallet: str) -> str:
    hexpart = wallet[2:] if wallet.startswith("0x") else wallet
    return "0x" + hexpart.lower().rjust(64, "0")


def _data_words(log: dict) -> list[str]:
    data = log["data"]
    if data.startswith("0x"):
        data = data[2:]
    return [data[i:i + 64] for i in range(0, len(data), 64)]


def decode_order_filled(log: dict) -> OrderFilledLog:
    topics = log["topics"]
    topic0 = topics[0].lower()
    if topic0 not in (ORDER_FILLED_V1_TOPIC0, ORDER_FILLED_V2_TOPIC0):
        raise ValueError(f"log topic0 {topics[0]} is not a known OrderFilled topic")
    if len(topics) < 4:
        raise ValueError(f"OrderFilled log has {len(topics)} topics, expected 4")

    words = _data_words(log)
    if topic0 == ORDER_FILLED_V1_TOPIC0:
        if len(words) < 5:
            raise ValueError(f"OrderFilled V1 data has {len(words)} words, expected 5")
        maker_asset_id = int(words[0], 16)
        taker_asset_id = int(words[1], 16)
        maker_amount_filled = int(words[2], 16)
        taker_amount_filled = int(words[3], 16)
        fee = int(words[4], 16)
    else:
        if len(words) < 7:
            raise ValueError(f"OrderFilled V2 data has {len(words)} words, expected 7")
        side = int(words[0], 16)
        token_id = int(words[1], 16)
        maker_amount_filled = int(words[2], 16)
        taker_amount_filled = int(words[3], 16)
        fee = int(words[4], 16)
        if side == 0:
            maker_asset_id = 0
            taker_asset_id = token_id
        elif side == 1:
            maker_asset_id = token_id
            taker_asset_id = 0
        else:
            raise ValueError(f"OrderFilled V2 side {side} is not BUY(0) or SELL(1)")

    return OrderFilledLog(
        order_hash=topics[1].lower(),
        maker=_addr_from_topic(topics[2]),
        taker=_addr_from_topic(topics[3]),
        maker_asset_id=maker_asset_id,
        taker_asset_id=taker_asset_id,
        maker_amount_filled=maker_amount_filled,
        taker_amount_filled=taker_amount_filled,
        fee=fee,
        transaction_hash=str(log.get("transactionHash", "")).lower(),
        block_number=int(str(log.get("blockNumber", "0x0")), 16),
    )


@dataclass(frozen=True)
class RpcFetch:
    logs: tuple[OrderFilledLog, ...]
    head_block: int
    requests_made: int


class RpcSource:
    def __init__(
        self,
        url: str,
        *,
        client: httpx.Client | None = None,
        retry: RetryConfig | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        if not url and client is None:
            raise ValueError("RpcSource requires a url (PMR_RPC_URL) or a client.")
        kwargs = {}
        if sleep_fn is not None:
            kwargs["sleep_fn"] = sleep_fn
        self._url = url
        self._adapter = SourceAdapter(url, client=client, retry=retry, **kwargs)

    def close(self) -> None:
        self._adapter.close()

    def __enter__(self) -> "RpcSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _rpc_call(self, method: str, params: list) -> dict:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        response, payload = self._adapter.post_json(self._url, body)
        if isinstance(payload, dict) and payload.get("error"):
            raise RpcError(f"{method} failed: {payload['error']}")
        response.raise_for_status()
        if not isinstance(payload, dict):
            raise RpcError(f"{method} returned a non-object payload: {payload!r}")
        return payload

    def get_block_number(self) -> int:
        return int(self._rpc_call("eth_blockNumber", [])["result"], 16)

    def _block_timestamp(self, block_number: int) -> int:
        payload = self._rpc_call("eth_getBlockByNumber", [hex(block_number), False])
        return int(payload["result"]["timestamp"], 16)

    def find_block_by_timestamp(self, ts: int, *, hi: int | None = None) -> int:
        lo = 0
        hi = self.get_block_number() if hi is None else hi
        while lo < hi:
            mid = (lo + hi) // 2
            if self._block_timestamp(mid) < ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def fetch_order_filled_logs(
        self, raw_store: RawStore, *, wallet: str, from_block: int, to_block: int
    ) -> RpcFetch:
        wallet_topic = _wallet_topic(wallet)
        logs: list[OrderFilledLog] = []
        head_block = from_block
        requests_made = 0
        seen: set[tuple[str, str]] = set()

        for role, topic_index in (("maker", 2), ("taker", 3)):
            for topic0 in (ORDER_FILLED_V1_TOPIC0, ORDER_FILLED_V2_TOPIC0):
                addresses = [
                    address for address, contract_topic0 in EXCHANGE_CONTRACTS
                    if contract_topic0 == topic0
                ]
                topics: list[str | None] = [topic0, None, None, None]
                topics[topic_index] = wallet_topic
                params = [{
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "address": addresses,
                    "topics": topics,
                }]
                body = {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": params}
                response, payload = self._adapter.post_json(self._url, body)
                raw_store.persist(
                    source="rpc",
                    endpoint="eth_getLogs",
                    wallet=wallet,
                    params={
                        "fromBlock": from_block,
                        "toBlock": to_block,
                        "role": role,
                        "topic0": topic0,
                    },
                    payload=payload if payload is not None else {},
                    http_status=response.status_code,
                )
                requests_made += 1
                err = payload.get("error") if isinstance(payload, dict) else None
                if err is not None:
                    raise RpcError(f"eth_getLogs [{from_block},{to_block}] {role} failed: {err}")
                if response.status_code == 400:
                    raise RpcError(
                        f"eth_getLogs [{from_block},{to_block}] {role} HTTP 400: {response.text[:300]}"
                    )
                response.raise_for_status()
                result = payload.get("result", []) if isinstance(payload, dict) else []
                for raw_log in result:
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
        return RpcFetch(tuple(logs), head_block, requests_made)
