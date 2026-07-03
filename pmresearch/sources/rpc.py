"""Optional JSON-RPC adapter for OrderFilled logs (Phase 11, OFF by default).

Gated entirely behind `settings.rpc_url`: when empty this module is never
called and subgraph-only enrichment works. Used to close the recent gap the
subgraph lags behind, by reading `OrderFilled` logs via `eth_getLogs` on both
Polymarket exchange contracts.

Event (from the CTFExchange / NegRiskCTFExchange ABI):

    OrderFilled(
        bytes32 orderHash,
        address indexed maker,
        address indexed taker,
        uint256 makerAssetId,
        uint256 takerAssetId,
        uint256 makerAmountFilled,
        uint256 takerAmountFilled,
        uint256 fee
    )

`maker` and `taker` are indexed (topics[1], topics[2]); the remaining six
words are ABI-encoded in `data` in declaration order. Same
`makerAssetId == 0 ⇒ maker paid USDC` convention as the subgraph adapter.

Contract addresses on Polygon (documented constants):
  CTFExchange:        0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
  NegRiskCTFExchange: 0xC5d563A36AE78145C45a50134d48A1215220f80a
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

import httpx

from ..rawstore.store import RawStore
from .base import RetryConfig, SourceAdapter
from .subgraph import resolve_traded, to_shares

logger = logging.getLogger(__name__)

CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E".lower()
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a".lower()
EXCHANGE_ADDRESSES = (CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE)

ORDER_FILLED_SIGNATURE = (
    "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
)


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
    for rnd in range(24):
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
        state[0][0] ^= _KECCAK_RC[rnd]


def keccak256(data: bytes) -> bytes:
    rate = 136  # 1088-bit rate for keccak-256
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
    for i in range(4):  # 32 bytes
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out[:32])


ORDER_FILLED_TOPIC0 = "0x" + keccak256(ORDER_FILLED_SIGNATURE.encode()).hex()


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

    # Uniform interface with subgraph OrderFill (no source-side timestamp).
    timestamp = 0


def _addr_from_topic(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def decode_order_filled(log: dict) -> OrderFilledLog:
    """Pure decoder: topics + ABI-encoded data words → OrderFilledLog.

    topics[0] must be the OrderFilled signature hash; topics[1]/[2] are the
    indexed maker/taker addresses; `data` holds six 32-byte words:
    orderHash, makerAssetId, takerAssetId, makerAmountFilled,
    takerAmountFilled, fee."""
    topics = log["topics"]
    if topics[0].lower() != ORDER_FILLED_TOPIC0:
        raise ValueError(
            f"log topic0 {topics[0]} is not OrderFilled ({ORDER_FILLED_TOPIC0})"
        )
    data = log["data"]
    if data.startswith("0x"):
        data = data[2:]
    words = [data[i:i + 64] for i in range(0, len(data), 64)]
    if len(words) < 6:
        raise ValueError(f"OrderFilled data has {len(words)} words, expected 6")
    return OrderFilledLog(
        order_hash="0x" + words[0],
        maker=_addr_from_topic(topics[1]),
        taker=_addr_from_topic(topics[2]),
        maker_asset_id=int(words[1], 16),
        taker_asset_id=int(words[2], 16),
        maker_amount_filled=int(words[3], 16),
        taker_amount_filled=int(words[4], 16),
        fee=int(words[5], 16),
        transaction_hash=str(log.get("transactionHash", "")).lower(),
        block_number=int(str(log.get("blockNumber", "0x0")), 16),
    )


@dataclass(frozen=True)
class RpcFetch:
    logs: tuple[OrderFilledLog, ...]
    head_block: int
    requests_made: int


class RpcSource:
    """Thin JSON-RPC client for eth_getLogs. Only constructed when rpc_url is
    configured (see cli/enrich.py and enrichment.run_enrichment)."""

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
        self._adapter = SourceAdapter(url, client=client, retry=retry, **kwargs)

    def close(self) -> None:
        self._adapter.close()

    def __enter__(self) -> "RpcSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_order_filled_logs(
        self, raw_store: RawStore, *, from_block: int, to_block: int
    ) -> RpcFetch:
        """eth_getLogs OrderFilled on both exchange contracts over a block
        range. Raw-stores each JSON-RPC response before decoding."""
        logs: list[OrderFilledLog] = []
        head_block = from_block
        requests_made = 0
        for address in EXCHANGE_ADDRESSES:
            params = [{
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": address,
                "topics": [ORDER_FILLED_TOPIC0],
            }]
            body = {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": params}
            response, payload = self._adapter.post_json("", body)
            response.raise_for_status()
            raw_store.persist(
                source="rpc",
                endpoint="eth_getLogs",
                wallet=address,
                params={"fromBlock": from_block, "toBlock": to_block, "address": address},
                payload=payload if payload is not None else {},
                http_status=response.status_code,
            )
            requests_made += 1
            result = payload.get("result", []) if isinstance(payload, dict) else []
            for raw_log in result:
                decoded = decode_order_filled(raw_log)
                logs.append(decoded)
                head_block = max(head_block, decoded.block_number)
        return RpcFetch(tuple(logs), head_block, requests_made)
