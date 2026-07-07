WITH
params AS (
  SELECT
    '0x2005d16a84ceefa912d4e380cd32e7ff827875ea' AS wallet
),

search_terms(term) AS (
  VALUES
    ('Will United States win on 2026-07-06'),
    ('Will Belgium win on 2026-07-06'),
    ('Exact Score: United States 2 - 3 Belgium')
),

candidate_markets AS (
  SELECT DISTINCT
    m.condition_id,
    m.question,
    m.event_id,
    pe.title AS event_title
  FROM markets m
  LEFT JOIN pm_events pe
    ON pe.event_id = m.event_id
  JOIN search_terms st
    ON lower(m.question) LIKE '%' || lower(st.term) || '%'
),

token_map AS (
  SELECT
    t.condition_id,
    MAX(CASE WHEN lower(t.outcome_label) = 'yes' THEN t.token_id END) AS yes_token_id,
    MAX(CASE WHEN lower(t.outcome_label) = 'no'  THEN t.token_id END) AS no_token_id
  FROM tokens t
  GROUP BY t.condition_id
),

wallet_activity AS (
  SELECT
    we.condition_id,
    COUNT(*) AS events,
    SUM(CASE WHEN we.event_type = 'TRADE' THEN 1 ELSE 0 END) AS trades,
    SUM(CASE WHEN we.event_type = 'MERGE' THEN 1 ELSE 0 END) AS merges,
    SUM(CASE WHEN we.event_type IN ('REDEEM','REDEEM_PAYOUT','RESOLUTION_SETTLEMENT') THEN 1 ELSE 0 END) AS redeems,

    ROUND(SUM(
      CASE
        WHEN we.event_type = 'TRADE'
        THEN ABS(CAST(COALESCE(NULLIF(we.delta_shares,''),'0') AS REAL))
        ELSE 0
      END
    ), 4) AS trade_shares,

    ROUND(SUM(
      CASE
        WHEN we.event_type = 'TRADE'
        THEN ABS(CAST(COALESCE(NULLIF(we.delta_usdc,''), NULLIF(we.usdc_size,''), '0') AS REAL))
        ELSE 0
      END
    ), 4) AS trade_notional,

    datetime(MIN(we.ts), 'unixepoch') AS first_utc,
    datetime(MAX(we.ts), 'unixepoch') AS last_utc

  FROM wallet_events we
  JOIN params p
    ON lower(we.wallet) = lower(p.wallet)
  WHERE we.condition_id IN (SELECT condition_id FROM candidate_markets)
  GROUP BY we.condition_id
)

SELECT
  cm.condition_id,
  cm.question,
  cm.event_title,
  tm.yes_token_id,
  tm.no_token_id,
  COALESCE(wa.events, 0) AS events,
  COALESCE(wa.trades, 0) AS trades,
  COALESCE(wa.merges, 0) AS merges,
  COALESCE(wa.redeems, 0) AS redeems,
  COALESCE(wa.trade_shares, 0) AS trade_shares,
  COALESCE(wa.trade_notional, 0) AS trade_notional,
  wa.first_utc,
  wa.last_utc
FROM candidate_markets cm
LEFT JOIN token_map tm
  ON tm.condition_id = cm.condition_id
LEFT JOIN wallet_activity wa
  ON wa.condition_id = cm.condition_id
ORDER BY
  COALESCE(wa.trade_notional, 0) DESC,
  cm.question;