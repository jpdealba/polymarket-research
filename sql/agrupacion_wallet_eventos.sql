WITH target_wallets(wallet, label) AS (
  VALUES
    ('0x2005d16a84ceefa912d4e380cd32e7ff827875ea', 'RN1'),
    ('0x83255595ba1fadd2e734cb30a0fb8110301a19cc', 'Gap')
),
first_trade AS (
  SELECT
    lower(wallet) AS wallet,
    MIN(ts) AS first_ts
  FROM wallet_events
  WHERE event_type = 'TRADE'
  GROUP BY lower(wallet)
),
evented AS (
  SELECT
    tw.label,
    lower(we.wallet) AS wallet,
    we.ts,
    we.event_type,
    we.side,
    we.condition_id,
    we.token_id,
    CAST(we.delta_usdc AS REAL) AS delta_usdc,
    CAST(we.delta_shares AS REAL) AS delta_shares,
    CAST(we.price AS REAL) AS price,
    m.event_id,
    COALESCE(pe.title, m.question, 'UNKNOWN_EVENT') AS event_title,
    m.question,
    m.category,
    CAST((we.ts - ft.first_ts) / 86400 AS INTEGER) AS days_since_first_trade,
    CASE
      WHEN (we.ts - ft.first_ts) < 30 * 86400 THEN 'early_30d'
      WHEN (we.ts - ft.first_ts) < 90 * 86400 THEN 'early_90d'
      ELSE 'mature_after_90d'
    END AS stage
  FROM wallet_events we
  JOIN target_wallets tw ON tw.wallet = lower(we.wallet)
  JOIN first_trade ft ON ft.wallet = lower(we.wallet)
  LEFT JOIN markets m ON m.condition_id = we.condition_id
  LEFT JOIN pm_events pe ON pe.event_id = m.event_id
  WHERE we.condition_id IS NOT NULL
)
SELECT
  label,
  stage,
  event_id,
  event_title,
  MIN(datetime(ts, 'unixepoch')) AS first_action_utc,
  MAX(datetime(ts, 'unixepoch')) AS last_action_utc,
  MIN(days_since_first_trade) AS days_since_wallet_start,
  COUNT(DISTINCT condition_id) AS markets_traded,
  COUNT(DISTINCT token_id) AS tokens_traded,
  COUNT(*) AS wallet_events,
  SUM(CASE WHEN event_type = 'TRADE' THEN 1 ELSE 0 END) AS trades,
  SUM(CASE WHEN event_type = 'TRADE' AND side = 'BUY' THEN 1 ELSE 0 END) AS buy_trades,
  SUM(CASE WHEN event_type = 'TRADE' AND side = 'SELL' THEN 1 ELSE 0 END) AS sell_trades,
  ROUND(
    1.0 * SUM(CASE WHEN event_type = 'TRADE' AND side = 'SELL' THEN 1 ELSE 0 END)
    / NULLIF(SUM(CASE WHEN event_type = 'TRADE' THEN 1 ELSE 0 END), 0),
    4
  ) AS sell_trade_share,
  ROUND(SUM(CASE WHEN event_type = 'TRADE' AND side = 'BUY' THEN ABS(delta_usdc) ELSE 0 END), 2) AS gross_buy_usdc,
  ROUND(SUM(CASE WHEN event_type = 'TRADE' AND side = 'SELL' THEN ABS(delta_usdc) ELSE 0 END), 2) AS gross_sell_usdc,
  ROUND(SUM(CASE WHEN event_type = 'TRADE' THEN delta_usdc ELSE 0 END), 2) AS trade_cashflow,
  SUM(CASE WHEN event_type = 'MERGE' THEN 1 ELSE 0 END) AS merge_count,
  ROUND(SUM(CASE WHEN event_type = 'MERGE' THEN delta_usdc ELSE 0 END), 2) AS merge_usdc,
  SUM(CASE WHEN event_type = 'REDEEM' THEN 1 ELSE 0 END) AS redeem_count,
  ROUND(SUM(CASE WHEN event_type = 'REDEEM' THEN delta_usdc ELSE 0 END), 2) AS redeem_usdc,
  ROUND(SUM(delta_usdc), 2) AS event_cashflow_pnl_approx,
  ROUND(
    1000.0 * SUM(delta_usdc)
    / NULLIF(SUM(CASE WHEN event_type = 'TRADE' AND side = 'BUY' THEN ABS(delta_usdc) ELSE 0 END), 0),
    2
  ) AS pnl_per_1000_buy_usdc
FROM evented
GROUP BY label, stage, event_id, event_title
HAVING trades > 0
ORDER BY label, stage, gross_buy_usdc DESC;