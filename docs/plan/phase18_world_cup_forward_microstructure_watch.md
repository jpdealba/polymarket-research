# Phase 18 — World Cup Forward Microstructure Watch

## Goal

Build a forward-looking collector and Streamlit dashboard for World Cup markets where RN1 is likely to operate.

This phase does **not** try to reconstruct historical order-book context. That is not possible when the book was not sampled before the fill. Instead, Phase 18 starts collecting live order-book snapshots, wallet activity, and maker-fill context from now onward, focused on active World Cup markets.

The objective is to answer:

> When RN1 gets maker fills in World Cup markets, what did the book look like shortly before and shortly after the fill?

This phase is explicitly **forward-only**.

---

## Motivation

Previous phases showed that RN1 is:

- Sports-heavy.
- Maker-heavy.
- Inventory-cycling biased.
- Strongly exposed to sports markets.
- Not explainable as pure value betting, pure rewards farming, or pure directional betting.

However, maker-fill behavior cannot be evaluated from historical trades alone because historical books are not available unless they were sampled at the time.

Existing `book_snapshots` only help if they were captured near the trade timestamp. If RN1 opened a position at 03:00–04:00 and the first book snapshot was taken at 07:00, that book is not valid entry-context evidence.

Therefore, Phase 18 creates a live World Cup watch system.

---

## Non-goals

This phase does **not**:

- Identify RN1's currently open limit orders directly.
- Attribute public book liquidity levels to RN1.
- Reconstruct historical books before the sampler existed.
- Trade automatically.
- Assume that every large bid/ask belongs to RN1.
- Use future book snapshots to explain past fills.

This phase only captures and aligns:

```text
book before fill
fill
book after fill
```

when the timing actually overlaps.

---

## Core invariant

A book snapshot is valid fill context only if it is close enough to the fill timestamp.

Default thresholds:

```text
excellent: <= 5 seconds
good:      <= 15 seconds
usable:    <= 30 seconds
weak:      <= 60 seconds
stale:     > 60 seconds
missing:   no book before fill
```

Interpretation:

```text
excellent/good = useful for maker-entry analysis
usable         = acceptable but should be marked with caution
weak           = visible but not strong enough for firm conclusions
stale/missing  = insufficient evidence
```

No detector, report, or dashboard card may treat stale or missing book context as valid evidence.

If no valid book exists, return:

```text
NULL with reason: no_book_snapshot_within_max_age
```

Recommended default:

```text
PMR_WORLDCUP_CONTEXT_MAX_AGE_S=60
```

For stricter analysis, use:

```text
PMR_WORLDCUP_CONTEXT_MAX_AGE_S=15
```

---

## Data model

Add migration:

```text
m0018_world_cup_watch
```

### `watchlists`

Stores named token watchlists.

Columns:

```text
id INTEGER PRIMARY KEY
name TEXT NOT NULL UNIQUE
description TEXT
created_at INTEGER NOT NULL
updated_at INTEGER NOT NULL
is_active INTEGER NOT NULL DEFAULT 1
```

Example:

```text
name = world_cup_2026
```

---

### `watchlist_tokens`

Stores tokens selected for active sampling.

Columns:

```text
watchlist_id INTEGER NOT NULL
token_id TEXT NOT NULL
condition_id TEXT
market_id TEXT
question TEXT
outcome_label TEXT
market_category TEXT
market_slug TEXT
source TEXT NOT NULL
priority INTEGER NOT NULL DEFAULT 100
reason TEXT
first_seen_ts INTEGER NOT NULL
last_seen_ts INTEGER NOT NULL
is_active INTEGER NOT NULL DEFAULT 1

PRIMARY KEY (watchlist_id, token_id)
```

Allowed `source` values:

```text
rn1_recent_trade
rn1_open_holding
world_cup_keyword
world_cup_market_type
manual
```

Allowed `reason` examples:

```text
rn1 traded this token recently
rn1 has nonzero local holding
question contains world cup keyword
question contains team name
manual add
```

---

### `book_sample_runs`

Groups one sampling cycle.

Columns:

```text
id INTEGER PRIMARY KEY
watchlist_id INTEGER NOT NULL
started_at INTEGER NOT NULL
finished_at INTEGER
selector_wallet_latest_event_ts INTEGER
selector_wallet_latest_event_utc TEXT
tokens_selected INTEGER NOT NULL DEFAULT 0
tokens_sampled INTEGER NOT NULL DEFAULT 0
books_found INTEGER NOT NULL DEFAULT 0
books_empty INTEGER NOT NULL DEFAULT 0
errors INTEGER NOT NULL DEFAULT 0
status TEXT NOT NULL
```

This fixes the ambiguity from Phase 12, where old and new snapshots were mixed without knowing which selector state created them.

---

### Update `book_snapshots`

Add nullable columns:

```text
sample_run_id INTEGER
watchlist_id INTEGER
selector_reason TEXT
```

If modifying the existing table is too risky, create a companion table:

```text
book_snapshot_context
```

with:

```text
token_id TEXT
ts INTEGER
sample_run_id INTEGER
watchlist_id INTEGER
selector_reason TEXT

PRIMARY KEY (token_id, ts)
```

---

### `maker_fill_context`

Aligns RN1 maker fills with nearest valid books.

Columns:

```text
event_id INTEGER PRIMARY KEY
wallet TEXT NOT NULL
token_id TEXT NOT NULL
condition_id TEXT
trade_ts INTEGER NOT NULL
trade_utc TEXT NOT NULL
side TEXT
fill_price TEXT
fill_size TEXT
delta_usdc TEXT
role TEXT NOT NULL

book_before_ts INTEGER
book_before_age_s INTEGER
best_bid_before TEXT
best_ask_before TEXT
spread_before TEXT
mid_before TEXT
depth_top_before_json TEXT

book_after_ts INTEGER
book_after_age_s INTEGER
best_bid_after TEXT
best_ask_after TEXT
spread_after TEXT
mid_after TEXT
depth_top_after_json TEXT

context_status TEXT NOT NULL
null_reason TEXT

created_at INTEGER NOT NULL
updated_at INTEGER NOT NULL
```

Allowed `context_status` values:

```text
excellent
good
usable
weak
stale
missing
```

Rules:

```text
excellent = before book age <= 5s
good      = before book age <= 15s
usable    = before book age <= 30s
weak      = before book age <= 60s
stale     = before book exists but age > 60s
missing   = no book before fill
```

If `context_status IN ('weak', 'stale', 'missing')`, the dashboard must show the row with a warning.

If `context_status IN ('stale', 'missing')`, the row must be treated as insufficient evidence.

---

## Market selection

Create module:

```text
pmresearch/watchlists/world_cup.py
```

Responsibilities:

1. Find World Cup-related markets in local `markets`.
2. Add RN1-relevant World Cup tokens.
3. Add manually selected tokens.
4. Prioritize tokens for sampling.

World Cup matching should be conservative.

Initial keyword list:

```text
World Cup
FIFA
Canada
Morocco
Paraguay
France
Brazil
Norway
Mexico
England
Portugal
Spain
United States
Belgium
Argentina
Egypt
Switzerland
Colombia
Quarterfinal
Semifinal
Final
Team to Advance
O/U
Over
Under
Spread
Exact Score
```

Priority rules:

```text
priority 10: RN1 recent trade + World Cup market
priority 20: RN1 open holding + World Cup market
priority 30: active World Cup match market
priority 40: active World Cup derivative market
priority 90: manual watchlist token
```

---

## Collector behavior

Add CLI commands:

```powershell
pmr watchlist build-world-cup --wallet $WALLET
pmr watchlist show --name world_cup_2026
pmr watchlist add-token --name world_cup_2026 --token-id <TOKEN_ID> --reason "manual"
pmr watchlist deactivate-token --name world_cup_2026 --token-id <TOKEN_ID>
```

Add sampling command:

```powershell
pmr books sample-watchlist --name world_cup_2026 --limit 200
```

Add context builder:

```powershell
pmr context maker-fills --wallet $WALLET --watchlist world_cup_2026 --max-age-s 60
```

Add one-shot full cycle:

```powershell
pmr worldcup tick --wallet $WALLET
```

`pmr worldcup tick` should run:

```text
1. sync incremental wallet activity
2. ingest wallet events
3. replay holdings
4. build/update World Cup watchlist
5. sample books for watchlist tokens
6. enrich fills if possible
7. build maker_fill_context
8. write run metrics
```

---

## Scheduler integration

Add scheduled job support.

Default intervals:

```text
wallet sync:        60s
watchlist rebuild:  300s
book sampling:      5s–15s for priority <= 20
book sampling:      30s–60s for lower-priority tokens
maker context:      30s–60s
prune:              daily
```

Recommended environment variables:

```text
PMR_WORLDCUP_WATCH_ENABLED=true
PMR_WORLDCUP_WATCHLIST_NAME=world_cup_2026
PMR_WORLDCUP_BOOK_INTERVAL_S=10
PMR_WORLDCUP_FAST_BOOK_INTERVAL_S=5
PMR_WORLDCUP_SYNC_INTERVAL_S=60
PMR_WORLDCUP_CONTEXT_MAX_AGE_S=60
PMR_WORLDCUP_STRICT_CONTEXT_MAX_AGE_S=15
PMR_WORLDCUP_SAMPLE_LIMIT=200
```

Important:

- Streamlit must not be responsible for running the collector.
- The collector should run through `pmr run` or `pmr worldcup watch`.
- Streamlit only reads the DB and visualizes status.

Add command:

```powershell
pmr worldcup watch --wallet $WALLET
```

This should run continuously.

---

## Streamlit dashboard

Add a visible World Cup page or tab:

```text
pages/World_Cup_Watch.py
```

or section:

```text
World Cup Microstructure Watch
```

The dashboard must show the sections below.

---

### 1. Collector status

Cards:

```text
collector status
last wallet sync
latest wallet event UTC
last watchlist rebuild
last book sample run
last maker context build
book sample interval
active watchlist tokens
```

Warn if:

```text
last book sample > 2x configured interval
last wallet sync > 2x configured interval
latest book snapshot is older than 2 minutes
```

Hard warning if:

```text
latest book snapshot is older than 5 minutes
```

---

### 2. Watchlist table

Columns:

```text
priority
token_id
question
outcome_label
source
reason
last_seen_ts
is_active
latest best_bid
latest best_ask
latest spread
latest mid
latest book age seconds
```

Filters:

```text
source
priority
team / keyword
active only
spread > X
has RN1 recent trade
has RN1 holding
```

---

### 3. Live books view

For selected token:

```text
best_bid
best_ask
spread
mid
top 10 bids
top 10 asks
latest snapshot age
snapshot history chart
spread history chart
```

Show book age badge:

```text
fresh: <= 15s
ok:    <= 60s
stale: > 60s
```

---

### 4. RN1 recent World Cup fills

Table:

```text
trade_utc
token_id
question
outcome
side
fill_price
fill_size
role
book_before_age_s
spread_before
mid_before
context_status
```

Highlight statuses:

```text
excellent = strong evidence
good      = useful evidence
usable    = cautious evidence
weak      = weak evidence
stale     = insufficient evidence
missing   = insufficient evidence
```

---

### 5. Maker fill context detail

For one fill:

```text
fill timestamp
fill price
role
book before timestamp
book before age
best_bid_before
best_ask_before
spread_before
mid_before
book after timestamp
book after age
best_bid_after
best_ask_after
spread_after
mid_after
```

Add interpretation fields:

```text
fill_vs_mid_before
fill_vs_best_bid_before
fill_vs_best_ask_before
spread_capture_estimate
```

These are presentation calculations only. If added to DB, they must be marked as derived.

---

### 6. Data quality panel

Show:

```text
maker fills total
maker fills with excellent context
maker fills with good context
maker fills with usable context
maker fills with weak context
maker fills with stale/missing context
strict coverage %: excellent + good
loose coverage %: excellent + good + usable
```

Example:

```text
Strict maker-context coverage: 18 / 42 fills = 42.9%
Loose maker-context coverage: 27 / 42 fills = 64.3%
```

No strategy conclusion should be shown if strict coverage is low.

---

## Validity rules

For every maker fill:

```text
if no book_before_ts:
    context_status = missing
    null_reason = no_book_before_fill

elif trade_ts - book_before_ts > 60:
    context_status = stale
    null_reason = book_before_too_old

elif trade_ts - book_before_ts <= 5:
    context_status = excellent

elif trade_ts - book_before_ts <= 15:
    context_status = good

elif trade_ts - book_before_ts <= 30:
    context_status = usable

else:
    context_status = weak
```

Never use book snapshots after the trade as the primary explanation for entry.

`book_after` is only for post-fill impact analysis.

---

## Reports

Add optional report section to Phase 15 wallet profile:

```text
World Cup Forward Watch
```

Only render this section if Phase 18 tables exist.

Include:

```text
active watchlist tokens
latest sample time
maker fills with excellent/good context
strict coverage %
loose coverage %
top markets by maker fills
average spread before valid maker fills
```

If insufficient:

```text
Insufficient forward book context. The collector has not been running long enough before RN1 fills.
```

---

## Tests

Add tests for:

1. Watchlist build detects World Cup markets by keyword.
2. RN1 recent trade gets higher priority than keyword-only market.
3. Manual token add is idempotent.
4. `book_sample_runs` groups snapshots correctly.
5. `maker_fill_context` returns missing when no book exists.
6. `maker_fill_context` returns stale when book is older than `max_age_s`.
7. `maker_fill_context` returns weak when book age is 31–60s.
8. `maker_fill_context` returns usable when book age is 16–30s.
9. `maker_fill_context` returns good when book age is 6–15s.
10. `maker_fill_context` returns excellent when book age is <= 5s.
11. Book after fill is never used as entry context.
12. Streamlit data fetch functions return collector status.
13. Dashboard gracefully handles no collector data.
14. Dashboard flags stale collector status.
15. Report section degrades to insufficient data when coverage is low.

---

## Acceptance criteria

Phase 18 is accepted when:

- `pmr worldcup tick --wallet $WALLET` runs successfully.
- `pmr worldcup watch --wallet $WALLET` can run continuously.
- `book_sample_runs` records each run with selector state.
- `watchlist_tokens` contains active World Cup tokens.
- `book_snapshots` are linked to sample runs.
- `maker_fill_context` is built only when book snapshots are time-valid.
- Streamlit shows a visible World Cup Watch page.
- Streamlit displays collector freshness, watchlist tokens, latest books, RN1 fills, and maker context coverage.
- No historical maker-fill claim is made from books sampled after the trade.
- Tests pass.

---

## Evidence to save

```powershell
mkdir -Force docs/evidence/phase18

pmr watchlist build-world-cup --wallet $WALLET > docs/evidence/phase18/worldcup_watchlist_build.txt
pmr watchlist show --name world_cup_2026 > docs/evidence/phase18/worldcup_watchlist_show.txt
pmr worldcup tick --wallet $WALLET > docs/evidence/phase18/worldcup_tick.txt
pmr books status > docs/evidence/phase18/books_status_after_worldcup_tick.txt
pmr context maker-fills --wallet $WALLET --watchlist world_cup_2026 --max-age-s 60 > docs/evidence/phase18/maker_fill_context_build.txt
```

Streamlit evidence:

```text
docs/evidence/phase18/streamlit_worldcup_watch_screenshot.png
```

---

## Expected interpretation

At first, coverage may be low:

```text
valid maker context = 0
```

That is not a failure if the collector just started.

The goal is to accumulate forward data. Once RN1 trades again in watched World Cup markets, the system should begin producing valid maker-fill context.

The first useful claim should look like:

> RN1 received a maker fill in a World Cup market while the book snapshot from 4 seconds before showed spread X, mid Y, best bid Z, and top-of-book depth W.

Not:

> RN1 had a position and the current book looks like X.

---

## Status

Initial status after implementation should be:

```text
Phase 18: accepted_for_forward_collection
```

Only after enough valid RN1 fills occur should it become:

```text
Phase 18: accepted_for_RN1_microstructure_analysis
```
