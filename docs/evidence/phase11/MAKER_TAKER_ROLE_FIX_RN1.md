# Phase 11 Maker/Taker Role Fix - RN1

Date: 2026-07-04

Wallet: `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`

Status: maker/taker role attribution is no longer artificially 100% maker.
The enrichment replay used deterministic candidate resolution: all matching
OrderFilled candidates for a wallet event are collected before `fill_enrichment`
is inserted or updated.

Fee reporting was not changed. `fill_enrichment.fee` remains raw/unverified and
is not used as actual fee evidence.

## Fix Summary

- Exchange-contract counterparties are recognized:
  - `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e`
  - `0xc5d563a36ae78145c45a50134d48a1215220f80a`
  - `0xe111180000d2663c0091e4f400237545b87b996b`
  - `0xe2222d279d744050d28e00520010520000310f59`
- `fill.maker == wallet` and `fill.taker` is an exchange contract is classified
  as taker-order owner, not passive maker.
- Matching candidates are buffered in a SQLite temp table, then streamed grouped
  by `event_id` for deterministic resolution.
- Existing enrichment rows are revisitable and updated when the deterministic
  result differs; the old first-wins `ON CONFLICT(event_id) DO NOTHING` behavior
  is gone from the join path.
- If normalized candidate roles disagree, the event resolves to `ambiguous`.

## Replay

RN1 `fill_enrichment` rows were rebuilt from stored raw pages.

| source | raw pages | unique decoded fills/logs | enriched | ambiguous | unmatched | already |
|---|---:|---:|---:|---:|---:|---:|
| subgraph | 5,708 | 2,836,058 | 2,546,159 | 4,919 | 264,659 | 0 |
| polygonscan | 1,994 | 602,225 | 546,396 | 18 | 53,142 | 0 |

Subgraph replay elapsed time: `1125.6s`.
PolygonScan replay elapsed time: `216.0s`.

## Role And Volume

```text
SELECT fe.source, fe.role, COUNT(*) AS fills,
       SUM(volume) AS volume
FROM fill_enrichment fe
JOIN wallet_events we ON we.id = fe.event_id
WHERE we.wallet = RN1
GROUP BY fe.source, fe.role;
```

| source | role | fills | volume |
|---|---|---:|---:|
| polygonscan | maker | 517,957 | 45,045,799.921968 |
| polygonscan | taker | 28,439 | 8,882,523.770382 |
| subgraph | maker | 2,377,344 | 181,548,421.913562 |
| subgraph | taker | 168,814 | 41,210,831.815927 |

Taker is no longer zero:

| role | fills | volume |
|---|---:|---:|
| maker | 2,895,301 | 226,594,221.835530 |
| taker | 197,253 | 50,093,355.586309 |

Maker still dominates by count, but the prior 100% maker output was caused by
source-order first-wins behavior and is no longer present.

## Coverage

| status | fills | volume |
|---|---:|---:|
| ambiguous | 4,919 | 401,493.648820 |
| enriched | 3,092,554 | 276,687,577.421399 |
| missing | 19 | 10,474.157466 |
| pending | 606,562 | 110,579,964.822454 |

The ambiguous coverage rows here are unresolved duplicate ledger candidates
left unenriched by amount matching. No stored `fill_enrichment.role='ambiguous'`
rows were needed in this RN1 replay after exchange-facing normalization.

## Fee Non-Regression

```text
SELECT COUNT(*) AS fee_actual_rows, COALESCE(SUM(CAST(actual_fee AS REAL)),0)
FROM fee_estimates
WHERE actual_fee IS NOT NULL;

SELECT fee_source, COUNT(*)
FROM fee_estimates
GROUP BY fee_source;
```

| metric | value |
|---|---:|
| fee_actual_rows | 0 |
| actual_fee_sum | 0 |

| fee_source | rows |
|---|---:|
| estimated_schedule | 3,704,054 |

This confirms the role fix did not re-enable actual fee usage from raw
`fill_enrichment.fee`.

## Tests

```text
.venv\Scripts\python.exe -m pytest tests/test_phase11_enrichment.py tests/test_fees.py
```

Result: `42 passed`.

```text
.venv\Scripts\python.exe -m pytest
```

Result: `194 passed`.
