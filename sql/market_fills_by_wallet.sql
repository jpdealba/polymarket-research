-- belgium win 0x02103f049a5fe127c4ea03c9531772e82f3b9f5e9f5f30d67fda5b472fb56c17
-- united states win 0xf5a0c2d5b6bf0a7b9899aaf4c6237b08e1dbf8d17430fac4de8468a6e2df2e0e
-- draw 0x982941d6030e30ed0f56043622c52d71db557f6ef2774a11a06945248e7076ab

-- GAP 0x83255595ba1fadd2e734cb30a0fb8110301a19cc
-- RN1 0x2005d16a84ceefa912d4e380cd32e7ff827875ea

WITH
params AS (
  SELECT
    '0x83255595ba1fadd2e734cb30a0fb8110301a19cc' AS wallet,
    '0x02103f049a5fe127c4ea03c9531772e82f3b9f5e9f5f30d67fda5b472fb56c17' AS condition_id
),

base AS (
  SELECT
    we.id,
    we.ts,
    we.wallet,
    we.condition_id,
    we.token_id,
    we.event_type,
    we.side,
    tok.outcome_label AS outcome,
    COALESCE(fe.role, 'UNKNOWN') AS role,

    CASE
      WHEN CAST(COALESCE(NULLIF(we.price,''),'0') AS REAL) > 1
        THEN CAST(COALESCE(NULLIF(we.price,''),'0') AS REAL) / 100.0
      ELSE CAST(COALESCE(NULLIF(we.price,''),'0') AS REAL)
    END AS price_prob,

    ABS(CAST(COALESCE(NULLIF(we.delta_shares,''),'0') AS REAL)) AS delta_shares_raw,

    ABS(
      CAST(
        COALESCE(
          NULLIF(we.delta_usdc,''),
          NULLIF(we.usdc_size,''),
          '0'
        ) AS REAL
      )
    ) AS notional_usdc_raw

  FROM wallet_events we
  JOIN params p
    ON lower(we.wallet) = lower(p.wallet)
   AND we.condition_id = p.condition_id
  LEFT JOIN tokens tok
    ON tok.token_id = we.token_id
  LEFT JOIN fill_enrichment fe
    ON fe.event_id = we.id
  WHERE we.event_type IN (
    'TRADE',
    'MERGE',
    'REDEEM',
    'REDEEM_PAYOUT',
    'RESOLUTION_SETTLEMENT'
  )
),

normalized AS (
  SELECT
    base.*,

    CASE
      WHEN event_type = 'TRADE'
       AND price_prob > 0
       AND notional_usdc_raw > 0
        THEN notional_usdc_raw / price_prob

      WHEN event_type = 'TRADE'
       AND delta_shares_raw > 0
        THEN delta_shares_raw

      WHEN event_type <> 'TRADE'
       AND notional_usdc_raw > 0
        THEN notional_usdc_raw

      ELSE delta_shares_raw
    END AS qty_shares,

    CASE
      WHEN event_type = 'TRADE'
       AND notional_usdc_raw > 0
        THEN notional_usdc_raw

      WHEN event_type = 'TRADE'
       AND delta_shares_raw > 0
        THEN delta_shares_raw * price_prob

      ELSE 0
    END AS trade_cost_usdc,

    CASE
      WHEN event_type = 'TRADE'
       AND side = 'BUY'
       AND outcome = 'Yes'
       AND price_prob > 0
       AND notional_usdc_raw > 0
        THEN notional_usdc_raw / price_prob

      WHEN event_type = 'TRADE'
       AND side = 'BUY'
       AND outcome = 'Yes'
        THEN delta_shares_raw

      WHEN event_type = 'TRADE'
       AND side = 'SELL'
       AND outcome = 'Yes'
       AND price_prob > 0
       AND notional_usdc_raw > 0
        THEN -1 * (notional_usdc_raw / price_prob)

      WHEN event_type = 'TRADE'
       AND side = 'SELL'
       AND outcome = 'Yes'
        THEN -1 * delta_shares_raw

      ELSE 0
    END AS delta_yes_qty,

    CASE
      WHEN event_type = 'TRADE'
       AND side = 'BUY'
       AND outcome = 'No'
       AND price_prob > 0
       AND notional_usdc_raw > 0
        THEN notional_usdc_raw / price_prob

      WHEN event_type = 'TRADE'
       AND side = 'BUY'
       AND outcome = 'No'
        THEN delta_shares_raw

      WHEN event_type = 'TRADE'
       AND side = 'SELL'
       AND outcome = 'No'
       AND price_prob > 0
       AND notional_usdc_raw > 0
        THEN -1 * (notional_usdc_raw / price_prob)

      WHEN event_type = 'TRADE'
       AND side = 'SELL'
       AND outcome = 'No'
        THEN -1 * delta_shares_raw

      ELSE 0
    END AS delta_no_qty,

    CASE
      WHEN event_type = 'TRADE'
       AND side = 'BUY'
       AND outcome = 'Yes'
        THEN
          CASE
            WHEN price_prob > 0 AND notional_usdc_raw > 0
              THEN notional_usdc_raw / price_prob
            ELSE delta_shares_raw
          END
      ELSE 0
    END AS yes_buy_qty,

    CASE
      WHEN event_type = 'TRADE'
       AND side = 'BUY'
       AND outcome = 'Yes'
        THEN
          CASE
            WHEN notional_usdc_raw > 0
              THEN notional_usdc_raw
            ELSE delta_shares_raw * price_prob
          END
      ELSE 0
    END AS yes_buy_cost,

    CASE
      WHEN event_type = 'TRADE'
       AND side = 'BUY'
       AND outcome = 'No'
        THEN
          CASE
            WHEN price_prob > 0 AND notional_usdc_raw > 0
              THEN notional_usdc_raw / price_prob
            ELSE delta_shares_raw
          END
      ELSE 0
    END AS no_buy_qty,

    CASE
      WHEN event_type = 'TRADE'
       AND side = 'BUY'
       AND outcome = 'No'
        THEN
          CASE
            WHEN notional_usdc_raw > 0
              THEN notional_usdc_raw
            ELSE delta_shares_raw * price_prob
          END
      ELSE 0
    END AS no_buy_cost

  FROM base
),

running AS (
  SELECT
    n.*,

    COALESCE(
      SUM(delta_yes_qty) OVER (
        PARTITION BY wallet, condition_id
        ORDER BY ts, id
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
      ),
      0
    ) AS yes_before,

    COALESCE(
      SUM(delta_no_qty) OVER (
        PARTITION BY wallet, condition_id
        ORDER BY ts, id
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
      ),
      0
    ) AS no_before,

    COALESCE(
      SUM(delta_yes_qty) OVER (
        PARTITION BY wallet, condition_id
        ORDER BY ts, id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ),
      0
    ) AS yes_after,

    COALESCE(
      SUM(delta_no_qty) OVER (
        PARTITION BY wallet, condition_id
        ORDER BY ts, id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ),
      0
    ) AS no_after,

    COALESCE(
      SUM(yes_buy_qty) OVER (
        PARTITION BY wallet, condition_id
        ORDER BY ts, id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ),
      0
    ) AS yes_buy_qty_cum,

    COALESCE(
      SUM(yes_buy_cost) OVER (
        PARTITION BY wallet, condition_id
        ORDER BY ts, id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ),
      0
    ) AS yes_buy_cost_cum,

    COALESCE(
      SUM(no_buy_qty) OVER (
        PARTITION BY wallet, condition_id
        ORDER BY ts, id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ),
      0
    ) AS no_buy_qty_cum,

    COALESCE(
      SUM(no_buy_cost) OVER (
        PARTITION BY wallet, condition_id
        ORDER BY ts, id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ),
      0
    ) AS no_buy_cost_cum

  FROM normalized n
),

calc AS (
  SELECT
    r.*,

    CASE
      WHEN yes_buy_qty_cum > 0
        THEN yes_buy_cost_cum / yes_buy_qty_cum
      ELSE NULL
    END AS yes_wap_calc,

    CASE
      WHEN no_buy_qty_cum > 0
        THEN no_buy_cost_cum / no_buy_qty_cum
      ELSE NULL
    END AS no_wap_calc

  FROM running r
),

final_rows AS (
  SELECT
    datetime(ts, 'unixepoch') AS utc,

    event_type AS method,

    side AS SIDE,

    outcome,

    ROUND(price_prob, 9) AS price,

    ROUND(qty_shares, 4) AS qty,

    CASE
      WHEN outcome = 'Yes' THEN yes_before
      WHEN outcome = 'No'  THEN no_before
      ELSE NULL
    END AS qty_this_before,

    CASE
      WHEN outcome = 'Yes' THEN no_before
      WHEN outcome = 'No'  THEN yes_before
      ELSE NULL
    END AS qty_comp_before,

    CASE
      WHEN outcome IN ('Yes','No') THEN MIN(yes_before, no_before)
      ELSE NULL
    END AS set_before,

    CASE
      WHEN outcome IN ('Yes','No') THEN yes_before - no_before
      ELSE NULL
    END AS directional_before,

    CASE
      WHEN outcome = 'Yes' THEN yes_after
      WHEN outcome = 'No'  THEN no_after
      ELSE NULL
    END AS qty_this_after,

    CASE
      WHEN outcome = 'Yes' THEN no_after
      WHEN outcome = 'No'  THEN yes_after
      ELSE NULL
    END AS qty_comp_after,

    CASE
      WHEN outcome IN ('Yes','No') THEN MIN(yes_after, no_after)
      ELSE NULL
    END AS set_after,

    CASE
      WHEN outcome IN ('Yes','No') THEN yes_after - no_after
      ELSE NULL
    END AS directional_after,

    yes_wap_calc AS yes_wap,

    no_wap_calc AS no_wap,

    CASE
      WHEN event_type = 'MERGE'
        THEN 'MERGE_REAL_CAPITAL_RECYCLE'

      WHEN event_type IN ('REDEEM','REDEEM_PAYOUT','RESOLUTION_SETTLEMENT')
        THEN 'REDEEM_REAL'

      WHEN outcome IN ('Yes','No')
       AND MIN(yes_after, no_after) > MIN(yes_before, no_before)
        THEN 'COMPRA_COMPLEMENTO_FORMA_SET'

      WHEN outcome IN ('Yes','No')
       AND ABS(yes_after - no_after) > ABS(yes_before - no_before)
        THEN 'AUMENTA_DIRECTIONAL'

      WHEN outcome IN ('Yes','No')
       AND ABS(yes_after - no_after) < ABS(yes_before - no_before)
        THEN 'REDUCE_DIRECTIONAL'

      ELSE event_type
    END AS NOTA,

    role,

    CASE
      WHEN yes_wap_calc IS NOT NULL
       AND no_wap_calc IS NOT NULL
        THEN 1 - yes_wap_calc - no_wap_calc
      ELSE NULL
    END AS edge,

    ts,
    id

  FROM calc
)

SELECT
  utc,
  method,
  SIDE,
  outcome,
  price,
  qty,
  ROUND(qty_this_before, 4) AS QTY_THIS_BEFORE,
  ROUND(qty_comp_before, 4) AS QTY_COMP_BEFORE,
  ROUND(set_before, 4) AS SET_BEFORE,
  ROUND(directional_before, 4) AS DIRECTIONAL_BEFORE,
  ROUND(qty_this_after, 4) AS QTY_THIS_AFTER,
  ROUND(qty_comp_after, 4) AS QTY_COMP_AFTER,
  ROUND(set_after, 4) AS SET_AFTER,
  ROUND(directional_after, 4) AS DIRECTIONAL_AFTER,
  ROUND(yes_wap, 9) AS yes_wap,
  ROUND(no_wap, 9) AS no_wap,
  NOTA,
  role,
  ROUND(edge, 9) AS edge
FROM final_rows
ORDER BY ts, id;