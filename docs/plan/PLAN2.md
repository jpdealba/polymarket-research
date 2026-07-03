---

## Phase 18 — Entry Pattern Analysis: where, when, how much, and how RN1 enters

**Goal:** convert episodes into a concrete entry-behavior dataset that answers: where RN1 enters, when RN1 enters, at what price, with how much size, and what it does immediately after entry.

**Scope:** build an entry-level analytical projection from episodes, holdings, markets, events, fees, marks, exposures, enrichment, and book snapshots where available. This phase does not infer hidden intent yet; it measures observable entry behavior mechanically.

**Core questions answered:**

- Where does RN1 enter?
  - category
  - market question
  - event
  - outcome
  - market structure
  - sport/league/team when available
- When does RN1 enter?
  - absolute timestamp
  - time to event start
  - time to market close
  - time to resolution
  - hour-of-day / day-of-week
  - pre-game / live / near-close when inferable
- How much does RN1 enter with?
  - first-entry shares
  - first-entry USDC
  - total episode entry cost
  - max episode exposure
  - size as % of wallet open value/equity when available
  - size as % of market depth when book snapshots are available
- At what price does RN1 enter?
  - first entry price
  - weighted average entry price
  - entry price bucket
  - distance from mid / bid / ask where book data exists
- How does RN1 enter?
  - one-shot entry vs scale-in
  - number of adds
  - time between adds
  - maker/taker role when enrichment exists
  - whether entry begins as directional or paired/bond inventory
- What happens after entry?
  - add behavior
  - partial exits
  - MERGE/SPLIT usage
  - hold time
  - exit reason
  - realized PnL after Phase 8
  - whether entry became a winner, loser, merge-cycle, or unresolved open position

**Files/modules:**

- `pmresearch/entrypatterns/build.py` — builds entry-level rows from episodes and wallet_events.
- `pmresearch/entrypatterns/features.py` — pure functions for entry price, timing, sizing, scale-in, post-entry behavior.
- `pmresearch/entrypatterns/context.py` — joins markets, events, marks, exposure, maker/taker enrichment, and book snapshots.
- `pmresearch/entrypatterns/report.py` — grouped summaries by category, structure, price bucket, timing bucket, and size bucket.
- `pmresearch/cli/entrypatterns.py`

**Migrations:**

- `m0018_entry_patterns`
  - `entry_patterns`
    - id
    - wallet
    - episode_id
    - token_id
    - condition_id
    - event_id nullable
    - category nullable
    - structure_type
    - outcome_label
    - question
    - entry_ts
    - first_entry_price
    - weighted_entry_price
    - first_entry_shares
    - first_entry_usdc
    - total_entry_usdc
    - peak_qty
    - peak_cost_basis
    - entry_size_wallet_share nullable
    - entry_size_depth_share nullable
    - time_to_event_start_s nullable
    - time_to_market_close_s nullable
    - time_to_resolution_s nullable
    - entry_hour_utc
    - entry_day_of_week
    - entry_price_bucket
    - entry_size_bucket
    - timing_bucket
    - maker_taker_at_entry nullable
    - entry_spread nullable
    - entry_mid nullable
    - entry_best_bid nullable
    - entry_best_ask nullable
    - add_count
    - partial_exit_count
    - merge_count
    - redeem_count
    - split_count
    - hold_duration_s nullable
    - close_reason
    - realized_pnl nullable
    - open_unrealized_pnl nullable
    - trust_status
    - known_exception_flag
    - projection_version
    - computed_at

**CLI:**

- `pmr entry build [--wallet]`
- `pmr entry show --wallet <addr> [--category] [--open|--closed] [--limit N]`
- `pmr entry stats --wallet <addr> [--by-category] [--by-price-bucket] [--by-size-bucket] [--by-timing-bucket]`
- `pmr entry top --wallet <addr> --metric pnl|size|duration|frequency`

**Tests:**

- Golden episode with one buy → first_entry and total_entry equal.
- Golden episode with scale-in → first_entry_usdc differs from total_entry_usdc; weighted_entry_price hand-computed.
- Partial exit after scale-in preserves entry stats.
- Open episode produces no close duration if unavailable, but keeps entry data.
- Time-to-event-start computed from Gamma start_date.
- Entry price bucket boundaries are deterministic.
- Entry size bucket boundaries are deterministic.
- Maker/taker nullable when enrichment missing, never forced to false/zero.
- Book context nullable when no snapshot exists, never invented.
- Entry pattern rows preserve wallet_trust and known_exception flags.
- Rebuild is deterministic.

**Manual verification:**

- Run:
  - `pmr entry build --wallet <RN1>`
  - `pmr entry stats --wallet <RN1> --by-category`
  - `pmr entry stats --wallet <RN1> --by-price-bucket`
  - `pmr entry stats --wallet <RN1> --by-size-bucket`
  - `pmr entry stats --wallet <RN1> --by-timing-bucket`
- Hand-check 3 RN1 episodes:
  1. one simple one-shot entry,
  2. one scale-in entry,
  3. one MERGE-heavy/bond-style entry.
- For each, verify from raw events:
  - first entry timestamp,
  - first entry size,
  - total entry size,
  - weighted entry price,
  - add count,
  - exit/close reason.

**Acceptance criteria:**

- RN1 entry patterns build for all episodes.
- Every entry row traces back to an episode_id and token_id.
- Entry sizing answers both:
  - first-entry size,
  - total episode committed size.
- Reports can answer:
  - where RN1 enters,
  - when RN1 enters,
  - with how much size,
  - at what price,
  - whether it scales in,
  - what happens after entry.
- No hidden strategy inference yet; this phase is descriptive measurement only.
- Missing enrichment/book/equity context is represented as NULL with reason, not zero.
- Wallet trust caveats are carried into every row.

**Common failure modes:**

- Confusing first-entry size with total episode size.
- Comparing entry size to current wallet equity when only stale equity is available without flagging staleness.
- Treating missing maker/taker enrichment as taker.
- Treating missing book snapshot as zero spread.
- Merging separate episodes because they occur close together; Phase 6 flat-to-flat boundaries remain source of truth.
- Overfitting one anecdotal RN1 trade instead of aggregating all episodes.

**Prompt:**

`Implement Phase 18 of IMPLEMENTATION_PLAN.md exactly as scoped. This phase is descriptive entry analysis only: where RN1 enters, when it enters, at what price, with how much size, whether it scales in, and what happens after entry. Do not infer hidden signals yet. Use Phase 6 episodes as the source of truth, carry wallet_trust into every row, and keep missing enrichment/book context nullable. Show RN1 entry stats by category, price bucket, size bucket, and timing bucket, plus three hand-walked entry examples. Commit when acceptance criteria pass.`

---

## Phase 19 — Signal Reconstruction: infer repeatable entry conditions and pre-entry context

**Goal:** move from descriptive entry behavior to measurable hypotheses about why RN1 enters. This phase reconstructs candidate signals by comparing entry context against non-entry baselines and against losing/winning episodes.

**Scope:** candidate signal generation and scoring. Use entry patterns, marks, book snapshots, exposure, maker/taker enrichment, market metadata, episode outcomes, and PnL decomposition. This phase does not produce copy-trading rules yet; it produces evidence-ranked signal hypotheses with confidence, sample size, and blind spots.

**Core questions answered:**

- What tends to happen before RN1 enters?
  - price movement before entry
  - spread/depth conditions
  - market age
  - time to event start
  - time to close
  - recent volume/activity if available
  - maker/taker context
- Which entry conditions correlate with profitable episodes?
  - entry price buckets
  - timing buckets
  - size buckets
  - category/league/team/event type
  - scale-in behavior
  - maker vs taker
  - bond/merge inventory setup
- Does RN1 seem to enter because of:
  - value price,
  - liquidity/rebate opportunity,
  - spread capture,
  - inventory cycling,
  - merge/bond setup,
  - time-to-event pattern,
  - category-specific edge,
  - stale/mispriced market behavior?

**Files/modules:**

- `pmresearch/signals/baselines.py` — builds non-entry comparison baselines by market/category/time bucket.
- `pmresearch/signals/features.py` — pre-entry feature extraction.
- `pmresearch/signals/hypotheses.py` — candidate signal definitions.
- `pmresearch/signals/score.py` — scores signal hypotheses by lift, PnL, frequency, sample size, and robustness.
- `pmresearch/signals/report.py` — summarizes signal evidence and blind spots.
- `pmresearch/cli/signals.py`

**Migrations:**

- `m0019_signal_reconstruction`
  - `signal_hypotheses`
    - id
    - wallet
    - scope
    - signal_name
    - signal_version
    - description
    - sample_size
    - baseline_size
    - entry_count
    - win_rate nullable
    - avg_pnl nullable
    - median_pnl nullable
    - total_pnl nullable
    - pnl_per_dollar nullable
    - avg_hold_duration_s nullable
    - entry_size_median nullable
    - entry_size_p90 nullable
    - lift_vs_baseline nullable
    - confidence_score
    - robustness_score
    - evidence_json
    - blind_spots
    - computed_at
  - `signal_instances`
    - id
    - wallet
    - signal_hypothesis_id
    - episode_id
    - token_id
    - condition_id
    - entry_ts
    - matched_features_json
    - realized_pnl nullable
    - outcome_status open/flat/resolution
    - trust_status
    - known_exception_flag

**CLI:**

- `pmr signals compute [--wallet]`
- `pmr signals show --wallet <addr> [--scope category|event|all]`
- `pmr signals explain --wallet <addr> --signal <name>`
- `pmr signals instances --wallet <addr> --signal <name> [--limit N]`

**Candidate signal families:**

- `cheap_outcome_accumulation`
  - repeated entries in low price buckets such as 0.001–0.05 or 0.05–0.15.
- `near_event_entry`
  - entries concentrated shortly before event start or market close.
- `early_event_positioning`
  - entries far before event start with long hold duration.
- `scale_in_after_price_move`
  - adds after adverse or favorable price movement.
- `spread_capture_entry`
  - entries when spread is wide and maker/taker suggests liquidity provision.
- `rebate_or_maker_entry`
  - entry associated with maker role/rebate-eligible behavior.
- `bond_inventory_setup`
  - entries that later become MERGE/bond inventory.
- `category_specialist_edge`
  - signal limited to specific category/league/market type.
- `resolution_hold_edge`
  - entries held to resolution with positive expected/realized payoff after Phase 8.
- `stale_market_entry`
  - entries where mark/book context suggests stale pricing.
- `timing_skew_sensitive`
  - explicitly marks signals unreliable if local/reconcile data is stale.

**Tests:**

- Synthetic entry dataset where one signal has clear positive lift → signal score high.
- Synthetic no-signal random dataset → low confidence, no false strong signal.
- Missing book data → spread signals become NULL/insufficient data, not zero.
- Missing maker/taker enrichment → maker signals become NULL/insufficient data.
- Sample size below threshold → low confidence even if PnL high.
- Baseline construction excludes the entry itself.
- Signal instance rows link back to episode_id.
- Known untrusted wallet exception propagates into signal evidence.
- Deterministic recompute.

**Manual verification:**

- Run:
  - `pmr signals compute --wallet <RN1>`
  - `pmr signals show --wallet <RN1>`
  - `pmr signals explain --wallet <RN1> --signal <top_signal>`
- Inspect top 5 signals.
- For each top signal, show:
  - sample size,
  - average entry size,
  - median entry price,
  - timing distribution,
  - PnL contribution,
  - blind spots.
- Hand-check 5 signal instances against entry patterns and raw events.

**Acceptance criteria:**

- RN1 has ranked signal hypotheses with evidence, confidence, and blind spots.
- Every signal includes sample size and baseline size.
- No signal is presented as certainty.
- Signals with missing enrichment/book/mark data are explicitly marked insufficient, not scored as zero.
- At least one signal explains:
  - where RN1 enters,
  - when RN1 enters,
  - how much RN1 typically commits,
  - what pre-entry conditions are present.
- Signals read from projections and entry patterns only; no raw ad-hoc report logic.
- Outputs carry wallet trust caveat.

**Common failure modes:**

- Overfitting to high-PnL outliers.
- Treating correlation as intent.
- Ranking a signal highly with tiny sample size.
- Using post-entry information as if it were pre-entry signal.
- Ignoring staleness of marks/book snapshots.
- Treating unresolved episodes as wins/losses before Phase 8/9 can value them properly.
- Comparing RN1 entries to no baseline, which makes every behavior look meaningful.

**Prompt:**

`Implement Phase 19 of IMPLEMENTATION_PLAN.md exactly as scoped. This phase reconstructs candidate entry signals from entry patterns and pre-entry context. Do not output copy rules yet and do not claim certainty. Every signal must include sample size, baseline size, evidence_json, confidence score, robustness score, and blind spots. Show RN1's top signal hypotheses, explain the top 5, and hand-check 5 signal instances against the underlying entry events. Commit when acceptance criteria pass.`

---

## Phase 20 — Replication Candidates: turn signal hypotheses into testable candidate rules

**Goal:** convert the strongest RN1 signal hypotheses into conservative, testable replication candidates. This phase answers: what would we try to replicate, with how much size, under what entry conditions, and what risk limits.

**Scope:** generate candidate rules from Phase 19 signals. Backtest them on historical RN1 data as observational rules, not as proof of future profitability. Rank candidates by evidence, sample size, PnL, drawdown proxy, liquidity feasibility, complexity, and data-quality risk. No live trading, no copy bot integration, no automatic execution.

**Core questions answered:**

- Which RN1 patterns are potentially replicable?
- What exact entry conditions define the candidate?
- How much does RN1 usually enter with under this pattern?
- What sizing rule would approximate RN1 conservatively?
- What exit behavior is associated with the pattern?
- What risk limits are needed?
- What evidence supports or weakens the candidate?
- What data gaps make the candidate unsafe?

**Files/modules:**

- `pmresearch/replication/candidates.py` — converts signal hypotheses into rule candidates.
- `pmresearch/replication/rules.py` — transparent candidate rule schema and matching logic.
- `pmresearch/replication/backtest.py` — observational historical matching over episodes/signals.
- `pmresearch/replication/risk.py` — drawdown proxy, exposure caps, liquidity feasibility, stale-data flags.
- `pmresearch/replication/report.py` — human-readable candidate cards.
- `pmresearch/cli/replicate.py`

**Migrations:**

- `m0020_replication_candidates`
  - `replication_candidates`
    - id
    - wallet
    - candidate_name
    - candidate_version
    - source_signal_id
    - scope
    - rule_json
    - sizing_json
    - exit_behavior_json
    - risk_limits_json
    - sample_size
    - matched_episode_count
    - total_entry_usdc
    - median_entry_usdc
    - p90_entry_usdc
    - median_first_entry_usdc
    - p90_first_entry_usdc
    - median_entry_price
    - entry_price_range_json
    - median_hold_duration_s nullable
    - total_pnl nullable
    - avg_pnl nullable
    - pnl_per_dollar nullable
    - win_rate nullable
    - max_loss_episode nullable
    - drawdown_proxy nullable
    - liquidity_feasibility_score nullable
    - confidence_score
    - risk_score
    - replication_score
    - evidence_json
    - blind_spots
    - computed_at
  - `replication_candidate_matches`
    - id
    - candidate_id
    - episode_id
    - token_id
    - condition_id
    - entry_ts
    - entry_usdc
    - entry_price
    - realized_pnl nullable
    - match_features_json
    - excluded_reason nullable

**CLI:**

- `pmr replicate generate --wallet <addr>`
- `pmr replicate show --wallet <addr> [--top N]`
- `pmr replicate explain --wallet <addr> --candidate <id|name>`
- `pmr replicate matches --candidate <id> [--limit N]`
- `pmr replicate export --wallet <addr> --out /data/exports/rn1_replication_candidates.md`

**Candidate rule schema:**

Each candidate must include:

- `where_to_enter`
  - categories
  - market structures
  - event/market filters
  - outcome filters only if label-based logic is explicitly justified; otherwise avoid hardcoding team/outcome names.
- `when_to_enter`
  - time-to-event range
  - time-to-close range
  - allowed hours/days if statistically supported.
- `price_conditions`
  - entry price range
  - price bucket
  - mark/book constraints if available.
- `size_conditions`
  - median RN1 first-entry size
  - median RN1 total-entry size
  - conservative suggested size
  - max size cap
  - liquidity/depth cap if book data exists.
- `execution_conditions`
  - maker/taker preference if enrichment supports it.
  - spread/depth requirement if book data supports it.
- `exit_behavior`
  - hold-to-resolution
  - partial exits
  - scale-in
  - MERGE/bond handling
  - stop condition if inferable.
- `risk_limits`
  - max per-market exposure
  - max per-event exposure
  - max category concentration
  - max stale-data exposure
  - minimum sample size requirement.
- `do_not_trade_when`
  - missing marks/book/enrichment if candidate depends on them.
  - unresolved trust issue materially affects candidate scope.
  - data staleness above threshold.

**Tests:**

- Candidate generation from strong synthetic signal.
- No candidate generated from low-confidence signal.
- Candidate sizing uses conservative percentile, not RN1 max outlier.
- Candidate includes first-entry size and total-entry size separately.
- Candidate excludes episodes with known data-quality exceptions unless configured.
- Backtest counts matched episodes correctly.
- Risk score worsens with low sample size, high concentration, high drawdown proxy, or stale data.
- Exported Markdown contains all required sections.
- No candidate has missing evidence_json or blind_spots.
- Deterministic recompute.

**Manual verification:**

- Run:
  - `pmr replicate generate --wallet <RN1>`
  - `pmr replicate show --wallet <RN1> --top 10`
  - `pmr replicate explain --wallet <RN1> --candidate <top_candidate>`
  - `pmr replicate export --wallet <RN1> --out /data/exports/rn1_replication_candidates.md`
- For the top 3 candidates, verify:
  - rule conditions match Phase 19 signal evidence,
  - suggested sizing is conservative versus RN1 historical sizing,
  - sample size is sufficient,
  - PnL is not dominated by one outlier,
  - data-quality caveats are visible.

**Acceptance criteria:**

- At least 3 replication candidates generated for RN1, unless insufficient evidence is explicitly reported.
- Each candidate answers:
  - where to enter,
  - when to enter,
  - with how much size,
  - at what price,
  - how to execute,
  - how to manage/exit,
  - what risks invalidate the candidate.
- Each candidate has:
  - machine-readable rule_json,
  - sizing_json,
  - risk_limits_json,
  - evidence_json,
  - blind_spots.
- No live trading integration.
- No candidate is presented as guaranteed profitable.
- Candidates carry wallet trust caveat and data-quality caveats.
- Candidate reports are exportable to `/data/exports/`.

**Common failure modes:**

- Turning weak correlations into rules.
- Using RN1's maximum position size instead of conservative sizing.
- Ignoring liquidity/depth.
- Ignoring category/event concentration risk.
- Producing rules that depend on unavailable real-time features.
- Hiding that the backtest is observational and not a true out-of-sample strategy test.
- Letting one huge winner dominate the replication score.

**Prompt:**

`Implement Phase 20 of IMPLEMENTATION_PLAN.md exactly as scoped. Generate conservative replication candidates from Phase 19 signals. No live trading, no copy bot integration, no guaranteed-profit language. Each candidate must clearly state where to enter, when to enter, with how much size, at what price, how to execute, how to manage/exit, and what data/risk caveats apply. Show RN1's top replication candidates and export the Markdown report. Commit when acceptance criteria pass.`

---