# 0006 — Reconciliation is a permanent projection; wallets carry trust status

Date: 2026-07-03
Status: Accepted

## Context

Event-sourced accounting fails silently: a parsing bug corrupts every downstream projection while dashboards keep rendering. Verified: Data-API `/positions` exposes Polymarket's own per-position accounting (`size`, `avgPrice`, `realizedPnl`, `currentValue`, `totalBought`) and `/value` exposes total portfolio value — an independent external oracle for ledger reconstruction.

## Decision

1. After every sync, for every watchlist wallet, compare ledger-derived state to the oracle:
   - holdings per token vs `/positions.size` — exact match required; persistent drift is a hard alert (missed/duplicated/misparsed events);
   - WAC average entry vs `/positions.avgPrice` — small numeric tolerance; persistent drift marks the wallet untrusted;
   - realized PnL vs `/positions.realizedPnl` — tolerance band (timing/redemption accounting may differ);
   - portfolio value vs `/value` — ~1–2% band (mark timing/source differences).
2. Every check result is a stored timestamped fact: (wallet, ts, check_type, expected, computed, abs diff, pct diff, tolerance, status, source, notes/suspected cause).
3. Wallets carry a derived **trusted/untrusted** status. Untrusted wallets' analytics are visibly flagged; strategy conclusions are never silently presented as reliable.
4. Reconciliation doubles as upstream-change detection: field renames or semantic changes in Polymarket APIs surface as reconciliation failures, not silent corruption.

## MVP definition of done (recorded here as the acceptance contract)

1. RN1 + at least two deliberately different wallets (suspected MM, suspected value bettor) fully supported.
2. Full backfill + ≥7 days stable incremental sync with zero/near-zero holdings drift.
3. Ledger replay computes episodes, token positions, market- and event-level exposure, daily equity, staleness indicators.
4. Fingerprints + ≥3 scored detectors (market_making, inventory_cycling, value_betting), each storing score, evidence, feature values, version, blind spots.
5. Dashboard renders from the core library only; deletion test passes.
6. One restore drill passed end-to-end (stop containers → simulate DB loss → restore → replay → reconciliation green).
7. The platform generates the research deliverable: a written "Why is RN1 profitable?" report (PnL decomposition: directional / bond–merge–inventory cycling / rewards / redemptions; category breakdown; episode behavior; maker/taker evidence; hypothesis scores; limitations and data-quality notes). Without this report, the MVP is not done regardless of infrastructure.

## Consequences

- Correctness is a monitored invariant, not a hope; the cost is a few extra API calls per sync.
- Tolerance bands must be tuned to avoid alert fatigue; all thresholds live in config, and band breaches are investigated, not widened silently.
