# Descubrimientos sobre RN1 en Polymarket

Wallet analizada:

```text
0x2005d16a84ceefa912d4e380cd32e7ff827875ea
```

Fecha del análisis: 2026-07-04  
Estado: análisis exploratorio con base en datos locales, ledger `pmr`, cashflows ERC-20 y clasificación manual de contrapartes.

---

## 1. Tesis principal

RN1 no parece ser principalmente un trader direccional clásico ni un scalper que compra barato y vende caro en mercado secundario.

La evidencia apunta a que RN1 opera como:

```text
maker-dominant sports inventory cycler / completion-set arbitrageur
```

En español:

```text
proveedor de liquidez dominante en deportes que acumula inventario, forma pares complementarios, recicla capital con MERGE/REDEEM y monetiza diferencias pequeñas a gran escala.
```

La ganancia parece venir principalmente de:

1. acumulación pasiva como maker;
2. formación de posiciones complementarias;
3. `MERGE` / bond inventory / completion sets;
4. redenciones o cierres por resolución;
5. rebates/rewards como componente adicional, no como fuente principal.

---

## 2. Evidencia de que no es un trader direccional normal

Del ledger local:

```text
TRADE BUY total   ≈ 386.58M USDC
TRADE SELL total  ≈   1.10M USDC
REDEEM total      ≈ 237.21M USDC
MERGE total       ≈ 158.37M USDC
```

El dato clave es:

```text
SELL / BUY ≈ 0.285%
```

Esto significa que RN1 casi no sale vendiendo en mercado secundario. La mayor parte de las salidas ocurre por:

```text
MERGE + REDEEM / resolución
```

Eso no se parece a una estrategia de:

```text
comprar barato → vender caro
```

Se parece más a:

```text
comprar/agregar inventario → formar pares o mantener hasta resolución → reciclar capital
```

---

## 3. Maker vs taker

El enriquecimiento maker/taker mostró:

```text
enriched trades: 3,092,554
pending:           606,562
ambiguous:           4,919
missing:                19

maker: 2,895,301
taker:   197,253
```

Entre trades enriquecidos:

```text
maker share ≈ 93.6%
taker share ≈ 6.4%
```

Incluso si todos los pendientes fueran taker, el maker share mínimo seguiría siendo aproximadamente:

```text
≈ 78.2%
```

Conclusión:

```text
RN1 es maker-dominant.
```

Esto reduce la probabilidad de que su edge venga de arbitraje taker puro. Más bien parece estar posteando órdenes pasivas, absorbiendo flujo, acumulando inventario y cerrando por mecanismos internos de Polymarket.

---

## 4. Descomposición de PnL

El reporte de PnL por categoría mostró, para Sports:

```text
Sports gross PnL ≈ 7.357M
bond_merge       ≈ 5.537M
redemption       ≈ 1.379M
directional      ≈ 0.441M
estimated fees   ≈ 1.264M
estimated net    ≈ 6.093M
```

Porcentaje aproximado del PnL bruto Sports:

```text
bond_merge  ≈ 75.3%
redemption  ≈ 18.7%
directional ≈  6.0%
```

Conclusión:

```text
La mayor parte del edge viene de bond/merge/inventory cycling, no de predicción direccional.
```

---

## 5. Episodios: no parece micro-scalping puro

El análisis de episodios mostró:

```text
episodes total:      159,035
open:                    128
flat:                 25,597
resolution:          133,310
micro episodes:          276
micro episode share:   0.17%
```

Duración:

```text
p50 ≈ 22,262 segundos ≈ 6.2 horas
p90 ≈ 651,573 segundos ≈ 7.5 días
```

Conclusión:

```text
No es principalmente un bot de micro-episodios flat-to-flat.
```

Aunque ejecuta muchísimos fills, las posiciones suelen mantenerse horas o días y muchas cierran por resolución.

---

## 6. Rewards y rebates

Del ledger bruto:

```text
REWARD total       ≈ 235,865
MAKER_REBATE total ≈ 531,556
TAKER_REBATE total ≈  34,179
```

Total aproximado rewards/rebates:

```text
≈ 801,600 USDC
```

Comparado contra PnL bruto total aproximado:

```text
≈ 10.9M
```

Rewards/rebates representan alrededor de:

```text
≈ 7.3%
```

Conclusión:

```text
Los rewards ayudan, pero no explican la mayoría de la rentabilidad.
```

---

## 7. Reconciliación y calidad de datos

El sistema local no estaba totalmente actualizado en tiempo real, pero la calidad del ledger era razonable para inferencia estratégica.

Datos relevantes:

```text
negative_token_count: 0
negative_condition_count: 0
reconciliation fails: 0
trust status: warn, no fail
```

La diferencia contra `/value` parecía venir principalmente de:

```text
timing skew / datos stale / metadata upstream no disponible
```

Conclusión:

```text
Hay caveats de timing, pero no señales fuertes de ledger roto.
```

---

## 8. Drawdown / equity: cuidado con la métrica local

El `daily_equity` mostraba un aparente drawdown gigante alrededor de marzo 2026, pero al revisar:

```text
marked_pnl = realized_pnl_cum + unrealized_pnl + reward_income_cum
```

la caída se suaviza y no parece un colapso real de varios millones.

Ejemplo:

```text
2026-03-07 marked_pnl ≈ 4.267M
2026-03-08 marked_pnl ≈ 4.260M
Diferencia ≈ -7.5k
```

Conclusión:

```text
El drawdown local basado en realized + portfolio puede ser engañoso si ignora unrealized/reclasificaciones.
```

Para leer la curva, conviene usar:

```text
realized + unrealized + rewards
```

no solo el drawdown mostrado.

---

## 9. Cashflows externos: depósitos y retiros

Se revisaron transferencias ERC-20 para distinguir:

```text
flujo interno Polymarket
vs
entrada/salida externa real
```

Esto fue importante porque el bruto de token transfers puede engañar: muchos movimientos grandes son contra contratos internos de Polymarket.

### Rango 2025-07-01 a 2025-08-15

Resultado limpio:

```text
external IN candidate  ≈ 1,180 USDC.e
external OUT candidate ≈ 0
```

La mayoría del bruto era interno de Polymarket.

Conclusión:

```text
No hay evidencia de fondeo externo grande en este primer tramo.
```

---

### Rango 2025-08-15 a 2025-10-01

Después de clasificar las contrapartes:

```text
external IN  ≈      5 USDC.e
external OUT ≈ 95,100 USDC.e
```

Conclusión:

```text
RN1 ya estaba retirando capital neto en este periodo.
```

Ritmo aproximado:

```text
95,100 / 6.7 semanas ≈ 14,200 USDC.e por semana
```

Estimación de etapa inicial:

```text
≈ 10k–15k por semana
```

---

### Rango 2025-10-01 a 2025-12-01 aprox.

El resumen mostró:

```text
external OUT candidate ≈ 925,020 USDC.e
external IN/unknown    ≈      38 USDC.e
```

Las wallets externas eran las mismas ya revisadas.

Ritmo aproximado:

```text
925,020 / 8.7 semanas ≈ 106,000 USDC.e por semana
```

Estimación de etapa de escalamiento:

```text
≈ 100k por semana
```

---

### Último mes revisado: 2026-06-01 a 2026-07-03

Resumen del archivo reciente:

```text
PUSD MINT_TO_RN1       ≈ 80.79M
PUSD TRANSFER_IN       ≈ 283.56k
PUSD TRANSFER_OUT      ≈ 92.62M
USDC_E TRANSFER_IN     ≈ 1.000M
```

La mayor parte de `PUSD TRANSFER_OUT` va a contratos de Polymarket:

```text
0xe111... = Polymarket CTF Exchange V2
0xe222... = Polymarket Neg Risk CTF Exchange V2
```

Eso no debe contarse como retiro externo.

Pero también apareció una conversión:

```text
PUSD OUT ≈ 1,000,003
USDC_E IN ≈ 1,000,003
```

Fechas del `USDC_E IN`:

```text
2026-06-10  ≈ 300,001
2026-06-19  ≈ 300,001
2026-06-30  ≈ 400,001
```

Total:

```text
≈ 1,000,003 USDC.e
```

Interpretación asumida:

```text
RN1 convirtió/retiró aproximadamente 1M desde pUSD hacia USDC.e líquido.
```

Ritmo aproximado:

```text
1,000,003 / 4.6 semanas ≈ 217,000 USDC.e por semana
```

Cruzando con el gráfico de Polymarket visto antes:

```text
1W ≈ +369,644
1M ≈ +1,297,721
```

Estimación actual:

```text
normal reciente: ≈ 200k–300k por semana
semana fuerte:   ≈ 350k–370k por semana
```

---

## 10. Estimación de ganancias por etapa

Usando retiros/conversiones como proxy de ganancias:

| Etapa | Rango aproximado | Evidencia principal | Estimación semanal |
|---|---:|---:|---:|
| Inicio | 2025-08-15 a 2025-10-01 | 95.1k OUT en 6.7 semanas | 10k–15k |
| Escalamiento | 2025-10-01 a 2025-12-01 | 925k OUT en 8.7 semanas | ~100k |
| Actual normal | 2026-06-01 a 2026-07-03 | 1M pUSD→USDC.e en 4.6 semanas | 200k–300k |
| Semana fuerte reciente | gráfico Polymarket 1W | +369.6k | 350k–370k |

Estimación resumida:

```text
Al inicio:              ~10k–15k por semana
Cuando escaló:          ~100k por semana
Ahora, ritmo normal:    ~200k–300k por semana
Semana fuerte reciente: ~350k–370k por semana
```

Número prudente para “ahora”:

```text
≈ 250k USD por semana
```

---

## 11. Qué NO queda demostrado

Este análisis no demuestra que:

1. todos los retiros sean ganancias puras;
2. no haya capital movido por otros tokens/bridges no revisados;
3. el `1M` reciente haya salido definitivamente a una wallet externa después de convertirse a USDC.e;
4. se pueda replicar la estrategia sin infraestructura, latencia, capital y control de inventario;
5. el edge sea risk-free.

La interpretación correcta es:

```text
Los flujos observados son consistentes con ganancias generadas dentro de Polymarket y retiradas/convertidas periódicamente.
```

No es una prueba contable perfecta de profit neto final.

---

## 12. Lectura final

La historia más probable de RN1 es:

```text
1. Entró con capital pequeño o al menos sin fondeo externo grande visible en los rangos revisados.
2. Empezó ganando decenas de miles por semana.
3. Escaló a alrededor de 100k por semana hacia finales de 2025.
4. Actualmente parece estar en cientos de miles por semana.
5. Su edge no parece venir de predecir resultados deportivos, sino de microestructura, maker flow, inventory cycling, bond/merge y resolución.
```

En una frase:

```text
RN1 parece haber construido una máquina de liquidez/inventario en Polymarket, no una estrategia simple de picks deportivos.
```

---

## 13. Hipótesis operativa para replicar conceptualmente

No copiar operaciones. Replicar el modelo conceptual:

```text
buscar mercados binarios con flujo suficiente,
postear como maker,
controlar inventario de ambos lados,
medir bond inventory,
monetizar completion sets,
merge/redeem cuando convenga,
y evitar quedar demasiado direccional.
```

Componentes necesarios:

1. **Base de datos actualizada** de activity, fills, markets y resoluciones.
2. **Maker/taker enrichment** para medir si realmente se está ganando como maker.
3. **Exposure engine** para separar directional vs bond.
4. **PnL decomposition** para saber si el edge viene de directional, merge, redemption o rewards.
5. **Reconciliación constante** contra `/positions` y `/value`.
6. **Control de inventario** por mercado y por evento.
7. **Book sampler** para no operar ciego en spreads/depth actuales.

La pregunta que debe responder cualquier bot inspirado en RN1 no es:

```text
¿Qué pick compro?
```

sino:

```text
¿Puedo proveer liquidez, acumular inventario complementario y reciclar capital con edge neto después de fees?
```
