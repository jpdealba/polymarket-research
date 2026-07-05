# CLI de pmresearch

El ejecutable principal es `pmr`:

```bash
pmr [COMANDO] [OPCiones]
```

Todos los comandos aceptan `--help` para ver la ayuda integrada.

## Variables de entorno

| Variable | Default | Uso |
| --- | --- | --- |
| `PMR_DATA_DIR` | `/data` | Directorio raiz de datos persistentes: `db/`, `raw/`, `backups/`, `exports/`, `logs/`. |
| `PMR_LOG_LEVEL` | `INFO` | Nivel de logging Python (DEBUG, INFO, WARNING, ERROR). |
| `PMR_RPC_URL` | vacio | URL JSON-RPC de Polygon para enriquecimiento por RPC (Fase 11). Vacio = RPC apagado; solo funciona subgraph. |
| `PMR_SUBGRAPH_URL` | vacio | Endpoint del subgraph de Goldsky (Fase 11). Vacio = subgraph deshabilitado; `pmr enrich run --source subgraph` da error. |
| `PMR_POLYGONSCAN_API_KEY` | vacio | API key gratis de Etherscan/PolygonScan V2 para enriquecimiento por logs paginados. Vacio = `--source polygonscan` deshabilitado. |
| `PMR_RCLONE_REMOTE` | vacio | Remote de rclone para sync de backups a almacenamiento externo. Vacio = sin sync remoto. |
| `PMR_DUST_EPSILON` | `0.000001` | Umbral de dust: holdings con |qty| <= epsilon se consideran planos (flat). |
| `PMR_BACKUP_RETAIN` | `14` | Cantidad de backups a conservar en `ops/backup.sh`. Los mas viejos se eliminan. |
| `PMR_BOOK_SAMPLE_INTERVAL_S` | `300` | Intervalo en segundos entre samples del orderbook (Fase 12). |
| `PMR_BOOK_RETENTION_RAW_DAYS` | `30` | Dias de retencion para snapshots raw del book sampler. Summaries se conservan indefinidamente. |
| `PMR_FINGERPRINT_WINDOW_DAYS` | `90` | Ventana trailing en dias para la variante de fingerprints (Fase 13). |

## Comandos

### Root

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr version` | ninguno | Imprime la version instalada de `pmresearch`. |
| `pmr run` | ninguno | Ejecuta el scheduler del collector en primer plano. Comando del contenedor Docker. |

### db

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr db upgrade` | ninguno | Aplica todas las migraciones pendientes de Alembic a la base SQLite. |
| `pmr db current` | ninguno | Muestra la revision actual de Alembic en la base de datos. |

### backup / restore

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr backup` | ninguno | Crea un backup timestamped con `VACUUM INTO` en `{PMR_DATA_DIR}/backups/` e imprime la ruta. |
| `pmr restore FILE` | `FILE` (argumento, path existente) | Restaura la base desde `FILE`, reemplazando la base activa. |

### wallet

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr wallet add ADDRESS` | `ADDRESS` (argumento); `--name TEXT` (opcional) | Agrega una wallet a la watchlist con nombre visible opcional. Si ya existe, lo informa. |
| `pmr wallet remove ADDRESS` | `ADDRESS` (argumento) | Desactiva/remueve una wallet de la watchlist. |
| `pmr wallet list` | ninguno | Lista wallets activas en la watchlist. |

### sync

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr sync backfill ADDRESS` | `ADDRESS` (argumento) | Descarga historial completo de actividad desde Data API. Imprime checkpoints de cursor; cada pagina se persiste en Raw Store. |
| `pmr sync incremental [ADDRESS]` | `ADDRESS` (argumento, opcional) | Sincroniza actividad nueva. Sin wallet = todas las activas de la watchlist. |
| `pmr sync status` | ninguno | Muestra estado de sincronizacion: status, backfill, ultimo exito, fallas, ultimo error. |

### ingest

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr ingest run` | `--wallet TEXT` (opcional) | Procesa payloads crudos del Raw Store e inserta eventos en `wallet_events`. Sin wallet = todas. |
| `pmr ingest reparse` | `--wallet TEXT` (requerido) | Reprocesa desde cero los payloads crudos de una wallet. Borra y reinserta su ledger. |

### ledger

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr ledger stats` | `--wallet TEXT` (opcional); `--open-value TEXT` (default `0`) | Resume eventos, totales USDC, PnL y periodos antes/despues del fee deportivo `2026-03-30`. |

### markets

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr markets sync` | `--all` (flag); `--condition TEXT` (multiple, repetible) | Descarga metadata desde Gamma. Sin flags = incremental: metadata/token rows faltantes + mercados que siguen abiertos. `--all` = refresh completo de todos los condition_id del ledger (lento). `--condition` = uno o mas especificos. No combinables. |
| `pmr markets stats` | ninguno | Muestra conteos: total, resueltos, descriptores sin clasificar, condiciones sin metadata. |

### fees

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr fees schedules` | ninguno | Muestra schedules de fees configurados; siembra defaults si faltan. |
| `pmr fees compute` | `--wallet TEXT` (opcional); `--batch-size INT` (default `25000`, min `1`) | Calcula estimaciones de fees por trade. Progreso y commit por batches. |
| `pmr fees report` | `--wallet TEXT` (requerido); `--by-category` (flag); `--pre-post-sports-fee` (flag) | Reporte de atribucion de fees, PnL bruto/neto, ROI y cobertura. Si faltan estimaciones, las calcula primero. |

### replay

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr replay holdings` | `--wallet TEXT` (opcional) | Reconstruye proyeccion `holdings` (qty + WAC por wallet x token). Sin wallet = todas. |
| `pmr replay episodes` | `--wallet TEXT` (opcional) | Reconstruye episodios flat-to-flat desde `wallet_events`. Sin wallet = todas. |

### holdings

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr holdings show` | `--wallet TEXT` (requerido); `--nonzero` (flag) | Muestra holdings con metadata de mercado. `--nonzero` = solo por encima de dust. |
| `pmr holdings dq` | `--wallet TEXT` (requerido); `--json` (flag) | Reporte de calidad: holdings negativos, condition_id sin match, holdings sin metadata, eventos fuera de enum. Solo lectura. |

### episodes

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr episodes show` | `--wallet TEXT` (requerido); `--token TEXT` (opcional); `--open` (flag) | Muestra episodios: timestamps, razon de cierre, peak qty, WAC, PnL, adds, partial exits, eventos consumidos. |
| `pmr episodes stats` | `--wallet TEXT` (requerido) | Resumen: conteos, abiertos/cerrados/resolution, duraciones p50/p90, micro-episode share, PnL realizado. |

### derive

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr derive run` | `--wallet TEXT` (opcional) | Inserta REDEEM_PAYOUT derivados, reconstruye episodios y actualiza descomposicion PnL. Sin wallet = todas. |

### pnl

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr pnl show` | `--wallet TEXT` (requerido); `--by-category` (flag) | Muestra PnL: directional, bond/merge, rewards, redemption, fees, total. `--by-category` = por categoria de mercado. |

### equity

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr equity build` | `--wallet TEXT` (opcional) | Construye `daily_equity`: portfolio value, PnL realizado, unrealized, rewards, drawdown, stale_equity_share. Sin wallet = watchlist o todas. |
| `pmr equity show` | `--wallet TEXT` (requerido); `--limit INT` (default `10`) | Muestra resumen y ultimas filas de la curva de equity. Drawdown es daily-close based. |

### exposure

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr exposure build` | `--wallet TEXT` (opcional) | Construye `exposures_daily` (directional+bond o vector unclassified) y `event_exposures_daily` (vector + neteo negRisk). Sin wallet = watchlist o todas. |
| `pmr exposure show` | `--wallet TEXT` (requerido); `--market TEXT` (opcional); `--event TEXT` (opcional); `--limit INT` (default `10`) | Muestra exposicion. Sin flags = market-level. `--event` = vector de evento. `--market` y `--event` no combinables. |

### enrich

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr enrich run` | `--wallet TEXT` (opcional); `--source [subgraph\|rpc\|polygonscan]` (default `subgraph`); `--from-block INT` (default `0`); `--to-block INT` (opcional); `--chunk-blocks INT` (default `200000`); `--ignore-watermark` | Trae fills OrderFilled y los une a TRADE por (tx_hash, wallet, token, monto). RPC requiere `PMR_RPC_URL`; PolygonScan requiere `PMR_POLYGONSCAN_API_KEY`. `--chunk-blocks` = rango por fetch; PolygonScan pagina por resultados dentro del rango. `--ignore-watermark` fuerza reescaneo desde `--from-block`. |
| `pmr enrich coverage` | `--wallet TEXT` (opcional) | Muestra cobertura de enrichment: enriched / pending / ambiguous / missing por bucket de recencia. |

### reconcile

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr reconcile run` | `--wallet TEXT` (requerido); `--json` (flag) | Reconciliacion contra Data API: `/positions` vs holdings, WAC/realizedPnl, `/value` vs daily_equity. Actualiza `wallet_trust`. |
| `pmr reconcile status` | `--wallet TEXT` (opcional) | Muestra ultima reconciliacion: trust, excepciones, top discrepancias, negativos, WAC/realizedPnl, `/value`. |

### trust

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr trust status` | `--wallet TEXT` (opcional) | Muestra estado de confianza: `trusted`, `warn`, `untrusted` y razon. |

### books (Fase 12)

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr books sample-once` | ninguno | Snapshot del orderbook para Tokens Relevantes (posiciones abiertas + tradeados 24h). Almacena best_bid, best_ask, spread, mid, top-10 depth. |
| `pmr books status` | ninguno | Estado del sampler: tokens trackeados, snapshots, almacenamiento, retencion. |
| `pmr books prune` | ninguno | Elimina snapshots raw segun retencion. Advertencia si excede storage budget. |

### fingerprints (Fase 13)

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr fingerprints compute` | `--wallet TEXT` (opcional) | Calcula features behaviorales (funciones puras sobre proyecciones): maker/taker_fill_share, enrichment_coverage, reward_income_share, realized/unrealized_pnl, bond_inventory_ratio, merge/redeem_frequency, episode_count, episode_duration_p50/p90, micro_episode_share, adds_per_episode, partial_exit_frequency, avg/median_position_size, market_category_concentration (HHI), time_to_event_start_at_entry, entry_price_distribution, resolution_outcome_calibration, stale_mark_share. Por scope (`all` + `category:<Label>`) y ventana (`all` + `90d`). NULL-con-razon cuando no computable, nunca 0 silencioso. |
| `pmr fingerprints show` | `--wallet TEXT` (requerido); `--scope TEXT` (default `all`); `--window TEXT` (default `all`) | Muestra el fingerprint de un scope/ventana agrupado por familia: valor o `NULL (razon)`, con version y computed_at. |
| `pmr fingerprints compare` | `--wallets TEXT` (requerido, separadas por coma); `--scope TEXT` (default `all`); `--window TEXT` (default `all`) | Compara fingerprints de multiples wallets lado a lado, una columna por wallet. |

### detect (Fase 14)

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr detect run` | `--wallet TEXT` (opcional) | Ejecuta detectores (market_making, inventory_cycling, value_betting). Emite Strategy Labels con score 0-1, evidence y blind spots. |
| `pmr detect show` | `--wallet TEXT` (requerido) | Muestra labels con scores y evidencia expandible. |
| `pmr detect explain` | `--wallet TEXT` (requerido); `--detector TEXT` (requerido) | Explica un detector: features de entrada, valores, score final, blind spots. |

### report (Fase 15)

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr report wallet ADDRESS` | `ADDRESS` (argumento); `--out PATH` (opcional); `--window TEXT` (opcional) | Genera reporte Markdown: descomposicion PnL, episodios, maker/taker, hipotesis estrategia, reconciliacion, limitaciones. Output a `/data/exports/` por defecto. |

### evidence

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr evidence rn1-completion-sets` | `--wallet TEXT` (default RN1); `--out PATH` (default `docs/evidence/rn1_completion_sets/`) | Genera una auditoria read-only de la hipotesis completion-set / inventory-cycling: lifecycle por mercado, edge MERGE, orphans REDEEM, temporalidad, bridge PnL y Markdown resumen. |

### acceptance (Fase 17)

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr acceptance` | ninguno | Verifica los 7 puntos de ADR 0006: reconciliacion, soak uptime, deletion-test, reportes, backups. Pass/fail con evidencia. |

## Flujos comunes

```bash
# Infraestructura
pmr db upgrade
pmr wallet add 0x... --name "Wallet ejemplo"
pmr sync backfill 0x...
pmr ingest run --wallet 0x...
pmr markets sync

# Fees
pmr fees compute --wallet 0x...
pmr fees report --wallet 0x... --by-category --pre-post-sports-fee

# Holdings y calidad de datos
pmr replay holdings --wallet 0x...
pmr holdings show --wallet 0x... --nonzero
pmr holdings dq --wallet 0x...

# Episodios
pmr replay episodes --wallet 0x...
pmr episodes stats --wallet 0x...

# PnL derivado y descomposicion
pmr derive run --wallet 0x...
pmr pnl show --wallet 0x... --by-category

# Equity diaria (Fase 9)
pmr equity build --wallet 0x...
pmr equity show --wallet 0x...

# Exposicion (Fase 10)
pmr exposure build --wallet 0x...
pmr exposure show --wallet 0x... --event <event_id>

# Enriquecimiento maker/taker (Fase 11)
pmr enrich run --wallet 0x...
pmr enrich coverage --wallet 0x...

# Orderbook snapshots (Fase 12)
pmr books sample-once
pmr books status
pmr books prune

# Fingerprints behaviorales (Fase 13)
pmr fingerprints compute --wallet 0x...
pmr fingerprints show --wallet 0x...
pmr fingerprints compare --wallets 0x...,0x...

# Detectores de estrategia (Fase 14)
pmr detect run --wallet 0x...
pmr detect show --wallet 0x...
pmr detect explain --wallet 0x... --detector market_making

# Reportes (Fase 15)
pmr report wallet 0x... --out /data/exports/rn1.md

# Evidencia RN1 completion-set / inventory-cycling
pmr evidence rn1-completion-sets --wallet 0x2005d16a84ceefa912d4e380cd32e7ff827875ea --out docs/evidence/rn1_completion_sets/

# Reconciliacion y confianza
pmr reconcile run --wallet 0x...
pmr reconcile status
pmr trust status

# Aceptacion MVP (Fase 17)
pmr acceptance
```
