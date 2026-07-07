# RN1 Playbook — Reglas Vivas V1

Versión limpia para implementación inicial en paper trading. La lógica copia el régimen temprano de RN1, no el tamaño actual de RN1 maduro.

## 0. Parámetros base

```yaml
strategy: rn1_inventory_cycling_v1
mode: paper_only
bankroll: capital_inicial_real
starting_bankroll_reference: 1180

maker_only: true
allow_cross_ask: false
allow_market_buy: false
allow_sell: false

base_order_pct: 0.025
hard_max_order_usdc: 100
min_order_usdc: 5
base_order_shares: 100
round_shares_to: 10

target_set_cost_default: 0.98
target_set_cost_risk_reducing: 0.99
target_set_cost_shadow: 1.00
target_set_cost_1_00_trading_enabled: false

condition_gross_cap_pct: 0.25
condition_unmatched_cap_pct: 0.125

event_gross_cap_pct: 0.40
event_unmatched_cap_pct: 0.20

global_gross_cap_pct: 0.75
global_unmatched_cap_pct: 0.35

gross_cap_mode: soft_for_risk_reducing_complement
unmatched_cap_mode: hard

risk_reducing_override:
  enabled: true
  applies_to: BUY_COMPLEMENT
  max_target_set_cost: 0.99
  max_extra_condition_gross_pct: 0.10
  max_extra_event_gross_pct: 0.10
  require_unmatched_risk_reduction: true
  require_expected_mergeable_sets_increase: true

merge_dust_threshold_sets: 50
merge_priority_threshold_sets: 250
merge_fraction: 0.99

weekly_scale_cap: 1.30

daily_loss_kill_pct: 0.05
open_drawdown_kill_pct: 0.10
```

## 1. Selección y expansión de mercados

RN1 no abre todas las condiciones de un evento al mismo tiempo.

Regla:

```text
Empezar con pocas conditions por event.
Expandir sólo si aparece oportunidad concreta.
No abrir un basket completo por default.
```

Una nueva condition sólo se permite si cumple:

```text
liquidez suficiente
edge o inventario útil
espacio en event cap
book fresco
mercado todavía operable
```

## 2. BUY_COMPLEMENT

Comprar la pata faltante cuando permite completar sets con costo aceptable.

Fórmula:

```text
max_price = target_set_cost - other_leg_wac
```

Entrada default:

```text
other_leg_wac + limit_price <= 0.98
```

Entrada risk-reducing:

```text
other_leg_wac + limit_price <= 0.99
sólo si reduce unmatched risk y aumenta expected mergeable sets
```

Shadow / diagnóstico:

```text
0.99 < set_cost <= 1.00
registrar señal, pero no operar en V1
```

Regla:

```text
Si hay inventario unilateral y la pata contraria cumple el set_cost, poner BUY pasivo.
BUY_COMPLEMENT es la señal principal del sistema.
```

## 3. ADD_DIRECTIONAL

ADD_DIRECTIONAL existe, pero es la parte más peligrosa.

### 3.1 Average Down

Comprar más de la misma pata si:

```text
current_price <= same_leg_wac - 0.05
```

### 3.2 Layering

Repostear cerca del costo promedio si:

```text
ABS(current_price - same_leg_wac) < 0.05
```

### 3.3 Momentum Add

Comprar arriba del WAC sólo con límites estrictos:

```text
current_price >= same_leg_wac + 0.05
```

Condiciones obligatorias:

```text
hay inventario contrario suficiente
no rompe unmatched caps
no rompe condition/event/global caps
mercado sigue líquido
orden pasiva, no cruzar ask en V1
size reducido frente a BUY_COMPLEMENT
```

Regla V1:

```text
ADD_DIRECTIONAL_RESTRICTED
size_multiplier = 0.50
allow_above_wac = false_initially
```

## 4. Entrada pasiva

La entrada base es maker/passive.

Permitido:

```text
best_bid
best_bid + 1 tick si no cruza ask
best_bid - 1 tick
```

No permitido en V1:

```text
market buy
cross ask
perseguir precio
```

## 5. Edge y naturaleza de la estrategia

La estrategia no es arbitraje puro. Es inventory cycling con riesgo direccional.

Regla V1:

```text
BUY_COMPLEMENT default: set_cost <= 0.98.
BUY_COMPLEMENT risk-reducing: set_cost <= 0.99.
0.99 < set_cost <= 1.00 se registra como shadow, no se opera en V1.
ADD_DIRECTIONAL sólo se permite dentro de caps y con size reducido.
```

## 6. Sizing

Usar porcentaje de bankroll con hard cap.

Fórmula:

```text
raw_order_usdc = bankroll * base_order_pct

order_usdc = min(
  raw_order_usdc,
  hard_max_order_usdc,
  remaining_condition_cap,
  remaining_event_cap,
  remaining_global_cap,
  remaining_unmatched_cap
)

target_shares = floor_to_multiple(order_usdc / limit_price, round_shares_to)
```

Con `bankroll = 1180`:

```text
base_order = 1180 * 0.025 = 29.50 USDC
```

Con `bankroll = 2844`:

```text
base_order = 2844 * 0.025 = 71.10 USDC
```

Con `bankroll = 4421`:

```text
base_order = 4421 * 0.025 = 110.53 USDC
aplicar hard cap = 100 USDC
```

Regla:

```text
Copiar sizing temprano de RN1: clips chicos, redondeados en shares.
No copiar sizing maduro actual.
```

## 7. Caps de exposición

### 7.1 Condition cap

```text
condition_gross_cap = bankroll * 0.25
condition_unmatched_cap = bankroll * 0.125
```

### 7.2 Event cap

```text
event_gross_cap = bankroll * 0.40
event_unmatched_cap = bankroll * 0.20
```

### 7.3 Global cap

```text
global_gross_cap = bankroll * 0.75
global_unmatched_cap = bankroll * 0.35
```

Regla:

```text
Unmatched caps son hard caps.
Gross caps son hard caps excepto para BUY_COMPLEMENT risk-reducing.
```

Override permitido:

```text
BUY_COMPLEMENT puede exceder ligeramente gross caps sólo si:
  reduce unmatched risk,
  aumenta expected mergeable sets,
  set_cost <= 0.99,
  no rompe ningún unmatched cap.
```

## 8. Cálculo de exposición

La exposición no es el total comprado histórico.

Fórmula:

```text
open_risk =
  buy_cost
  - sell_proceeds
  - merge_proceeds
  - redeem_proceeds
  - resolution_settlement_proceeds
```

Después de BUY:

```text
open_risk sube
```

Después de MERGE:

```text
open_risk baja
capital se libera inmediatamente
```

Después de REDEEM:

```text
open_risk baja
capital se libera post-close
```

Recalcular caps después de cada fill, merge o redeem.

## 9. Unmatched risk

Medir exposición no hedgeada por condition, event y global.

Aproximación:

```text
condition_unmatched_risk = ABS(cost_yes_open - cost_no_open)
```

Para BUY_COMPLEMENT:

```text
No comprar más shares que el gap necesario para balancear,
salvo que ADD_DIRECTIONAL esté explícitamente permitido.
```

Para ADD_DIRECTIONAL:

```text
Sólo permitir si queda espacio en unmatched caps.
```

## 10. Ladder de BUY_COMPLEMENT

Primero calcular:

```text
max_price = target_set_cost - other_leg_wac
```

Sólo poner órdenes con:

```text
limit_price <= max_price
```

Ladder base:

```text
40% del clip: best_bid + 1 tick, si no cruza ask y <= max_price
40% del clip: best_bid, si <= max_price
20% del clip: best_bid - 1 tick, si > 0 y <= max_price
```

Si el spread está muy cerrado:

```text
70% del clip: best_bid
30% del clip: best_bid - 1 tick
```

## 11. Ladder de ADD_DIRECTIONAL

Ladder base:

```text
60% del clip: best_bid
25% del clip: best_bid - 1 tick
15% del clip: best_bid - 2 ticks
```

Condiciones:

```text
no romper condition_unmatched_cap
no romper event_unmatched_cap
no romper global_unmatched_cap
no usar si la condition ya está demasiado unilateral
no cruzar ask en V1
```

## 12. MERGE

MERGE recicla capital y no depende de halftime.

Fórmula:

```text
complete_sets_available = min(open_yes_shares, open_no_shares)
```

Regla:

```text
Si complete_sets_available >= 50, mergear casi todo.
```

Cantidad:

```text
merge_sets = complete_sets_available * 0.99
```

Prioridad alta:

```text
complete_sets_available >= 250
```

Después de MERGE:

```text
condition_open_risk -= merge_usdc
event_open_risk -= merge_usdc
global_open_risk -= merge_usdc
```

## 13. REDEEM

REDEEM es post-close y separado de MERGE.

Regla:

```text
Después de close/resolution, redimir winning inventory.
```

Cashflow real:

```text
usar REDEEM con NONZERO_USDC
ignorar REDEEM_PAYOUT con 0 USDC
ignorar eventos contables duplicados de 0 USDC
```

Cadencia:

```text
0-2h post-close: revisar cada 15-30 min
2-12h post-close: revisar cada 1h
12h+: revisar con baja prioridad
```

## 14. SELL

SELL no es salida estructural.

V1:

```text
allow_sell = false
```

Salida normal:

```text
MERGE para complete sets
REDEEM para winning inventory
```

SELL sólo queda como excepción futura:

```text
high-certainty cleanup si price >= 0.98
risk cleanup manual
salida táctica rara
```

## 15. Cancel / Reprice

Cancelar o repricear si:

```text
book se movió >= 1 tick
spread cambió fuerte
max_price ya no cumple edge
condition cap alcanzado
event cap alcanzado
global cap alcanzado
unmatched cap alcanzado
fill parcial cambió inventario
orden lleva demasiado tiempo sin fill
book está stale
mercado cerca de resolución con directional risk
```

Cadencia inicial:

```text
market scan: 1-3s en mercados activos
order reprice: 15-30s
inventory/merge loop: 30-60s o después de fills
redeem loop: 15-60min post-close
```

## 16. Weekly scaling

Escalar por semana cerrada, no por trade individual.

Fórmula:

```text
actual_bankroll = starting_bankroll + realized_pnl
sizing_bankroll = min(actual_bankroll, previous_week_bankroll * 1.30)
```

Reglas:

```text
semana positiva sin breach: subir máximo 20-30%
semana negativa: mantener o bajar 20%
breach de drawdown/exposure: bajar aunque PnL sea positivo
no duplicar tamaño sólo porque el bankroll duplicó
```

## 17. Pause / Kill switch

Pausar condition/event si:

```text
condition_open_risk >= condition_cap
event_open_risk >= event_cap
global_open_risk >= global_cap
condition_unmatched_risk >= condition_unmatched_cap
event_unmatched_risk >= event_unmatched_cap
global_unmatched_risk >= global_unmatched_cap
spread demasiado amplio
liquidez insuficiente
book stale
datos inconsistentes
market cerca de resolution con directional risk
```

Kill switch global si:

```text
daily realized loss <= -5% bankroll
estimated open drawdown <= -10% bankroll
API/order manager falla
no se puede calcular inventario
MERGE/REDEEM falla repetidamente
fills fuera de límites
órdenes duplicadas o descontroladas
```

## 18. Flujo operativo V1

```text
1. Seleccionar mercados líquidos.
2. Abrir pocas conditions por event.
3. Colocar bids pasivos.
4. Si se llena una pata, buscar BUY_COMPLEMENT.
5. Comprar complemento default sólo si set_cost <= 0.98.
6. Permitir set_cost <= 0.99 sólo si reduce unmatched risk.
7. Registrar 0.99 < set_cost <= 1.00 como shadow; no operar ese rango en V1.
8. Permitir ADD_DIRECTIONAL limitado si hay espacio de riesgo.
9. Mantener ladder pasivo.
10. Repricear/cancelar según book e inventario.
11. Si complete_sets >= 50, ejecutar MERGE casi total.
12. Recalcular exposure después de MERGE.
13. Después de close, ejecutar REDEEM.
14. Escalar tamaño semanalmente, máximo +30%.
15. No usar SELL en V1.
```

## 19. Prohibiciones V1

```text
no copiar tamaño actual de RN1
no cruzar ask por default
no usar market buys
no usar SELL como salida base
no abrir todas las conditions de un evento
no escalar por una sola trade ganadora
no dejar complete sets sin MERGE por mucho tiempo
no calcular exposición como total comprado histórico
no ignorar unmatched risk
no usar closed_time como game clock exacto
no asumir halftime sin datos reales de partido
no operar 0.99 < set_cost <= 1.00 en V1; sólo loggear como shadow
```

## 20. Selección de mercados

```text
Seleccionar con mínimo de liquidez
Watchlist limitada (soccer, tennis, baseball, esports)
spread razonable
dos outcomes binarios claros
```

## Lo más peligroso

```text
ADD_DIRECTIONAL es la parte más peligrosa
  BUY_COMPLEMENT está mucho mejor definido porque tiene fórmula clara:
    default: other_leg_wac + current_price <= 0.98
    risk-reducing: other_leg_wac + current_price <= 0.99
    shadow: 0.99 < set_cost <= 1.00, no operativo en V1
  ADD_DIRECTIONAL es más ambiguo porque depende de inventario, momentum, WAC, timing y tolerancia al riesgo.

Ladder exacto no validado

Queue position / probabilidad de fill

Mark-to-market / drawdown
  Hasta ahora usamos exposición por cashflow:
  BUY - SELL - MERGE - REDEEM

  Eso sirve para riesgo operativo, pero no mide drawdown real de posiciones abiertas.
  Para paper necesitamos registrar también:
    mark_price actual
    unrealized_pnl
    worst_case_loss
    unmatched directional exposure
```


## Métricas obligatorias de paper

```text
signals_total
orders_placed
orders_skipped_by_cap
orders_skipped_by_unmatched_cap
orders_skipped_by_set_cost
cap_block_rate = orders_skipped_by_cap / signals_total
shadow_signals_0_99_to_1_00
missed_signal_would_reduce_unmatched
missed_signal_would_create_mergeable_sets
```

## Acceptance Criteria para paper

Paper mínimo: 7 días o 100+ señales.

PASS si:

```text
1. No rompe caps.
2. BUY_COMPLEMENT ejecutado tiene set_cost promedio <= 0.99; señales 0.99-1.00 quedan separadas como shadow.
3. Al menos 50% del capital mergeado viene de complete sets formados recientemente.
4. MERGE libera capital correctamente.
5. REDEEM detecta resolución y no duplica cashflow.
6. Drawdown estimado no supera 10%-15% del bankroll.
7. El bot no se queda atorado en inventario unilateral grande.
8. Fill assumptions están separadas de señales.
```
