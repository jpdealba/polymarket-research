# RN1 Research Checkpoint

Wallet: `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`

## Executive summary

RN1 is now reconstructed through the core accounting pipeline:
raw activity → ledger → metadata → holdings → reconciliation → episodes → WAC validation → derived redemption PnL → PnL decomposition.

The main finding so far is that RN1's profits do not appear to come mainly from simple directional trading. Most of the explainable PnL comes from bond/merge mechanics and redemption outcomes.

Current strict trust status:
- Not fully trusted in strict mode.
- Reason: one known upstream historical gap, SDSU / Grand Canyon, where the live/source activity has two SELL rows and no acquisition event.
- This is treated as a source/API historical gap, not a local ingest bug.

---

## Phase 5 — Holdings reconciliation

### What we validated

We validated reconstructed holdings against Polymarket `/positions.size`.

### What we learned

The pipeline is largely correct at the position-size level. Initial failures were mostly not true local bugs; they were caused by condition-scoped MERGE semantics, metadata gaps, and historical source quirks.

After targeted fixes, RN1 was reduced to one hard failure:
- SDSU / Grand Canyon token.
- Local/live activity show two SELL rows.
- No BUY, MERGE, SPLIT, REDEEM, or dedupe collision explains it.
- Treated as an upstream historical gap.

### Conclusion

Holdings reconstruction is usable for RN1 with one documented exception.

### Evidence

- `docs/evidence/phase5/holdings_dq_rn1.json`
- `docs/evidence/phase5/reconcile_rn1.json`
- `docs/evidence/phase5/reconcile_status_rn1.txt`
- `docs/evidence/phase5/trust_status_rn1.txt`

### Caveats

- RN1 remains untrusted under strict mode because the single source gap is intentionally kept visible.
- Do not fabricate acquisition events to make strict trust green.

---

## Phase 6 — Episodes projection

### What we validated

We segmented the ledger into flat-to-flat token-level episodes using WAC accounting.

### What we learned

Open episodes match nonzero holdings exactly:
- Open episodes: 12,450.
- Nonzero holdings: 12,450.
- Missing either direction: 0.

This confirms that the episode projection is consistent with the holdings projection.

### Conclusion

Episode boundaries and WAC accounting are structurally sound.

### Evidence

- `docs/evidence/phase6/episodes_stats_rn1.txt`

### Caveats

At Phase 6, realized PnL for resolution-closed episodes was still understated because redemption proceeds had not yet been derived.

---

## Phase 7 — WAC and realized PnL reconciliation

### What we validated

We compared local WAC against Polymarket `/positions.avgPrice`, using the current open episode rather than lifetime WAC.

We also compared local realized PnL against `/positions.realizedPnl`, warning-only until Phase 8.

### What we learned

The key WAC check works:
- `positions_wac_avg_price`: no hard failures in latest RN1 run.
- WAC is compared against `current_open_episode`, not lifetime history.
- A hand-walked Frosinone/Mantova position matched oracle avgPrice within tolerance.

Realized PnL discrepancies remain mostly timing-skew driven and are not hard failures.

### Conclusion

The current-open-episode WAC model is validated against oracle avgPrice for RN1.

### Evidence

- `docs/evidence/phase7/reconcile_rn1_phase7.json`
- `docs/evidence/phase7/reconcile_status_rn1_phase7.txt`
- `docs/evidence/phase7/trust_status_rn1_phase7.txt`

### Caveats

- Realized PnL reconciliation remains warning-only until derived redemption semantics are complete.
- Some WAC warnings are downstream of non-clean size checks, not clean WAC failures.
- Three-wallet validation is deferred because the local DB currently has projection-backed data only for RN1.

---

## Phase 8 — Derived redemption PnL and PnL decomposition

### What we validated

We derived idempotent `REDEEM_PAYOUT` events for zero-valued API REDEEM rows and updated episodes so resolution-close PnL includes derived payouts.

We also built `pnl_decomposition` and validated that:
- `scope='all'` equals the sum of category scopes.
- Decomposition components sum exactly to total PnL.

### What we learned

RN1's explained Phase 8 PnL is approximately:

- Total PnL: `13,030,374.38`
- Directional PnL: `441,323.54`
- Bond/Merge PnL: `5,544,232.17`
- Reward income: `292,423.97`
- Redemption PnL: `6,752,394.70`
- Fees: `0`

By category:
- Sports: `12,744,824.08`
- Uncategorized: `292,423.97`
- Unknown: `-6,873.67`

The key strategic learning is that RN1's PnL is not primarily directional. Most explainable PnL comes from bond/merge mechanics and redemption outcomes.

### Conclusion

RN1 appears to be highly driven by inventory conversion, merge/bond mechanics, and resolution/redemption, rather than simple directional betting alone.

### Evidence

- `docs/evidence/phase8/pnl_rn1_phase8.txt`
- `docs/evidence/phase8/reconcile_status_rn1_phase8.txt`
- `docs/evidence/phase8/trust_status_rn1_phase8.txt`

### Caveats

- Fees are still zero because actual fee attribution/enrichment is not complete.
- Remaining realized-PnL warnings appear timing-skew dominated.
- Unknown category is small but should remain visible.

---

## Phase 9 — Equity, marks, and /value reconciliation

### What we validated

TODO after Phase 9 completes.

### What we learned

TODO after Phase 9 completes.

### Conclusion

TODO after Phase 9 completes.

### Evidence

- `docs/evidence/phase9/...`

### Caveats

TODO.

---

## Current main finding

RN1's edge appears less like simple “pick winners” trading and more like a structure-heavy strategy involving:
- large sports exposure,
- repeated bond/merge mechanics,
- inventory recycling,
- resolution/redemption capture,
- some rewards income.

The next useful question is not “is the ledger correct?” anymore. It is:

**What exact strategy behavior produces the bond/merge and redemption PnL?**

That points to:
- Phase 9 for equity curve and valuation,
- Phase 11 for maker/taker and actual fill enrichment,
- Phase 13/14 for fingerprints and strategy detectors.

---

## Remaining unknowns

- Actual fees and whether they materially reduce the edge.
- Maker/taker split.
- Whether RN1 earns mainly from providing liquidity, inventory cycling, value betting, or a hybrid.
- Equity curve / drawdown behavior.
- Mark staleness and live portfolio value reconciliation.
- Whether unknown/uncategorized categories hide meaningful strategy behavior.