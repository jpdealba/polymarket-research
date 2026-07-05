# RN1 vs Mind.The.Gap — descubrimientos y tesis actual

*Generado: 2026-07-05*

## 0. Resumen ejecutivo

La evidencia actual apunta a que **RN1** y **Mind.The.Gap** pertenecen a la misma familia de estrategia, pero con perfiles distintos:

- **RN1** valida que la mecánica de *complete-set / inventory cycling* escala a muchísimo volumen.
- **Mind.The.Gap** valida que, en la ventana reciente, existe una variante más selectiva, más rápida y con mayor edge unitario.
- Para capital pequeño, Gap parece más útil como plantilla operativa inicial.
- Para entender robustez estructural, RN1 sigue siendo mejor benchmark.

La tesis actual:

```text
RN1 = completion-set inventory cycler maduro, diversificado y de gran escala.
Gap = variante reciente, concentrada, rápida y con mayor edge por set, posiblemente con más winner-carry / exits secundarios.
```

---



## 1. Cómo se interpreta el edge

El edge que se está midiendo viene de construir un **complete set** en un mercado binario.

En un mercado binario, si compras ambos outcomes:

```text
YES + NO = complete set
```

Ese set vale aproximadamente:

```text
$1.00
```

La fórmula usada fue:

```text
edge_per_set = 1 - (price_leg_0 + price_leg_1)
```

Ejemplo:

```text
price_leg_0 + price_leg_1 = 0.9559
edge = 1.0000 - 0.9559 = 0.0441
```

Eso significa:

```text
Compraste un set que vale $1.00 por $0.9559.
Margen bruto teórico: $0.0441 por set.
```

Traducción correcta:

```text
4.41 centavos de edge bruto por cada $1 de valor redimible construido como complete set.
```

No es exactamente “por cada dólar total invertido en el wallet”, porque el wallet también puede tener:

- inventario no emparejado,
- trades que no terminan en complete set,
- pérdidas,
- fees,
- slippage,
- capital bloqueado,
- ventas secundarias,
- REDEEM/settlement no capturado por el matching FIFO.

Para convertirlo a ROI sobre el costo del complete set:

```text
ROI sobre costo = edge / costo
```

Para Gap:

```text
edge = 0.044126
cost = 0.955874
ROI sobre costo ≈ 0.044126 / 0.955874 = 4.62%
```

Para RN1 reciente:

```text
edge = 0.011195
cost = 0.988805
ROI sobre costo ≈ 0.011195 / 0.988805 = 1.13%
```

---

## 3. RN1 global — hallazgo base

Query FIFO de complete sets sobre RN1 global:

```text
matched_pair_rows:          2,876,320
binary_questions:           64,930
matched_complete_sets:      300,836,896.066255
total_pair_cost_usdc:       292,427,436.338910
theoretical_pair_edge_usdc: 8,409,459.727345
weighted_edge_per_pair:     0.027954
weighted_edge_cents:        2.7954¢
weighted_edge_bps:          279.54 bps
avg_leg_gap_s:              5,156.72
avg_leg_gap_min:            85.95
```

Interpretación:

```text
RN1 compró complete sets teóricos a un costo promedio de ~97.2046¢.
Su edge bruto promedio fue ~2.7954¢ por set.
```

Esto apoya la tesis de que RN1 no es principalmente un trader direccional clásico. Su edge principal parece venir de construir inventario complementario en mercados binarios con costo agregado menor a $1.

---



## 4. Cashflows de RN1

Validación contra eventos reales:

```text
MERGE                 56,143 eventos   161,532,889.397044
REDEEM                81,282 eventos   243,071,639.613772
REDEEM_PAYOUT         18,632 eventos   232,870.638154
RESOLUTION_SETTLEMENT 13,564 eventos   46,305,203.491594
```

Lectura:

```text
RN1 monetiza muchísimo por MERGE y REDEEM.
```

Esto es coherente con la mecánica:

```text
comprar ambos outcomes -> formar complete set -> MERGE/REDEEM/resolution
```

Caveat importante:

```text
matched_complete_sets FIFO ≈ 300.8M
MERGE real ≈ 161.5M
```

Por tanto, el matching FIFO mide **pares comprados emparejables**, no necesariamente pares efectivamente mergeados. Aun así, el orden de magnitud confirma que la mecánica existe.

---



## 5. Comparación global reciente: Gap vs RN1 desde 2026-05-23

Esta ventana controla por la vida aproximada de Mind.The.Gap.

```text
Mind.The.Gap
matched_pair_rows:          11,403
binary_questions:           614
matched_complete_sets:      6,049,969.137555
total_pair_cost_usdc:       5,783,010.341295
theoretical_pair_edge_usdc: 266,958.796260
weighted_edge_per_pair:     0.044126
weighted_edge_cents:        4.4126¢
weighted_edge_bps:          441.26 bps
pct_qty_positive_edge:      63.73%
pct_qty_under_60s:          5.09%
avg_leg_gap_s:              1,330.44
avg_leg_gap_min:            22.17
```

```text
RN1 desde 2026-05-23
matched_pair_rows:          537,152
binary_questions:           10,732
matched_complete_sets:      95,753,340.349253
total_pair_cost_usdc:       94,681,389.911880
theoretical_pair_edge_usdc: 1,071,950.437373
weighted_edge_per_pair:     0.011195
weighted_edge_cents:        1.1195¢
weighted_edge_bps:          111.95 bps
pct_qty_positive_edge:      54.54%
pct_qty_under_60s:          3.79%
avg_leg_gap_s:              4,203.58
avg_leg_gap_min:            70.06
```

Lectura:

```text
Gap tiene ~4x más edge por set que RN1 reciente.
RN1 tiene ~15.8x más matched sets.
RN1 tiene ~4x más edge total.
```

Conclusión:

```text
Gap = mayor ROI unitario.
RN1 = mayor capacidad total.
```

---



## 6. Mercados comunes: Gap vs RN1

Comparación solo en mercados binarios donde ambos wallets compraron ambos outcomes.

```text
Mind.The.Gap
matched_pair_rows:          11,306
common_binary_questions:    596
matched_complete_sets:      6,024,372.382200
total_pair_cost_usdc:       5,759,159.753042
theoretical_pair_edge_usdc: 265,212.629158
weighted_edge_per_pair:     0.044023
weighted_edge_cents:        4.4023¢
weighted_edge_bps:          440.23 bps
pct_qty_positive_edge:      63.70%
pct_qty_under_60s:          5.11%
avg_leg_gap_s:              1,326.88
avg_leg_gap_min:            22.11
```

```text
RN1 en esos mismos mercados
matched_pair_rows:          103,224
common_binary_questions:    596
matched_complete_sets:      25,943,061.889415
total_pair_cost_usdc:       25,757,337.470372
theoretical_pair_edge_usdc: 185,724.419043
weighted_edge_per_pair:     0.007159
weighted_edge_cents:        0.7159¢
weighted_edge_bps:          71.59 bps
pct_qty_positive_edge:      52.56%
pct_qty_under_60s:          4.30%
avg_leg_gap_s:              4,756.57
avg_leg_gap_min:            79.28
```

Esto es uno de los hallazgos más importantes:

```text
Incluso en los mismos mercados, Gap captura mucho más edge por set que RN1.
```

Esto descarta parcialmente la explicación de que Gap solo gana más edge porque opera mercados completamente diferentes.

La lectura correcta:

```text
Gap no solo elige otro universo.
En mercados compartidos también parece ejecutar o seleccionar mejor las ventanas de entrada.
```

---



## 7. Distribución temporal entre patas

Comparación global:

```text
Mind.The.Gap
matched_complete_sets:      6,049,969.137555
pct_matched_qty_under_60s:  5.0878%
p50_leg_gap_s:              470
p90_leg_gap_s:              3,447
p50_leg_gap_min:            7.83
p90_leg_gap_min:            57.45
weighted_edge_cents:        4.4126¢
```

```text
RN1
matched_complete_sets:      300,836,896.066255
pct_matched_qty_under_60s:  4.1241%
p50_leg_gap_s:              1,194
p90_leg_gap_s:              5,369
p50_leg_gap_min:            19.90
p90_leg_gap_min:            89.48
weighted_edge_cents:        2.7954¢
```

Lectura:

```text
Gap completa patas más rápido que RN1.
No es flash arbitrage: solo ~5% completa en menos de 60s.
Pero sí es más rápido que RN1.
```

Gap parece operar ciclos más cortos:

```text
Gap p50 ≈ 7.8 min
RN1 p50 ≈ 19.9 min
```

---



## 8. Cashflows de Gap

Validación de eventos:

```text
Mind.The.Gap
REDEEM                613 eventos   8,498,703.796193
REDEEM_PAYOUT         1 evento      0.0
RESOLUTION_SETTLEMENT 413 eventos   5.941691 delta_usdc
```

No apareció MERGE claro en la salida agregada.

Auditoría de transacciones sospechosas mostró muchas transacciones con:

```text
TRADE
TRADE,RESOLUTION_SETTLEMENT
BUY,SELL
2 tokens por condition_id
```

Pero no se observó `MERGE` explícito.

Interpretación provisional:

```text
RN1 monetiza mucho vía MERGE + REDEEM.
Gap parece monetizar principalmente vía REDEEM, settlement y/o exits secundarios.
```

Caveat:

```text
Todavía puede existir un problema de ingest/parsing/on-chain coverage.
Para cerrarlo completamente habría que revisar 3-5 tx_hash directamente on-chain.
```

---



## 9. Edge por bucket de volumen



### [Mind.The.Gap](http://Mind.The.Gap)

```text
<1k        markets=259 matched_sets=82,750.295344    edge=5,106.328654   edge_cents=6.1708¢
1k-10k     markets=239 matched_sets=925,387.947813   edge=12,587.741834  edge_cents=1.3603¢
10k-50k    markets=89  matched_sets=2,047,748.108998 edge=118,517.760898 edge_cents=5.7877¢
50k-100k   markets=13  matched_sets=837,502.566857   edge=56,276.476723  edge_cents=6.7196¢
100k-250k  markets=13  matched_sets=1,878,375.963211 edge=55,661.485465  edge_cents=2.9633¢
250k-500k  markets=1   matched_sets=278,204.255332   edge=18,809.002685  edge_cents=6.7609¢
```



### RN1 reciente

```text
<1k        markets=5,397 matched_sets=1,044,038.485483  edge=25,982.995229  edge_cents=2.4887¢
1k-10k     markets=3,185 matched_sets=12,623,532.675331 edge=209,235.256166 edge_cents=1.6575¢
10k-50k    markets=1,676 matched_sets=37,614,064.892485 edge=574,209.101449 edge_cents=1.5266¢
50k-100k   markets=339   matched_sets=23,257,765.497741 edge=112,361.387132 edge_cents=0.4831¢
100k-250k  markets=127   matched_sets=18,395,011.427983 edge=52,638.424012  edge_cents=0.2862¢
250k-500k  markets=8     matched_sets=2,818,927.370230  edge=97,523.273385  edge_cents=3.4596¢
```

Lectura:

```text
RN1 sí muestra compresión de edge conforme aumenta el tamaño por mercado.
Gap no muestra una relación lineal limpia; parece más selectivo y concentrado.
```

RN1, quitando el bucket outlier de 250k-500k:

```text
2.49¢ -> 1.66¢ -> 1.53¢ -> 0.48¢ -> 0.29¢
```

Esto apoya la hipótesis de **capacity decay**:

```text
A mayor volumen/capacidad usada, menor edge promedio por set.
```

---



## 10. Concentración del edge



### Resultado agregado

```text
Mind.The.Gap
markets_total:                  614
markets_positive_edge:          430
matched_sets:                   6,049,969.137555
net_edge_usdc:                  266,958.796260
positive_edge_usdc:             396,384.851149
net_edge_cents:                 4.4126¢
positive_edge_cents_all_sets:   6.5518¢
top_5_edge_pct:                 27.16%
top_10_edge_pct:                37.09%
top_25_edge_pct:                57.87%
top_50_edge_pct:                75.29%
top_100_edge_pct:               89.22%
markets_to_50pct_edge:          19
markets_to_80pct_edge:          62
hhi_edge_concentration:         0.022594
```

```text
RN1
markets_total:                  10,732
markets_positive_edge:          6,392
matched_sets:                   95,753,340.349253
net_edge_usdc:                  1,071,950.437373
positive_edge_usdc:             4,688,625.525661
net_edge_cents:                 1.1195¢
positive_edge_cents_all_sets:   4.8966¢
top_5_edge_pct:                 4.32%
top_10_edge_pct:                7.38%
top_25_edge_pct:                13.70%
top_50_edge_pct:                21.04%
top_100_edge_pct:               31.15%
markets_to_50pct_edge:          253
markets_to_80pct_edge:          844
hhi_edge_concentration:         0.001699
```

Lectura:

```text
Gap tiene edge alto, pero está mucho más concentrado.
RN1 tiene edge menor, pero está mucho más distribuido.
```

Gap:

```text
Top 5 mercados = 27.16% del positive edge
Top 10 mercados = 37.09%
Top 25 mercados = 57.87%
Top 50 mercados = 75.29%
```

RN1:

```text
Top 5 mercados = 4.32%
Top 10 mercados = 7.38%
Top 25 mercados = 13.70%
Top 50 mercados = 21.04%
```

Esto confirma:

```text
Gap = high edge, high concentration.
RN1 = lower edge, high diversification.
```

---



## 11. Top mercados de Gap

Los primeros mercados que explican el edge positivo de Gap fueron:

```text
1. Will Germany win on 2026-06-20?
   matched_sets: 111,594.567773
   edge_usdc:    28,492.191622
   edge_cents:   25.5319¢
   cumulative:   7.19%

2. Will United States win on 2026-06-25?
   matched_sets: 78,183.286893
   edge_usdc:    27,355.192759
   edge_cents:   34.9885¢
   cumulative:   14.09%

3. Will Uruguay win on 2026-06-21?
   matched_sets: 108,792.963650
   edge_usdc:    20,448.200260
   edge_cents:   18.7955¢
   cumulative:   19.25%

4. Will Norway win on 2026-06-22?
   matched_sets: 278,204.255332
   edge_usdc:    18,809.002685
   edge_cents:   6.7609¢
   cumulative:   23.99%

5. Will Czechia win on 2026-06-18?
   matched_sets: 26,093.159728
   edge_usdc:    12,536.540773
   edge_cents:   48.0453¢
   cumulative:   27.16%
```

Top 10 de Gap llega a:

```text
37.09% del positive edge
```

Top 25:

```text
57.87% del positive edge
```

Top 50:

```text
75.29% del positive edge
```

---



## 12. Top mercados de RN1 reciente

RN1 está mucho más distribuido. Sus primeros mercados aportan muy poco individualmente:

```text
Top 1:  0.99%
Top 5:  4.32%
Top 10: 7.38%
Top 25: 13.70%
Top 50: 21.04%
```

Esto muestra que RN1 no depende de pocos mercados.

Lectura:

```text
RN1 escala por diversificación masiva.
Gap captura edge alto en pocas oportunidades.
```

---



## 13. Interpretación estratégica



### RN1

RN1 parece optimizar:

```text
edge total absoluto
alta escala
alta diversificación
rotación masiva de inventario
capacidad de operar miles de mercados
```

Su problema no es encontrar el mayor ROI unitario, sino desplegar mucho volumen.

```text
RN1 acepta menor edge por set para operar muchísimo más tamaño.
```



### [Mind.The.Gap](http://Mind.The.Gap)

Gap parece optimizar:

```text
edge unitario alto
selección de mercados
ciclos más rápidos
concentración en oportunidades específicas
```

Su riesgo es mayor concentración:

```text
19 mercados explican 50% del positive edge.
62 mercados explican 80%.
```

---



## 14. Implicación para una estrategia con capital pequeño

Para una cuenta pequeña, por ejemplo $1,000, Gap es más relevante que RN1 como plantilla inicial.

Motivo:

```text
No necesitas capacidad para operar 95M sets.
Necesitas detectar oportunidades pequeñas con edge alto.
```

La estrategia no debería intentar copiar el promedio de RN1.

Debería buscar:

```text
mercados tipo Gap
edge alto
book fresco
spread suficiente
ambas patas disponibles
riesgo de imbalance controlado
concentración limitada por evento/torneo
```

---



## 15. Riesgos y caveats



### 15.1 FIFO no es PnL realizado exacto

El matching FIFO estima pares emparejables por compras acumuladas. No modela perfectamente:

- MERGE real,
- REDEEM real,
- ventas secundarias,
- fees,
- slippage,
- órdenes no llenadas,
- colas de maker,
- mark-to-market,
- inventario temporal.

Por tanto, el edge calculado es:

```text
edge bruto teórico por complete-set FIFO
```

No es PnL neto auditado.

### 15.2 Gap puede tener componente adicional

Gap tiene aproximadamente:

```text
complete-set theoretical edge ≈ 267k
reported P/L visible ≈ 1.37M
```

Por tanto, el complete-set edge explica una parte, pero no todo.

Hipótesis adicional:

```text
Gap = complete-set edge + winner carry + exits secundarios + REDEEM/settlement
```



### 15.3 Concentración

Gap es atractivo, pero concentrado. Si no se detectan los mercados top, el edge baja mucho.

### 15.4 Forward validation pendiente

La gran pregunta no es solo si existió edge histórico. La pregunta operativa es:

```text
¿Se puede detectar antes del fill con book snapshots frescos?
```

---



## 16. Próxima fase recomendada

La siguiente fase debe ser forward/paper, no más histórico solamente.

Objetivo:

```text
Detectar oportunidades tipo Gap antes del fill usando orderbook fresco.
```

Reglas mínimas:

```text
book age <= 5s excelente
book age <= 15s bueno
book age <= 30s usable
book age > 60s no usar para conclusiones
```

Señales candidatas:

```text
pair_cost = candidate_price_leg_A + current_available_price_leg_B
edge = 1 - pair_cost
edge_after_fees > threshold
spread suficiente
profundidad suficiente para tamaño pequeño
no evento informativo extremo
inventario resultante no excede límite
```

Paper trading debe medir:

```text
signals
simulated orders
conservative fills
queue assumptions
completed pairs
unmatched inventory
gross edge
fees/slippage
net PnL
capital lock time
concentration by event
```

---



## 17. Tesis final actual

```text
RN1 demuestra que la mecánica de complete-set / inventory cycling puede escalar a cientos de millones de sets, pero su edge reciente por set se comprime con el volumen.

Mind.The.Gap muestra una variante reciente, más concentrada y más rápida, con edge unitario mucho mayor. En la ventana desde 2026-05-23, Gap obtiene ~4.41¢ por complete set frente a ~1.12¢ de RN1 reciente. En mercados comunes, la diferencia es aún más clara: Gap ~4.40¢ vs RN1 ~0.72¢.

La contrapartida es la concentración: Gap depende mucho más de pocos mercados. Sus top 25 mercados explican ~57.9% del positive edge y sus top 50 explican ~75.3%, mientras RN1 necesita cientos de mercados para acumular la misma proporción.

Para capital pequeño, Gap es mejor plantilla operativa. Para validar robustez estructural, RN1 sigue siendo el benchmark más fuerte.
```

---



## 18. Frase corta para recordar

```text
RN1 gana por escala y diversificación.
Gap gana por selección y edge unitario.
```





Hipotesis:  
Cuando Gap/RN1 llenan una pata, ¿el book ya permitía construir un set con edge positivo antes del fill?


| Pregunta             | Estado actual                                                                                                                                          |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| ¿Qué mercado?        | Probablemente deportes binarios con ineficiencia temporal, especialmente fútbol/torneos.                                                               |
| ¿Qué precio?         | Debe ser precio que deje `YES + NO < 1`, idealmente con buffer.                                                                                        |
| ¿Cuánto comprar?     | No está resuelto. Depende de liquidez, exposición máxima y probabilidad de completar la otra pata.                                                     |
| ¿Compra ambos lados? | Sí, la evidencia apunta fuerte a eso.                                                                                                                  |
| ¿Merge o redeem?     | RN1 sí mergea mucho. Gap parece más redeem/settlement/trading.                                                                                         |
| ¿Fees?               | Probablemente no destruyen la estrategia, pero falta netear formalmente.                                                                               |
| ¿Ventas anticipadas? | En RN1 parecen poco relevantes. En Gap sí hay más señal de BUY/SELL, falta separar si son exits, rebalances o trades de resolución.                    |
| ¿Se puede replicar?  | Aún no. Falta forward watch + paper fill model.Cuando Gap/RN1 llenan una pata, ¿el book ya permitía construir un set con edge positivo antes del fill? |


