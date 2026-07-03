# Phase 5 Acceptance

Date: 2026-07-03
Status: accepted_with_known_exception

## Final RN1 State

- Wallet: `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`
- Replay holdings: `3,840,239` events -> `148,193` tokens
- Nonzero holdings: `12,450`
- Final negative holdings: `14`
- Reconciliation fails: `1`
- Test suite: `95 passed`

## Resolved

- G2 / PARIVISION is fixed. It now reports only timing skew / dust.
- Leverkusen No is correctly classified as `merge_condition_scoped_size_gap` warning.
- Historical bytea `condition_id` normalization is fixed.
- Duplicate raw activity handling is fixed with `duplicate_index`.
- The reconciliation classifier downgrades closed-market condition-scoped MERGE gaps correctly.

## Known Exception

- Token: `59880265195995334934669367383530286127953489732062523515730749463314801350746`
- Market: San Diego State Aztecs vs. Grand Canyon Antelopes
- Outcome: Grand Canyon Antelopes
- Classification: `source_api_missing_fill` / `upstream_historical_gap`

Evidence:

- Local raw activity and live Data API activity both show exactly two `SELL` rows and zero `BUY` rows for the token.
- There is no duplicate-ingest collision.
- There is no condition-scoped MERGE/REDEEM explanation.
- Remote `/positions` size is `0`.
- The local ledger remains negative by `42.67` shares.

Policy:

- Keep this exception visible in downstream reports.
- Do not fabricate an acquisition event.
- Do not mark RN1 fully trusted in strict mode.
- Downstream analytics may proceed only with this caveat:
  - `trust_status=untrusted`
  - `known_exception_count=1`
  - `known_exception_type=source_api_missing_fill`

## Phase 6 Guardrails

- Consume `wallet_trust` and `known_exceptions` from reconciliation output before presenting analytics.
- Strict mode must continue to treat RN1 as `untrusted` while the known exception remains.
- Do not run full `markets sync --all` unless a later phase requires fresh metadata.
