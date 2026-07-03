Phase 7: accepted_for_RN1

Accepted:
- WAC vs avgPrice implemented.
- WAC compares against current open episode.
- No WAC hard failures in latest RN1 run.
- realizedPnl parsed and compared.
- realizedPnl discrepancies are warning-only.
- Trust remains untrusted only because of the known source_api_missing_fill case.

Caveats:
- 3-wallet validation deferred because local DB only has projection-backed data for RN1.
- realizedPnl remains preliminary until Phase 8.
- Legacy positions_avg_price_info rows still exist historically, but the Phase 7 checks are positions_wac_avg_price and positions_realized_pnl.