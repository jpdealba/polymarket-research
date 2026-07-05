# Phase 18 — Forward Microstructure Watch — Acceptance

Watchlist: `world_cup_2026`  
Generated: 2026-07-05 17:18:41Z

**7/7 checks passed — no failures.**

| Check | Status | Evidence |
|---|---|---|
| watch tick runs end-to-end | PASS | 36605/36640 sample runs finished; last run 2s ago |
| watch run sustains loop | PASS | 36638 sample runs over 23.8h in last 24h |
| watchlist_tokens has active tokens | PASS | 77 active tokens in 'world_cup_2026' |
| book_snapshots linked to sample runs | PASS | 630057 snapshots linked to sample runs (0 unlinked) |
| fill context classifies by freshness | PASS | 0x2005d16a…: 8351 fills across 5 buckets; 0x83255595…: 312 fills across 4 buckets |
| dashboard surfaces phase 18 state | PASS | page 11 renders status/watchlist/books/context via pmresearch.api only |
| no post-fill book labelled as pre-fill | PASS | 0 before/after ordering violations across all fill contexts |

## Book-before-fill coverage (exit question)

| Wallet | Fills | strict (exc+good) | loose (+usable) | strict share | loose share |
|---|---|---|---|---|---|
| RN1 | 8351 | 4182 | 4578 | 50.1% | 54.8% |
| Mind.The.Gap | 312 | 293 | 311 | 93.9% | 99.7% |

