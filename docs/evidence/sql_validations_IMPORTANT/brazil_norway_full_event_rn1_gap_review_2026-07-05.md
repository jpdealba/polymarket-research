# Brazil vs. Norway (evento completo) — validación de query y primeras conclusiones

*Generado: 2026-07-05*
*Origen: revisión del token `637491...881285` ("Brazil vs. Norway: O/U 0.5" - Over) para RN1 y Mind.The.Gap*

## 0. Qué es este documento

Se partió de una pregunta puntual sobre un solo token (O/U 0.5 - Over) y se terminó validando una query que cubre **todo el evento negRisk** "Brazil vs. Norway" (47 mercados: O/U a distintas líneas, spreads, exact score, halftime, team-to-advance, moneyline) para ambas wallets. Este documento:

1. Confirma que la query es correcta para el propósito de analizar comportamiento de fills con contexto de orderbook.
2. Documenta un matiz real encontrado en la validación (columnas de cash-flow acumulado en filas MERGE/REDEEM).
3. Registra los hallazgos iniciales de comportamiento que motivaron la revisión.

## 1. Validación de la query

Se ejecutó el query completo (CTEs `target_wallets` → `event_candidates` → `related_markets` → `related_tokens` → `events` → `running` → `booked` → SELECT final) contra `C:\data\db\pmresearch.db`. Resultado: **7,099 filas, sin errores**.

Verificaciones hechas:

- **Cobertura del evento**: 47 preguntas distintas recuperadas (O/U 0.5 a 8.5, Brazil/Norway O/U individual, spreads -1.5 a -3.5, 16 exact-scores, halftime, draw, team-to-advance, moneyline). Coincide con el `eventSlug` real `fifwc-bra-nor-2026-07-05-more-markets` visto en el raw feed. **La query sí captura el evento completo, no solo el token pedido** — esto es más útil que limitarse al token original, porque el comportamiento relevante (merges cross-market) solo se ve al nivel de evento.
- **event_type presentes**: `TRADE`, `MERGE`, `REDEEM` para RN1 (4,996 / 9 / 43); `TRADE`, `REDEEM` para Mind.The.Gap (2,036 / 0 / 15) — **cero MERGE para Gap**, confirmando el hallazgo ya documentado en `rn1_gap_discoveries_2026-07-05.md` §8.
- **Calidad de contexto de book** (columna `context_status`, solo aplica a `TRADE`): de 7,032 trades, 6,146 `excellent` (≤5s), 592 `good` (≤15s), 1 `usable`, 293 `missing` (~4%, mayormente trades tempranos antes de que el token entrara al book sampler). **87% de cobertura excelente — el dataset es sólido para juzgar si los fills tenían edge visible en el book antes de ejecutarse.**
- **WAC y qty corriendo por TRADE**: verificado a mano contra los 3 primeros trades de RN1 en Over — `token_qty_after`, `token_cash_out_after`, `token_running_wac_after` calculan correctamente (ej. tras comprar 421@0.95 + 200@0.95 + 4408@0.941 + 4399.83@0.941 → wac≈0.9416, qty≈9428.83). Correcto.

### 1.1 Caveat encontrado: columnas de cash-flow en filas MERGE/REDEEM

Las columnas `token_qty_before/after`, `token_cash_out_before/after` y `token_running_wac_after` se calculan con `PARTITION BY wallet, token_id`. Para filas `TRADE` esto es correcto (cada token tiene su propia serie). Pero **`MERGE` y `REDEEM` siempre tienen `token_id = NULL`** (la fuente no da atribución por-token para estos tipos — ver `pmresearch/ledger/model.py`), y en SQL, `NULL` se trata como **un solo valor de partición**. Resultado: las 43 filas `REDEEM` de RN1 (una por cada uno de los 43 sub-mercados donde tenía holdings) y las 9 filas `MERGE` comparten la misma partición `(wallet, NULL)` y su cash-flow se acumula **en una sola serie continua mezclando mercados no relacionados entre sí**.

Verificado empíricamente: `token_cash_out_after` para el REDEEM de "O/U 5.5" (-454,399.90) sigue sumando el REDEEM de "O/U 4.5" (-459,893.51), luego "O/U 3.5" (-463,896.96), etc. — 43 valores decrecientes monótonamente, mezclando preguntas completamente distintas.

**Implicación práctica**: las columnas `question`, `outcome_label`, `delta_shares`, `delta_usdc`, `price` en filas MERGE/REDEEM son confiables y están bien atribuidas (usan `we.condition_id` directo, no dependen del join a `token_id`). Pero **`token_qty_before/after`, `token_cash_out_before/after` y `token_running_wac_after` NO deben usarse para leer valores en filas MERGE/REDEEM** — son ruido acumulado del evento completo, no la posición real de ese mercado. Para TRADE, esas mismas columnas son correctas y confiables.

**Actualización: corregido.** Se cambió `PARTITION BY wallet, token_id` a `PARTITION BY wallet, COALESCE(token_id, condition_id)` en las 4 ventanas de la CTE `running`. Verificado contra la DB: las 43 filas REDEEM y 9 filas MERGE de RN1 ahora acumulan cash-flow de forma aislada por condición (ej. REDEEM de "O/U 5.5" → `cash_out_after = -5,493.61`, ya no arrastra las otras 42). Efecto secundario correcto: en las 2 condiciones donde RN1 hizo MERGE parcial a mitad de partido y luego REDEEM del remanente ("O/U 1.5" y "O/U 3.5"), el `cash_out_before` del REDEEM ahora refleja exactamente el monto que dejó el MERGE de *esa misma condición* — antes era ruido, ahora es señal real de esas dos secuencias merge→redeem.

Nota: esto no afecta ningún número de producción — se confirmó que el código real de PnL (`pmresearch/projections/episodes.py:386-450`) ya maneja el fan-out de REDEEM por condición correctamente. El bug vivía únicamente en esta query exploratoria ad hoc.

Query corregida (reemplazar en las 4 apariciones dentro de `running`):
```sql
PARTITION BY wallet, COALESCE(token_id, condition_id)
```

## 2. Hallazgos iniciales de comportamiento (con este query)

### 2.1 Actividad de RN1 en un solo partido: enorme

4,996 trades + 43 REDEEM + 9 MERGE — **más de 5,000 eventos en un solo evento deportivo**, cubriendo 47 sub-mercados distintos. Consistente con la tesis ya documentada de RN1 como "inventory cycler" de gran escala y alta diversificación.

### 2.2 Tamaño de orden fijo "4408" — posible firma de ejecución programática

El tamaño exacto `4408` shares aparece en **4 trades BUY dentro de este solo evento**, en 2 mercados distintos (O/U 0.5 Over dos veces, y Under una vez) y también se confirmó fuera de este evento en al menos otro partido de fecha muy anterior. Un humano no repite un tamaño exacto de orden así de manera consistente entre mercados y fechas — es más consistente con un tamaño de lote fijo programado (order-slicing).

### 2.3 Merge coordinado de 9 condiciones en un solo segundo

A las **21:42:38 UTC** (`ts=1783287758`), RN1 ejecutó `MERGE` en 9 condition_ids distintos del mismo evento **al mismo segundo** — liberando ~$454k en capital combinado antes de la resolución final (que llegó ~33 min después, entre 22:15-22:16 UTC). Esto es gestión de exposición multi-mercado coordinada, no clics manuales aislados.

### 2.4 Gap de ingesta de ~4,408 shares en el token Over de O/U 0.5

RN1 compró (solo BUY, sin ventas) 33,683.30 shares de "Over" vía trades registrados, pero el REDEEM real (reportado directo por Polymarket, `is_derived=0`) fue de 38,091.29 shares — una diferencia de ~4,408 shares (~$4,150 nocional) no explicada por ningún TRADE/MERGE/SPLIT/CONVERSION capturado en el raw store completo de RN1 (10,316 fetches revisados, sin límite de fecha). Conclusión provisional: **gap de ingesta durante la ráfaga de trading más intensa** (123 trades en ~6 min en este par Over/Under), no un mecanismo on-chain exótico. Pendiente: aislar la transacción exacta si se decide perseguirlo.

**Actualización 2026-07-05 (noche): resuelto.** Eran trades reales perdidos por una carrera de late-arrival en el watermark del sync incremental (el Data API aún no había indexado los eventos cuando se consultó la ventana, y la siguiente ventana arrancó después de sus timestamps). Se re-fetchearon las ventanas del partido y se re-ingirió; residuales ahora en cero. Diagnóstico completo y reparación en `bra_nor_mex_eng_playbook_y_conciliacion_polymarket_2026-07-05.md` §5.

### 2.5 Resultado en O/U 0.5

Ambas wallets apostaron a "Over" y ganaron: RN1 +$8,444 netos en ese mercado; Mind.The.Gap +$4,418 netos. Ninguna cubrió con Under de forma perfecta en ese mercado específico (RN1 sí operó Under activamente con 82 trades, terminando neto largo 16,331.56 shares — perdedoras, ya incluidas en el neto).

## 2.6 Totales del evento completo (agregado, 47 mercados)

Calculado con la misma metodología FIFO de complete-sets ya usada en `scripts.sql` (línea 1), escalada a las 47 preguntas del evento para ambas wallets:

| | RN1 | Mind.The.Gap |
|---|---|---|
| **PnL neto realizado** | **+$245,281** | **-$157,797** |
| Cashflow en trades | -$1,326,497 | -$361,175 |
| Cashflow en MERGE | +$454,400 | $0 |
| Cashflow en REDEEM | +$1,117,378 | +$203,378 |
| Condiciones redimidas | 43/47 | 15/47 |
| Complete-sets emparejados (FIFO) | 910,470 | 276,105 |
| Edge promedio ponderado por set | +1.90¢ | **-2.73¢** |
| Gap promedio entre patas | 74.5 min | 32.5 min |

**Hallazgo clave**: en este evento específico, Mind.The.Gap perdió dinero neto (-$157,797) con edge promedio **negativo** (-2.73¢/set, pagó de más por sus pares Over+Under) — contrasta con su perfil histórico documentado en `rn1_gap_discoveries_2026-07-05.md` (+4.41¢/set). Ganar la apuesta puntual de O/U 0.5 (§2.5) no significa ganar el evento completo; este partido fue una mala noche para Gap en agregado.

RN1 cerró positivo (+$245,281) con edge modesto pero positivo (+1.90¢/set) sobre ~3.3x más volumen de complete-sets que Gap — consistente con su perfil de escala/diversificación ya documentado.

**Caveat de PnL pendiente**: RN1 tiene 4 condiciones sin evento REDEEM; de esas, 2 son posiciones ganadoras aún no reclamadas por un valor de ~$218,763 (pueden aparecer en syncs futuros — su PnL real subiría a ~$464K una vez reclamadas). Gap no tiene monto material pendiente (~$12 de dust en 5 tokens).

**Actualización 2026-07-05 (noche): corregido con el ledger reparado.** El monto no reclamado real era un único REDEEM de "Both Teams to Score" por $92,363.38, que RN1 sí cobró a las 22:16:31 UTC — nuestro ledger lo perdió por la misma carrera de late-arrival del §2.4. Tras re-fetch + re-ingest (+42 trades, +1 REDEEM en este evento), el **PnL real de RN1 en este evento es +$323,215** (no +$245,281 ni ~$464K) y no queda nada pendiente (residuales = $0, verificado contra el positions API de Polymarket). Detalle en `bra_nor_mex_eng_playbook_y_conciliacion_polymarket_2026-07-05.md` §5.4.

## 3. Próximos pasos sugeridos

- Confirmar si el patrón "4408" se repite como tamaño de lote en más eventos de RN1 (validaría firma de bot con más fuerza que 4 ocurrencias).
- Si se quiere usar `token_cash_out_after`/`wac` en filas REDEEM para análisis futuro, reparticionar por `COALESCE(token_id, condition_id)`.
- Perseguir el gap de ~4,408 shares del §2.4 solo si se necesita PnL exacto a nivel centavo; no bloquea las conclusiones de comportamiento.
