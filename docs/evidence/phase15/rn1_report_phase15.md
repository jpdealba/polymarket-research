# Why is `0x2005d16a84ceefa912d4e380cd32e7ff827875ea` profitable?

_Generated 2026-07-04T13:06:30.739224+00:00 · window `all` · fingerprint version 1_

> ⚠️ **DATA QUALITY: trust = warn** — warn: timing_skew=9045, metadata_unavailable_upstream=51, wac_size_reconciliation_not_clean=10, wac_timing_skew=10

## Executive summary

- **Total decomposed PnL:** 7642636.51 USDC (gross base 7642636.51 − fees 0).
- **Dominant income source:** `bond_merge` at 5544232.17 USDC (72.54% of gross magnitude).
- **Leading strategy hypothesis:** `inventory_cycling` (score 0.6563902204789999632560314225, confidence 1).

## PnL decomposition

Projection version 1. Components (signed USDC):

| Component | USDC | Share of gross magnitude |
|---|---:|---:|
| directional | 441323.54 | 5.77% |
| bond_merge | 5544232.17 | 72.54% |
| reward_income | 292423.97 | 3.83% |
| redemption | 1364656.83 | 17.86% |
| **gross base (pre-fee)** | **7642636.51** | |
| estimated fees | 0 | |
| **total (net of projection fees)** | **7642636.51** | |

_`bond_merge` is the inventory-cycling / bond leg (SPLIT↔MERGE pairs); `redemption` is resolution/redeem proceeds. Fees here are the projection's own estimate — see the fee caveat in Limitations._

## Category breakdown

| Category | directional | bond_merge | rewards | redemption | fees | total |
|---|---:|---:|---:|---:|---:|---:|
| Sports | 441398.69 | 5536539.45 | 0 | 1379148.06 | 0 | 7357086.21 |
| uncategorized | 0 | 0 | 292423.97 | 0 | 0 | 292423.97 |
| unknown | -75.15 | 7692.72 | 0 | -14491.24 | 0 | -6873.67 |

## Episode behavior

- **Episodes:** 159391 (133 open, 25620 flat-closed, 133638 resolution-closed).
- **Duration (s):** min 1, p50 22201.5, p90 637594.0, max 18060679.
- **Micro-episodes:** 276 (0.17% of all episodes).
- **Realized PnL (episodes):** -946324.04; **reward income:** 0.

## Equity & drawdown

- **Date range:** 2025-07-09 → 2026-07-03 (360 days).
- **Latest portfolio value:** 458639.76 USDC.
- **Latest marked PnL:** 7665639.34 USDC.
- **Max drawdown:** 312985.67 (basis: marked_pnl; daily-close based — intraday drawdown is approximate).
- **Latest stale-equity share:** 8.43%.

## Maker/taker & execution evidence

- `maker_fill_share`: 0.9362167968611057397865970974
- `taker_fill_share`: 0.06378320313889426021340290258
- `enrichment_coverage`: 0.8310732387803040828430512338

_Maker/taker fill share is only meaningful over the subgraph-covered period; recent fills may be pending enrichment (see `enrichment_coverage`)._

## Income & sizing evidence

- `reward_income_share`: 0.03826218461082964231670844545
- `realized_pnl`: 7350212.536942193320782772078
- `unrealized_pnl`: 23002.8344758859228422871205

## Inventory & behavior fingerprint

- `bond_inventory_ratio`: 0.1449123495349651986447653696
- `merge_frequency`: 155.3518005540166204986149584
- `redeem_frequency`: 276.1357340720221606648199446
- `episode_count`: 159391
- `episode_duration_p50`: 22201.5
- `episode_duration_p90`: 637594
- `micro_episode_share`: 0.001733036958896884300945635384
- `adds_per_episode`: 22.34477479907899442252071949
- `partial_exit_frequency`: 0.5421385147216593157706520443
- `market_category_concentration`: 0.9964552888026521261689295048

## Strategy hypotheses

Scores are 0–1 with a separate confidence (share of input features available). A high score at low confidence is not a verdict.

| Detector | Version | Score | Confidence |
|---|---:|---:|---:|
| inventory_cycling | 1 | 0.6563902204789999632560314225 | 1 |
| value_betting | 1 | 0.4006757481613178602877953387 | 1 |
| market_making | 1 | 0.3665705023528668360936282965 | 1 |

### Blind spots

- **inventory_cycling:** Cadence is measured per active day, so a bursty cycler (many merges in a few days) and a steady one look alike. Merge/redeem counts ignore size - a wallet cycling large notional and a small one score the same. Redemption attribution depends on resolution data being present; unresolved holdings are not yet counted as cycled.
- **value_betting:** Calibration edge is only as trustworthy as its resolved-episode sample - few resolutions make the edge noisy, and it is survivorship-scoped to markets that actually resolved. ~1-minute price fidelity limits any read of entry timing or momentum. taker_fill_share is conditioned on enrichment coverage; market_category_concentration is only defined at the all scope and is NULL (drops out) inside a single-category scope.
- **market_making:** Quote placement is unobservable - no historical order-book/quote data was collected, so passive liquidity provision cannot be distinguished from active market making, and true two-sided quoting is only proxied by bond (paired) inventory. maker_fill_share is conditioned on enrichment coverage: fills outside the subgraph-covered window carry no maker/taker role and are invisible to this score.

_Labels also exist for scopes: `all`, `category:Sports`, `category:unknown` (see `pmr detect explain`)._

## Reconciliation & trust

- **Trust:** warn — warn: timing_skew=9045, metadata_unavailable_upstream=51, wac_size_reconciliation_not_clean=10, wac_timing_skew=10 (since ts 1783097602, last reconciliation ts 1783097602).
- **Holdings vs /positions:** remote 9130, local nonzero 128, exact matches 28, pass 78, warn 9096, fail 0.
- **Tolerance:** 0.0001. **Known exceptions:** 0.

| Check | total | pass | warn | fail | skip |
|---|---:|---:|---:|---:|---:|
| missing_token_metadata_presence | 82 | 0 | 82 | 0 | 0 |
| portfolio_value | 1 | 0 | 1 | 0 | 0 |
| positions_realized_pnl | 84 | 82 | 2 | 0 | 0 |
| positions_size | 9174 | 78 | 9096 | 0 | 0 |
| positions_wac_avg_price | 84 | 64 | 20 | 0 | 0 |

- **Portfolio /value:** oracle 332370.17, local 458639.76, pct_diff 0.3799064787210955753882544144, status warn (value_drift_categorized), stale_equity_share 0.08433528488257533515429561117.

## Limitations & data-quality notes

- **Fees are estimates.** The decomposition's fee figure is the schedule-based estimate, not per-fill actuals, except where Phase 11 enrichment supplied a real fee.
- **Maker/taker coverage.** Shares depend on on-chain enrichment; the `enrichment_coverage` feature above bounds how much of the fill history is observed.
- **Mark staleness.** Unrealized value uses marks that may be stale; the equity section reports the stale-equity share, and drawdown is daily-close based.
- **Redemption PnL.** Resolution-closed episode PnL relies on derived redemption events (`pmr derive run`); markets not yet resolved on Gamma are excluded, not assumed lost.
- **Detector blind spots.** Each strategy score carries its own blind spots (listed above); scores read fingerprints only and are not ground truth.
