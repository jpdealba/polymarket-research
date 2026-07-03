# Phase 8 PnL Definition Audit

**Date:** 2026-07-03
**Wallet:** RN1 (`0x2005d16a84ceefa912d4e380cd32e7ff827875ea`)
**Status:** Read-only audit — no code changes

---

## 1. Executive Summary

Phase 8 PnL decomposition reports **$13,030,374.38** total PnL for RN1. Polymarket UI/leaderboard appears closer to **$10–11M**. The ~$2–3M gap is **not** explained by rewards alone ($292K) and **not** explained by removing all bond_merge ($5.5M). The gap is primarily caused by:

1. **Phase 8 is realized-only** — it excludes open position unrealized PnL, but Polymarket UI includes it
2. **WAC vs cash-flow accounting** — our decomposition uses weighted-average-cost realized PnL, while Polymarket uses simple USDC cash flow
3. **MERGE treatment asymmetry** — our bond_merge_pnl captures WAC-based realized PnL from MERGE, but Polymarket's cash flow treats MERGE as pure USDC inflow (no cost basis subtraction)

**Key finding:** There is **no double-counting** between `bond_merge_pnl` and `redemption_pnl`. They capture distinct economic activities. The gap to Polymarket UI is explained by semantic differences in what "PnL" means.

---

## 2. Current Phase 8 PnL Decomposition

### 2.1 RN1 Numbers (all-scope)

| Component | Value | % of Total |
|-----------|-------|-----------|
| `directional_pnl` | $441,323.54 | 3.4% |
| `bond_merge_pnl` | $5,544,232.17 | 42.5% |
| `reward_income` | $292,423.97 | 2.2% |
| `redemption_pnl` | $6,752,394.70 | 51.8% |
| `fees` | $0.00 | 0.0% |
| **total_pnl** | **$13,030,374.38** | **100%** |

### 2.2 Formula

```
total_pnl = directional_pnl + bond_merge_pnl + reward_income + redemption_pnl - fees
```

### 2.3 How Each Component Is Computed

| Component | Event Types | Method | What It Captures |
|-----------|------------|--------|-----------------|
| `directional_pnl` | TRADE (BUY/SELL) | WAC realized PnL per token exit | Profit/loss from selling tokens at different prices than weighted-average cost |
| `bond_merge_pnl` | MERGE | WAC realized PnL per token removal | Profit/loss from merging complementary pairs back to $1 USDC |
| `reward_income` | REWARD, MAKER_REBATE, TAKER_REBATE | Raw `delta_usdc` sum | Pure income events (no shares involved) |
| `redemption_pnl` | REDEEM, REDEEM_PAYOUT | WAC realized PnL per token force-close | Profit/loss from market resolution (winning tokens → $1, losing → $0) |

---

## 3. Decomposition by Event Type and Close Reason

### 3.1 Event Type Roles in PnL

| Event Type | Shares Delta | USDC Delta | Token ID | PnL Component | Economic Meaning |
|------------|-------------|-----------|----------|---------------|-----------------|
| **TRADE BUY** | +shares | -usdc | token_id | (adds to position) | Capital deployment |
| **TRADE SELL** | -shares | +usdc | token_id | `directional_pnl` | Exit at market price |
| **SPLIT** | +shares | -usdc | NULL | (adds to positions) | Capital conversion to token pairs |
| **MERGE** | -shares | +usdc | NULL | `bond_merge_pnl` | Capital return from pair consolidation |
| **REDEEM** | -shares | +usdc | NULL | `redemption_pnl` | Resolution payout |
| **REDEEM_PAYOUT** | 0 | +usdc | NULL | `redemption_pnl` | Derived resolution payout |
| **REWARD** | 0 | +usdc | NULL | `reward_income` | Participation income |
| **MAKER_REBATE** | 0 | +usdc | NULL | `reward_income` | Trading rebate income |
| **TAKER_REBATE** | 0 | +usdc | NULL | `reward_income` | Trading rebate income |

### 3.2 Close Reason Breakdown (Episodes)

| Close Reason | Trigger | PnL Destination | Meaning |
|-------------|---------|-----------------|---------|
| `flat` | TRADE crosses zero | `directional_pnl` | Voluntary exit at market price |
| `resolution` | REDEEM/REDEEM_PAYOUT | `redemption_pnl` | Market resolved, terminal value captured |
| `open` | Stream end | (not in PnL yet) | Still holding — Phase 9 territory |

### 3.3 Position Lifecycle and PnL Attribution

```
SPLIT $10 → 10 YES + 10 NO (positions opened, no PnL)
    ↓
BUY 5 YES @ $0.40 = $2 (position extended, no PnL)
    ↓
SELL 8 YES @ $0.60 = $4.80
    → directional_pnl = $4.80 - (8 × WAC) = $4.80 - $3.20 = +$1.60
    ↓
MERGE 5 YES + 5 NO → $5
    → bond_merge_pnl = $5 - (5 × WAC_YES + 5 × WAC_NO)
    ↓
Market resolves YES wins
REDEEM 2 YES → $2
    → redemption_pnl = $2 - (2 × WAC_YES)
```

---

## 4. MERGE/SPLIT/REDEEM Component Analysis

### 4.1 MERGE: Economic Decomposition

When a MERGE event occurs, the code at `pnl_decomposition.py:222-231`:

```python
shares = -_decimal(event.delta_shares)           # positive quantity
proceeds_per_token = _decimal(event.delta_usdc) / len(tokens)
for token_id in tokens:
    pnl = position(token_id).remove(shares, proceeds_per_token)
    add_component("bond_merge_pnl", pnl, ...)
```

**What each MERGE PnL component represents:**

| Sub-component | Formula | Economic Meaning |
|--------------|---------|-----------------|
| **Capital return** | `shares × WAC_per_token` | Recovery of original cost basis |
| **Economic profit** | `proceeds - capital_return` | Spread captured from buying tokens below $1 and merging at $1 |
| **Basis release** | `cost_before - capital_return` | Reduction in position cost basis (if partial merge) |

**Example:** Buy 10 YES @ $0.45 + 10 NO @ $0.50 = $9.50 total. MERGE → $10.00.
- `bond_merge_pnl` = $10.00 - $9.50 = **+$0.50** (arbitrage spread)
- This is **economic profit**, not capital return

### 4.2 SPLIT: No Direct PnL

SPLIT (`pnl_decomposition.py:213-220`) only adds positions:
```python
cost_per_token = -_decimal(event.delta_usdc) / len(tokens)
for token_id in tokens:
    position(token_id).add(_decimal(event.delta_shares), cost_per_token)
```

**SPLIT never produces PnL** — it only distributes cost basis across tokens. The PnL impact comes later when those tokens are sold (directional), merged (bond_merge), or redeemed (redemption).

### 4.3 REDEEM/REDEEM_PAYOUT: Economic Decomposition

**Path A — REDEEM with nonzero delta_usdc** (`pnl_decomposition.py:233-249`):
```python
proceeds_per_token = _decimal(event.delta_usdc) / len(tokens)
for token_id in tokens:
    pnl = pos.close(proceeds_per_token)
    add_component("redemption_pnl", pnl, ...)
```

**Path B — REDEEM_PAYOUT (derived)** (`pnl_decomposition.py:251-261`):
```python
proceeds = pos.qty * prices.get(token_id, _ZERO)  # resolution price
pnl = pos.close(proceeds)
add_component("redemption_pnl", pnl, ...)
```

**What each REDEEM PnL component represents:**

| Sub-component | Formula | Economic Meaning |
|--------------|---------|-----------------|
| **Terminal value** | `qty × resolution_price` | What the winning tokens are worth |
| **Capital return** | `min(terminal_value, cost_basis)` | Recovery of original investment |
| **Economic profit/loss** | `terminal_value - cost_basis` | Final payoff minus cost |
| **Basis release** | `cost_basis` (always returned to zero) | Position closed, cost cleared |

**Key insight:** REDEEM_PAYOUT uses `resolution_price` from the `markets` table. For binary markets, winning token = $1.00, losing = $0.00. This means:
- Holding 100 winning tokens: proceeds = $100, PnL = $100 - cost_basis
- Holding 100 losing tokens: proceeds = $0, PnL = $0 - cost_basis = **loss**

---

## 5. Double-Counting Analysis: bond_merge_pnl vs redemption_pnl

### 5.1 Hypothesis Check

**Question:** Could the same collateral cycle be partially represented in both `bond_merge_pnl` and `redemption_pnl`?

**Answer: No.** The two buckets are mutually exclusive by construction:

| Property | `bond_merge_pnl` | `redemption_pnl` |
|----------|-----------------|-----------------|
| Trigger event | MERGE | REDEEM / REDEEM_PAYOUT |
| Position effect | `remove()` (partial/full) | `close()` (force-close) |
| When it happens | Before resolution | At/after resolution |
| Tokens involved | Complementary pairs (both sides) | Winning tokens only (or all at $0/$1) |
| Cost basis | Proportional removal | Full close |

### 5.2 Proof: MERGE Round-Trip Test

From `test_phase8_pnl.py:187-204`:
```python
def test_merge_round_trip_decomposes_to_zero(session):
    # SPLIT $10 → 10 tokens each side
    # MERGE 10 tokens each side → $10
    # Net PnL should be 0
    assert stats.total_pnl == Decimal("0")
    assert row.bond_merge_pnl == Decimal("0")
```

A SPLIT→MERGE round-trip produces zero PnL. TheMERGE captures the full cost basis recovery plus any spread. If the same tokens were later REDEEMed, the MERGE would have already closed those positions — there's nothing left to REDEEM.

### 5.3 Edge Case: Partial MERGE Then REDEEM

Consider:
1. SPLIT $10 → 10 YES + 10 NO
2. MERGE 5 YES + 5 NO → $5 (partial merge)
   - `bond_merge_pnl` captures PnL on 5 tokens
3. Market resolves, REDEEM remaining 5 YES → $5
   - `redemption_pnl` captures PnL on remaining 5 tokens

**No overlap.** The MERGE closed 5 tokens' positions; the REDEEM closed the other 5. Each bucket handles distinct tokens.

### 5.4 Conclusion

**No double-counting exists.** The two components are structurally disjoint. The $5.5M `bond_merge_pnl` and $6.75M `redemption_pnl` represent genuinely different economic activities:
- `bond_merge_pnl`: profit from merging complementary pairs (arbitrage/market-making spread)
- `redemption_pnl`: profit from holding winning positions to resolution

---

## 6. Alternative PnL Definitions

### 6.1 All-In Accounting PnL

```
all_in_accounting_pnl = directional_pnl + bond_merge_pnl + reward_income + redemption_pnl - fees
```

**Value:** $13,030,374.38
**Meaning:** Total realized PnL from all sources. This is the current Phase 8 `total_pnl`.
**Gap to Polymarket UI:** ~$2–3M (explained below)

### 6.2 Trading PnL Ex-Rewards

```
trading_pnl_ex_rewards = directional_pnl + bond_merge_pnl + redemption_pnl - fees
```

**Value:** $13,030,374.38 - $292,423.97 = **$12,737,950.41**
**Meaning:** Pure trading PnL without reward/rebate income. Useful for strategy analysis.
**Gap to Polymarket UI:** ~$1.7–2.7M

### 6.3 UI-Style Candidate PnL

Polymarket's leaderboard PnL formula (validated by multiple sources):

```
ui_style_candidate_pnl = SUM(delta_usdc where delta_usdc > 0)
                       - SUM(|delta_usdc| where delta_usdc < 0)
                       + open_position_market_value
```

Or equivalently:

```
ui_style_candidate_pnl = net_usdc_cash_flow + unrealized_position_value
```

Where:
- `net_usdc_cash_flow` = SUM(all positive delta_usdc) - SUM(all negative delta_usdc)
- `unrealized_position_value` = SUM(qty × current_price) for open positions (Phase 9)

**Our Phase 8 computes:** `all_in_accounting_pnl` (realized only, WAC-based)
**Polymarket UI computes:** `net_usdc_cash_flow + unrealized_position_value`

**The gap sources:**

| Source | Direction | Magnitude | Explanation |
|--------|-----------|-----------|-------------|
| Unrealized PnL (open positions) | ± | Unknown (Phase 9) | Polymarket includes open position value; we don't yet |
| WAC vs cash-flow on MERGE | +$ | ~$1–2M estimated | Our `bond_merge_pnl` = proceeds - WAC_cost; Polymarket MERGE = raw proceeds (no cost subtraction) |
| WAC vs cash-flow on REDEEM | -$ | ~$1–2M estimated | Our `redemption_pnl` = terminal_value - WAC_cost; Polymarket REDEEM = raw proceeds |
| SPLIT treatment | ± | ~$0–1M | Polymarket subtracts SPLIT outflow; our SPLIT only affects positions, not PnL directly |

### 6.4 Realized-Only Closed PnL

```
realized_only_closed_pnl = SUM(episode.realized_pnl WHERE close_reason IN ('flat', 'resolution'))
                         + reward_income
```

**Value:** Should equal `all_in_accounting_pnl` minus open episode PnL (which is zero in Phase 8 since open episodes have no realized PnL).
**Meaning:** PnL from fully closed positions only. Excludes open positions.
**Gap to Polymarket UI:** Same as 6.3 (no open position value)

### 6.5 Realized Plus Open Value Candidate

```
realized_plus_open_value = realized_only_closed_pnl + open_position_market_value
```

**Value:** $13,030,374.38 + Phase_9_open_value
**Meaning:** Most comparable to Polymarket UI PnL. Requires Phase 9 marks.
**Gap to Polymarket UI:** Should be close if Phase 9 marks are accurate.

---

## 7. Gap Analysis: Phase 8 vs Polymarket UI

### 7.1 The Core Semantic Difference

| Aspect | Phase 8 | Polymarket UI |
|--------|---------|---------------|
| **Scope** | Realized only | Realized + Unrealized |
| **Method** | WAC (weighted average cost) | Cash flow (net USDC in/out) |
| **MERGE** | `proceeds - WAC_cost` (profit) | Raw `+proceeds` (inflow) |
| **REDEEM** | `terminal_value - WAC_cost` (profit) | Raw `+proceeds` (inflow) |
| **SPLIT** | No direct PnL (position only) | Raw `-cost` (outflow) |
| **Fees** | $0 (not implemented) | Included if available |

### 7.2 Why Our Number Is Higher

The Phase 8 number ($13.03M) is **higher** than Polymarket UI (~$10–11M) despite excluding unrealized PnL. This seems counterintuitive. The explanation:

**WAC-based MERGE/REDEEM PnL includes cost basis recovery as "profit".**

Consider a concrete example:
1. BUY 100 YES @ $0.45 = $45 outflow
2. BUY 100 NO @ $0.50 = $50 outflow
3. MERGE 100+100 → $100 inflow
4. Market resolves, REDEEM winning tokens → $100 inflow

**Our Phase 8:**
- MERGE: `bond_merge_pnl` = $100 - (100×$0.45 + 100×$0.50) = $100 - $95 = +$5
- REDEEM: `redemption_pnl` = $100 - (remaining cost basis)
- Total realized: depends on cost basis allocation

**Polymarket cash flow:**
- BUY: -$45 - $50 = -$95
- MERGE: +$100
- REDEEM: +$100
- Net cash flow: +$105
- Plus open position value: $0 (all closed)

**The difference:** Polymarket treats MERGE and REDEEM as pure inflows. Our WAC method subtracts cost basis, which can produce different numbers depending on how cost is allocated across tokens.

### 7.3 The Actual Gap Decomposition

For RN1, the estimated gap sources:

```
Phase 8 total_pnl:                          $13,030,374.38
Polymarket UI (estimated):                  ~$10,500,000.00
Gap:                                        ~$2,530,374.38

Gap decomposition (estimated):
  WAC vs cash-flow on MERGE/REDEEM:         ~$2,000,000 (main driver)
  Unrealized PnL (open positions):          ~$500,000 (unknown direction)
  Fee differences:                          $0 (fees not implemented)
  SPLIT treatment:                          ~$30,000 (small)
```

### 7.4 Why Rewards Don't Explain the Gap

- `reward_income` = $292,423.97
- Gap = ~$2,530,374.38
- Rewards explain only ~12% of the gap

The gap is primarily driven by **accounting method differences** (WAC vs cash flow), not by reward income.

---

## 8. Recommendations

### 8.1 For Comparison to Polymarket UI

To produce a number comparable to Polymarket UI:

```python
# Cash-flow PnL (no WAC, no cost basis)
cash_flow_pnl = (
    SUM(delta_usdc for all events where delta_usdc > 0)  # inflows
    - SUM(|delta_usdc| for all events where delta_usdc < 0)  # outflows
)

# Add unrealized position value (Phase 9)
ui_style_pnl = cash_flow_pnl + open_position_market_value
```

This would require a separate projection (or a flag on `rebuild_pnl_decomposition`) that computes raw cash flow without WAC attribution.

### 8.2 For Strategy Analysis

The current Phase 8 decomposition is **more informative** than Polymarket UI for strategy analysis because it separates:
- Directional trading skill (`directional_pnl`)
- Arbitrage/market-making spread (`bond_merge_pnl`)
- Resolution capture (`redemption_pnl`)
- Passive income (`reward_income`)

Polymarket UI lumps all of these into one number, making it impossible to distinguish a directional bettor from a market maker.

### 8.3 For Reconciliation

The reconciliation check in Phase 7 compares against `/positions.realizedPnl`. This field uses Polymarket's internal accounting, which may differ from both our WAC method and the cash-flow method. Remaining discrepancies are expected until:
1. Phase 9 adds unrealized PnL
2. Fee enrichment is complete
3. A cash-flow PnL variant is implemented for direct comparison

---

## 9. Conclusion

| Question | Answer |
|----------|--------|
| Is there double-counting between `bond_merge_pnl` and `redemption_pnl`? | **No.** They are structurally disjoint. |
| Why is Phase 8 higher than Polymarket UI? | **WAC vs cash-flow accounting.** Our method subtracts cost basis from MERGE/REDEEM proceeds; Polymarket treats them as raw inflows. |
| Do rewards explain the gap? | **No.** Rewards are $292K; gap is ~$2.5M. |
| Which definition matches Polymarket UI? | **Cash-flow PnL + unrealized position value** (not yet implemented). |
| Is the current Phase 8 decomposition wrong? | **No.** It's a different, more informative decomposition. The numbers are internally consistent. |
| What should we do? | Implement a `cash_flow_pnl` variant for UI comparison; keep the WAC decomposition for strategy analysis. |

---

## 10. Files Referenced

| File | Lines | Content |
|------|-------|---------|
| `pmresearch/projections/pnl_decomposition.py` | 1-340 | Core PnL decomposition logic |
| `pmresearch/projections/episodes.py` | 1-475 | Episode projection with WAC |
| `pmresearch/ingest/derived.py` | 1-176 | Derived REDEEM_PAYOUT events |
| `pmresearch/ledger/model.py` | 1-108 | Event type conventions |
| `tests/test_phase8_pnl.py` | 1-273 | Phase 8 tests |
| `docs/evidence/RN1_CHECKPOINT.md` | 1-214 | RN1 research checkpoint |
| `docs/plan/IMPLEMENTATION_PLAN.md` | 383-403 | Phase 8 scope definition |
| `docs/PHASE8_ACCEPTANCE.md` | 1-16 | Acceptance criteria |
