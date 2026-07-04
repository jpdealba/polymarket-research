# Phase 11 Fee Integration — RN1 Verification

Date: 2026-07-04
Wallet: `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`

## Erratum

The first implementation treated `fill_enrichment.fee` as a user-paid actual
fee. RN1 validation showed this is not safe: for subgraph rows, the field can
sum to far more than the schedule worst-case estimate and can be ~10% of share
quantity on individual fills. Therefore `fill_enrichment.fee` remains raw
enrichment data only. `fee_estimates.actual_fee` is intentionally left `NULL`
until the event field's semantics and maker/taker attribution are independently
verified.

## Commands

```bash
pmr db upgrade
pmr enrich coverage --wallet 0x2005d16a84ceefa912d4e380cd32e7ff827875ea
pmr fees report --wallet 0x2005d16a84ceefa912d4e380cd32e7ff827875ea --by-category --pre-post-sports-fee
pmr ledger stats --wallet 0x2005d16a84ceefa912d4e380cd32e7ff827875ea
pmr pnl show --wallet 0x2005d16a84ceefa912d4e380cd32e7ff827875ea --by-category
```

## Enrichment Coverage

- total trades: `3,704,054`
- enriched trades: `2,644,189`
- actual fee coverage: `71.39%`
- pending: `1,000,928`
- ambiguous: `4,919`
- missing: `54,018`

## Fee Report Totals

- actual_fee_total: `0`
- estimated_fee_total: `1,263,982.5968`
- estimated_fee_fallback_total: `1,263,982.5968`
- fee_scenario_total: `1,263,982.5968`
- maker volume with role: `218,130,521.218139`
- taker volume with role: `0`
- maker fees with role: estimated only until actual fee semantics are verified
- taker fees with role: `0`

Fee sources used:

- `estimated_schedule`: all trades

## Gross/Base vs Net After Fees

`wallet_events` remains gross/base observed cashflow and is not mutated.

- gross/base ledger PnL: `10,902,067.765281`
- estimated net PnL scenario: `9,638,085.168481`

Post-2026-03-30 ledger scenario:

- post gross/base PnL: `4,286,198.261281`
- post actual fee trades: `1,024,693`
- post actual_fee_total: `0`
- post estimated_fallback_total: `1,263,982.596800`
- post fee_scenario_total: `1,263,982.596800`
- post estimated_net_pnl_scenario: `3,022,215.664481`

`pnl show --by-category` prints `gross_base_total`, `estimated_fees`, and
`estimated_net_pnl_scenario` explicitly; the stored
`pnl_decomposition.projection_fees` column remains visible as the historical
projection value.
