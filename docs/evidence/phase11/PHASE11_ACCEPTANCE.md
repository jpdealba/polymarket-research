# Phase 11 — Maker/taker enrichment: acceptance

Fecha: 2026-07-04 · Wallet: RN1 `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`
Fuente: Goldsky orderbook subgraph (`orderFilledEvents`). Ruta RPC: no configurada.

## Criterios de aceptación

- [x] El enrichment nunca crea ni borra eventos del ledger (solo inserta en `fill_enrichment`).
- [x] La métrica de cobertura distingue enriched / pending / ambiguous / missing por bucket de recencia.
- [x] Maker-share de RN1 calculable para el período cubierto por el subgraph.
- [x] Ruta RPC opcional y config-gated (`PMR_RPC_URL`); subgraph-only funciona sin ella.
- [x] Todos los payloads se persisten en el Raw Store antes de parsear.
- [x] Address-space verificado: la dirección de RN1 aparece directamente como `maker` on-chain — **no hay problema de proxy vs signer**.
- [ ] Ventana reciente (post head del subgraph) — requiere ruta RPC (ver abajo).

## Resultado de cobertura (RN1)

Salida cruda: [`enrich_coverage_rn1_phase11.txt`](enrich_coverage_rn1_phase11.txt)

```
trades=3,704,054  enriched=2,492,160 (67.28%)  pending=1,152,957  ambiguous=58,934  missing=3
subgraph_head_ts=1777373910  (2026-04-28)
```

### Lectura

- **Dentro del rango indexado por el subgraph (ts ≤ 2026-04-28): cobertura ≈ 97.7%**
  `2,492,160 / (2,492,160 + 58,934 ambiguous + 3 missing)`. El 67% global lo arrastra hacia
  abajo el bucket `pending`, no un fallo de enrichment.
- **pending (1,152,957)** — trades más nuevos que el head del subgraph. No son datos perdidos:
  el subgraph no los ha indexado. Solo se cierran con la ruta RPC.
- **ambiguous (58,934 · ~1.6%)** — fills idénticos en la misma tx (mismo tx+token+monto); no
  se atribuye maker/taker por monto sin adivinar, se dejan sin enriquecer a propósito (ADR 0006).
- **missing (3)** — huecos reales (viejos, sin enriquecer, sin gemelo), de 3.7M eventos.

## Caveat: lag / congelamiento del subgraph

El head del subgraph está clavado en **2026-04-28**; el evento más nuevo de *todo* el endpoint
(no solo RN1) es de esa fecha. El endpoint público de Goldsky parece detenido en abril (~2 meses),
más que el "weeks behind" documentado en ADR 0001. Todo lo posterior a esa fecha queda `pending`
y **solo la ruta RPC** (`eth_getLogs` de `OrderFilled`) puede cerrar esa ventana.

## Bug corregido durante la verificación (commit 8f0cf68)

La primera corrida live daba `fills=0` pese a que RN1 tiene millones de fills. Tres defectos
tapados por el mock del test:

1. POST a `base_url + ""` → httpx lo unía como `.../gn/` (barra final), ruta que no llega al
   resolver GraphQL y devuelve `{"message": ...}`. Ahora se hace POST a la URL absoluta.
2. El filtro `or:[{maker},{taker}]` lo rechaza/timeoutea el subgraph. Ahora se consultan maker y
   taker por separado (cada uno paginado por cursor) y se fusionan por id.
3. Un payload con `errors` (HTTP 200, `data` nulo) se leía como cero fills. Ahora `_rows` lanza
   `SubgraphError` (ADR 0006: nunca descartar en silencio).

Verificado live: una ventana de ~15 min de RN1 devuelve 166 fills (139 maker, 27 taker).

## Pendiente

- Configurar `PMR_RPC_URL` (RPC de Polygon) y correr `pmr enrich run --wallet RN1 --source rpc
  --to-block <n>` para enriquecer la ventana 2026-04-28 → hoy y reducir el `pending`.
