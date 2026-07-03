# CLI de pmresearch

El ejecutable principal es `pmr`:

```bash
pmr [COMANDO] [OPCIONES]
```

Todos los comandos aceptan `--help` para ver la ayuda integrada.

## Variables de entorno

| Variable | Default | Uso |
| --- | --- | --- |
| `PMR_DATA_DIR` | `/data` | Directorio raiz de datos persistentes: `db/`, `raw/`, `backups/`, `exports/`, `logs/`. |
| `PMR_LOG_LEVEL` | `INFO` | Nivel de logging. |
| `PMR_RPC_URL` | vacio | URL RPC disponible para componentes que la requieran. |
| `PMR_RCLONE_REMOTE` | vacio | Remote de rclone para operaciones externas de backup/sync. |
| `PMR_DUST_EPSILON` | `0.000001` | Umbral de "dust": holdings con cantidad absoluta menor o igual se consideran planos (flat). |

## Comandos

| Comando | Parametros | Que hace |
| --- | --- | --- |
| `pmr version` | ninguno | Imprime la version instalada de `pmresearch`. |
| `pmr run` | ninguno | Ejecuta el scheduler del collector en primer plano. Es el comando pensado para el contenedor. |
| `pmr db upgrade` | ninguno | Aplica todas las migraciones pendientes de Alembic a la base SQLite. |
| `pmr db current` | ninguno | Muestra la revision actual de Alembic en la base de datos. |
| `pmr backup` | ninguno | Crea un backup timestamped con `VACUUM INTO` en `{PMR_DATA_DIR}/backups/` e imprime la ruta. |
| `pmr restore FILE` | `FILE`: archivo de backup existente | Restaura la base desde `FILE`, reemplazando la base activa. |
| `pmr wallet add ADDRESS` | `ADDRESS`: wallet; `--name TEXT`: nombre visible opcional | Agrega una wallet a la watchlist. Si ya existe, lo informa sin duplicarla. |
| `pmr wallet remove ADDRESS` | `ADDRESS`: wallet | Desactiva/remueve una wallet de la watchlist. |
| `pmr wallet list` | ninguno | Lista wallets activas en la watchlist. |
| `pmr sync backfill ADDRESS` | `ADDRESS`: wallet | Descarga el historial completo de actividad cruda para una wallet desde Data API y lo guarda en Raw Store. Imprime checkpoints de cursor mientras avanza; cada pagina cruda se persiste en Raw Store y cada checkpoint se commitea. |
| `pmr sync incremental [ADDRESS]` | `ADDRESS`: wallet opcional | Sincroniza actividad nueva. Si no se pasa wallet, corre para todas las wallets activas de la watchlist. Imprime checkpoints de cursor y raw-storea/commitea paginas conforme se reciben. |
| `pmr sync status` | ninguno | Muestra estado de sincronizacion por wallet: status, backfill, ultimo exito, fallas y ultimo error. |
| `pmr ingest run` | `--wallet TEXT`: limita a una wallet | Procesa payloads crudos del Raw Store e inserta eventos nuevos en `wallet_events`. Imprime progreso por raw fetch; cada raw fetch se marca `ingested_at` y se commitea al terminar. |
| `pmr ingest reparse` | `--wallet TEXT`: requerido | Reprocesa desde cero los payloads crudos de una wallet y vuelve a insertar su ledger. Imprime progreso por raw fetch y commitea por raw fetch. |
| `pmr ledger stats` | `--wallet TEXT`: filtra por wallet; `--open-value TEXT`: valor USDC de posiciones abiertas, default `0` | Resume eventos del ledger, totales USDC, PnL y periodos antes/despues del fee deportivo de `2026-03-30`. Si necesita fee estimates para el escenario post-cutoff, imprime progreso y commitea ese calculo por batches. |
| `pmr markets sync` | `--all`: sincroniza todos los `condition_id` del ledger; `--condition TEXT`: sincroniza uno o mas `condition_id` especificos | Descarga metadata de mercados desde Gamma y actualiza mercados, tokens y eventos. `--all` y `--condition` no se pueden combinar. |
| `pmr markets stats` | ninguno | Muestra conteos de mercados, resueltos, descriptores sin clasificar y condiciones del ledger sin metadata. |
| `pmr fees schedules` | ninguno | Muestra los schedules de fees configurados; si hace falta, siembra defaults. |
| `pmr fees compute` | `--wallet TEXT`: limita a una wallet; `--batch-size INT`: trades por commit/progreso, default del modulo | Calcula estimaciones de fees por trade. Imprime progreso y commitea por batches configurables. |
| `pmr fees report` | `--wallet TEXT`: requerido; `--by-category`: agrupa por categoria; `--pre-post-sports-fee`: separa antes/despues de `2026-03-30` | Genera reporte de atribucion de fees, PnL bruto/neto estimado, ROI y cobertura para una wallet. Si faltan estimaciones, las calcula con progreso y commits por batch antes del reporte. |
| `pmr replay holdings` | `--wallet TEXT`: limita a una wallet; sin flag corre para todas las wallets del ledger | Reconstruye la proyeccion `holdings` (cantidad actual + WAC por wallet x token) replayeando `wallet_events` en orden `(ts, id)`. Imprime progreso durante el replay y hace flush/commit por batches al insertar holdings finales. Reporta warnings de calidad de datos. |
| `pmr holdings show` | `--wallet TEXT`: requerido; `--nonzero`: solo holdings por encima del dust epsilon | Muestra la proyeccion de holdings con metadata de mercado (pregunta y outcome) cuando existe. |
| `pmr holdings dq` | `--wallet TEXT`: requerido; `--json`: salida en JSON en vez de texto | Reporte de calidad de datos (Phase 4): holdings negativos con causa diagnosticada (evento y timestamp), `condition_id` de MERGE/REDEEM sin match en `markets` (clasificados como bug de encoding bytea `\x` vs `0x`, o realmente no disponibles en Gamma), holdings sin metadata de token, y eventos fuera del enum documentado del ledger (p. ej. `CONVERSION`). Solo lectura; no cambia `wallet_events` ni `holdings`. |
| `pmr replay episodes` | `--wallet TEXT`: limita a una wallet; sin flag corre para todas las wallets del ledger | Reconstruye episodios flat-to-flat desde `wallet_events`, con cierres por flat o resolucion y warning si faltan tokens para eventos condition-scoped. Imprime progreso durante el replay y hace flush/commit por batches de episodios conforme se cierran; episodios abiertos se insertan al final. |
| `pmr episodes show` | `--wallet TEXT`: requerido; `--token TEXT`: filtra por `token_id`; `--open`: solo episodios abiertos | Muestra episodios con open/close timestamp, razon de cierre, peak qty, WAC, PnL realizado, adds, partial exits, numero de eventos consumidos y token. |
| `pmr episodes stats` | `--wallet TEXT`: requerido | Resume conteos de episodios, abiertos/cerrados/resolution, duraciones min/p50/p90/max, micro-episode share, PnL realizado y reward income. |
| `pmr derive run` | `--wallet TEXT`: limita a una wallet; sin flag corre para todas las wallets del ledger | Inserta eventos derivados idempotentes para redenciones con proceeds reportados en cero, reconstruye episodios y actualiza la descomposicion de PnL. Imprime progreso en derivacion, episodios y PnL; commitea derivados/episodios por batches. |
| `pmr pnl show` | `--wallet TEXT`: requerido; `--by-category`: muestra scopes por categoria | Muestra PnL descompuesto en directional, bond/merge, rewards, redemption, fees y total. |
| `pmr equity build` | `--wallet TEXT`: limita a una wallet; sin flag usa wallets activas de watchlist o, si no hay, todas las del ledger | Construye `daily_equity` por dia UTC: portfolio value, PnL realizado acumulado, unrealized PnL, rewards acumulados, drawdown diario aproximado y `stale_equity_share`. Imprime progreso durante el replay y hace flush/commit por batches de `price_points` y `daily_equity`; tambien persiste marks con source, age y stale flag. |
| `pmr equity show` | `--wallet TEXT`: requerido; `--limit INT`: filas finales a mostrar, default `10` | Muestra resumen y ultimas filas de la curva diaria de equity. Incluye la caveat de que el drawdown es daily-close based e intradia aproximado. |
| `pmr exposure build` | `--wallet TEXT`: limita a una wallet; sin flag usa wallets activas de watchlist o, si no hay, todas las del ledger | Construye las proyecciones `exposures_daily` (exposicion market-level directional+bond o vector unclassified por wallet x condition x dia UTC) y `event_exposures_daily` (vector de exposicion por condition + neteo `net_after_exclusivity` para eventos negRisk). Despacha solo por `structure_type`; estructuras desconocidas van al camino unclassified con warning contado. Imprime progreso durante el replay y hace flush/commit por batches. |
| `pmr exposure show` | `--wallet TEXT`: requerido; `--market TEXT`: filtra por `condition_id`; `--event TEXT`: filtra por `event_id`; `--limit INT`: filas finales a mostrar, default `10` | Muestra filas de exposicion. Sin `--event` muestra market-level (structure_type, directional, bond, event_id); con `--event` muestra el vector de exposicion del evento y su neteo. `--market` y `--event` no se pueden combinar. |
| `pmr reconcile run` | `--wallet TEXT`: requerido; `--json`: salida JSON estable | Ejecuta reconciliacion contra Data API: raw-storea `/positions`, compara `positions.size` contra `holdings.qty`, revisa WAC/realizedPnl cuando el oracle trae esos campos, agrega chequeo de portfolio value contra `/value` usando la ultima fila de `daily_equity`, persiste facts por batches y actualiza `wallet_trust`. En salida humana imprime progreso; con `--json` no imprime progreso para mantener JSON estable. |
| `pmr reconcile status` | `--wallet TEXT`: opcional | Muestra la ultima reconciliacion por wallet: conteos, trust, excepciones conocidas, top discrepancias por cantidad/notional, negativos, holdings sin metadata presentes en `/positions`, discrepancias WAC/realizedPnl y resultado de `/value`. |
| `pmr trust status` | `--wallet TEXT`: opcional | Muestra el estado derivado de confianza de cada wallet (`trusted`, `warn`, `untrusted`) y la razon de la ultima reconciliacion. |

## Flujos comunes

```bash
pmr db upgrade
pmr wallet add 0x... --name "Wallet ejemplo"
pmr sync backfill 0x...
pmr ingest run --wallet 0x...
pmr markets sync --all
pmr fees report --wallet 0x... --by-category --pre-post-sports-fee
pmr replay holdings --wallet 0x...
pmr holdings show --wallet 0x... --nonzero
pmr holdings dq --wallet 0x...
pmr replay episodes --wallet 0x...
pmr episodes stats --wallet 0x...
pmr derive run --wallet 0x...
pmr pnl show --wallet 0x... --by-category
pmr equity build --wallet 0x...
pmr equity show --wallet 0x...
pmr exposure build --wallet 0x...
pmr exposure show --wallet 0x... --event <event_id>
pmr reconcile run --wallet 0x...
pmr reconcile status
pmr trust status
```
