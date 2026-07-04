# Why is `0xe9076a87c5ed90ef16e6fe6529c943baeca0cff6` profitable?

_Generated 2026-07-04T13:06:43.409418+00:00 · window `all` · fingerprint version None_

> ⚠️ **DATA QUALITY: trust status unknown** — no reconciliation has run for this wallet. Treat every figure below as unverified.

## Executive summary

_Insufficient data — no PnL decomposition; run `pmr derive run`._

## PnL decomposition

_Insufficient data — no `pnl_decomposition` rows; run `pmr derive run`._

## Category breakdown

_Insufficient data — no per-category PnL rows._

## Episode behavior

_Insufficient data — no episodes; run `pmr replay episodes`._

## Equity & drawdown

_Insufficient data — no daily equity; run `pmr equity build`._

## Maker/taker & execution evidence

> Maker/taker shares are conditioned on on-chain enrichment coverage, which is **null** for this scope — the shares below may be uncomputable or unreliable.

- `maker_fill_share`: _null — feature not computed_
- `taker_fill_share`: _null — feature not computed_
- `enrichment_coverage`: _null — feature not computed_

_Maker/taker fill share is only meaningful over the subgraph-covered period; recent fills may be pending enrichment (see `enrichment_coverage`)._

## Income & sizing evidence

- `reward_income_share`: _null — feature not computed_
- `realized_pnl`: _null — feature not computed_
- `unrealized_pnl`: _null — feature not computed_

## Inventory & behavior fingerprint

- `bond_inventory_ratio`: _null — feature not computed_
- `merge_frequency`: _null — feature not computed_
- `redeem_frequency`: _null — feature not computed_
- `episode_count`: _null — feature not computed_
- `episode_duration_p50`: _null — feature not computed_
- `episode_duration_p90`: _null — feature not computed_
- `micro_episode_share`: _null — feature not computed_
- `adds_per_episode`: _null — feature not computed_
- `partial_exit_frequency`: _null — feature not computed_
- `market_category_concentration`: _null — feature not computed_

## Strategy hypotheses

_Insufficient data — no strategy labels; run `pmr detect run`._

## Reconciliation & trust

_Insufficient data — no reconciliation facts; run `pmr reconcile run`._

## Limitations & data-quality notes

- **Fees are estimates.** The decomposition's fee figure is the schedule-based estimate, not per-fill actuals, except where Phase 11 enrichment supplied a real fee.
- **Maker/taker coverage.** Shares depend on on-chain enrichment; the `enrichment_coverage` feature above bounds how much of the fill history is observed.
- **Mark staleness.** Unrealized value uses marks that may be stale; the equity section reports the stale-equity share, and drawdown is daily-close based.
- **Redemption PnL.** Resolution-closed episode PnL relies on derived redemption events (`pmr derive run`); markets not yet resolved on Gamma are excluded, not assumed lost.
- **Detector blind spots.** Each strategy score carries its own blind spots (listed above); scores read fingerprints only and are not ground truth.
