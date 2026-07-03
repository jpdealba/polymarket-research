# 0001 — Multi-source canonical data: Data-API activity feed + on-chain enrichment

Date: 2026-07-03
Status: Accepted

## Context

The platform analyzes the trading behavior of external Polymarket wallets. Candidate data sources were verified empirically (2026-07-03):

- **Data-API `/activity`**: fresh (seconds), complete per wallet via `start`/`end` time-windowing (the `offset ≤ 3000` cap makes plain pagination insufficient; `/trades` ignores time filters entirely). No maker/taker flag, no order ids, no counterparty.
- **Goldsky orderbook subgraph** (`orderFilledEvents`): explicit per-fill maker, taker, orderHash, fee, amounts, filterable by wallet — but observed lagging weeks behind chain head, and it is an unversioned public endpoint that could degrade.
- **On-chain Polygon `OrderFilled` events**: same content as the subgraph, ground truth, real-time — but requires an RPC provider key and asset-id decoding.
- **Gamma API**: authoritative market metadata and resolutions, joinable by conditionId.
- **CLOB REST**: current orderbook snapshot only; ~1-min price history. Historical depth/spread is unobtainable retroactively.
- **The copy bot's logs**: filtered, gappy, bot-centric; contain nothing about external wallets that public sources lack.

No single source has freshness + maker/taker truth + market metadata.

## Decision

- Canonical fill feed: **Data-API `/activity`, time-windowed per wallet**.
- Maker/taker, orderHash, fee: **enrichment layer** from the Goldsky subgraph, with direct on-chain `eth_getLogs` as fallback/verification. Enrichment is expected to lag and never mutates the canonical feed's existence of fills.
- Market dimension: **Gamma API**, joined on conditionId.
- Bot logs: secondary import for cross-validation only — not a foundation.

## Consequences

- Every fill row carries `transactionHash` + wallet + asset as the join key for enrichment; maker/taker fields are nullable by design.
- Recent fills are analyzable immediately; maker-dependent analytics (maker-share, rebate detection) trail by the subgraph lag unless the RPC path is used.
- If Goldsky is deprecated, only the enrichment adapter changes.
- Historical orderbook microstructure is permanently unavailable; a live book collector must be started early if book-dependent research is wanted later.
