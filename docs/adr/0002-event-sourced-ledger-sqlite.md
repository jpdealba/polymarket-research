# 0002 — Event-sourced ledger on SQLite; projections are disposable; raw snapshots kept

Date: 2026-07-03
Status: Accepted

## Context

The platform must (a) compute correct PnL including non-trade cash flows (MERGE/REDEEM/REWARD verified present in wallet activity), (b) survive container rebuilds on a Dokploy VPS without data loss, (c) stay near-free at MVP scale (10–50 wallets), and (d) not block a later move to Postgres/multi-tenant SaaS.

Alternatives considered: a mutable trades/positions database (simpler, but positions and PnL become unauditable and non-reproducible); DuckDB as primary store (better analytics, weaker transactional/backup story); Postgres from day one (violates near-free constraint, adds ops burden with no MVP benefit).

## Decision

1. **Append-only `wallet_events` ledger is the single source of truth.** Every wallet action is a typed immutable event with signed share/USDC deltas. Corrections are new events.
2. **All derived state is a Projection**: rebuildable from the ledger, written only by replay, droppable at any time. Schema-breaking changes are handled by rebuilding projections, not migrating them.
3. **SQLite is the system of record** (single file, transactional, trivial backup). DuckDB may attach to it read-only for ad-hoc analytics; it is not a second store. Schema kept portable (standard SQL, migrations via a tool from day one) so Postgres is a routine migration later.
4. **Raw Snapshots**: every external API response is persisted verbatim (gzipped, append-only) on a mounted volume before parsing. Recovery tiers: rebuild DB from raw snapshots; re-fetch from APIs as last resort (verified: APIs retain full per-wallet history — except live orderbook data, which is unrecoverable and must be treated as the most precious raw capture).
5. **Containers are disposable**: code in images, all persistent data (DB, raw snapshots, backups, exports, logs) under one host-mounted data root.
6. **Global Facts vs Workspace split**: researcher-owned data (watchlist, tags, notes) lives in separate tables from wallet/market facts. Future multi-tenancy adds workspace scoping to the small workspace tables only.

## Consequences

- PnL/holdings are reproducible and auditable by replay; bugs in analytics never corrupt source data.
- Storage is duplicated (raw + ledger + projections) — acceptable at MB–GB scale, and the redundancy is the backup strategy.
- Replay cost grows with history; mitigated by projections and, if ever needed, periodic snapshots of replay state.
- Event schema design carries the long-term burden: new event types must be additive.
