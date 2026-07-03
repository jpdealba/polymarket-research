# 0005 — Collect irreplaceable data early, for the relevant slice only

Date: 2026-07-03
Status: Accepted

## Context

Two datasets have permanent loss-if-not-collected characteristics: recent maker/taker/fee data (the free Goldsky subgraph lags weeks behind chain head) and orderbook state (never available retroactively at any layer). A full WSS orderbook collector would dominate MVP effort; ignoring both would permanently blind the platform's most research-relevant window — recent behavior of watched wallets.

## Decision

**Principle: collect irreplaceable data early, but only for the relevant slice of the market.**

1. **Enrichment source layering** (extends ADR 0001): Goldsky subgraph for deep maker/taker backfill; direct RPC `OrderFilled` reads (free-tier key, e.g. Alchemy/Infura) for the recent window the subgraph hasn't indexed; Data-API `/activity` remains the canonical wallet feed; Gamma remains metadata/resolution. The RPC key is optional via config; the adapter exists from day one.
2. **Minimal REST book sampler in MVP; no WSS.** Polls CLOB `/book` every 1–5 min for Relevant Tokens only (tokens with open watchlist positions + tokens traded by watchlist wallets in the last 24h). Stores timestamp, token_id, best_bid, best_ask, spread, depth, top ~10 levels, and the raw JSON in the raw capture area, schema-compatible with a future WSS collector.
3. **Strict storage hygiene from day one**: raw snapshots compressed; old snapshots archived; explicit retention policies and storage limits so the sampler cannot silently grow unbounded.
4. **Operational cadences**: `/activity` incremental sync 5 min/wallet; Gamma refresh hourly (touched markets); resolution sweep hourly; enrichment daily; prices-history lazy + cached; nightly `VACUUM INTO` backup; off-VPS rclone sync; monthly scripted restore drill.

## Consequences

- Spread/depth context around watched wallets' activity accrues from day one; its absence can never be fixed later.
- Maker/taker fingerprints cover recent behavior, not just as-of-last-month.
- The goal is context, not perfect book reconstruction; sampled books must not be presented as complete microstructure (a detector Blind Spot).
- Full WSS capture remains a cleanly separable later phase landing in the same raw-capture area.
