-- ============================================================
-- RN1 complete-set FIFO matching approximation
-- Objetivo:
--   Emparejar compras BUY de los dos outcomes de cada mercado binario
--   por cantidad acumulada, y calcular:
--
--   pair_cost = price_outcome_0 + price_outcome_1
--   edge_per_pair = 1 - pair_cost
--
-- Si weighted_edge_per_pair ≈ 0.0279:
--   = +2.79 cents por complete set
--   = +279 bps
--
-- Nota:
--   Esto NO prueba que cada par fue mergeado exactamente.
--   Es un matching FIFO aproximado por compras acumuladas.
-- ============================================================

WITH
params AS (
    SELECT
        lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea') AS wallet
),

-- ------------------------------------------------------------
-- 1. Mercados binarios: deben tener exactamente 2 tokens.
--    No usamos etiquetas YES/NO, solo outcome_index.
-- ------------------------------------------------------------
binary_markets AS (
    SELECT
        t.condition_id
    FROM tokens t
    GROUP BY t.condition_id
    HAVING COUNT(*) = 2
),

-- ------------------------------------------------------------
-- 2. Mapa token -> outcome_index.
-- ------------------------------------------------------------
token_map AS (
    SELECT
        t.condition_id,
        t.token_id,
        CAST(t.outcome_index AS INTEGER) AS outcome_index
    FROM tokens t
    JOIN binary_markets bm
      ON bm.condition_id = t.condition_id
),

-- ------------------------------------------------------------
-- 3. Solo compras BUY de RN1 en mercados binarios.
--
-- Ajustes importantes:
--   - delta_shares debe ser positivo en BUY según tu ledger.
--   - si en tu DB hay ruido, ABS(delta_shares) protege.
--   - price se castea a REAL por si está guardado como TEXT.
-- ------------------------------------------------------------
buy_fills AS (
    SELECT
        we.id AS event_id,
        lower(we.wallet) AS wallet,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        datetime(we.ts, 'unixepoch') AS trade_utc,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

-- ------------------------------------------------------------
-- 4. Para cada outcome, ordenamos sus compras por tiempo/id
--    y construimos intervalos de cantidad acumulada.
--
-- Ejemplo:
--   fill 1 qty 100  => intervalo [0, 100]
--   fill 2 qty 50   => intervalo [100, 150]
--
-- Luego emparejamos outcome 0 vs outcome 1 por overlap
-- de intervalos acumulados.
-- ------------------------------------------------------------
buy_lots AS (
    SELECT
        bf.*,

        COALESCE(
            SUM(qty) OVER (
                PARTITION BY condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,

        SUM(qty) OVER (
            PARTITION BY condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

-- ------------------------------------------------------------
-- 5. Separamos las dos patas del binario.
-- ------------------------------------------------------------
lots_0 AS (
    SELECT *
    FROM buy_lots
    WHERE outcome_index = 0
),

lots_1 AS (
    SELECT *
    FROM buy_lots
    WHERE outcome_index = 1
),

-- ------------------------------------------------------------
-- 6. Matching FIFO por overlap de cantidad acumulada.
--
-- Un par existe cuando el intervalo acumulado de outcome 0
-- se cruza con el intervalo acumulado de outcome 1.
--
-- matched_qty =
--   min(cum_after_0, cum_after_1)
--   -
--   max(cum_before_0, cum_before_1)
-- ------------------------------------------------------------
matched_pairs AS (
    SELECT
        l0.condition_id,

        l0.token_id AS token_0,
        l1.token_id AS token_1,

        l0.event_id AS event_id_0,
        l1.event_id AS event_id_1,

        l0.ts AS ts_0,
        l1.ts AS ts_1,

        ABS(l1.ts - l0.ts) AS leg_gap_s,

        l0.price AS price_0,
        l1.price AS price_1,

        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,

        MAX(l0.cum_qty_before, l1.cum_qty_before) AS match_start_qty,
        MIN(l0.cum_qty_after, l1.cum_qty_after) AS match_end_qty,

        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty

    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.condition_id = l0.condition_id
     AND l0.cum_qty_after  > l1.cum_qty_before
     AND l1.cum_qty_after  > l0.cum_qty_before
),

-- ------------------------------------------------------------
-- 7. Filtramos overlaps válidos.
-- ------------------------------------------------------------
valid_pairs AS (
    SELECT *
    FROM matched_pairs
    WHERE matched_qty > 0
),

-- ------------------------------------------------------------
-- 8. Agregado final.
-- ------------------------------------------------------------
agg AS (
    SELECT
        COUNT(*) AS matched_pair_rows,
        COUNT(DISTINCT condition_id) AS binary_questions,
        SUM(matched_qty) AS matched_complete_sets,

        SUM(matched_qty * pair_cost) AS total_pair_cost_usdc,
        SUM(matched_qty * edge_per_pair) AS theoretical_pair_edge_usdc,

        SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0) AS weighted_edge_per_pair,

        100.0 * (
            SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
        ) AS weighted_edge_cents,

        10000.0 * (
            SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
        ) AS weighted_edge_bps,

        AVG(leg_gap_s) AS avg_leg_gap_s
    FROM valid_pairs
)

SELECT
    matched_pair_rows,
    binary_questions,
    ROUND(matched_complete_sets, 6) AS matched_complete_sets,

    ROUND(total_pair_cost_usdc, 6) AS total_pair_cost_usdc,
    ROUND(theoretical_pair_edge_usdc, 6) AS theoretical_pair_edge_usdc,

    ROUND(weighted_edge_per_pair, 6) AS weighted_edge_per_pair,
    ROUND(weighted_edge_cents, 4) AS weighted_edge_cents,
    ROUND(weighted_edge_bps, 2) AS weighted_edge_bps,

    ROUND(avg_leg_gap_s, 2) AS avg_leg_gap_s,
    ROUND(avg_leg_gap_s / 60.0, 2) AS avg_leg_gap_min
FROM agg;



























-- ============================================================
-- Edge por mercado / question
-- Sirve para detectar outliers.
-- ============================================================

WITH
params AS (
    SELECT lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea') AS wallet
),

binary_markets AS (
    SELECT condition_id
    FROM tokens
    GROUP BY condition_id
    HAVING COUNT(*) = 2
),

token_map AS (
    SELECT
        condition_id,
        token_id,
        CAST(outcome_index AS INTEGER) AS outcome_index
    FROM tokens
    WHERE condition_id IN (SELECT condition_id FROM binary_markets)
),

buy_fills AS (
    SELECT
        we.id AS event_id,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

buy_lots AS (
    SELECT
        bf.*,
        COALESCE(
            SUM(qty) OVER (
                PARTITION BY condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,
        SUM(qty) OVER (
            PARTITION BY condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

lots_0 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 0
),

lots_1 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 1
),

valid_pairs AS (
    SELECT
        l0.condition_id,
        ABS(l1.ts - l0.ts) AS leg_gap_s,
        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,
        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty
    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.condition_id = l0.condition_id
     AND l0.cum_qty_after > l1.cum_qty_before
     AND l1.cum_qty_after > l0.cum_qty_before
    WHERE (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
          ) > 0
)

SELECT
    vp.condition_id,
    COALESCE(m.question, '[missing question]') AS question,

    ROUND(SUM(vp.matched_qty), 6) AS matched_complete_sets,
    ROUND(SUM(vp.matched_qty * vp.edge_per_pair), 6) AS theoretical_pair_edge_usdc,

    ROUND(
        SUM(vp.matched_qty * vp.edge_per_pair) / NULLIF(SUM(vp.matched_qty), 0),
        6
    ) AS weighted_edge_per_pair,

    ROUND(
        100.0 * SUM(vp.matched_qty * vp.edge_per_pair) / NULLIF(SUM(vp.matched_qty), 0),
        4
    ) AS weighted_edge_cents,

    ROUND(
        10000.0 * SUM(vp.matched_qty * vp.edge_per_pair) / NULLIF(SUM(vp.matched_qty), 0),
        2
    ) AS weighted_edge_bps,

    ROUND(
        SUM(CASE WHEN vp.edge_per_pair > 0 THEN vp.matched_qty ELSE 0 END)
        * 100.0 / NULLIF(SUM(vp.matched_qty), 0),
        2
    ) AS pct_qty_positive_edge,

    ROUND(AVG(vp.leg_gap_s) / 60.0, 2) AS avg_leg_gap_min

FROM valid_pairs vp
LEFT JOIN markets m
  ON m.condition_id = vp.condition_id
GROUP BY vp.condition_id, m.question
HAVING SUM(vp.matched_qty) > 0
ORDER BY theoretical_pair_edge_usdc DESC
LIMIT 100;







-- ============================================================
-- Cashflow bruto de MERGE/REDEEM para comparar contra el matching.
--
-- Ojo:
--   MERGE en wallet_events puede estar como delta_usdc positivo
--   o puede requerir ABS según tu convención.
-- ============================================================

SELECT
    event_type,
    COUNT(*) AS events,
    ROUND(SUM(ABS(CAST(delta_shares AS REAL))), 6) AS abs_shares,
    ROUND(SUM(CAST(delta_usdc AS REAL)), 6) AS sum_delta_usdc,
    ROUND(SUM(ABS(CAST(delta_usdc AS REAL))), 6) AS abs_delta_usdc
FROM wallet_events
WHERE lower(wallet) = lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea')
  AND event_type IN ('MERGE', 'REDEEM', 'REDEEM_PAYOUT', 'RESOLUTION_SETTLEMENT')
GROUP BY event_type
ORDER BY event_type;









-- ============================================================
-- Compare complete-set FIFO matching:
-- RN1 vs Mind.The.Gap
--
-- Métrica principal:
--   weighted_edge_per_pair = SUM(matched_qty * (1 - pair_cost)) / SUM(matched_qty)
--
-- Si RN1 ≈ 0.027954:
--   = 2.7954 cents
--   = 279.54 bps
--
-- Para Mind.The.Gap buscamos:
--   - matched_complete_sets
--   - theoretical_pair_edge_usdc
--   - weighted_edge_cents
--   - avg/p50/p90 leg gap
-- ============================================================

WITH
params(wallet_label, wallet) AS (
    VALUES
        ('RN1', lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea')),
        ('Mind.The.Gap', lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc'))
),

binary_markets AS (
    SELECT
        t.condition_id
    FROM tokens t
    GROUP BY t.condition_id
    HAVING COUNT(*) = 2
),

token_map AS (
    SELECT
        t.condition_id,
        t.token_id,
        CAST(t.outcome_index AS INTEGER) AS outcome_index
    FROM tokens t
    JOIN binary_markets bm
      ON bm.condition_id = t.condition_id
),

buy_fills AS (
    SELECT
        p.wallet_label,
        lower(we.wallet) AS wallet,
        we.id AS event_id,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        datetime(we.ts, 'unixepoch') AS trade_utc,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

buy_lots AS (
    SELECT
        bf.*,

        COALESCE(
            SUM(qty) OVER (
                PARTITION BY wallet, condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,

        SUM(qty) OVER (
            PARTITION BY wallet, condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

lots_0 AS (
    SELECT *
    FROM buy_lots
    WHERE outcome_index = 0
),

lots_1 AS (
    SELECT *
    FROM buy_lots
    WHERE outcome_index = 1
),

matched_pairs AS (
    SELECT
        l0.wallet_label,
        l0.wallet,
        l0.condition_id,

        l0.token_id AS token_0,
        l1.token_id AS token_1,

        l0.event_id AS event_id_0,
        l1.event_id AS event_id_1,

        l0.ts AS ts_0,
        l1.ts AS ts_1,

        ABS(l1.ts - l0.ts) AS leg_gap_s,

        l0.price AS price_0,
        l1.price AS price_1,

        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,

        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty

    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.wallet = l0.wallet
     AND l1.condition_id = l0.condition_id
     AND l0.cum_qty_after > l1.cum_qty_before
     AND l1.cum_qty_after > l0.cum_qty_before
),

valid_pairs AS (
    SELECT *
    FROM matched_pairs
    WHERE matched_qty > 0
),

agg AS (
    SELECT
        wallet_label,
        wallet,

        COUNT(*) AS matched_pair_rows,
        COUNT(DISTINCT condition_id) AS binary_questions,

        SUM(matched_qty) AS matched_complete_sets,

        SUM(matched_qty * pair_cost) AS total_pair_cost_usdc,
        SUM(matched_qty * edge_per_pair) AS theoretical_pair_edge_usdc,

        SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_per_pair,

        100.0 * SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_cents,

        10000.0 * SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_bps,

        SUM(CASE WHEN edge_per_pair > 0 THEN matched_qty ELSE 0 END)
            * 100.0 / NULLIF(SUM(matched_qty), 0)
            AS pct_qty_positive_edge,

        SUM(CASE WHEN leg_gap_s <= 60 THEN matched_qty ELSE 0 END)
            * 100.0 / NULLIF(SUM(matched_qty), 0)
            AS pct_qty_under_60s,

        AVG(leg_gap_s) AS avg_leg_gap_s

    FROM valid_pairs
    GROUP BY wallet_label, wallet
)

SELECT
    wallet_label,
    wallet,

    matched_pair_rows,
    binary_questions,

    ROUND(matched_complete_sets, 6) AS matched_complete_sets,
    ROUND(total_pair_cost_usdc, 6) AS total_pair_cost_usdc,
    ROUND(theoretical_pair_edge_usdc, 6) AS theoretical_pair_edge_usdc,

    ROUND(weighted_edge_per_pair, 6) AS weighted_edge_per_pair,
    ROUND(weighted_edge_cents, 4) AS weighted_edge_cents,
    ROUND(weighted_edge_bps, 2) AS weighted_edge_bps,

    ROUND(pct_qty_positive_edge, 2) AS pct_qty_positive_edge,
    ROUND(pct_qty_under_60s, 2) AS pct_qty_under_60s,

    ROUND(avg_leg_gap_s, 2) AS avg_leg_gap_s,
    ROUND(avg_leg_gap_s / 60.0, 2) AS avg_leg_gap_min

FROM agg
ORDER BY wallet_label;












-- ============================================================
-- Leg gap distribution: RN1 vs Mind.The.Gap
-- Weighted by matched_qty.
-- ============================================================

WITH
params(wallet_label, wallet) AS (
    VALUES
        ('RN1', lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea')),
        ('Mind.The.Gap', lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc'))
),

binary_markets AS (
    SELECT condition_id
    FROM tokens
    GROUP BY condition_id
    HAVING COUNT(*) = 2
),

token_map AS (
    SELECT
        condition_id,
        token_id,
        CAST(outcome_index AS INTEGER) AS outcome_index
    FROM tokens
    WHERE condition_id IN (SELECT condition_id FROM binary_markets)
),

buy_fills AS (
    SELECT
        p.wallet_label,
        lower(we.wallet) AS wallet,
        we.id AS event_id,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

buy_lots AS (
    SELECT
        bf.*,
        COALESCE(
            SUM(qty) OVER (
                PARTITION BY wallet, condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,
        SUM(qty) OVER (
            PARTITION BY wallet, condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

lots_0 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 0
),

lots_1 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 1
),

valid_pairs AS (
    SELECT
        l0.wallet_label,
        l0.wallet,
        ABS(l1.ts - l0.ts) AS leg_gap_s,
        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,
        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty
    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.wallet = l0.wallet
     AND l1.condition_id = l0.condition_id
     AND l0.cum_qty_after > l1.cum_qty_before
     AND l1.cum_qty_after > l0.cum_qty_before
    WHERE (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
          ) > 0
),

ranked AS (
    SELECT
        *,
        SUM(matched_qty) OVER (
            PARTITION BY wallet
        ) AS total_qty,

        SUM(matched_qty) OVER (
            PARTITION BY wallet
            ORDER BY leg_gap_s
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty
    FROM valid_pairs
)

SELECT
    wallet_label,
    wallet,

    ROUND(SUM(matched_qty), 6) AS matched_complete_sets,

    ROUND(
        SUM(CASE WHEN leg_gap_s <= 60 THEN matched_qty ELSE 0 END)
        * 100.0 / NULLIF(SUM(matched_qty), 0),
        4
    ) AS pct_matched_qty_under_60s,

    ROUND(MIN(CASE WHEN cum_qty >= total_qty * 0.50 THEN leg_gap_s END), 2)
        AS p50_leg_gap_s,

    ROUND(MIN(CASE WHEN cum_qty >= total_qty * 0.90 THEN leg_gap_s END), 2)
        AS p90_leg_gap_s,

    ROUND(MIN(CASE WHEN cum_qty >= total_qty * 0.50 THEN leg_gap_s END) / 60.0, 2)
        AS p50_leg_gap_min,

    ROUND(MIN(CASE WHEN cum_qty >= total_qty * 0.90 THEN leg_gap_s END) / 60.0, 2)
        AS p90_leg_gap_min,

    ROUND(
        SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0),
        6
    ) AS weighted_edge_per_pair,

    ROUND(
        100.0 * SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0),
        4
    ) AS weighted_edge_cents,

    ROUND(
        10000.0 * SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0),
        2
    ) AS weighted_edge_bps

FROM ranked
GROUP BY wallet_label, wallet
ORDER BY wallet_label;





-- ============================================================
-- Cashflows MERGE / REDEEM:
-- RN1 vs Mind.The.Gap
-- ============================================================

WITH
params(wallet_label, wallet) AS (
    VALUES
        ('RN1', lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea')),
        ('Mind.The.Gap', lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc'))
)

SELECT
    p.wallet_label,
    lower(we.wallet) AS wallet,
    we.event_type,

    COUNT(*) AS events,

    ROUND(SUM(ABS(CAST(we.delta_shares AS REAL))), 6) AS abs_shares,
    ROUND(SUM(CAST(we.delta_usdc AS REAL)), 6) AS sum_delta_usdc,
    ROUND(SUM(ABS(CAST(we.delta_usdc AS REAL))), 6) AS abs_delta_usdc

FROM wallet_events we
JOIN params p
  ON lower(we.wallet) = p.wallet
WHERE we.event_type IN (
    'MERGE',
    'REDEEM',
    'REDEEM_PAYOUT',
    'RESOLUTION_SETTLEMENT'
)
GROUP BY
    p.wallet_label,
    lower(we.wallet),
    we.event_type
ORDER BY
    p.wallet_label,
    we.event_type;





-- ============================================================
-- Top complete-set candidates / edge by market:
-- Mind.The.Gap only
-- ============================================================

WITH
params AS (
    SELECT
        'Mind.The.Gap' AS wallet_label,
        lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc') AS wallet
),

binary_markets AS (
    SELECT condition_id
    FROM tokens
    GROUP BY condition_id
    HAVING COUNT(*) = 2
),

token_map AS (
    SELECT
        condition_id,
        token_id,
        CAST(outcome_index AS INTEGER) AS outcome_index
    FROM tokens
    WHERE condition_id IN (SELECT condition_id FROM binary_markets)
),

buy_fills AS (
    SELECT
        p.wallet_label,
        lower(we.wallet) AS wallet,
        we.id AS event_id,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

buy_lots AS (
    SELECT
        bf.*,
        COALESCE(
            SUM(qty) OVER (
                PARTITION BY wallet, condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,
        SUM(qty) OVER (
            PARTITION BY wallet, condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

lots_0 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 0
),

lots_1 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 1
),

valid_pairs AS (
    SELECT
        l0.condition_id,
        ABS(l1.ts - l0.ts) AS leg_gap_s,
        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,
        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty
    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.wallet = l0.wallet
     AND l1.condition_id = l0.condition_id
     AND l0.cum_qty_after > l1.cum_qty_before
     AND l1.cum_qty_after > l0.cum_qty_before
    WHERE (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
          ) > 0
)

SELECT
    vp.condition_id,
    COALESCE(m.question, '[missing question]') AS question,

    ROUND(SUM(vp.matched_qty), 6) AS matched_complete_sets,
    ROUND(SUM(vp.matched_qty * vp.edge_per_pair), 6) AS theoretical_pair_edge_usdc,

    ROUND(
        SUM(vp.matched_qty * vp.edge_per_pair) / NULLIF(SUM(vp.matched_qty), 0),
        6
    ) AS weighted_edge_per_pair,

    ROUND(
        100.0 * SUM(vp.matched_qty * vp.edge_per_pair) / NULLIF(SUM(vp.matched_qty), 0),
        4
    ) AS weighted_edge_cents,

    ROUND(
        10000.0 * SUM(vp.matched_qty * vp.edge_per_pair) / NULLIF(SUM(vp.matched_qty), 0),
        2
    ) AS weighted_edge_bps,

    ROUND(
        SUM(CASE WHEN vp.edge_per_pair > 0 THEN vp.matched_qty ELSE 0 END)
        * 100.0 / NULLIF(SUM(vp.matched_qty), 0),
        2
    ) AS pct_qty_positive_edge,

    ROUND(AVG(vp.leg_gap_s) / 60.0, 2) AS avg_leg_gap_min

FROM valid_pairs vp
LEFT JOIN markets m
  ON m.condition_id = vp.condition_id
GROUP BY vp.condition_id, m.question
HAVING SUM(vp.matched_qty) > 0
ORDER BY theoretical_pair_edge_usdc DESC
LIMIT 100;



-- ============================================================
-- RN1 vs Mind.The.Gap en mercados binarios comunes
--
-- Common market definition:
--   condition_id binario donde ambos wallets compraron ambos outcomes.
--
-- Métrica:
--   weighted_edge_per_pair = SUM(matched_qty * (1 - pair_cost)) / SUM(matched_qty)
--
-- Esto controla el sesgo de que cada wallet opere mercados distintos.
-- ============================================================

WITH
params(wallet_label, wallet) AS (
    VALUES
        ('RN1', lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea')),
        ('Mind.The.Gap', lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc'))
),

binary_markets AS (
    SELECT
        condition_id
    FROM tokens
    GROUP BY condition_id
    HAVING COUNT(*) = 2
),

token_map AS (
    SELECT
        t.condition_id,
        t.token_id,
        CAST(t.outcome_index AS INTEGER) AS outcome_index
    FROM tokens t
    JOIN binary_markets bm
      ON bm.condition_id = t.condition_id
),

buy_fills_all AS (
    SELECT
        p.wallet_label,
        lower(we.wallet) AS wallet,
        we.id AS event_id,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

-- Mercados donde cada wallet compró ambos outcomes.
wallet_two_sided_markets AS (
    SELECT
        wallet_label,
        wallet,
        condition_id
    FROM buy_fills_all
    GROUP BY wallet_label, wallet, condition_id
    HAVING COUNT(DISTINCT outcome_index) = 2
),

-- Mercados comunes: están two-sided para RN1 y Gap.
common_conditions AS (
    SELECT
        a.condition_id
    FROM wallet_two_sided_markets a
    JOIN wallet_two_sided_markets b
      ON b.condition_id = a.condition_id
     AND b.wallet_label <> a.wallet_label
    GROUP BY a.condition_id
    HAVING COUNT(DISTINCT a.wallet_label) = 2
),

buy_fills AS (
    SELECT *
    FROM buy_fills_all
    WHERE condition_id IN (SELECT condition_id FROM common_conditions)
),

buy_lots AS (
    SELECT
        bf.*,

        COALESCE(
            SUM(qty) OVER (
                PARTITION BY wallet, condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,

        SUM(qty) OVER (
            PARTITION BY wallet, condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

lots_0 AS (
    SELECT *
    FROM buy_lots
    WHERE outcome_index = 0
),

lots_1 AS (
    SELECT *
    FROM buy_lots
    WHERE outcome_index = 1
),

valid_pairs AS (
    SELECT
        l0.wallet_label,
        l0.wallet,
        l0.condition_id,

        ABS(l1.ts - l0.ts) AS leg_gap_s,

        l0.price AS price_0,
        l1.price AS price_1,

        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,

        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty
    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.wallet = l0.wallet
     AND l1.condition_id = l0.condition_id
     AND l0.cum_qty_after > l1.cum_qty_before
     AND l1.cum_qty_after > l0.cum_qty_before
    WHERE (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
          ) > 0
),

agg AS (
    SELECT
        wallet_label,
        wallet,

        COUNT(*) AS matched_pair_rows,
        COUNT(DISTINCT condition_id) AS common_binary_questions,

        SUM(matched_qty) AS matched_complete_sets,
        SUM(matched_qty * pair_cost) AS total_pair_cost_usdc,
        SUM(matched_qty * edge_per_pair) AS theoretical_pair_edge_usdc,

        SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_per_pair,

        100.0 * SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_cents,

        10000.0 * SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_bps,

        SUM(CASE WHEN edge_per_pair > 0 THEN matched_qty ELSE 0 END)
            * 100.0 / NULLIF(SUM(matched_qty), 0)
            AS pct_qty_positive_edge,

        SUM(CASE WHEN leg_gap_s <= 60 THEN matched_qty ELSE 0 END)
            * 100.0 / NULLIF(SUM(matched_qty), 0)
            AS pct_qty_under_60s,

        AVG(leg_gap_s) AS avg_leg_gap_s
    FROM valid_pairs
    GROUP BY wallet_label, wallet
)

SELECT
    wallet_label,
    wallet,

    matched_pair_rows,
    common_binary_questions,

    ROUND(matched_complete_sets, 6) AS matched_complete_sets,
    ROUND(total_pair_cost_usdc, 6) AS total_pair_cost_usdc,
    ROUND(theoretical_pair_edge_usdc, 6) AS theoretical_pair_edge_usdc,

    ROUND(weighted_edge_per_pair, 6) AS weighted_edge_per_pair,
    ROUND(weighted_edge_cents, 4) AS weighted_edge_cents,
    ROUND(weighted_edge_bps, 2) AS weighted_edge_bps,

    ROUND(pct_qty_positive_edge, 2) AS pct_qty_positive_edge,
    ROUND(pct_qty_under_60s, 2) AS pct_qty_under_60s,

    ROUND(avg_leg_gap_s, 2) AS avg_leg_gap_s,
    ROUND(avg_leg_gap_s / 60.0, 2) AS avg_leg_gap_min

FROM agg
ORDER BY wallet_label;











-- ============================================================
-- Mercado común, RN1 vs Mind.The.Gap lado a lado
-- Devuelve una fila por condition_id común.
-- ============================================================

WITH
params(wallet_label, wallet) AS (
    VALUES
        ('RN1', lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea')),
        ('Mind.The.Gap', lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc'))
),

binary_markets AS (
    SELECT condition_id
    FROM tokens
    GROUP BY condition_id
    HAVING COUNT(*) = 2
),

token_map AS (
    SELECT
        t.condition_id,
        t.token_id,
        CAST(t.outcome_index AS INTEGER) AS outcome_index
    FROM tokens t
    JOIN binary_markets bm
      ON bm.condition_id = t.condition_id
),

buy_fills_all AS (
    SELECT
        p.wallet_label,
        lower(we.wallet) AS wallet,
        we.id AS event_id,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

wallet_two_sided_markets AS (
    SELECT
        wallet_label,
        wallet,
        condition_id
    FROM buy_fills_all
    GROUP BY wallet_label, wallet, condition_id
    HAVING COUNT(DISTINCT outcome_index) = 2
),

common_conditions AS (
    SELECT
        condition_id
    FROM wallet_two_sided_markets
    GROUP BY condition_id
    HAVING COUNT(DISTINCT wallet_label) = 2
),

buy_fills AS (
    SELECT *
    FROM buy_fills_all
    WHERE condition_id IN (SELECT condition_id FROM common_conditions)
),

buy_lots AS (
    SELECT
        bf.*,

        COALESCE(
            SUM(qty) OVER (
                PARTITION BY wallet, condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,

        SUM(qty) OVER (
            PARTITION BY wallet, condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

lots_0 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 0
),

lots_1 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 1
),

valid_pairs AS (
    SELECT
        l0.wallet_label,
        l0.wallet,
        l0.condition_id,

        ABS(l1.ts - l0.ts) AS leg_gap_s,

        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,

        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty
    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.wallet = l0.wallet
     AND l1.condition_id = l0.condition_id
     AND l0.cum_qty_after > l1.cum_qty_before
     AND l1.cum_qty_after > l0.cum_qty_before
    WHERE (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
          ) > 0
),

by_wallet_market AS (
    SELECT
        wallet_label,
        condition_id,

        SUM(matched_qty) AS matched_complete_sets,
        SUM(matched_qty * edge_per_pair) AS theoretical_pair_edge_usdc,

        SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_per_pair,

        100.0 * SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_cents,

        SUM(CASE WHEN edge_per_pair > 0 THEN matched_qty ELSE 0 END)
            * 100.0 / NULLIF(SUM(matched_qty), 0)
            AS pct_qty_positive_edge,

        AVG(leg_gap_s) / 60.0 AS avg_leg_gap_min
    FROM valid_pairs
    GROUP BY wallet_label, condition_id
)

SELECT
    c.condition_id,
    COALESCE(m.question, '[missing question]') AS question,

    ROUND(g.matched_complete_sets, 6) AS gap_sets,
    ROUND(g.theoretical_pair_edge_usdc, 6) AS gap_edge_usdc,
    ROUND(g.weighted_edge_cents, 4) AS gap_edge_cents,
    ROUND(g.pct_qty_positive_edge, 2) AS gap_pct_positive,
    ROUND(g.avg_leg_gap_min, 2) AS gap_avg_leg_gap_min,

    ROUND(r.matched_complete_sets, 6) AS rn1_sets,
    ROUND(r.theoretical_pair_edge_usdc, 6) AS rn1_edge_usdc,
    ROUND(r.weighted_edge_cents, 4) AS rn1_edge_cents,
    ROUND(r.pct_qty_positive_edge, 2) AS rn1_pct_positive,
    ROUND(r.avg_leg_gap_min, 2) AS rn1_avg_leg_gap_min,

    ROUND(g.weighted_edge_cents - r.weighted_edge_cents, 4) AS gap_minus_rn1_edge_cents,
    ROUND(g.theoretical_pair_edge_usdc - r.theoretical_pair_edge_usdc, 6) AS gap_minus_rn1_edge_usdc

FROM common_conditions c
LEFT JOIN markets m
  ON m.condition_id = c.condition_id
LEFT JOIN by_wallet_market g
  ON g.condition_id = c.condition_id
 AND g.wallet_label = 'Mind.The.Gap'
LEFT JOIN by_wallet_market r
  ON r.condition_id = c.condition_id
 AND r.wallet_label = 'RN1'
WHERE g.condition_id IS NOT NULL
  AND r.condition_id IS NOT NULL
ORDER BY ABS(gap_minus_rn1_edge_usdc) DESC
LIMIT 200;



-- ============================================================
-- Buscar transacciones de Gap donde parece haber comportamiento de merge:
-- múltiples tokens del mismo condition_id en la misma tx,
-- con delta_shares negativo y/o delta_usdc positivo.
-- ============================================================

SELECT
    tx_hash,
    condition_id,
    datetime(MIN(ts), 'unixepoch') AS utc,
    COUNT(*) AS rows_in_tx,
    GROUP_CONCAT(DISTINCT event_type) AS event_types,
    GROUP_CONCAT(DISTINCT side) AS sides,

    COUNT(DISTINCT token_id) AS distinct_tokens,

    ROUND(SUM(CAST(delta_shares AS REAL)), 6) AS sum_delta_shares,
    ROUND(SUM(ABS(CAST(delta_shares AS REAL))), 6) AS abs_delta_shares,

    ROUND(SUM(CAST(delta_usdc AS REAL)), 6) AS sum_delta_usdc,
    ROUND(SUM(ABS(CAST(delta_usdc AS REAL))), 6) AS abs_delta_usdc

FROM wallet_events
WHERE lower(wallet) = lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc')
  AND tx_hash IS NOT NULL
GROUP BY tx_hash, condition_id
HAVING COUNT(DISTINCT token_id) = 2
   AND (
        SUM(CAST(delta_usdc AS REAL)) > 0
        OR SUM(CAST(delta_shares AS REAL)) < 0
   )
ORDER BY utc DESC
LIMIT 200;





-- ============================================================
-- RN1 vs Mind.The.Gap desde 2026-05-23
--
-- Esto controla por tiempo.
-- No controla por mercados comunes; solo por ventana.
-- ============================================================

WITH
params(wallet_label, wallet, start_ts) AS (
    VALUES
        ('RN1', lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea'), unixepoch('2026-05-23 00:00:00')),
        ('Mind.The.Gap', lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc'), unixepoch('2026-05-23 00:00:00'))
),

binary_markets AS (
    SELECT
        condition_id
    FROM tokens
    GROUP BY condition_id
    HAVING COUNT(*) = 2
),

token_map AS (
    SELECT
        t.condition_id,
        t.token_id,
        CAST(t.outcome_index AS INTEGER) AS outcome_index
    FROM tokens t
    JOIN binary_markets bm
      ON bm.condition_id = t.condition_id
),

buy_fills AS (
    SELECT
        p.wallet_label,
        lower(we.wallet) AS wallet,
        we.id AS event_id,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND we.ts >= p.start_ts
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

buy_lots AS (
    SELECT
        bf.*,

        COALESCE(
            SUM(qty) OVER (
                PARTITION BY wallet, condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,

        SUM(qty) OVER (
            PARTITION BY wallet, condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

lots_0 AS (
    SELECT *
    FROM buy_lots
    WHERE outcome_index = 0
),

lots_1 AS (
    SELECT *
    FROM buy_lots
    WHERE outcome_index = 1
),

valid_pairs AS (
    SELECT
        l0.wallet_label,
        l0.wallet,
        l0.condition_id,

        ABS(l1.ts - l0.ts) AS leg_gap_s,

        l0.price AS price_0,
        l1.price AS price_1,

        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,

        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty

    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.wallet = l0.wallet
     AND l1.condition_id = l0.condition_id
     AND l0.cum_qty_after > l1.cum_qty_before
     AND l1.cum_qty_after > l0.cum_qty_before
    WHERE (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
          ) > 0
),

agg AS (
    SELECT
        wallet_label,
        wallet,

        COUNT(*) AS matched_pair_rows,
        COUNT(DISTINCT condition_id) AS binary_questions,

        SUM(matched_qty) AS matched_complete_sets,
        SUM(matched_qty * pair_cost) AS total_pair_cost_usdc,
        SUM(matched_qty * edge_per_pair) AS theoretical_pair_edge_usdc,

        SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_per_pair,

        100.0 * SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_cents,

        10000.0 * SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS weighted_edge_bps,

        SUM(CASE WHEN edge_per_pair > 0 THEN matched_qty ELSE 0 END)
            * 100.0 / NULLIF(SUM(matched_qty), 0)
            AS pct_qty_positive_edge,

        SUM(CASE WHEN leg_gap_s <= 60 THEN matched_qty ELSE 0 END)
            * 100.0 / NULLIF(SUM(matched_qty), 0)
            AS pct_qty_under_60s,

        AVG(leg_gap_s) AS avg_leg_gap_s
    FROM valid_pairs
    GROUP BY wallet_label, wallet
)

SELECT
    wallet_label,
    wallet,

    matched_pair_rows,
    binary_questions,

    ROUND(matched_complete_sets, 6) AS matched_complete_sets,
    ROUND(total_pair_cost_usdc, 6) AS total_pair_cost_usdc,
    ROUND(theoretical_pair_edge_usdc, 6) AS theoretical_pair_edge_usdc,

    ROUND(weighted_edge_per_pair, 6) AS weighted_edge_per_pair,
    ROUND(weighted_edge_cents, 4) AS weighted_edge_cents,
    ROUND(weighted_edge_bps, 2) AS weighted_edge_bps,

    ROUND(pct_qty_positive_edge, 2) AS pct_qty_positive_edge,
    ROUND(pct_qty_under_60s, 2) AS pct_qty_under_60s,

    ROUND(avg_leg_gap_s, 2) AS avg_leg_gap_s,
    ROUND(avg_leg_gap_s / 60.0, 2) AS avg_leg_gap_min

FROM agg
ORDER BY wallet_label;






-- ============================================================
-- Edge por bucket de volumen emparejado:
-- RN1 vs Mind.The.Gap desde 2026-05-23
--
-- Objetivo:
--   Validar si a mayor matched_complete_sets por mercado,
--   menor weighted_edge_cents.
-- ============================================================

WITH
params(wallet_label, wallet, start_ts) AS (
    VALUES
        ('RN1', lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea'), unixepoch('2026-05-23 00:00:00')),
        ('Mind.The.Gap', lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc'), unixepoch('2026-05-23 00:00:00'))
),

binary_markets AS (
    SELECT condition_id
    FROM tokens
    GROUP BY condition_id
    HAVING COUNT(*) = 2
),

token_map AS (
    SELECT
        t.condition_id,
        t.token_id,
        CAST(t.outcome_index AS INTEGER) AS outcome_index
    FROM tokens t
    JOIN binary_markets bm
      ON bm.condition_id = t.condition_id
),

buy_fills AS (
    SELECT
        p.wallet_label,
        lower(we.wallet) AS wallet,
        we.id AS event_id,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND we.ts >= p.start_ts
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

buy_lots AS (
    SELECT
        bf.*,
        COALESCE(
            SUM(qty) OVER (
                PARTITION BY wallet, condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,
        SUM(qty) OVER (
            PARTITION BY wallet, condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

lots_0 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 0
),

lots_1 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 1
),

valid_pairs AS (
    SELECT
        l0.wallet_label,
        l0.wallet,
        l0.condition_id,
        ABS(l1.ts - l0.ts) AS leg_gap_s,
        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,
        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty
    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.wallet = l0.wallet
     AND l1.condition_id = l0.condition_id
     AND l0.cum_qty_after > l1.cum_qty_before
     AND l1.cum_qty_after > l0.cum_qty_before
    WHERE (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
          ) > 0
),

by_market AS (
    SELECT
        wallet_label,
        wallet,
        condition_id,

        SUM(matched_qty) AS matched_sets,
        SUM(matched_qty * edge_per_pair) AS edge_usdc,

        SUM(matched_qty * edge_per_pair) / NULLIF(SUM(matched_qty), 0)
            AS edge_per_set,

        AVG(leg_gap_s) / 60.0 AS avg_leg_gap_min
    FROM valid_pairs
    GROUP BY wallet_label, wallet, condition_id
),

bucketed AS (
    SELECT
        *,
        CASE
            WHEN matched_sets < 1000 THEN '<1k'
            WHEN matched_sets < 10000 THEN '1k-10k'
            WHEN matched_sets < 50000 THEN '10k-50k'
            WHEN matched_sets < 100000 THEN '50k-100k'
            WHEN matched_sets < 250000 THEN '100k-250k'
            WHEN matched_sets < 500000 THEN '250k-500k'
            ELSE '500k+'
        END AS volume_bucket,

        CASE
            WHEN matched_sets < 1000 THEN 1
            WHEN matched_sets < 10000 THEN 2
            WHEN matched_sets < 50000 THEN 3
            WHEN matched_sets < 100000 THEN 4
            WHEN matched_sets < 250000 THEN 5
            WHEN matched_sets < 500000 THEN 6
            ELSE 7
        END AS bucket_order
    FROM by_market
)

SELECT
    wallet_label,
    volume_bucket,

    COUNT(*) AS markets,

    ROUND(SUM(matched_sets), 6) AS matched_sets,
    ROUND(SUM(edge_usdc), 6) AS edge_usdc,

    ROUND(
        100.0 * SUM(edge_usdc) / NULLIF(SUM(matched_sets), 0),
        4
    ) AS weighted_edge_cents,

    ROUND(
        10000.0 * SUM(edge_usdc) / NULLIF(SUM(matched_sets), 0),
        2
    ) AS weighted_edge_bps,

    ROUND(AVG(avg_leg_gap_min), 2) AS avg_market_leg_gap_min

FROM bucketed
GROUP BY wallet_label, volume_bucket, bucket_order
ORDER BY wallet_label, bucket_order;











-- ============================================================
-- Edge concentration by market:
-- RN1 vs Mind.The.Gap desde 2026-05-23
--
-- Objetivo:
--   Ver si el edge de cada wallet viene de pocos mercados
--   o de una señal repetible distribuida.
--
-- Métrica base:
--   market_positive_edge_usdc = max(edge_usdc, 0)
--
-- Interpretación:
--   top_10_edge_pct alto  => edge concentrado / frágil
--   top_10_edge_pct bajo  => edge más repetible / distribuido
--
-- HHI:
--   cercano a 0  => diversificado
--   alto         => concentrado
-- ============================================================

WITH
params(wallet_label, wallet, start_ts) AS (
    VALUES
        ('RN1', lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea'), unixepoch('2026-05-23 00:00:00')),
        ('Mind.The.Gap', lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc'), unixepoch('2026-05-23 00:00:00'))
),

binary_markets AS (
    SELECT condition_id
    FROM tokens
    GROUP BY condition_id
    HAVING COUNT(*) = 2
),

token_map AS (
    SELECT
        t.condition_id,
        t.token_id,
        CAST(t.outcome_index AS INTEGER) AS outcome_index
    FROM tokens t
    JOIN binary_markets bm
      ON bm.condition_id = t.condition_id
),

buy_fills AS (
    SELECT
        p.wallet_label,
        lower(we.wallet) AS wallet,
        we.id AS event_id,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND we.ts >= p.start_ts
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

buy_lots AS (
    SELECT
        bf.*,

        COALESCE(
            SUM(qty) OVER (
                PARTITION BY wallet, condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,

        SUM(qty) OVER (
            PARTITION BY wallet, condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

lots_0 AS (
    SELECT *
    FROM buy_lots
    WHERE outcome_index = 0
),

lots_1 AS (
    SELECT *
    FROM buy_lots
    WHERE outcome_index = 1
),

valid_pairs AS (
    SELECT
        l0.wallet_label,
        l0.wallet,
        l0.condition_id,

        ABS(l1.ts - l0.ts) AS leg_gap_s,

        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,

        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty

    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.wallet = l0.wallet
     AND l1.condition_id = l0.condition_id
     AND l0.cum_qty_after > l1.cum_qty_before
     AND l1.cum_qty_after > l0.cum_qty_before
    WHERE (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
          ) > 0
),

by_market AS (
    SELECT
        vp.wallet_label,
        vp.wallet,
        vp.condition_id,
        COALESCE(m.question, '[missing question]') AS question,

        SUM(vp.matched_qty) AS matched_sets,
        SUM(vp.matched_qty * vp.edge_per_pair) AS net_edge_usdc,

        CASE
            WHEN SUM(vp.matched_qty * vp.edge_per_pair) > 0
            THEN SUM(vp.matched_qty * vp.edge_per_pair)
            ELSE 0
        END AS positive_edge_usdc,

        SUM(vp.matched_qty * vp.edge_per_pair) / NULLIF(SUM(vp.matched_qty), 0)
            AS edge_per_set,

        100.0 * SUM(vp.matched_qty * vp.edge_per_pair) / NULLIF(SUM(vp.matched_qty), 0)
            AS edge_cents,

        AVG(vp.leg_gap_s) / 60.0 AS avg_leg_gap_min

    FROM valid_pairs vp
    LEFT JOIN markets m
      ON m.condition_id = vp.condition_id
    GROUP BY
        vp.wallet_label,
        vp.wallet,
        vp.condition_id,
        m.question
),

ranked AS (
    SELECT
        bm.*,

        ROW_NUMBER() OVER (
            PARTITION BY wallet_label
            ORDER BY positive_edge_usdc DESC
        ) AS edge_rank,

        SUM(positive_edge_usdc) OVER (
            PARTITION BY wallet_label
        ) AS total_positive_edge_usdc,

        SUM(positive_edge_usdc) OVER (
            PARTITION BY wallet_label
            ORDER BY positive_edge_usdc DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cumulative_positive_edge_usdc

    FROM by_market bm
),

ranked_with_pct AS (
    SELECT
        *,

        positive_edge_usdc / NULLIF(total_positive_edge_usdc, 0)
            AS edge_share,

        cumulative_positive_edge_usdc / NULLIF(total_positive_edge_usdc, 0)
            AS cumulative_edge_share
    FROM ranked
)

SELECT
    wallet_label,

    COUNT(*) AS markets_total,
    SUM(CASE WHEN positive_edge_usdc > 0 THEN 1 ELSE 0 END) AS markets_positive_edge,

    ROUND(SUM(matched_sets), 6) AS matched_sets,
    ROUND(SUM(net_edge_usdc), 6) AS net_edge_usdc,
    ROUND(SUM(positive_edge_usdc), 6) AS positive_edge_usdc,

    ROUND(
        100.0 * SUM(net_edge_usdc) / NULLIF(SUM(matched_sets), 0),
        4
    ) AS net_edge_cents,

    ROUND(
        100.0 * SUM(positive_edge_usdc) / NULLIF(SUM(matched_sets), 0),
        4
    ) AS positive_edge_cents_per_all_sets,

    ROUND(
        100.0 * SUM(CASE WHEN edge_rank <= 5 THEN positive_edge_usdc ELSE 0 END)
        / NULLIF(SUM(positive_edge_usdc), 0),
        2
    ) AS top_5_edge_pct,

    ROUND(
        100.0 * SUM(CASE WHEN edge_rank <= 10 THEN positive_edge_usdc ELSE 0 END)
        / NULLIF(SUM(positive_edge_usdc), 0),
        2
    ) AS top_10_edge_pct,

    ROUND(
        100.0 * SUM(CASE WHEN edge_rank <= 25 THEN positive_edge_usdc ELSE 0 END)
        / NULLIF(SUM(positive_edge_usdc), 0),
        2
    ) AS top_25_edge_pct,

    ROUND(
        100.0 * SUM(CASE WHEN edge_rank <= 50 THEN positive_edge_usdc ELSE 0 END)
        / NULLIF(SUM(positive_edge_usdc), 0),
        2
    ) AS top_50_edge_pct,

    ROUND(
        100.0 * SUM(CASE WHEN edge_rank <= 100 THEN positive_edge_usdc ELSE 0 END)
        / NULLIF(SUM(positive_edge_usdc), 0),
        2
    ) AS top_100_edge_pct,

    MIN(CASE WHEN cumulative_edge_share >= 0.50 THEN edge_rank END)
        AS markets_to_50pct_edge,

    MIN(CASE WHEN cumulative_edge_share >= 0.80 THEN edge_rank END)
        AS markets_to_80pct_edge,

    ROUND(
        SUM(edge_share * edge_share),
        6
    ) AS hhi_edge_concentration

FROM ranked_with_pct
GROUP BY wallet_label
ORDER BY wallet_label;





-- ============================================================
-- Top markets by positive edge:
-- RN1 vs Mind.The.Gap desde 2026-05-23
-- ============================================================

WITH
params(wallet_label, wallet, start_ts) AS (
    VALUES
        ('RN1', lower('0x2005d16a84ceefa912d4e380cd32e7ff827875ea'), unixepoch('2026-05-23 00:00:00')),
        ('Mind.The.Gap', lower('0x83255595ba1fadd2e734cb30a0fb8110301a19cc'), unixepoch('2026-05-23 00:00:00'))
),

binary_markets AS (
    SELECT condition_id
    FROM tokens
    GROUP BY condition_id
    HAVING COUNT(*) = 2
),

token_map AS (
    SELECT
        t.condition_id,
        t.token_id,
        CAST(t.outcome_index AS INTEGER) AS outcome_index
    FROM tokens t
    JOIN binary_markets bm
      ON bm.condition_id = t.condition_id
),

buy_fills AS (
    SELECT
        p.wallet_label,
        lower(we.wallet) AS wallet,
        we.id AS event_id,
        we.condition_id,
        we.token_id,
        tm.outcome_index,
        we.ts,
        ABS(CAST(we.delta_shares AS REAL)) AS qty,
        CAST(we.price AS REAL) AS price
    FROM wallet_events we
    JOIN params p
      ON lower(we.wallet) = p.wallet
    JOIN token_map tm
      ON tm.token_id = we.token_id
     AND tm.condition_id = we.condition_id
    WHERE we.event_type = 'TRADE'
      AND upper(COALESCE(we.side, '')) = 'BUY'
      AND we.ts >= p.start_ts
      AND ABS(CAST(we.delta_shares AS REAL)) > 0
      AND CAST(we.price AS REAL) > 0
      AND CAST(we.price AS REAL) < 1
),

buy_lots AS (
    SELECT
        bf.*,
        COALESCE(
            SUM(qty) OVER (
                PARTITION BY wallet, condition_id, token_id
                ORDER BY ts, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cum_qty_before,
        SUM(qty) OVER (
            PARTITION BY wallet, condition_id, token_id
            ORDER BY ts, event_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_qty_after
    FROM buy_fills bf
),

lots_0 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 0
),

lots_1 AS (
    SELECT * FROM buy_lots WHERE outcome_index = 1
),

valid_pairs AS (
    SELECT
        l0.wallet_label,
        l0.wallet,
        l0.condition_id,
        ABS(l1.ts - l0.ts) AS leg_gap_s,
        (l0.price + l1.price) AS pair_cost,
        (1.0 - (l0.price + l1.price)) AS edge_per_pair,
        (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
        ) AS matched_qty
    FROM lots_0 l0
    JOIN lots_1 l1
      ON l1.wallet = l0.wallet
     AND l1.condition_id = l0.condition_id
     AND l0.cum_qty_after > l1.cum_qty_before
     AND l1.cum_qty_after > l0.cum_qty_before
    WHERE (
            MIN(l0.cum_qty_after, l1.cum_qty_after)
            -
            MAX(l0.cum_qty_before, l1.cum_qty_before)
          ) > 0
),

by_market AS (
    SELECT
        vp.wallet_label,
        vp.wallet,
        vp.condition_id,
        COALESCE(m.question, '[missing question]') AS question,

        SUM(vp.matched_qty) AS matched_sets,
        SUM(vp.matched_qty * vp.edge_per_pair) AS net_edge_usdc,

        CASE
            WHEN SUM(vp.matched_qty * vp.edge_per_pair) > 0
            THEN SUM(vp.matched_qty * vp.edge_per_pair)
            ELSE 0
        END AS positive_edge_usdc,

        100.0 * SUM(vp.matched_qty * vp.edge_per_pair) / NULLIF(SUM(vp.matched_qty), 0)
            AS edge_cents,

        SUM(CASE WHEN vp.edge_per_pair > 0 THEN vp.matched_qty ELSE 0 END)
            * 100.0 / NULLIF(SUM(vp.matched_qty), 0)
            AS pct_qty_positive_edge,

        AVG(vp.leg_gap_s) / 60.0 AS avg_leg_gap_min

    FROM valid_pairs vp
    LEFT JOIN markets m
      ON m.condition_id = vp.condition_id
    GROUP BY
        vp.wallet_label,
        vp.wallet,
        vp.condition_id,
        m.question
),

ranked AS (
    SELECT
        *,

        ROW_NUMBER() OVER (
            PARTITION BY wallet_label
            ORDER BY positive_edge_usdc DESC
        ) AS edge_rank,

        SUM(positive_edge_usdc) OVER (
            PARTITION BY wallet_label
        ) AS total_positive_edge_usdc,

        SUM(positive_edge_usdc) OVER (
            PARTITION BY wallet_label
            ORDER BY positive_edge_usdc DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cumulative_positive_edge_usdc

    FROM by_market
)

SELECT
    wallet_label,
    edge_rank,

    condition_id,
    question,

    ROUND(matched_sets, 6) AS matched_sets,
    ROUND(net_edge_usdc, 6) AS net_edge_usdc,
    ROUND(positive_edge_usdc, 6) AS positive_edge_usdc,
    ROUND(edge_cents, 4) AS edge_cents,
    ROUND(pct_qty_positive_edge, 2) AS pct_qty_positive_edge,
    ROUND(avg_leg_gap_min, 2) AS avg_leg_gap_min,

    ROUND(
        100.0 * positive_edge_usdc / NULLIF(total_positive_edge_usdc, 0),
        2
    ) AS edge_share_pct,

    ROUND(
        100.0 * cumulative_positive_edge_usdc / NULLIF(total_positive_edge_usdc, 0),
        2
    ) AS cumulative_edge_share_pct

FROM ranked
WHERE edge_rank <= 50
ORDER BY wallet_label, edge_rank;

