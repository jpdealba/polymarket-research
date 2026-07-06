# BRA-NOR y MEX-ENG (2026-07-05): playbook de RN1/Gap, conciliación con Polymarket y gap de ingesta reparado

*Generado: 2026-07-05 (noche, tras la resolución de ambos partidos)*
*Fuentes: exports `nor_vs_bra.csv` / `mex_vs_eng.csv` (Desktop), ledger vivo `C:\data\db\pmresearch.db`, Data API y user-pnl-api de Polymarket.*

## 0. Qué es este documento

Análisis conjunto de los dos partidos del día (Brasil 1-2 Noruega, kickoff 20:00 UTC; México 2-3 Inglaterra, kickoff 01:00 UTC del 07-06) para RN1 y Mind.The.Gap, con tres resultados:

1. **Playbook de ejecución de RN1** extraído de ~12,000 fills — consistente entre ambos partidos.
2. **Conciliación exacta** entre nuestro ledger y el perfil público de Polymarket (que mostraba "+$288,877 1D" para RN1, aparentando que "ganó" el día del partido de México).
3. **Bug de ingesta encontrado, diagnosticado y reparado**: carrera de late-arrival en el sync incremental que perdió 167 eventos, incluido un REDEEM de $92,363. Esto corrige números del doc `brazil_norway_full_event_rn1_gap_review_2026-07-05.md`.

## 1. Playbook de RN1 (idéntico en ambos partidos)

RN1 no es un apostador direccional: es un **market maker de evento completo** con ciclo de inventario.

1. **Siembra días antes (T-77h a T-24h)** en clips redondos de 7,000 shares sobre patas de alta probabilidad (Team to Advance, "No" de exact scores). A veces empareja ya con la pata barata (ej. Brazil advance @0.64 + Norway advance @0.29 = 0.93 combinado).
2. **Ramp en las últimas 1-3 horas**: MEX-ENG, 849 fills y ~$152k en las 2h previas al kickoff cubriendo toda la retícula. Aun así el pre-partido es solo **6-10% del volumen** ($82k/$1.34M BRA-NOR; $240k/$2.5M MEX-ENG) — el grueso es in-play.
3. **Maker puro, solo BUY**: 0 SELLs en ~12,000 fills. Con book context `excellent`: ~60-64% de fills al bid o debajo, ~30% dentro del spread, solo ~6% cruzando al ask. Mediana fill = mid − 0.5¢ en todas las fases. La regla de entrada es "ser el mejor bid en todos los mercados del evento", no detectar edge puntual.
4. **Clip de tamaño fijo por mercado (~4,300-4,700 shares), idéntico en ambos lados** del mismo mercado, repetido decenas de veces a cualquier precio (ej. 4,418×38 en una pregunta con precios 0.00-0.99; 4,611×22 y 4,318×22 en BRA-NOR). Generaliza la "firma 4408" del doc anterior: no es un número único, es un lote fijo por mercado. Mismo tamaño en shares en ambos lados ⇒ pares llenados = complete-sets exactos.
5. **MERGE en batch en pausas naturales**: BRA-NOR, 9 condiciones en el mismo segundo al min ~87 (+$454k). MEX-ENG, 40 condiciones en 4s exactamente al final del descanso (+$713k) y 22 más al min ~89 (+$686k). Libera capital ~30-75 min antes que esperar la resolución.
6. **REDEEM 15-30 min tras la resolución** para todo lo no mergeado.
7. **Tolerancia a inventario unilateral**: gap medio entre patas emparejadas (FIFO) de 67-127 min, vs 14-20 min de Gap. El edge se realiza en el agregado del evento, no por mercado.

## 2. Mind.The.Gap: contraste

No opera pre-partido — su primer trade real es exactamente al kickoff en ambos partidos (1,498 fills en los primeros 30 min de BRA-NOR). Compra cruzando el spread (taker), **sí vende** (SELL pasivo al ask) y empareja rápido. Perfil de trader in-play, no de maker de inventario.

## 3. Economics validados por evento (ledger reparado, residuales = $0)

| | BRA-NOR | MEX-ENG |
|---|---|---|
| RN1 PnL final | **+$323,215** | **−$53,896** |
| RN1 trades / merge / redeem | −$1,340,927 / +$454,400 / +$1,209,742 | −$2,516,655 / +$1,399,348 / +$1,063,411 |
| RN1 sets FIFO (edge medio) | 910,470 (+1.90¢/set) | 1,884,002 (−0.97¢/set) |
| Gap PnL final | −$157,797 | +$24,999 |

*Los FIFO se calcularon sobre los CSVs previos a la reparación (~1.3% de fills faltantes); dirección y orden de magnitud no cambian.*

**Hallazgo clave: RN1 PERDIÓ en el partido de México** (−$53,896 sobre $2.5M desplegados) pese a que su perfil de Polymarket mostraba un día muy positivo. Es un negocio de volumen estadístico: el edge por set fue negativo en MEX-ENG (−0.97¢) y el día lo cargó BRA-NOR (+1.90¢/set sobre 910k sets). Ninguna wallet gana cada evento: Gap perdió BRA-NOR y ganó MEX-ENG.

## 4. Conciliación con el perfil de Polymarket (+$288,877 "1D")

El número del perfil se reprodujo **exactamente** con su API pública:

```
curl "https://user-pnl-api.polymarket.com/user-pnl?user_address=0x2005d16a84ceefa912d4e380cd32e7ff827875ea&interval=1d&fidelity=1h"
→ serie horaria de PnL acumulado; último punto − primero = 10,924,767 − 10,635,890 = +288,877
```

- Es el **cambio mark-to-market del portafolio completo** en la ventana Jul 5 04:00 → Jul 6 03:00 UTC — incluye BRA-NOR, MEX-ENG, Wimbledon, MLB, etc. **No** es el resultado del partido de México.
- Nuestro cashflow del ledger en esa misma ventana: BRA-NOR +$347,015, MEX-ENG −$35,752, otros +$60,540 → **+$371,802 cash**. La diferencia vs +$288,877 MTM es la identidad contable esperada: MTM = cash + valor de posiciones abiertas al cierre (~$33k, verificado contra el positions API) − valor al inicio de ventana de posiciones pre-existentes (~$116k implícitos: pre-compras de ambos partidos + inventario MLB/tenis).
- El positions API (`data-api.polymarket.com/positions`) confirma que RN1 **no tiene nada material pendiente** de ninguno de los dos partidos: sus $32.3k de posiciones actuales son MLB/tenis. Los REDEEMs por condición cuadran **al centavo** contra el Data API en ambos eventos.

## 5. Bug de ingesta: carrera de late-arrival en el sync incremental (reparado)

### 5.1 Síntoma

El ledger decía que RN1 aún "tenía" 92,363 shares ganadoras de "Both Teams to Score" (BRA-NOR) sin redimir, pero el positions API mostraba cero. El Data API reporta un REDEEM de **92,363.378497 shares a ts=1783289791** (22:16:31 UTC) que no existía en `wallet_events`.

### 5.2 Causa raíz (verificada en el raw store)

```
fetch id=1080229  activity window [1783289553, 1783289792]  fetched_at=22:16:33.7  rows=21
  → payload gz: max ts = 1783289771; NO contiene ts=1783289791
fetch id=1080940  activity window [1783289793, 1783289852]  (siguiente ciclo)
```

El REDEEM ocurrió a las 22:16:31, **1 segundo antes del fin de ventana**, y el fetch corrió 2.5s después — antes de que el Data API lo indexara. La siguiente ventana arrancó *después* de su timestamp, así que nunca se volvió a consultar ese rango. El watermark de `run_incremental` consulta hasta "ahora" sin margen para el lag de indexado del API. El mismo mecanismo explica los residuales negativos por condición (compras faltantes durante ráfagas), incluido el gap de "~4,408 shares" del §2.4 del doc anterior.

### 5.3 Reparación ejecutada (pipeline nativo, idempotente)

1. `DataApiSource.fetch_activity_range` re-fetcheó las ventanas completas de ambos partidos para RN1: `[1783278000, 1783292400]` y `[1783287000, 1783308600]` → 35 páginas nuevas al Raw Store (17,100 filas).
2. `pmr ingest run --wallet 0x2005...` → **167 eventos nuevos insertados** (el resto dedupeó vía `UNIQUE(dedupe_key)` + `ON CONFLICT DO NOTHING`): +42 TRADEs y +1 REDEEM en BRA-NOR, +91 TRADEs en MEX-ENG, resto en otros mercados de las mismas ventanas.
3. Post-reparación: **cero residuales >|$100|** entre holdings ganadores calculados y redeems reales en las 95 condiciones de ambos eventos.

### 5.4 Correcciones al doc `brazil_norway_full_event_rn1_gap_review_2026-07-05.md`

- §2.4 (gap de ~4,408 shares): **resuelto** — eran trades perdidos por esta carrera; recuperados.
- §2.6 (PnL RN1 +$245,281 con caveat de ~$218,763 pendientes): el PnL real es **+$323,215**. El monto no reclamado real era el REDEEM de BTTS ($92,363), que RN1 sí cobró a las 22:16:31; nuestro ledger lo había perdido. El resto de la diferencia son los trades recuperados (−$14,430 de costo adicional).

### 5.5 Fix permanente pendiente (decisión de diseño)

`run_incremental` (`pmresearch/walletmanager/sync.py`) debería tolerar el lag de indexado del Data API, p. ej.: retrasar el fin de ventana (`end = now − 60s`) **o** re-escanear con overlap los últimos N minutos en cada ciclo (el dedupe por `dedupe_key` hace el overlap gratis en el ledger; el costo es solo re-fetch). Sin esto, cada ráfaga intensa cerca del corte de ventana puede perder eventos.

## 6. Próximos pasos sugeridos

- Implementar el overlap/lag del §5.5 y, opcionalmente, un chequeo periódico de conciliación "holdings ganadores vs positions API" como detector de gaps.
- Revisar si otras wallets/fechas tienen gaps equivalentes (el patrón es sistémico, no exclusivo de estos dos partidos).
- Para PLAN2: formalizar las 7 reglas del §1 como spec de detector/estrategia (cobertura de evento completo + bid pasivo + clip fijo por mercado + merge en pausas).
