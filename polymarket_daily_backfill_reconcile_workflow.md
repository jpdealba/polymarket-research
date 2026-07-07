# Daily workflow — backfill, reconcile, microstructure dataset, and manual event analysis

*Last updated: 2026-07-06*

## 0. Goal

This workflow is for the daily Polymarket research loop:

1. Bring wallet ledgers up to date.
2. Backfill missing wallet events when reconciliation says `local_open_episode_missing`.
3. Sync market/event metadata.
4. Enrich fills with RPC/orderbook context.
5. Build the microstructure lifecycle dataset.
6. Reconcile wallet holdings and WAC where possible.
7. List clean events/partidos for manual RN1 vs Gap analysis.

Important: a global reconciliation can be `untrusted` even when a specific event/partido is good enough for manual analysis. Do not block manual analysis only because unrelated open positions outside the watchlist are missing.

---



## 1. Set variables

Run this at the start of the terminal session:

```powershell
$RN1 = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
$GAP = "0x83255595ba1fadd2e734cb30a0fb8110301a19cc"
$WATCH = "world_cup_2026"
$OUT = "exports\daily_$(Get-Date -Format yyyyMMdd_HHmmss)"
New-Item -ItemType Directory -Force $OUT | Out-Null
```

Use the correct watchlist name for the day. For World Cup-style event analysis, keep `world_cup_2026`. For Wimbledon/MLB/other categories, use the watchlist you created for those markets.

---



## 2. Fast daily update

Use this when you only need the latest wallet events and the previous reconciliation was not badly broken.

```powershell
pmr sync incremental $RN1
pmr sync incremental $GAP

pmr markets sync

pmr dataset microstructure build --wallet $RN1 --watchlist $WATCH --min-context usable
pmr dataset microstructure build --wallet $GAP --watchlist $WATCH --min-context usable
```

Expected output from dataset build should look like:

```text
context_source=all_fills fills_seen=... rows_written=...
by_context_status={'good': ..., 'usable': ..., 'excellent': ...}
```

If `rows_written` is healthy, you can run the clean-event SQL query in section 8.

---



## 3. Full backfill when reconciliation is untrusted

Run this when `pmr reconcile run` shows something like:

```text
trust=untrusted reason=fail: local_open_episode_missing=69
remote_positions=9221 local_nonzero_holdings=76
```

That means the remote `/positions` endpoint sees open positions for tokens where your local ledger has no current open episode. This usually means you need more wallet history or missing events, not just another reconciliation run.

Try this first:

```powershell
pmr ingest wallet --wallet $RN1 --full
pmr ingest wallet --wallet $GAP --full
```

If that command does not exist in the CLI, use:

```powershell
pmr ingest run --wallet $RN1 --full
pmr ingest run --wallet $GAP --full
```

If `--full` does not exist either, run the available wallet ingest command and check `pmr --help` / `pmr ingest --help` for the exact full/backfill flag.

After full ingest/backfill:

```powershell
pmr markets sync
pmr reconcile run --wallet $RN1
pmr reconcile run --wallet $GAP
```

Then rebuild the microstructure datasets:

```powershell
pmr dataset microstructure build --wallet $RN1 --watchlist $WATCH --min-context usable
pmr dataset microstructure build --wallet $GAP --watchlist $WATCH --min-context usable
```

---



## 4. Optional: derive / project ledger state

Some versions of the repo may have a derive/projection step. If available, run it before reconciliation:

```powershell
pmr derive run --wallet $RN1
pmr derive run --wallet $GAP

pmr reconcile run --wallet $RN1
pmr reconcile run --wallet $GAP
```

If `pmr derive run` does not exist, skip this section.

The key point: reconciliation compares remote open positions against your local projected holdings/WAC. If the projection has not been rebuilt after ingest, reconciliation can remain stale.

---



## 5. Enrich context / fills

Use this when you need fresh book/microstructure context for new fills.

```powershell
pmr context fills --wallet $RN1 --watchlist $WATCH --max-age-s 60
pmr context fills --wallet $GAP --watchlist $WATCH --max-age-s 60
```

If your CLI uses the older name:

```powershell
pmr context maker-fills --wallet $RN1 --watchlist $WATCH --max-age-s 60
pmr context maker-fills --wallet $GAP --watchlist $WATCH --max-age-s 60
```

Then rebuild:

```powershell
pmr dataset microstructure build --wallet $RN1 --watchlist $WATCH --min-context usable
pmr dataset microstructure build --wallet $GAP --watchlist $WATCH --min-context usable
```

---



## 6. How to read reconciliation output



### Good enough / trusted

A good reconciliation should have low or no hard failures, and local holdings should match remote positions for current tokens.

### Common untrusted case

```text
trust=untrusted reason=fail: local_open_episode_missing=69
```

Meaning:

- Remote `/positions` reports open positions.
- Local ledger/projection has no open episode for those tokens.
- WAC and realized PnL checks fail because local cost basis is missing.

This is usually caused by missing historical wallet events, incomplete backfill, or projection state not rebuilt.

### Not blocking for manual event analysis

If your manual analysis only needs a specific event/partido, global reconciliation may be untrusted due to unrelated tokens from other sports/events. For manual analysis, prioritize:

- fills exist for the event,
- book context exists,
- real `MERGE`/`REDEEM` events exist in `wallet_events`,
- market metadata maps condition_id to event_id correctly.

---



## 7. Rebuild and verify daily datasets

After ingest/backfill/reconcile, always run:

```powershell
pmr dataset microstructure build --wallet $RN1 --watchlist $WATCH --min-context usable
pmr dataset microstructure build --wallet $GAP --watchlist $WATCH --min-context usable
```

Healthy example:

```text
context_source=all_fills fills_seen=11550 rows_written=11550
by_context_status={'good': 3135, 'usable': 1731, 'excellent': 6684}
by_close_path={'UNKNOWN': 11550}
```

`by_close_path={'UNKNOWN': ...}` is not ideal, but it does not block manual sequence analysis. It means the close-path field in the microstructure dataset is not populated. You can still count real `MERGE`/`REDEEM` from `wallet_events`.

---



## 8. SQL: list clean events/partidos for manual analysis



(+10 min query)

Run this after the dataset build. It uses:

- `microstructure_lifecycle_dataset` for fills and context,
- `wallet_events` for real MERGE/REDEEM actions,
- `markets` and `pm_events` for event names.

```sql
WITH params AS (
  SELECT
    '0x2005d16a84ceefa912d4e380cd32e7ff827875ea' AS rn1,
    '0x83255595ba1fadd2e734cb30a0fb8110301a19cc' AS gap
),

fills AS (
  SELECT
    m.event_id AS pm_event_id,
    pe.title AS event_title,
    ml.wallet,
    ml.condition_id,
    ml.token_id,
    ml.trade_ts,
    ml.role,
    CAST(COALESCE(NULLIF(ml.realized_pnl_wac,''),'0') AS REAL) AS realized_pnl_wac
  FROM microstructure_lifecycle_dataset ml
  LEFT JOIN markets m
    ON m.condition_id = ml.condition_id
  LEFT JOIN pm_events pe
    ON CAST(pe.event_id AS TEXT) = CAST(m.event_id AS TEXT)
  WHERE lower(ml.wallet) IN (
    lower((SELECT rn1 FROM params)),
    lower((SELECT gap FROM params))
  )
    AND m.event_id IS NOT NULL
),

actions AS (
  SELECT
    m.event_id AS pm_event_id,
    we.wallet,
    we.event_type,
    we.ts
  FROM wallet_events we
  LEFT JOIN markets m
    ON m.condition_id = we.condition_id
  WHERE lower(we.wallet) IN (
    lower((SELECT rn1 FROM params)),
    lower((SELECT gap FROM params))
  )
    AND m.event_id IS NOT NULL
),

fill_stats AS (
  SELECT
    pm_event_id,
    COALESCE(event_title, '(missing title)') AS event_title,

    datetime(MIN(trade_ts), 'unixepoch') AS first_fill_utc,
    datetime(MAX(trade_ts), 'unixepoch') AS last_fill_utc,

    COUNT(*) AS fills,
    COUNT(DISTINCT condition_id) AS markets,

    SUM(CASE WHEN lower(wallet) = lower((SELECT rn1 FROM params)) THEN 1 ELSE 0 END) AS rn1_fills,
    COUNT(DISTINCT CASE WHEN lower(wallet) = lower((SELECT rn1 FROM params)) THEN condition_id END) AS rn1_markets,

    SUM(CASE WHEN lower(wallet) = lower((SELECT gap FROM params)) THEN 1 ELSE 0 END) AS gap_fills,
    COUNT(DISTINCT CASE WHEN lower(wallet) = lower((SELECT gap FROM params)) THEN condition_id END) AS gap_markets,

    ROUND(100.0 * SUM(CASE WHEN role = 'maker' THEN 1 ELSE 0 END) / COUNT(*), 1) AS maker_pct,
    ROUND(SUM(realized_pnl_wac), 2) AS pnl_wac
  FROM fills
  GROUP BY pm_event_id, event_title
),

action_stats AS (
  SELECT
    pm_event_id,

    SUM(CASE WHEN event_type = 'MERGE' THEN 1 ELSE 0 END) AS real_merges,
    SUM(CASE WHEN event_type IN ('REDEEM','REDEEM_PAYOUT','RESOLUTION_SETTLEMENT') THEN 1 ELSE 0 END) AS real_redeems,

    SUM(CASE WHEN lower(wallet) = lower((SELECT rn1 FROM params)) AND event_type = 'MERGE' THEN 1 ELSE 0 END) AS rn1_merges,
    SUM(CASE WHEN lower(wallet) = lower((SELECT gap FROM params)) AND event_type = 'MERGE' THEN 1 ELSE 0 END) AS gap_merges,

    SUM(CASE WHEN lower(wallet) = lower((SELECT rn1 FROM params)) AND event_type IN ('REDEEM','REDEEM_PAYOUT','RESOLUTION_SETTLEMENT') THEN 1 ELSE 0 END) AS rn1_redeems,
    SUM(CASE WHEN lower(wallet) = lower((SELECT gap FROM params)) AND event_type IN ('REDEEM','REDEEM_PAYOUT','RESOLUTION_SETTLEMENT') THEN 1 ELSE 0 END) AS gap_redeems
  FROM actions
  GROUP BY pm_event_id
)

SELECT
  fs.pm_event_id,
  fs.event_title,
  fs.first_fill_utc,
  fs.last_fill_utc,

  fs.fills,
  fs.markets,

  fs.rn1_fills,
  fs.rn1_markets,
  fs.gap_fills,
  fs.gap_markets,

  fs.maker_pct,

  COALESCE(a.real_merges, 0) AS real_merges,
  COALESCE(a.real_redeems, 0) AS real_redeems,

  COALESCE(a.rn1_merges, 0) AS rn1_merges,
  COALESCE(a.gap_merges, 0) AS gap_merges,
  COALESCE(a.rn1_redeems, 0) AS rn1_redeems,
  COALESCE(a.gap_redeems, 0) AS gap_redeems,

  fs.pnl_wac,

  (
    fs.fills
    + fs.markets * 30
    + COALESCE(a.real_merges, 0) * 20
    + COALESCE(a.real_redeems, 0) * 10
  ) AS clean_score

FROM fill_stats fs
LEFT JOIN action_stats a
  ON a.pm_event_id = fs.pm_event_id

WHERE fs.fills >= 50

ORDER BY clean_score DESC, fs.fills DESC
LIMIT 20;
```

Use this table to choose events for manual analysis.

Recommended selection rules:

```text
RN1 manual event:
- rn1_fills high
- rn1_markets >= 3, ideally >= 5
- maker_pct high
- real_merges or real_redeems > 0 if possible

Gap manual event:
- gap_fills high
- gap_markets >= 3
- same event as RN1 if possible

Best manual candidates:
- same event has both RN1 and Gap
- enough markets to compare behavior
- not too many markets if doing by hand
```

---



## 9. SQL: list markets inside one event

Replace `654708` with the selected event ID.

```sql
WITH params AS (
  SELECT
    654708 AS target_event_id,
    '0x2005d16a84ceefa912d4e380cd32e7ff827875ea' AS rn1,
    '0x83255595ba1fadd2e734cb30a0fb8110301a19cc' AS gap
),

base AS (
  SELECT
    ml.*,
    CASE
      WHEN lower(ml.wallet) = lower((SELECT rn1 FROM params)) THEN 'RN1'
      WHEN lower(ml.wallet) = lower((SELECT gap FROM params)) THEN 'GAP'
      ELSE 'OTHER'
    END AS wallet_name,
    m.question AS market_question,
    ABS(CAST(COALESCE(NULLIF(ml.delta_usdc,''),'0') AS REAL)) AS abs_notional,
    CAST(COALESCE(NULLIF(ml.fill_price,''),'0') AS REAL) AS px,
    CAST(COALESCE(NULLIF(ml.fill_size,''),'0') AS REAL) AS qty,
    CAST(COALESCE(NULLIF(ml.book_before_age_s,''),'999999') AS INTEGER) AS book_age
  FROM microstructure_lifecycle_dataset ml
  LEFT JOIN markets m
    ON m.condition_id = ml.condition_id
  WHERE CAST(m.event_id AS TEXT) = CAST((SELECT target_event_id FROM params) AS TEXT)
    AND lower(ml.wallet) IN (
      lower((SELECT rn1 FROM params)),
      lower((SELECT gap FROM params))
    )
),

market_stats AS (
  SELECT
    condition_id,
    COALESCE(market_question, '(missing question)') AS market_question,

    datetime(MIN(trade_ts), 'unixepoch') AS first_utc,
    datetime(MAX(trade_ts), 'unixepoch') AS last_utc,

    COUNT(*) AS fills,
    COUNT(DISTINCT token_id) AS tokens,

    SUM(CASE WHEN wallet_name = 'RN1' THEN 1 ELSE 0 END) AS rn1_fills,
    SUM(CASE WHEN wallet_name = 'GAP' THEN 1 ELSE 0 END) AS gap_fills,

    ROUND(SUM(CASE WHEN wallet_name = 'RN1' THEN abs_notional ELSE 0 END), 2) AS rn1_notional,
    ROUND(SUM(CASE WHEN wallet_name = 'GAP' THEN abs_notional ELSE 0 END), 2) AS gap_notional,

    ROUND(100.0 * SUM(CASE WHEN role = 'maker' THEN 1 ELSE 0 END) / COUNT(*), 1) AS maker_pct,
    ROUND(100.0 * SUM(CASE WHEN book_age <= 15 THEN 1 ELSE 0 END) / COUNT(*), 1) AS book_le_15s_pct,

    ROUND(MIN(px), 4) AS min_price,
    ROUND(MAX(px), 4) AS max_price,
    ROUND(AVG(px), 4) AS avg_price
  FROM base
  GROUP BY condition_id, market_question
)

SELECT
  condition_id,
  market_question,
  first_utc,
  last_utc,
  tokens,
  fills,
  rn1_fills,
  rn1_notional,
  gap_fills,
  gap_notional,
  maker_pct,
  book_le_15s_pct,
  min_price,
  max_price,
  avg_price,
  (
    fills
    + maker_pct * 2
    + book_le_15s_pct * 2
    + CASE WHEN tokens >= 2 THEN 50 ELSE 0 END
  ) AS market_clean_score
FROM market_stats
WHERE fills >= 10
ORDER BY market_clean_score DESC, fills DESC
LIMIT 15;
```

Pick 3 to 10 markets for manual review. Start with 3 markets if doing by hand.

---



## 10. SQL: timeline for one market and one wallet

Replace `target_condition_id` and wallet address.

```sql
WITH params AS (
  SELECT
    '0xad9e90bf3ccd05f9c3d4a25f49109d5f860225c26faec54b52edec93c4ba4e62' AS target_condition_id,
    '0x2005d16a84ceefa912d4e380cd32e7ff827875ea' AS wallet_addr
)

SELECT
  datetime(trade_ts, 'unixepoch') AS trade_utc,
  event_id,
  condition_id,
  token_id,
  side,
  role,
  ROUND(CAST(fill_price AS REAL) * 100, 2) AS price_cents,
  ROUND(CAST(fill_size AS REAL), 4) AS qty,
  ROUND(ABS(CAST(delta_usdc AS REAL)), 4) AS notional,

  ROUND(CAST(qty_token_before AS REAL), 4) AS qty_token_before,
  ROUND(CAST(qty_complement_before AS REAL), 4) AS qty_complement_before,
  ROUND(CAST(directional_before AS REAL), 4) AS directional_before,
  ROUND(CAST(bond_before AS REAL), 4) AS bond_before,

  ROUND(CAST(qty_token_after AS REAL), 4) AS qty_token_after,
  ROUND(CAST(qty_complement_after AS REAL), 4) AS qty_complement_after,
  ROUND(CAST(directional_after AS REAL), 4) AS directional_after,
  ROUND(CAST(bond_after AS REAL), 4) AS bond_after,

  ROUND(CAST(bond_delta AS REAL), 4) AS bond_delta,
  ROUND(CAST(directional_delta AS REAL), 4) AS directional_delta,

  ROUND(CAST(best_bid_before AS REAL) * 100, 2) AS best_bid_before_cents,
  ROUND(CAST(best_ask_before AS REAL) * 100, 2) AS best_ask_before_cents,
  book_before_age_s,

  closed_by_merge,
  closed_by_redeem,
  closed_by_resolution,
  ROUND(CAST(COALESCE(NULLIF(realized_pnl_wac,''),'0') AS REAL), 4) AS realized_pnl_wac,

  CASE
    WHEN CAST(bond_delta AS REAL) > 0 THEN 'forma/completa set'
    WHEN CAST(directional_delta AS REAL) > 0 THEN 'aumenta directional'
    WHEN CAST(directional_delta AS REAL) < 0 THEN 'reduce directional'
    ELSE 'neutral'
  END AS manual_classification
FROM microstructure_lifecycle_dataset
WHERE condition_id = (SELECT target_condition_id FROM params)
  AND lower(wallet) = lower((SELECT wallet_addr FROM params))
ORDER BY trade_ts, event_id;
```

Export this to CSV and add manual columns:

```text
match_minute
manual_note
estimated_order_type
locked_edge_cents
complete_set_cost_cents
was_ladder
was_complement
was_rebalance
was_directional
```

---



## 11. Manual classification rules

For each fill, classify it as one of:

```text
A. opens inventory
B. increases dominant side
C. buys complement
D. reduces unpaired inventory
E. forms complete set with edge
F. merge/recycle
G. leaves directional exposure
H. final cleanup/compensation
I. redeem/resolution
```

Core formula:

```text
locked_edge_cents = 100 - (avg_cost_existing_leg_cents + new_complement_price_cents)
```

Examples:

```text
USA avg 2.5 + BEL buy 95.5 = 98.0
locked_edge = 2.0c

USA avg 2.0 + BEL buy 94.0 = 96.0
locked_edge = 4.0c
```

Approximate behavioral thresholds:

```text
RN1-style: edge >= 2c
Gap-style: edge >= 4c
Conservative bot: edge >= 2.5c or 3c
```

---



## 12. Daily checklist

Use this minimal daily checklist:

```powershell
$RN1 = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
$GAP = "0x83255595ba1fadd2e734cb30a0fb8110301a19cc"
$WATCH = "world_cup_2026"

pmr sync incremental $RN1
pmr sync incremental $GAP
pmr markets sync

pmr dataset microstructure build --wallet $RN1 --watchlist $WATCH --min-context usable
pmr dataset microstructure build --wallet $GAP --watchlist $WATCH --min-context usable

pmr reconcile run --wallet $RN1
pmr reconcile run --wallet $GAP
```

If reconciliation says `local_open_episode_missing`, run the full backfill block from section 3 later. For manual event analysis, continue with the SQL event query as long as the target event has fills and real MERGE/REDEEM actions.