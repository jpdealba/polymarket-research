# 0003 — Flat-to-flat Episodes with weighted-average-cost PnL

Date: 2026-07-03
Status: Accepted

## Context

Every per-position metric (holding time, adds, win rate, PnL per position) is undefined until two choices are fixed: the temporal boundary of a position, and the inventory accounting method. Candidates: FIFO / LIFO / weighted-average cost; boundary candidates: per-order, per-day, flat-to-flat, flat-to-flat with a debounce window.

Relevant verified facts: Data-API rows are per-fill (one order → many fills), so lot-matching methods see artificial fragmentation; MERGE/REDEEM events also change holdings, so boundaries must consume all ledger event types; market makers cross zero holdings constantly.

## Decision

- **Boundary: flat-to-flat Episode** at the token level (zero → non-zero opens; return to zero or market resolution closes). All holding-affecting ledger events are consumed inside the episode.
- **No debounce/grace period.** Micro-episodes from rapid flat crossings are recorded as-is; the pattern is an analytical signal, and any smoothing heuristic would embed a strategy assumption into the accounting layer.
- **Accounting: weighted-average cost** for primary realized PnL. Matches trader mental models and Polymarket's own avgPrice convention; insensitive to fill fragmentation; degrades gracefully for high-frequency flat-crossers.
- **FIFO**: only as an optional later projection for hold-time / lot-aging analytics. **LIFO**: not used.

## Consequences

- All episode metrics are reproducible from the ledger and mutually consistent.
- Episode-level metrics are strategy-dependent; cross-wallet comparisons require stratifying by behavior type (documented in CONTEXT.md as a standing caveat).
- Adding FIFO later is purely additive (a new projection); switching the *primary* method later would invalidate historical metric comparisons — this decision is intentionally hard to reverse.
