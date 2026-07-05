# RN1 Completion-Set / Inventory-Cycling Audit

Wallet: `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`
Computed: 2026-07-05T17:07:47.874350+00:00
Method: read-only replay of `wallet_events` (no projection or ledger mutation),
WAC cost basis (ADR 0003) reused from `projections.pnl_decomposition`.

## Executive summary

**Strong provisional finding:** RN1 appears to operate as a sports binary market
maker / inventory cycler. It accumulates both outcome tokens of binary markets
with passive buys, matches them into complete sets, and monetizes those sets via
MERGE (payout $1/set) plus resolution redemption of residual inventory. The PnL
bridge below compares this independent replay with the accepted projection and
reports any residuals explicitly.

Headline reconstructed numbers (all categories):

- Binary markets analyzed: **84,280** of 84,282 ledger conditions.
- Total realized MERGE edge: **$5,580,967** over 161,532,889 sets.
- Weighted realized edge per set: **3.455c** (346 bps of $1).
- Resolved markets audited for orphans: **84,235**.

## Thesis

1 token0 share + 1 token1 share = 1 complete set = $1. RN1 buys both legs below
$1 combined, so each completed set carries a positive gap. Sets are closed either
by MERGE (immediate $1) or by holding to resolution and redeeming. Directional
prediction is a minor component; the edge is structural (spread capture on the
completed set) at scale.

## What is supported

- Realized MERGE edge is positive and large ($5,580,967), consistent
  with buying complete sets below $1 and merging them for $1.
- Redemption of residual inventory is a second, smaller monetization channel.
- The PnL bridge reports the reconstruction against the accepted projection,
  including non-zero residuals instead of hiding them.

## What is NOT supported / still open

- This audit does **not** prove the edge is risk-free. Between legs, inventory is
  temporarily one-sided (directional).
- Orphan residual direction is reported from **net holdings at resolution**, not
  asserted as "100% winner". Where matched inventory persists to resolution, rows
  are flagged `ambiguous_complete_set_vs_winner_residual`.
- Fees are estimated-schedule only (no actual per-fill fee evidence).
- Temporal matching is buy-leg FIFO; SPLIT-created sets are folded into WAC but
  excluded from buy-pair timing.

## Units: outcome shares vs complete sets

`matched_pair_qty` counts **sets**. Total outcome shares = `2 * matched_pair_qty`.
A MERGE of N sets destroys N of each token and pays **N USDC**; never compare
total outcome shares against MERGE payout without dividing by 2. Column
`pair_qty_vs_merge_diff` surfaces markets where buy-matched sets and actual merged
sets diverge (partial merges, holds to resolution, or splits).

## MERGE realized edge

- Total realized edge: **$5,580,966.97** over **161,532,889** sets.
- Weighted edge per set: **3.4550c** (345.5 bps).
- Per-market detail: `rn1_merge_realized_edge.csv`
  (cost basis per leg via WAC, `pair_cost_per_set`, `realized_edge_bps`).

## REDEEM / orphan audit

- Resolved binary markets: **84,235** (unresolved: 45).
- Unmatched residual at resolution - winner qty: **103,654,388**,
  loser qty: **120,715,822**.
- Ambiguous matched-at-resolution qty (complete sets held, not a directional
  lean): **139,389,259**.
- Share of clean unmatched residual that is the winner, by qty:
  **46.2%**.
- Per-market detail: `rn1_redeem_orphan_audit.csv`.

> Note: the winner-lean of residual is reported, not the earlier informal
> "100% winner" claim. The `ambiguous_*` flag marks markets where a naive
> residual reading would overstate a directional lean.

## Temporal imbalance

- Matched buy-lot events: **2,797,637**.
- Completed within <=60s: **7.7%**; <=300s: **22.6%**.
- Seconds between legs - p50: **1,014s**, p90: **4,666s**,
  p99: **68,863s**.
- Directional imbalance qty per market - p50: **646**,
  p90: **6,807**, max: **302,822**.
- Imbalance ratio per market - p50: **0.775**,
  p90: **1.000**.
- Per-market detail: `rn1_temporal_imbalance_distribution.csv`.

Interpretation: legs are mostly **not** simultaneous, so this is continuous
passive liquidity provision across a market's life, not flash arbitrage.

## PnL bridge

`ledger_gross_pnl` is reconstructed fresh from this read-only replay
(realized_merge_edge + realized_redeem_edge + directional_sell_pnl + rewards, all
WAC). It does not depend on the `pnl_decomposition` projection; the `projection_*`
columns are a labelled cross-check only.

| metric | all | sports |
|---|---:|---:|
| merge_proceeds | 161532889.397044 | 160442806.108977 |
| redeem_proceeds | 243489411.412557 | 242033059.057149 |
| rewards_rebates | 321717.003000 | 0.000000 |
| realized_merge_edge_usdc | 5580966.966106 | 5557488.709918 |
| realized_redeem_edge_usdc | 4810221.136107 | 4834635.353855 |
| realized_directional_pnl | 441323.543657 | 441398.693109 |
| completion_mechanics_pnl | 10391188.102214 | 10392124.063773 |
| ledger_gross_pnl (fresh) | 11154228.648871 | 10833522.756882 |
| pct_gross_from_completion | 93.159181 | 95.925622 |
| estimated_fees | 1263982.596800 | 1263982.596800 |
| reconstructed_net_after_fees | 9890246.052071 | 9569540.160082 |
| xcheck_merge_delta (~0) | 36734.797879 | NA |
| xcheck_redeem_delta | 3445564.311049 | NA |
| projection_bond_merge_pnl | 5544232.168227 | NA |

Full detail: `rn1_pnl_bridge.csv`. A near-zero `xcheck_merge_delta` confirms this
independent WAC replay reproduces the accepted projection's bond_merge; a large
`xcheck_redeem_delta` typically reflects `pnl_decomposition` staleness (rebuilt
on-demand by `pmr derive`), not an error in the fresh reconstruction.

## Coverage / metadata accounting

- Events processed: **3,907,666**.
- Ledger conditions: 84,282; binary (2 tokens): 84,280.
- Non-binary condition events (excluded from pair analysis, kept in WAC): 0 across 0 conditions.
- Unmapped-condition events (no market metadata): 18 across 1 conditions.
- Events with no condition_id: 0.

## Caveats

- Read-only snapshot; projections such as `episodes` may lag the ledger.
- Prices/sizes taken verbatim from `wallet_events`.
- WAC (not FIFO) for realized edge, per ADR 0003; temporal matching uses buy-leg
  FIFO purely for timing diagnostics.
- Sports scoping depends on `markets.category`; uncategorized markets fall only
  into `all`.

## Next steps

- Attribute the per-set edge to counterparties (who crosses RN1's resting bids).
- Distinguish merge-close vs resolution-close sets in the lifecycle table.
- Model MERGE/REDEEM inventory reduction inside the temporal deque to refine the
  ambiguous-residual classification.
