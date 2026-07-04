# Plan — De Phase 18 a ejecución mínima

## Objetivo general

Convertir la investigación de RN1 y wallets correlacionadas en un sistema de investigación prospectiva capaz de responder:

```text
¿La estrategia observable de RN1 puede explicarse con señales disponibles antes del fill y puede replicarse de forma controlada?
```

Este plan empieza en Phase 18 porque ahí se captura la pieza que no puede recuperarse históricamente: el orderbook antes y después de los fills. Las fases siguientes convierten esos snapshots en evidencia, reglas, simulación, paper trading y, solo al final, ejecución mínima.

---

# Phase 18 — Forward Microstructure Watch

## Pregunta central de Phase 18

Phase 18 debe poder responder:

```text
Cuando RN1 recibe un maker fill en un mercado observado, ¿qué mostraba el book antes del fill y qué cambió después?
```

Más específicamente:

```text
1. ¿Había snapshot válido antes del fill?
2. ¿Qué tan viejo era ese snapshot?
3. ¿Cuál era el best bid, best ask, mid, spread y profundidad?
4. ¿El fill ocurrió cerca del bid, ask, mid o fuera del rango esperado?
5. ¿Qué pasó en el book después del fill?
6. ¿La evidencia es suficientemente fresca para analizar entrada maker?
```

## Regla de validez

No se debe usar un book viejo para explicar un fill.

Clasificación recomendada:

```text
excellent: <= 5 segundos antes del fill
good:      <= 15 segundos
usable:    <= 30 segundos
weak:      <= 60 segundos
stale:     > 60 segundos
missing:   sin snapshot previo
```

Solo `excellent`, `good` y, con cautela, `usable` sirven para análisis. `weak`, `stale` y `missing` deben mostrarse, pero no deben alimentar conclusiones fuertes.

## Scope

Phase 18 debe capturar datos hacia adelante, no reconstruir el pasado.

Incluye:

```text
- watchlist de tokens relevantes
- book snapshots frecuentes
- sync incremental de RN1 y wallets seleccionadas
- maker/taker enrichment cuando esté disponible
- construcción de maker_fill_context
- dashboard visible en Streamlit
- métricas de cobertura y frescura
```

No incluye:

```text
- inferir órdenes abiertas de RN1 directamente
- asumir que cierta liquidez visible pertenece a RN1
- reconstruir libros históricos no muestreados
- ejecutar trades
- afirmar causalidad con snapshots después del fill
```

## Entidades principales

```text
watchlists
watchlist_tokens
book_sample_runs
book_snapshots
maker_fill_context
```

## Tokens a muestrear

Prioridad:

```text
priority 10: tokens tradeados recientemente por RN1 en torneo/mercado relevante
priority 20: tokens con holding abierto de RN1
priority 30: tokens de evento activo importante
priority 40: derivados del evento activo
priority 90: tokens manuales
```

La watchlist debe ser configurable por torneo/evento. No debe estar hardcodeada solo a World Cup.

Modelo recomendado:

```text
tournaments:
  - world_cup_2026
  - wimbledon_2026
  - nba_finals
  - manual_event
```

Cada torneo debe poder tener:

```text
name
keywords
tags
active_from
active_to
max_tokens
priority_rules
sample_interval_s
```

## CLI esperado

```powershell
pmr tournament add world_cup_2026 --keywords "World Cup,FIFA,Team to Advance,O/U,Spread"
pmr tournament list
pmr tournament activate world_cup_2026

pmr watchlist build --tournament world_cup_2026 --wallet $WALLET
pmr watchlist show --name world_cup_2026
pmr watchlist add-token --name world_cup_2026 --token-id <TOKEN_ID> --reason "manual"
pmr watchlist deactivate-token --name world_cup_2026 --token-id <TOKEN_ID>

pmr books sample-watchlist --name world_cup_2026 --limit 200
pmr context maker-fills --wallet $WALLET --watchlist world_cup_2026 --max-age-s 60
pmr watch tick --wallet $WALLET --watchlist world_cup_2026
pmr watch run --wallet $WALLET --watchlist world_cup_2026
```

## Scheduler

Debe correr en el collector, no en Streamlit.

Cadencias sugeridas:

```text
wallet sync:          30s–60s
watchlist rebuild:   300s
priority <= 20 book: 5s–15s
priority > 20 book:  30s–60s
maker context:       30s–60s
prune:               diario
```

## Dashboard mínimo

Streamlit debe mostrar:

```text
1. Collector status
2. Watchlist activa
3. Último book por token
4. RN1 recent fills
5. Maker fill context
6. Cobertura de contexto
7. Data quality / freshness
```

Métricas clave:

```text
active tokens
latest book age
latest wallet event
maker fills total
maker fills excellent/good/usable
strict coverage = excellent + good
loose coverage = excellent + good + usable
missing/stale share
```

## Acceptance criteria

Phase 18 queda aceptada cuando:

```text
- pmr watch tick corre de inicio a fin
- pmr watch run puede correr continuamente
- watchlist_tokens contiene tokens activos
- book_snapshots se generan y se ligan a sample runs
- maker_fill_context clasifica fills por frescura
- dashboard muestra estado, watchlist, books y fill context
- no se hacen claims históricos con books capturados después del fill
```

## Output de investigación

Phase 18 debe producir una tabla tipo:

```text
trade_utc
token_id
question
side
role
fill_price
fill_size
book_before_age_s
best_bid_before
best_ask_before
mid_before
spread_before
book_after_age_s
best_bid_after
best_ask_after
context_status
null_reason
```

La pregunta que debe quedar resuelta al final es:

```text
¿Tenemos suficiente cobertura de book-before-fill para empezar a inferir reglas de entrada?
```

Si la respuesta es no, no se avanza a conclusiones de estrategia; se sigue recolectando.

---

# Phase 19 — RN1 Cluster / Overlap Detector

## Pregunta central

```text
¿La wallet chica y otras wallets similares son independientes, copy-traders o parte del mismo sistema que RN1?
```

## Motivación

Se observó que una wallet más chica abre menos posiciones, pero muchas veces:

```text
- en los mismos tokens que RN1
- en tiempos similares
- con lados similares
- con precios similares
```

Eso sugiere posible cluster operativo.

## Scope

Comparar RN1 contra una o más wallets candidatas.

Para cada trade de la wallet candidata, buscar el trade RN1 más cercano con:

```text
same token_id
same side
ventana ±60s / ±5m / ±30m
precio parecido
```

## Métricas

```text
matched_trade_count
total_candidate_trades
match_rate_same_token
same_side_match_rate
median_delay_seconds
p90_delay_seconds
price_diff_median
price_diff_p90
size_ratio_median
rn1_first_share
candidate_first_share
simultaneous_share
```

## Interpretación

```text
Delay 0–5s:
  probable mismo sistema / misma señal / ejecución multi-wallet

Delay 5–60s:
  posible copy muy rápido o señal compartida

Delay varios minutos:
  follower/copy-trader

Misma dirección + mismo precio + tamaño proporcional:
  fuerte evidencia de cluster
```

## CLI esperado

```powershell
pmr cluster compare --leader $RN1 --wallet 0x83255595ba1fadd2e734cb30a0fb8110301a19cc --window-s 300
pmr cluster report --leader $RN1 --wallets wallet1,wallet2,wallet3 --out /data/exports/rn1_cluster.md
```

## Output

Tabla de matches:

```text
candidate_trade_ts
rn1_trade_ts
delay_s
leader
token_id
condition_id
side_candidate
side_rn1
side_match
price_candidate
price_rn1
price_diff
size_candidate
size_rn1
size_ratio
```

Resumen:

```text
match rate
median delay
RN1 first %
candidate first %
same side %
median price diff
median size ratio
```

## Acceptance criteria

```text
- Se puede clasificar una wallet candidata como independent / follower / shared-signal / same-system-candidate
- La clasificación incluye evidencia cuantitativa
- No se afirma relación causal sin delay y match rate suficiente
```

---

# Phase 20 — Microstructure Feature Dataset

## Pregunta central

```text
¿Qué variables observables antes del fill describen las entradas maker de RN1?
```

## Motivación

Phase 18 captura snapshots. Phase 20 convierte esos snapshots en un dataset analítico por fill.

## Unidad del dataset

Una fila por maker fill con contexto válido.

## Features de book

```text
book_age_s
best_bid_before
best_ask_before
mid_before
spread_before
spread_bps
bid_depth_top1
ask_depth_top1
bid_depth_top5
ask_depth_top5
book_imbalance_top1
book_imbalance_top5
distance_fill_to_mid
distance_fill_to_bid
distance_fill_to_ask
fill_inside_spread
fill_at_best_bid
fill_at_best_ask
```

## Features de trade

```text
side
fill_price
fill_size
delta_usdc
role
trade_hour_utc
market_category
time_to_event_start_s
```

## Features de inventario antes del fill

```text
qty_token_before
qty_complement_before
directional_before
bond_before
bond_ratio_before
market_exposure_before
event_exposure_before
capital_used_before
```

## Features después del fill

```text
qty_token_after
qty_complement_after
directional_after
bond_after
bond_delta
exposure_delta
```

## Labels / resultados

```text
markout_5m
markout_15m
markout_1h
markout_24h
pnl_at_resolution
pnl_episode
closed_by_merge
closed_by_redeem
closed_by_sell
closed_by_resolution
```

## CLI esperado

```powershell
pmr dataset microstructure build --wallet $RN1 --watchlist world_cup_2026 --min-context usable
pmr dataset microstructure stats --wallet $RN1
pmr dataset microstructure export --wallet $RN1 --out /data/exports/rn1_microstructure_dataset.parquet
```

## Acceptance criteria

```text
- Cada fila tiene fill + book_before válido
- Cada feature indica NULL reason si no puede calcularse
- No se usan snapshots posteriores para explicar entrada
- Dataset exportable a CSV/Parquet
- Puede filtrarse por context_status
```

---

# Phase 21 — Entry Rule Reconstruction

## Pregunta central

```text
¿Qué regla observable antes del fill explica las entradas de RN1?
```

## Motivación

Antes de ML, intentar reglas interpretables. Si una regla simple explica gran parte de los fills y tiene PnL positivo, la estrategia es más replicable.

## Reglas candidatas

### Regla A — Spread capture

```text
Entrar como maker si spread_before >= X y fill_price mejora contra mid por Y.
```

### Regla B — Inventory balancing

```text
Entrar si el fill reduce directional exposure o aumenta bond inventory.
```

### Regla C — Completion-set edge

```text
Entrar si el costo esperado de token + complemento permite formar bond < 1.
```

### Regla D — Depth / imbalance

```text
Entrar si hay desequilibrio de profundidad que favorece llenar pasivamente sin quedar atrapado.
```

### Regla E — Event timing

```text
Entrar solo en ventanas específicas antes/durante eventos donde el flujo es alto.
```

### Regla F — Correlated sibling markets

```text
Entrar si mercados hermanos dentro del mismo evento están desalineados.
```

## Métricas

```text
fill_explained_rate
false_positive_rate
precision_against_RN1_fills
coverage_against_RN1_fills
avg_markout_5m
avg_markout_1h
avg_pnl_episode
avg_bond_delta
max_inventory_required
```

## CLI esperado

```powershell
pmr rules fit --wallet $RN1 --dataset rn1_microstructure_dataset.parquet
pmr rules evaluate --wallet $RN1 --rule inventory_balancing_v1
pmr rules explain-fill --event-id <EVENT_ID>
```

## Output

```text
Rule name
Parameters
Explained fills %
Expected PnL / markout
Inventory impact
Blind spots
Examples of fills explained
Examples of fills not explained
```

## Acceptance criteria

```text
- Al menos 3 reglas candidatas evaluadas
- Cada regla usa solo información disponible antes del fill
- Se reporta qué porcentaje de fills RN1 explica
- Se reportan falsos positivos
- No se optimiza una regla solo para overfit sin validación temporal
```

---

# Phase 22 — Counterfactual Simulator

## Pregunta central

```text
Si hubiéramos usado una regla parecida, ¿qué habría pasado con nuestro inventario, fills y PnL?
```

## Motivación

Explicar RN1 no basta. Hay que saber si una regla se puede operar con menor capital y sin ver a RN1.

## Simulación

Para cada snapshot histórico prospectivo capturado:

```text
1. evaluar si la regla habría querido postear bid/ask
2. simular probabilidad de fill bajo distintos escenarios
3. actualizar inventario hipotético
4. aplicar merge/redeem cuando convenga
5. marcar PnL y exposición
```

## Escenarios de fill

```text
optimista:
  recibimos fills similares a RN1

medio:
  recibimos 30%–50% de fills RN1

conservador:
  solo recibimos fill si el book muestra profundidad suficiente y el precio toca nuestro nivel
```

## Métricas

```text
simulated_pnl
simulated_pnl_net_fees
max_drawdown
max_directional_exposure
max_bond_inventory
capital_required
turnover
merge_count
redeem_count
stale_context_excluded_count
```

## CLI esperado

```powershell
pmr sim run --rule inventory_cycling_v1 --wallet $RN1 --scenario conservative
pmr sim report --run-id <ID> --out /data/exports/sim_inventory_cycling_v1.md
```

## Acceptance criteria

```text
- Simulación no usa información futura para decidir entrada
- PnL se calcula con reglas reproducibles
- Se comparan escenarios optimista/medio/conservador
- Se reporta capital requerido y drawdown
- Si el edge solo existe en escenario optimista, no se considera replicable todavía
```

---

# Phase 23 — Forward Paper Trading

## Pregunta central

```text
En vivo, sin dinero real, ¿la regla genera señales llenables y PnL hipotético estable?
```

## Motivación

El backtest y la simulación todavía dependen de supuestos. Paper trading prueba la regla hacia adelante.

## Scope

El sistema no envía órdenes reales. Solo registra intenciones:

```text
would_bid
would_ask
would_cancel
would_merge
would_redeem
would_reduce_exposure
```

## Tablas sugeridas

```text
paper_orders
paper_fills
paper_inventory
paper_pnl_daily
paper_risk_events
```

## Lógica

```text
1. leer book live
2. evaluar regla
3. crear paper order
4. monitorear si el mercado habría tocado el precio
5. crear paper fill bajo criterio conservador
6. actualizar inventario
7. aplicar límites de riesgo
8. reportar PnL hipotético
```

## Risk limits desde paper stage

```text
max_position_per_token
max_directional_per_market
max_event_exposure
max_daily_loss
max_open_orders
max_stale_book_age_s
kill_switch_on_data_stale
```

## CLI esperado

```powershell
pmr paper run --rule inventory_cycling_v1 --watchlist world_cup_2026
pmr paper status
pmr paper report --out /data/exports/paper_inventory_cycling_v1.md
```

## Acceptance criteria

```text
- Corre mínimo 7 días sin dinero real
- Registra señales, fills hipotéticos, inventario y PnL
- Reconciliación interna del paper ledger pasa
- La estrategia no depende de copiar fills RN1 después de verlos
- Se reporta fill-rate conservador
- Se reporta PnL neto de fees estimado
```

---

# Phase 24 — Minimal Execution Engine

## Pregunta central

```text
¿Se puede ejecutar una versión pequeña, limitada y apagable de la estrategia con riesgo controlado?
```

## Requisito previo

No implementar Phase 24 hasta que Phase 23 muestre:

```text
- señales estables
- PnL hipotético positivo
- drawdown aceptable
- inventario controlado
- fill-rate razonable
- reglas claras de cancelación
- kill switch probado
```

## Scope

Ejecución mínima con dinero real pequeño.

Incluye:

```text
quote placement
cancel/replace
inventory limits
risk checks antes de cada orden
paper/live parity
ledger local de órdenes
kill switch
manual approval mode opcional
```

No incluye:

```text
escalar capital automáticamente
trading sin límites
operar mercados no monitoreados
usar reglas no validadas
```

## Modos

```text
DRY_RUN:
  genera órdenes pero no firma ni envía

MANUAL_APPROVAL:
  propone órdenes y espera confirmación

LIVE_LIMITED:
  opera con capital y límites pequeños
```

## Risk controls obligatorios

```text
max_total_capital
max_order_size
max_market_exposure
max_event_exposure
max_daily_loss
max_open_orders
max_slippage
max_book_age_s
cancel_on_stale_book
cancel_on_sync_stale
kill_switch_manual
kill_switch_auto_on_reconcile_fail
```

## CLI esperado

```powershell
pmr exec dry-run --rule inventory_cycling_v1 --watchlist world_cup_2026
pmr exec propose --rule inventory_cycling_v1 --watchlist world_cup_2026
pmr exec live --rule inventory_cycling_v1 --watchlist world_cup_2026 --max-capital 100
pmr exec kill-switch
pmr exec status
```

## Acceptance criteria

```text
- Live mode requiere límites explícitos
- No opera si book está stale
- No opera si sync/reconcile está fallando
- Todas las órdenes quedan auditadas
- Puede apagar y cancelar órdenes abiertas
- PnL y exposición se reportan diariamente
```

---

# Orden recomendado

```text
Phase 18: capturar book-before-fill y fill context
Phase 19: detectar cluster RN1 / wallets similares
Phase 20: construir dataset de microestructura
Phase 21: reconstruir reglas de entrada
Phase 22: simular contrafactualmente
Phase 23: paper trading forward
Phase 24: ejecución mínima con límites
```

---

# Decisión sobre ML

No empezar con ML.

Primero construir:

```text
datos válidos
features interpretables
reglas simples
simulación
paper trading
```

Después, si hay suficiente dataset, ML puede entrar como:

```text
1. modelo de fair value
2. modelo de probabilidad de fill
3. clasificador de entradas RN1-like
4. sizing model
5. detector de riesgo de inventario
```

Pero el sistema base debe funcionar sin ML.

La hipótesis actual es:

```text
RN1 parece más un sistema de market making + inventory management + completion-set recycling que un bettor basado solo en predicción deportiva.
```

---

# Criterio para saber si vamos bien

Después de Phase 18–21, deberías poder responder:

```text
¿Puedo explicar una parte grande de las entradas de RN1 con información observable antes del fill?
```

Después de Phase 22–23:

```text
¿Esa explicación sobrevive en simulación y paper trading sin copiar a RN1?
```

Solo si ambas respuestas son sí, tiene sentido pasar a ejecución.
