# Phase 11 Fee Integration — RN1 Verification

Date: 2026-07-04
Wallet: `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`

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

- actual_fee_total: `9,676,847.576519`
- estimated_fee_total: `1,263,982.5968`
- estimated_fee_fallback_total: `847,938.0126`
- blended_fee_total: `10,524,785.589119`
- maker volume with role: `218,130,521.218139`
- taker volume with role: `0`
- maker fees with role: `9,676,847.576519`
- taker fees with role: `0`

Observed fee sources:

- `actual_subgraph`: `2,492,160`
- `actual_polygonscan`: `152,029`
- `estimated_schedule`: `1,059,865`

## Gross/Base vs Net After Fees

`wallet_events` remains gross/base observed cashflow and is not mutated.

- gross/base ledger PnL: `10,902,067.765281`
- net PnL after blended fees: `377,282.176162`

Post-2026-03-30 ledger scenario:

- post gross/base PnL: `4,286,198.261281`
- post actual fee trades: `1,024,693`
- post actual_fee_total: `8,870,985.842482`
- post estimated_fallback_total: `847,938.012600`
- post blended_fee_total: `9,718,923.855082`
- post net_pnl_after_blended_fees: `-5,432,725.593801`

`pnl show --by-category` now prints `gross_base_total`, `blended_fees`, and
`net_after_blended_fees` explicitly; the stored `pnl_decomposition.projection_fees`
column remains visible as the historical projection value.
