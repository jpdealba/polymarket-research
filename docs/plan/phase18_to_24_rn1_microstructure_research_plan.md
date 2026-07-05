# Plan actualizado — Phase 18 a Phase 24

## Objetivo general

Convertir la investigación de RN1, Mind.The.Gap y wallets correlacionadas en un sistema de investigación prospectiva capaz de responder:

```text
¿La estrategia observable puede explicarse con señales disponibles antes del fill y puede replicarse de forma controlada?
```

El plan empieza en Phase 18 porque ahí se captura la pieza que no puede recuperarse históricamente: el orderbook antes y después de los fills. Las fases siguientes convierten esos snapshots en evidencia, dataset, reglas, simulación, paper trading y, sólo al final, ejecución mínima con dinero real.

La guía principal es:

```text
No buscamos copiar a RN1.
Buscamos convertir observaciones de RN1 / Mind.The.Gap en reglas pre-fill,
probarlas en simulación, validarlas en paper trading,
y sólo después ejecutar pequeño con límites estrictos.
```

---

## Principios globales

Estos principios aplican a todas las fases.

### 1. No usar información futura para decidir entradas

Una regla de entrada sólo puede usar datos disponibles antes del fill o antes de la decisión hipotética:

```text
book_before
posición antes del fill
exposición antes del fill
metadata de mercado disponible
historial anterior al timestamp
```

No puede usar:

```text
book_after para justificar entrada
resultado final del mercado para decidir entrada
fills futuros de RN1
PnL posterior
resolución futura
```

### 2. Book freshness es una condición de validez

Clasificación de contexto:

```text
excellent: <= 5 segundos antes del fill
good:      <= 15 segundos
usable:    <= 30 segundos
weak:      <= 60 segundos
stale:     > 60 segundos
missing:   sin snapshot previo
```

Uso permitido:

```text
excellent/good: evidencia fuerte
usable: evidencia con cautela
weak: visible, pero no debe alimentar conclusiones fuertes
stale/missing: insuficiente para inferir entrada
```

### 3. Separar entrada, inventario y salida

Toda hipótesis debe distinguir:

```text
entry quality      = cómo entró contra el book
inventory impact   = cómo cambió la exposición
close path         = cómo salió
final result       = PnL realizado/no realizado
```

No basta con medir `edge_vs_mid_before`. Esa métrica mide calidad de entrada, no PnL final.

### 4. Fases de investigación y fases de ejecución están separadas

```text
Phase 18–21: investigación / explicación
Phase 22: simulación contrafactual
Phase 23: paper trading forward
Phase 24: ejecución mínima limitada
```

Phase 24 no puede empezar hasta que Phase 23 tenga `PASS` formal.

### 5. Todo hallazgo importante debe migrar al core

Notebooks/Colab sirven como laboratorio. No son la fuente final de verdad.

Flujo recomendado:

```text
notebook exploratory finding
        ↓
feature/detector candidate
        ↓
pmresearch core function
        ↓
CLI/dashboard/report
```

---

# Entidad transversal — Strategy Candidate Registry

## Objetivo

Evitar que las hipótesis se vuelvan una lista informal difícil de controlar.

Cada estrategia candidata debe registrarse, versionarse y avanzar por estados.

## Tabla sugerida: `strategy_candidates`

```text
id INTEGER PRIMARY KEY
name TEXT NOT NULL
description TEXT NOT NULL
source_wallet TEXT
source_phase TEXT
status TEXT NOT NULL
created_at INTEGER NOT NULL
updated_at INTEGER NOT NULL
promoted_from_notebook TEXT
rule_version TEXT
required_features_json TEXT
risk_limits_json TEXT
current_stage TEXT
last_eval_score TEXT
last_eval_result TEXT
notes TEXT
```

## Estados sugeridos

```text
observed
candidate_selected
dataset_ready
rule_fit
sim_passed
paper_running
paper_passed
live_limited
rejected
```

## Ejemplos

```text
inventory_cycling_v1
source_wallet = RN1
status = rule_fit
required_features = book_edge, bond_delta, event_exposure_delta, close_path
```

```text
event_market_making_v1
source_wallet = Mind.The.Gap
status = observed
required_features = buy_sell_symmetry, close_path, entry_edge, fill_rate
```

## Acceptance criteria

```text
- Cada hipótesis operativa tiene registro.
- Cada hipótesis tiene estado claro.
- Ninguna regla pasa a simulación sin dataset asociado.
- Ninguna regla pasa a paper sin simulación.
- Ninguna regla pasa a live sin paper PASS.
```

---

# Phase 18 — Forward Microstructure Watch

## Pregunta central

```text
Cuando RN1, Mind.The.Gap u otra wallet observada recibe un fill en un mercado observado,
¿qué mostraba el book antes del fill y qué cambió después?
```

Más específicamente:

```text
1. ¿Había snapshot válido antes del fill?
2. ¿Qué tan viejo era ese snapshot?
3. ¿Cuál era el best bid, best ask, mid, spread y profundidad?
4. ¿El fill ocurrió cerca del bid, ask, mid o fuera del rango esperado?
5. ¿Qué pasó en el book después del fill?
6. ¿La evidencia es suficientemente fresca para analizar entrada maker/taker?
```

## Scope

Captura prospectiva, no reconstrucción histórica.

Incluye:

```text
- torneos/eventos configurables
- watchlists de tokens relevantes
- book snapshots frecuentes
- sync incremental de wallets seleccionadas
- maker/taker enrichment cuando esté disponible
- construcción de fill context
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

## Generalización: torneos/eventos configurables

No debe estar hardcodeado a World Cup.

Modelo recomendado:

```text
tournaments:
  - world_cup_2026
  - wimbledon_2026
  - nba_finals
  - manual_event
```

Cada torneo/evento debe poder tener:

```text
name
keywords
tags
active_from
active_to
max_tokens
priority_rules
sample_interval_s
is_active
```

## Tokens a muestrear

Prioridad recomendada:

```text
priority 10: tokens tradeados recientemente por wallet observada en torneo/mercado relevante
priority 20: tokens con holding abierto de wallet observada
priority 30: tokens de evento activo importante
priority 40: derivados del evento activo
priority 90: tokens manuales
```

## Entidades principales

```text
tournaments
watchlists
watchlist_tokens
book_sample_runs
book_snapshots
maker_fill_context / fill_context
```

Nota: si la tabla existente se llama `maker_fill_context`, puede mantenerse para compatibilidad, pero conceptualmente debe poder soportar maker/taker/unenriched. Si se crea nueva tabla, preferir `fill_context`.

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
pmr context fills --wallet $WALLET --watchlist world_cup_2026 --max-age-s 60
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
fill context:         30s–60s
prune:               diario
```

## Dashboard mínimo

Streamlit debe mostrar:

```text
1. Collector status
2. Watchlist activa
3. Último book por token
4. Recent fills por wallet
5. Fill context detail
6. Cobertura de contexto
7. Data quality / freshness
```

Métricas clave:

```text
active tokens
latest book age
latest wallet event
fills total
fills excellent/good/usable
strict coverage = excellent + good
loose coverage = excellent + good + usable
missing/stale share
```

## Output de investigación

Tabla mínima:

```text
trade_utc
wallet
wallet_label
token_id
condition_id
question
outcome_label
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

## Acceptance criteria

```text
- pmr watch tick corre de inicio a fin.
- pmr watch run puede correr continuamente.
- watchlist_tokens contiene tokens activos.
- book_snapshots se generan y se ligan a sample runs.
- fill context clasifica fills por frescura.
- Dashboard muestra estado, watchlist, books y fill context.
- No se hacen claims históricos con books capturados después del fill.
```

## Pregunta de salida

```text
¿Tenemos suficiente cobertura de book-before-fill para empezar a inferir reglas de entrada?
```

Si la respuesta es no, no se avanza a conclusiones de estrategia; se sigue recolectando.

---

# Phase 19 — Wallet Cluster & Candidate Selection

## Pregunta central

```text
¿Qué wallets vale la pena estudiar junto a RN1?
¿Son independientes, followers, shared-signal o same-system candidates?
```

## Motivación

Se observaron wallets como Mind.The.Gap que pueden operar:

```text
- mismos eventos que RN1
- tokens similares
- ventanas temporales cercanas
- lados similares o complementarios
- PnL semanal comparable
- ciclos más cerrados
```

Phase 19 no busca probar causalidad. Busca priorizar wallets candidatas y clasificar relación operativa probable.

## Scope

Comparar RN1 contra wallets candidatas.

Para cada trade de la wallet candidata, buscar trade RN1 cercano bajo varios criterios:

```text
same token_id + same side
same token_id + any side
same question + same side
same question + any side
same event + related market
```

Ventanas:

```text
±5s
±15s
±60s
±5m
±30m
```

## Métricas

```text
matched_trade_count
total_candidate_trades
match_rate_same_token
match_rate_same_question
same_side_match_rate
median_delay_seconds
p90_delay_seconds
price_diff_median
price_diff_p90
size_ratio_median
rn1_first_share
candidate_first_share
simultaneous_share
same_event_share
```

## Clasificación sugerida

```text
independent:
  bajo overlap, delays dispersos, baja coincidencia de token/side

follower:
  candidate opera después de RN1 con delay minutos, mismo token/side frecuente

shared_signal:
  ambos operan mismo evento/market cluster, delays bajos o alternantes, no necesariamente mismo token

same_system_candidate:
  delays 0–5s, mismo token/side/precio, tamaño proporcional, match rate alto

research_candidate:
  no prueba relación con RN1, pero tiene PnL/ciclos/patrones interesantes
```

## Wallet candidate score

Tabla recomendada:

```text
wallet
label
overlap_score
same_token_side_match_rate
same_question_match_rate
median_delay_s
rn1_first_share
candidate_first_share
pnl_1d
pnl_1w
positions_value
closed_cycle_score
research_priority
classification
```

## CLI esperado

```powershell
pmr cluster compare --leader $RN1 --wallet 0x83255595ba1fadd2e734cb30a0fb8110301a19cc --window-s 300
pmr cluster report --leader $RN1 --wallets wallet1,wallet2,wallet3 --out /data/exports/rn1_cluster.md
pmr cluster candidates --leader $RN1 --out /data/exports/wallet_candidates_ranked.csv
```

## Output

Match table:

```text
candidate_trade_ts
leader_trade_ts
delay_s
leader
candidate
token_id
condition_id
question
side_candidate
side_leader
side_match
price_candidate
price_leader
price_diff
size_candidate
size_leader
size_ratio
match_type
```

Summary:

```text
match rate
median delay
RN1 first %
candidate first %
same side %
median price diff
median size ratio
classification
```

## Acceptance criteria

```text
- Se puede clasificar una wallet candidata como independent / follower / shared-signal / same-system-candidate / research-candidate.
- La clasificación incluye evidencia cuantitativa.
- No se afirma relación causal sin delay y match rate suficiente.
- Phase 19 no bloquea Phase 20; corre en paralelo.
```

---

# Phase 20 — Microstructure + Lifecycle Dataset

## Pregunta central

```text
¿Qué variables observables antes del fill describen las entradas,
y cómo terminaron esas posiciones después?
```

## Motivación

Phase 18 captura snapshots. Phase 20 convierte esos snapshots en un dataset analítico por fill que une:

```text
book before
fill
inventario before/after
exposición before/after
close path
resultado económico
```

Sin lifecycle, sólo se sabe si la entrada fue buena contra mid. Con lifecycle, se empieza a saber si la entrada llevó a PnL real o a mejor inventario.

## Unidad del dataset

Una fila por fill con contexto válido.

Dataset principal:

```text
microstructure_lifecycle_dataset
```

Filtros mínimos:

```text
context_status in excellent/good/usable
wallet in watchlist
market metadata disponible
```

## Features de book antes del fill

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
wallet_label
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
bond_ratio_after
exposure_delta
directional_delta
event_exposure_after
event_exposure_delta
```

## Lifecycle / close path

Campos obligatorios:

```text
close_path
close_ts
hold_seconds
exit_price_or_resolution_value
realized_pnl_wac
realized_pnl_per_share
realized_pnl_bps_on_cost
remaining_open_qty_after_24h
is_open_after_24h
closed_by_merge
closed_by_redeem
closed_by_sell
closed_by_resolution
closed_by_unresolved_open
```

Valores permitidos de `close_path`:

```text
SELL
MERGE
REDEEM
RESOLUTION
OPEN
MIXED
UNKNOWN
```

## Labels / resultados adicionales

```text
markout_5m
markout_15m
markout_1h
markout_24h
pnl_at_resolution
pnl_episode
```

## NULL reason

Cada feature no computable debe tener razón explícita:

```text
no_book_depth
no_complement_token
unclassified_market_structure
missing_market_metadata
position_still_open
no_resolution_yet
no_price_point
unenriched_role
```

## CLI esperado

```powershell
pmr dataset microstructure build --wallet $RN1 --watchlist world_cup_2026 --min-context usable
pmr dataset microstructure build --wallet 0x83255595ba1fadd2e734cb30a0fb8110301a19cc --watchlist world_cup_2026 --min-context usable
pmr dataset microstructure stats --wallet $RN1
pmr dataset microstructure export --wallet $RN1 --out /data/exports/rn1_microstructure_lifecycle.parquet
```

## Outputs obligatorios

```text
microstructure_lifecycle_dataset.parquet
dataset_quality_report.md
feature_null_reason_report.csv
close_path_summary.csv
```

## Acceptance criteria

```text
- Cada fila tiene fill + book_before válido.
- Cada feature indica NULL reason si no puede calcularse.
- No se usan snapshots posteriores para explicar entrada.
- close_path está presente para fills cerrados o marcado OPEN/UNKNOWN.
- Dataset exportable a CSV/Parquet.
- Puede filtrarse por context_status.
- Puede filtrarse por wallet, event, question, role y close_path.
```

---

# Phase 21 — Interpretable Rule Reconstruction

## Pregunta central

```text
¿Qué regla observable antes del fill explica una parte material de las entradas de RN1 o Mind.The.Gap?
```

## Motivación

Antes de ML, evaluar reglas interpretables. Si una regla simple explica parte de los fills y tiene resultado positivo fuera de muestra, la estrategia es más replicable.

## Reglas candidatas

### Regla A — Spread capture

```text
Entrar como maker si spread_before >= X
y el precio objetivo mejora contra mid por Y.
```

### Regla B — Inventory balancing

```text
Entrar si el fill reduce directional exposure
o aumenta bond inventory.
```

### Regla C — Completion-set edge

```text
Entrar si token + complemento puede formar bond con costo esperado < 1.
```

### Regla D — Depth / imbalance

```text
Entrar si hay desequilibrio de profundidad que favorece llenar pasivamente
sin quedar atrapado.
```

### Regla E — Event timing

```text
Entrar sólo en ventanas específicas antes/durante eventos donde el flujo es alto.
```

### Regla F — Correlated sibling markets

```text
Entrar si mercados hermanos dentro del mismo evento están desalineados.
```

### Regla G — Closed-cycle event trading

Especialmente para Mind.The.Gap:

```text
Entrar si hay evento activo con liquidez alta,
spread suficiente,
y posibilidad de cerrar exposición en el mismo evento.
```

## Validación temporal obligatoria

Toda regla debe evaluarse con separación temporal:

```text
train_window: primeros 60% de fills/días
validation_window: siguiente 20%
out_of_sample_window: último 20%
```

O por días:

```text
fit: days 1–3
validate: days 4–5
test: days 6–7
```

Una regla sólo puede pasar si mantiene señal positiva en periodo out-of-sample.

## Métricas

```text
fill_explained_rate
false_positive_rate
precision_against_wallet_fills
coverage_against_wallet_fills
avg_markout_5m
avg_markout_1h
avg_pnl_episode
avg_bond_delta
avg_exposure_delta
max_inventory_required
out_of_sample_edge_bps
out_of_sample_pnl
```

## Outputs por regla

```text
rule_name
rule_version
parameters
features_used
train_result
validation_result
test_result
explained_fills_pct
expected_pnl_or_markout
inventory_impact
risk_requirements
blind_spots
examples_explained
examples_unexplained
```

## CLI esperado

```powershell
pmr rules fit --wallet $RN1 --dataset /data/exports/rn1_microstructure_lifecycle.parquet
pmr rules evaluate --wallet $RN1 --rule inventory_balancing_v1
pmr rules evaluate --wallet 0x83255595ba1fadd2e734cb30a0fb8110301a19cc --rule event_market_making_v1
pmr rules explain-fill --event-id <EVENT_ID>
pmr rules report --rule inventory_balancing_v1 --out /data/exports/rule_inventory_balancing_v1.md
```

## Acceptance criteria

```text
- Al menos 3 reglas candidatas evaluadas.
- Cada regla usa sólo información disponible antes del fill.
- Se reporta qué porcentaje de fills explica.
- Se reportan falsos positivos.
- Se reporta performance train/validation/test.
- No se promueve una regla que sólo funciona in-sample.
- Cada regla queda registrada en strategy_candidates.
```

---

# Phase 22 — Counterfactual Simulator

## Pregunta central

```text
Si hubiéramos usado una regla parecida sin ver a RN1,
¿qué habría pasado con inventario, fills, PnL y riesgo?
```

## Motivación

Explicar RN1 no basta. Hay que saber si una regla se puede operar con menor capital y sin copiar fills de RN1 después de verlos.

## Simulación

Para cada snapshot histórico prospectivo capturado:

```text
1. evaluar si la regla habría querido postear bid/ask
2. crear intención hipotética
3. simular probabilidad/conservadurismo de fill
4. actualizar inventario hipotético
5. aplicar merge/redeem cuando convenga
6. marcar PnL y exposición
7. aplicar límites de riesgo
```

## Escenarios de fill

```text
optimista:
  recibimos fills similares a RN1 o a la wallet observada

medio:
  recibimos 30%–50% de fills observados o fills cuando el book toca nivel

conservador:
  sólo recibimos fill si el book muestra profundidad suficiente,
  el precio toca nuestro nivel,
  y el contexto no está stale
```

El escenario conservador es el criterio principal. Si el edge sólo existe en optimista, la regla no es replicable todavía.

## Métricas

```text
simulated_pnl
simulated_pnl_net_fees
max_drawdown
max_directional_exposure
max_event_exposure
max_bond_inventory
capital_required
turnover
merge_count
redeem_count
stale_context_excluded_count
orders_created
orders_filled
fill_rate
risk_limit_breaches
```

## Tablas sugeridas

```text
simulation_runs
simulation_orders
simulation_fills
simulation_inventory
simulation_pnl_daily
simulation_risk_events
```

## CLI esperado

```powershell
pmr sim run --rule inventory_cycling_v1 --wallet $RN1 --scenario conservative
pmr sim run --rule event_market_making_v1 --wallet 0x83255595ba1fadd2e734cb30a0fb8110301a19cc --scenario conservative
pmr sim compare --rule inventory_cycling_v1 --scenarios optimistic,medium,conservative
pmr sim report --run-id <ID> --out /data/exports/sim_inventory_cycling_v1.md
```

## Acceptance criteria

```text
- Simulación no usa información futura para decidir entrada.
- PnL se calcula con reglas reproducibles.
- Se comparan escenarios optimista/medio/conservador.
- Escenario conservador se reporta como criterio principal.
- Se reporta capital requerido y drawdown.
- Se reportan límites de riesgo violados.
- Si el edge sólo existe en escenario optimista, la regla no pasa.
```

---

# Phase 23 — Forward Paper Trading

## Pregunta central

```text
En vivo, sin dinero real, ¿la regla genera señales llenables y PnL hipotético estable?
```

## Motivación

Backtest/simulación todavía dependen de supuestos. Paper trading prueba la regla hacia adelante.

## Scope

El sistema no envía órdenes reales. Sólo registra intenciones:

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
paper_reconciliation
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
8. simular merge/redeem si aplica
9. reportar PnL hipotético
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
kill_switch_on_reconcile_fail
```

## PASS gate para Phase 24

Phase 23 debe producir `PASS` antes de permitir Phase 24.

Criterios mínimos:

```text
paper_run_days >= 7
paper_net_pnl_after_estimated_fees > 0
max_drawdown <= configured_limit
no_reconciliation_failures
no_stale_book_trades
no_sync_stale_trades
fill_rate_conservative >= minimum_threshold
manual_kill_switch_tested = true
auto_kill_switch_tested = true
paper_ledger_reconciles = true
```

Recomendación: idealmente correr 14–30 días antes de LIVE, pero el mínimo formal son 7 días.

## CLI esperado

```powershell
pmr paper run --rule inventory_cycling_v1 --watchlist world_cup_2026
pmr paper status
pmr paper report --out /data/exports/paper_inventory_cycling_v1.md
pmr paper gate --rule inventory_cycling_v1
```

## Acceptance criteria

```text
- Corre mínimo 7 días sin dinero real.
- Registra señales, fills hipotéticos, inventario y PnL.
- Reconciliación interna del paper ledger pasa.
- La estrategia no depende de copiar fills RN1 después de verlos.
- Se reporta fill-rate conservador.
- Se reporta PnL neto de fees estimado.
- Kill switch manual y automático fueron probados.
- Produce PASS/FAIL explícito.
```

---

# Phase 24 — Minimal Live Execution

## Pregunta central

```text
¿Se puede ejecutar una versión pequeña, limitada y apagable de la estrategia con riesgo controlado?
```

## Requisito previo obligatorio

No implementar ni activar live hasta que Phase 23 tenga `PASS`.

```text
Phase 24 cannot start unless Phase 23 has PASS status.
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
operar si datos están stale
operar si reconciliation/sync falla
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

Orden recomendado:

```text
DRY_RUN → MANUAL_APPROVAL → LIVE_LIMITED
```

## Límite inicial recomendado

La primera ejecución real debe ser muy pequeña:

```text
max_total_capital <= 100 USDC
```

Aumentos posteriores requieren nuevo paper/live review.

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
kill_switch_auto_on_daily_loss
kill_switch_auto_on_unexpected_position
```

## Tablas sugeridas

```text
live_orders
live_fills
live_inventory
live_pnl_daily
live_risk_state
live_kill_switch_log
execution_audit_log
```

## CLI esperado

```powershell
pmr exec dry-run --rule inventory_cycling_v1 --watchlist world_cup_2026
pmr exec propose --rule inventory_cycling_v1 --watchlist world_cup_2026
pmr exec live --rule inventory_cycling_v1 --watchlist world_cup_2026 --max-capital 100
pmr exec kill-switch
pmr exec status
pmr exec audit --out /data/exports/live_audit.md
```

## Acceptance criteria

```text
- Live mode requiere límites explícitos.
- No opera si book está stale.
- No opera si sync/reconcile está fallando.
- No opera si paper gate no está PASS.
- Todas las órdenes quedan auditadas.
- Puede apagar y cancelar órdenes abiertas.
- PnL y exposición se reportan diariamente.
- Kill switch fue probado antes de live.
- Primer live limitado usa capital pequeño.
```

---

# Orden recomendado actualizado

## Pipeline principal

```text
Phase 18 → Phase 20 → Phase 21 → Phase 22 → Phase 23 → Phase 24
```

## Phase 19 corre en paralelo

```text
          Phase 19
             ↑
Phase 18 → Phase 20 → Phase 21 → Phase 22 → Phase 23 → Phase 24
```

Razón:

```text
Phase 19 ayuda a elegir wallets candidatas,
pero no debe bloquear la construcción del dataset ni las reglas.
```

---

# Outputs mínimos por fase

## Phase 18

```text
context_coverage_report.md
fill_context_table.csv
watchlist_freshness_report.csv
```

Pregunta:

```text
¿Tenemos books frescos antes de fills?
```

## Phase 19

```text
cluster_candidate_report.md
wallet_similarity_score.csv
match_table.csv
wallet_candidates_ranked.csv
```

Pregunta:

```text
¿Qué wallets vale la pena seguir?
```

## Phase 20

```text
microstructure_lifecycle_dataset.parquet
dataset_quality_report.md
feature_null_reason_report.csv
close_path_summary.csv
```

Pregunta:

```text
¿Tenemos dataset suficientemente limpio para reglas?
```

## Phase 21

```text
rules_evaluation_report.md
explained_fills_table.csv
unexplained_fills_table.csv
out_of_sample_report.csv
```

Pregunta:

```text
¿Podemos explicar entradas con datos previos al fill?
```

## Phase 22

```text
simulation_runs.csv
capital_required_report.md
scenario_comparison_report.md
risk_report.md
```

Pregunta:

```text
¿La regla se puede operar sin copiar a RN1?
```

## Phase 23

```text
paper_orders.csv
paper_fills.csv
paper_inventory.csv
paper_pnl_daily.csv
paper_risk_report.md
paper_gate_result.md
```

Pregunta:

```text
¿La regla funciona forward sin dinero?
```

## Phase 24

```text
live_orders_audit.csv
live_risk_state.csv
live_daily_pnl.csv
kill_switch_log.csv
live_audit.md
```

Pregunta:

```text
¿La ejecución mínima es segura y apagable?
```

---

# Métricas clave finales

## Por wallet

```text
PnL UI 1D / 1W / 1M
positions_value
fills
notional
maker_share
taker_share
buy_notional
sell_notional
entry_edge_bps
winsorized_entry_edge_bps
strict_context_share
closed_position_share
avg_hold_seconds
net_to_gross_ratio
event_basket_density
completion_candidate_edge
```

## Por evento

```text
wallet
event
PnL
notional
fills
entry_edge_bps
open_exposure
closed_share
questions_traded
tokens_traded
completion_candidates
bond_delta_total
max_directional_exposure
```

## Por regla

```text
rule_name
rule_version
fills_explained_pct
false_positive_rate
out_of_sample_edge_bps
out_of_sample_pnl
capital_required
max_drawdown
max_event_exposure
conservative_fill_rate
paper_status
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

---

# Criterio para saber si vamos bien

Después de Phase 18–21:

```text
¿Puedo explicar una parte grande de las entradas de RN1/Mind.The.Gap
con información observable antes del fill?
```

Después de Phase 22–23:

```text
¿Esa explicación sobrevive en simulación y paper trading
sin copiar a RN1 después de ver sus fills?
```

Sólo si ambas respuestas son sí, tiene sentido pasar a ejecución.

---

# Estado recomendado de prioridades

## RN1

Hipótesis actual:

```text
Maker-first event basket inventory cycler.
Compra inventario barato contra mid, acumula complementos,
y sale por redeem/merge/resolution más que por SELL.
```

Prioridad:

```text
Alta como north star / edge profundo.
```

## Mind.The.Gap

Hipótesis actual:

```text
Event trader / local market maker con ciclos más cerrados.
Menos predicciones, PnL semanal alto, posiciones actuales en cero,
posiblemente más fácil de reconstruir lifecycle.
```

Prioridad:

```text
Alta como research candidate y contraste operativo.
```

## Objetivo de investigación inmediata

```text
Entrada contra book + evolución de inventario + close path + PnL por evento.
```

No seguir agregando métricas si no ayudan a contestar ese loop.
