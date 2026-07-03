# Phase 8 PnL Definition Audit

**Date:** 2026-07-03
**Wallet:** RN1 (`0x2005d16a84ceefa912d4e380cd32e7ff827875ea`)
**Status:** Read-only audit — no code changes. All numbers below were computed by
re-executing the exact `pnl_decomposition.py` algorithm directly against
`data/db/pmresearch.db` (not estimated) and cross-checked to match the production
`pnl_decomposition` table total to the penny ($13,030,374.379...). Anywhere a number
could not be directly computed from the local DB, it is labeled **[unverified]** —
do not treat those as confirmed.

---

## 1. Executive summary

Phase 8 reports **$13,030,374.38** total realized PnL for RN1. The user's read of
Polymarket's UI/leaderboard is **~$10–11M**. This audit does **not** find a single
clean explanation that forces the two numbers together — and per the task
instructions, it does not try to. Instead, three concrete, quantified, currently-real
gaps were found:

1. **Fees are hard-coded to $0** in `pnl_decomposition.py`, but a fee *estimate*
   already exists in the DB (`fee_estimates` table, Phase 5 output) that was never
   wired in: **$1,262,382.99** of worst-case sports taker fees for RN1. Subtracting
   it brings the total to **$11,767,991.39** — close to, though still slightly above,
   the 10–11M range.
2. **$5,639,450.35 of capital is currently parked in 12,450 open (unresolved)
   positions.** Phase 8 is realized-only: this money is neither a gain nor a loss in
   the $13.03M figure. If Polymarket's UI marks open inventory to current price
   (rather than ignoring it, as Phase 8 does), and those positions are on average
   underwater relative to WAC cost, that would pull the UI number further below
   $13.03M. This cannot be quantified without Phase 9 marks.
3. Independently, `redemption_pnl` ($6,752,394.70) is **not** one homogeneous
   thing — it nets **+$15,055,531.90** of real, cash-backed REDEEM proceeds against
   **-$8,303,137.20** of non-cash write-offs from derived `REDEEM_PAYOUT` events
   (losing-side inventory marked to $0 at resolution). Both sides are legitimate
   realized economics, but this split matters for interpreting where the number
   comes from (see §4.3).

A fourth finding surfaced during this audit that is **not** part of the UI gap, but
is a data-quality flag worth raising on its own: **the `episodes` table's
`realized_pnl` totals ($1.15M flat + $0.85M open + $2.39M resolution ≈ $4.4M) are
wildly inconsistent with `pnl_decomposition`'s $13.03M**, and `episodes.reward_income`
sums to exactly $0 wallet-wide even though `REWARD` events clearly exist. See §6.

**On double-counting (the question that motivated this audit):** No, `bond_merge_pnl`
and `redemption_pnl` do not double-represent the same collateral cycle. Confirmed by
re-reading and re-executing the code: each token's cost basis lives in a single
mutable position object per `token_id`, consumed exactly once by whichever event
closes it (TRADE sell, MERGE, REDEEM, or REDEEM_PAYOUT). There is no code path that
lets two components draw from the same basis. See §5 for the mechanism-level detail.

---

## 2. Current Phase 8 PnL decomposition (verified against production)

| Component | Value | % of total |
|-----------|------:|-----------:|
| `directional_pnl` | $441,323.54 | 3.4% |
| `bond_merge_pnl` | $5,544,232.17 | 42.5% |
| `reward_income` | $292,423.97 | 2.2% |
| `redemption_pnl` | $6,752,394.70 | 51.8% |
| `fees` | $0.00 | 0.0% |
| **total_pnl** | **$13,030,374.38** | **100%** |

```
total_pnl = directional_pnl + bond_merge_pnl + reward_income + redemption_pnl - fees
```

### 2.1 Event-type → component mapping (`pnl_decomposition.py:196-264`)

| Component | Event types consumed | Method |
|-----------|----------------------|--------|
| `directional_pnl` | `TRADE` (SELL leg only) | WAC realized PnL: `proceeds - basis_consumed` via `Position.remove()` |
| `bond_merge_pnl` | `MERGE` | Same `Position.remove()`, fanned out evenly across the condition's tokens |
| `reward_income` | `REWARD`, `MAKER_REBATE`, `TAKER_REBATE` | Straight sum of `delta_usdc`, no position/WAC math at all |
| `redemption_pnl` | `REDEEM` (real, nonzero) + `REDEEM_PAYOUT` (derived) | `Position.close()` — force-closes the *entire* remaining position, not just the redeemed quantity |
| `fees` | none wired | Hard-coded `0` in every code path |

`SPLIT` produces **zero events for RN1** (confirmed by direct query — RN1 never
splits collateral). It's worth noting for completeness that if it did occur, it
would only add inventory (`Position.add`), never realize PnL — see §5.2.

### 2.2 Raw event-type totals for RN1 (`wallet_events`, all-time)

| event_type | is_derived | count | Σ delta_usdc | Σ delta_shares |
|---|---|---:|---:|---:|
| TRADE | 0 | 3,703,043 | -385,130,074.88 | +808,292,121.14 |
| MERGE | 0 | 55,986 | +158,372,220.22 | -158,372,220.22 |
| REDEEM | 0 | 80,865 | +236,975,289.06 | -236,975,289.06 |
| REDEEM_PAYOUT | 1 | 18,632 | +232,870.64 | 0 |
| REWARD | 0 | 200 | +235,864.68 | 0 |
| MAKER_REBATE | 0 | 114 | +56,559.29 | 0 |
| TAKER_REBATE | 0 | 30 | 0 | 0 |
| CONVERSION | 0 | 1 | 0 | 0 |

Note MERGE and REDEEM both show `Σ delta_usdc ≈ -Σ delta_shares` almost exactly —
i.e. RN1's merges and (non-derived) redemptions are overwhelmingly settling at ~$1
per share, as expected for complementary-pair collateral mechanics.

---

## 3. Close reason (episodes projection)

Set in `pmresearch/projections/episodes.py`:

| close_reason | Trigger | Meaning |
|---|---|---|
| `flat` | A TRADE or MERGE reduction brings qty to ~0 | Natural exit at market price |
| `resolution` | REDEEM or REDEEM_PAYOUT | Force-closed regardless of remaining qty because the market resolved |
| `open` | Still nonzero at end of ledger replay | Not yet realized |

Live counts from the `episodes` table (projection_version=2, includes derived
REDEEM_PAYOUT events per a direct check of `events_consumed`):

| close_reason | count | Σ realized_pnl | Σ reward_income |
|---|---:|---:|---:|
| flat | 25,597 | $1,154,616.27 | $0.00 |
| open | 12,450 | $847,163.88 | $0.00 |
| resolution | 120,962 | $2,393,489.60 | $0.00 |

See §6 — these numbers are flagged as unreliable for cross-checking against
`pnl_decomposition`, not because they're stale, but because they appear to measure
something structurally different.

---

## 4. MERGE / SPLIT / REDEEM: economic classification

### 4.1 MERGE → `bond_merge_pnl`: **economic profit/loss**, not capital return

```python
shares = -delta_shares
proceeds_per_token = delta_usdc / len(tokens)
for token_id in tokens:
    pnl = position(token_id).remove(shares, proceeds_per_token)  # proceeds - WAC basis
```

Since real MERGE proceeds settle almost exactly at $1/share (§2.2), `bond_merge_pnl`
is capturing the spread between what RN1 paid (via TRADE, not SPLIT — RN1 has no
SPLIT events) to accumulate a complementary pair, and the $1 it recovers by merging.
Aggregate: $158.37M merged, releasing ~$152.83M of cost basis, netting +$5.54M — i.e.
RN1's average acquisition cost across everything it later merged was ~3.5% below
par. This is **genuine realized trading edge**, not a reclassified capital return —
a pure SPLIT→MERGE round-trip with no price movement nets to exactly $0 (verified
against `test_phase8_pnl.py::test_merge_round_trip_decomposes_to_zero`).

### 4.2 SPLIT: no PnL event, ever

Not exercised by RN1, but for completeness: SPLIT only distributes cost basis across
the condition's tokens (`Position.add`), it never touches a PnL bucket. Any profit or
loss on split-derived inventory shows up later, whichever way it's eventually closed.

### 4.3 REDEEM / REDEEM_PAYOUT → `redemption_pnl`: two economically distinct halves

Re-running the algorithm with the two paths tracked separately:

| Sub-path | Trigger | Σ proceeds | Σ realized pnl |
|---|---|---:|---:|
| Real REDEEM (nonzero `delta_usdc`, actual cash) | API-reported cash redemption | $233,692,969.71 | **+$15,055,531.90** |
| Derived REDEEM_PAYOUT (non-cash) | Zero-value API REDEEM rows, backfilled from `qty × resolution_price` | $162,313.29 (recomputed at replay time*) | **-$8,303,137.20** |
| **Total `redemption_pnl`** | | | **$6,752,394.70** |

\* The stored `delta_usdc` on `REDEEM_PAYOUT` rows sums to $232,870.64, computed at
derivation time; recomputing `qty × resolution_price` at replay order gives
$162,313.29. The two don't need to match — `pnl_decomposition.py` never reads the
stored `delta_usdc` for `REDEEM_PAYOUT`, it always recomputes proceeds live
(`pnl_decomposition.py:251-261`) — but the fact that they differ at all confirms the
stored value is informational only, not authoritative.

**What this means economically:**
- The **+$15.06M** real-REDEEM side is capital return plus economic profit on
  winning positions that actually paid out in USDC — unambiguous realized profit.
- The **-$8.30M** derived side is **not a cash event at all**. It's Phase 8 correctly
  recognizing that inventory sitting in losing-side tokens (often accumulated via
  ordinary TRADE activity across many small sports markets, per the `Sports:
  $12,744,824.08` category share) is now worthless, and writing off its cost basis
  as a realized loss — even though the source API never recorded any cash movement
  for it. This is real economic loss recognition that the API-only, cash-flow view
  would simply never surface (the "complete, honest PnL" goal stated in
  `docs/plan/IMPLEMENTATION_PLAN.md`'s Phase 8 section).

This split is the strongest evidence that RN1's real strategy involves holding both
sides of a large number of sports markets and letting most losing legs expire
worthless while a smaller number of winners (plus the merge spread) carry the book.

---

## 5. Double-counting check: `bond_merge_pnl` vs `redemption_pnl`

**Conclusion: no double-counting.** Verified two ways.

**By construction:** both components draw from the same `dict[token_id, Position]`,
a single mutable object per token for the wallet's *entire* history (not
re-instantiated per episode). `MERGE` calls `.remove()`, `REDEEM`/`REDEEM_PAYOUT`
call `.close()`. Both mutate `qty`/`cost` in place and return the realized delta for
exactly the shares consumed in that call. Once shares are removed by one event, they
are gone — a later event physically cannot re-consume the same basis, because
there's no remaining `qty`/`cost` left for it to act on.

**By example (partial merge then resolution):**
1. SPLIT $10 → 10 YES + 10 NO (not RN1's pattern, but illustrative)
2. MERGE 5 YES + 5 NO → $5: `bond_merge_pnl` realizes PnL on exactly those 5+5 shares
3. Market resolves; REDEEM the remaining 5 YES → $5: `redemption_pnl` realizes PnL on
   the other 5 shares only

No overlap — the two events act on disjoint quantities of the same token.

**One caveat found, immaterial to the total:** for a real (non-derived) `REDEEM`
event, `pnl_decomposition.py:239-249` splits `delta_usdc` **evenly across every
token in the condition**, including losing-side tokens that in reality receive $0.
This means a losing token can be force-closed with a nonzero `proceeds_per_token`
that rightfully belonged to the winner. This does **not** change the grand total
(the sum across a condition's tokens is invariant to how the split is done — it's
always `Σproceeds - Σcost_before` either way), but it does **mis-attribute** PnL
between two tokens of the same condition. Since category is assigned at the
condition level, `by-category` totals are unaffected too. This is a real but narrow
bug worth fixing before any *per-token* or *per-market* Phase 8 breakdown is trusted.

---

## 6. A second finding: `episodes` and `pnl_decomposition` disagree by ~$8.6M

This was not asked for directly, but surfaced while validating against the episodes
projection and is material enough to flag. `episodes.realized_pnl` summed across all
`close_reason` values is **~$4.4M**, vs. `pnl_decomposition`'s **$13.03M** — a ~$8.6M
gap between two projections in the same codebase that both claim to compute
"realized PnL" from the same ledger.

Two concrete, verified contributing factors:

1. **`episodes.reward_income` sums to exactly $0** wallet-wide. `REWARD` /
   `MAKER_REBATE` / `TAKER_REBATE` events have `token_id = NULL` at the ledger level
   (per `ledger/model.py`'s documented convention — these are unscoped to any
   market). `episodes.py` attributes reward income to "the currently open episode
   for that token" — but a `NULL`-token event can never match a per-token episode,
   so it's silently dropped. `pnl_decomposition.py` instead sums `REWARD`/rebate
   `delta_usdc` directly with no token/episode requirement, which is why it captures
   the full $292,423.97. This accounts for a small, known slice of the $8.6M gap,
   not the bulk of it.
2. **Ruled out:** a hypothesis that 14 tokens with negative ending quantity
   (pre-existing, documented "upstream historical gap" tokens like SDSU/Grand
   Canyon) might be leaking unbounded profit in `pnl_decomposition.py`'s
   `Position.remove()` (its `qty_before <= 0` branch returns the full sale proceeds
   as pnl without touching cost basis, and never re-closes/resets). Checked directly:
   total TRADE volume across those 14 tokens is only $95,130.84 net — far too small
   to explain the gap. Not the cause.

The remaining ~$8M+ is unexplained by this audit and looks like a genuine
methodological divergence between the two projections (e.g., `episodes.py`'s
flat-to-flat episode boundaries vs. `pnl_decomposition.py`'s single continuous
per-token position object may not, in fact, behave identically once REDEEM's
even-split-across-tokens quirk (§5) and MERGE/REDEEM ordering interact across
120,962 resolution-closed episodes — this needs its own dedicated investigation, not
folded into the UI-comparison question this audit was scoped to answer. **Recommend
treating `pnl_decomposition` as the authoritative Phase 8 number** (it's the one
`RN1_CHECKPOINT.md` and the CLI (`pmr pnl show`) actually surface), and opening a
separate audit specifically reconciling `episodes.py` against it before relying on
episode-level realized PnL for anything.

---

## 7. Alternative PnL definitions

| Definition | Formula | Value | Status |
|---|---|---:|---|
| `all_in_accounting_pnl` | current Phase 8 `total_pnl` | **$13,030,374.38** | Verified (= production) |
| `trading_pnl_ex_rewards` | `total_pnl - reward_income` | **$12,737,950.41** | Verified |
| `fee_adjusted_pnl` | `total_pnl -` worst-case estimated fees | **$11,767,991.39** | Verified subtraction; fee estimate itself is a worst-case/taker-only model, see §7.1 |
| `fee_adjusted_ex_rewards` | `trading_pnl_ex_rewards -` worst-case fees | **$11,475,567.42** | Same caveat |
| `ui_style_candidate_pnl` | raw net cash flow, Σ`delta_usdc` across every event type, no WAC at all | **$10,742,729.01** | Verified computation; **not verified as Polymarket's actual method** — see §7.2 |
| `realized_only_closed_pnl` | Σ`episodes.realized_pnl` where `close_reason IN ('flat','resolution')` | **$3,548,105.87** | Verified from DB, but **not recommended** — see §6, this projection appears to disagree structurally with `pnl_decomposition` |
| `realized_plus_open_value_candidate` | `total_pnl` + mark-to-market delta on open positions | **not computable** | Needs Phase 9 marks; open cost basis is $5,639,450.35 (see §7.3), but current market value of that inventory is unknown |

### 7.1 Fees: a real, already-quantified gap that Phase 8 ignores

`fee_estimates` (Phase 5 output, 3,699,456 rows for RN1) already contains an
estimate that `pnl_decomposition.py` never reads:

| category | rows | Σ estimated_fee | Σ worst_case_fee | Σ actual_fee |
|---|---:|---:|---:|---:|
| sports | 3,695,514 | $1,262,382.99 | $1,262,382.99 | $0.00 (never populated) |
| unclassified | 3,942 | $0.00 | $0.00 | $0.00 |

The active fee schedule (`fee_schedules` table): `polymarket_sports_taker_fee_v1`,
effective 2026-03-30, `fee = shares × price × 0.03 × (price×(1-price))^1`. Since
`estimated_fee == worst_case_fee` for every row and `actual_fee` is never populated,
this is a **taker-only upper bound** — Phase 11 (maker/taker fill enrichment,
per `CLAUDE.md`'s roadmap) hasn't run, so any maker fills RN1 actually got (which pay
$0 taker fee, and separately already show up as `MAKER_REBATE` income in
`reward_income`) are being over-charged here. True fees are somewhere between $0 and
$1,262,382.99, most likely meaningfully below the ceiling given RN1's `Sports`
category dominates volume and market-making-style behavior (large merge/redemption
share of PnL) usually implies a non-trivial maker fraction.

### 7.2 `ui_style_candidate_pnl`: closest empirical match, but not a confirmed method

This is literally `Σ delta_usdc` across every wallet_events row, with no cost-basis
matching at all — money in minus money out, full stop. At $10,742,729.01 it's the
number closest to the user's stated 10-11M read of the UI. **This proximity should
be treated as a data point, not confirmation** — this audit has no independent
verification of how Polymarket's UI actually computes its displayed PnL, and this
candidate has a specific, known blind spot: it doesn't distinguish "loss on a
position that's still open" from "loss on a position that's now worthless" — it just
reflects whatever cash has crossed the wallet boundary so far, which structurally
undercounts if RN1's still-open $5.64M of inventory eventually resolves as
profitable (the profit hasn't crossed the wallet boundary yet), and doesn't credit
the realized-but-non-cash losses recognized in §4.3 at all, since no cash moved for
those. Do not present this number as "the" answer without independently confirming
Polymarket's own PnL methodology.

### 7.3 Open capital: real, but not yet a gain or loss

12,450 tokens hold nonzero quantity at the end of the ledger replay (matches Phase 6's
"open episodes: 12,450 = nonzero holdings: 12,450" reconciliation exactly). Total
cost basis parked in these open positions: **$5,639,450.35**. This is money RN1 has
spent that hasn't come back yet — not a loss, not a profit, just unresolved capital.
Phase 8 correctly excludes it from `total_pnl` (it's realized-only by design). If
Polymarket's UI marks open inventory at current price rather than ignoring it
entirely, the UI number would differ from Phase 8's by exactly
`current_market_value(open) - $5,639,450.35`, which could go either direction and
requires Phase 9 marks to determine.

---

## 8. Conclusion

| Question | Answer |
|---|---|
| Double-counting between `bond_merge_pnl` and `redemption_pnl`? | **No** — verified by code path (disjoint mutations of a single per-token position) and by example. One narrow, total-preserving mis-attribution bug exists in REDEEM's even token split (§5). |
| Is the $13.03M forced or fabricated? | No — it's a faithful WAC-based realized PnL, reproduced independently against the raw ledger and matching production exactly. |
| What explains most of the gap to the ~10-11M UI read? | Two concrete, quantified, currently-missing pieces: (1) $1.26M of worst-case sports fees that exist in the DB but aren't wired into Phase 8 (§7.1), and (2) $5.64M of capital in currently-open positions whose mark-to-market value under Polymarket's method is unknown without Phase 9 (§7.3). Applying just the fee estimate lands at $11.77M — still above the stated range, meaning open-position marking is likely also contributing, but its sign and size can't be determined here. |
| Does rewards income explain the gap? | No — $292,423.97 is ~12% of a ~$2.5M gap at most. |
| Should the $13.03M number be changed? | Not without doing the actual work: wire real fee attribution (Phase 11 maker/taker enrichment first, so fees aren't overstated), then add Phase 9 marks for open positions. Do not force-fit a "cash flow" reinterpretation (§7.2) without independently confirming that's genuinely how Polymarket computes its displayed number. |
| Anything else worth fixing first? | Yes — `episodes.py` and `pnl_decomposition.py` disagree by ~$8.6M on "realized PnL" from the same ledger (§6). This is independent of the UI-comparison question but should be reconciled before episode-level PnL is used for anything downstream (e.g. strategy detectors in Phase 13/14). |

---

## 9. Files referenced

| File | Content |
|---|---|
| `pmresearch/projections/pnl_decomposition.py` | Core Phase 8 PnL decomposition logic (re-executed directly against the DB for this audit) |
| `pmresearch/projections/episodes.py` | Episode projection with WAC, `close_reason` assignment |
| `pmresearch/ingest/derived.py` | Derived `REDEEM_PAYOUT` event construction |
| `pmresearch/ledger/model.py` | Event type sign conventions, `token_id = NULL` documentation for MERGE/SPLIT/REDEEM/REWARD |
| `pmresearch/fees/schedules.py` (schema: `fee_schedules` table) | Sports taker fee schedule, effective 2026-03-30, 3% with curvature |
| `data/db/pmresearch.db` — `fee_estimates` table | Existing but unused $1.26M worst-case sports fee estimate for RN1 |
| `tests/test_phase8_pnl.py` | Golden tests, incl. `test_merge_round_trip_decomposes_to_zero` cited in §4.1 |
| `docs/evidence/RN1_CHECKPOINT.md` | RN1 research checkpoint, source of the headline numbers audited here |
| `docs/plan/IMPLEMENTATION_PLAN.md` (Phase 8 section) | Stated goal: "complete, honest PnL — including cash flows the API reports as zero" |
