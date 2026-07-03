Phase 8: accepted_for_RN1

Validated:
- REDEEM_PAYOUT derived events inserted.
- is_derived=1 confirmed.
- Nonzero REDEEM rows skipped.
- PnL decomposition exists.
- all scope equals sum(category scopes).
- diff effectively zero.
- resolution episode PnL now uses derived payouts.
- full suite passed: 115 tests.

Known caveats:
- fees remain 0 until actual fee/enrichment phase.
- remaining realizedPnl reconciliation warnings are timing_skew-dominated.
- unknown category still exists but is small: about -6.9k.