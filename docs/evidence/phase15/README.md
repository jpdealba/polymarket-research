# Phase 15 evidence — wallet research report generator

`pmr report wallet <addr>` assembles a "Why is <wallet> profitable?" Markdown memo
from stored projections/fingerprints/labels/reconciliation — **zero new computation**
in the report layer (`pmresearch/reports/wallet_profile.py` + `render.py`).

## Files

- `rn1_report_phase15.md` — the real RN1 memo (`0x2005d16a…`), generated from the
  live local DB. Headline finding: RN1's decomposed PnL is dominated by the
  **`bond_merge` (inventory-cycling / SPLIT↔MERGE bond) leg at ~72.5% of gross
  magnitude**, then redemption (~17.9%), directional (~5.8%), rewards (~3.8%);
  the leading strategy hypothesis is **`inventory_cycling`**. Trust = warn
  (categorised avgPrice/size timing skew), surfaced in the memo's data-quality banner.

- `contrast_unloaded_wallet_phase15.md` — a contrast wallet that is **not backfilled
  in the local DB**, showing the report's graceful degradation: every section renders
  an explicit *insufficient data* block and an "trust status unknown" banner instead of
  fabricating numbers.

## Contrast wallets

The two deliberately-different contrast wallets (suspected MM / suspected value bettor)
are **not loaded locally** — their full backfill was deferred across prior phases
(same "no live data loaded locally" note as Phases 10–13). The report layer is
wallet-generic (`--wallet`, no RN1 literals); that meaningfully-different narratives
are produced when data exists is proven synthetically by
`tests/test_phase15_report.py::test_contrast_wallets_differ`. To produce real contrast
memos, backfill those wallets (Phase 1 → 14) then re-run `pmr report wallet <addr>`.
