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
| `pmr sync backfill ADDRESS` | `ADDRESS`: wallet | Descarga el historial completo de actividad cruda para una wallet desde Data API y lo guarda en Raw Store. |
| `pmr sync incremental [ADDRESS]` | `ADDRESS`: wallet opcional | Sincroniza actividad nueva. Si no se pasa wallet, corre para todas las wallets activas de la watchlist. |
| `pmr sync status` | ninguno | Muestra estado de sincronizacion por wallet: status, backfill, ultimo exito, fallas y ultimo error. |
| `pmr ingest run` | `--wallet TEXT`: limita a una wallet | Procesa payloads crudos del Raw Store e inserta eventos nuevos en `wallet_events`. |
| `pmr ingest reparse` | `--wallet TEXT`: requerido | Reprocesa desde cero los payloads crudos de una wallet y vuelve a insertar su ledger. |
| `pmr ledger stats` | `--wallet TEXT`: filtra por wallet; `--open-value TEXT`: valor USDC de posiciones abiertas, default `0` | Resume eventos del ledger, totales USDC, PnL y periodos antes/despues del fee deportivo de `2026-03-30`. |
| `pmr markets sync` | `--all`: sincroniza todos los `condition_id` del ledger; `--condition TEXT`: sincroniza uno o mas `condition_id` especificos | Descarga metadata de mercados desde Gamma y actualiza mercados, tokens y eventos. `--all` y `--condition` no se pueden combinar. |
| `pmr markets stats` | ninguno | Muestra conteos de mercados, resueltos, descriptores sin clasificar y condiciones del ledger sin metadata. |
| `pmr fees schedules` | ninguno | Muestra los schedules de fees configurados; si hace falta, siembra defaults. |
| `pmr fees compute` | `--wallet TEXT`: limita a una wallet | Calcula estimaciones de fees por trade y muestra cobertura/totales. |
| `pmr fees report` | `--wallet TEXT`: requerido; `--by-category`: agrupa por categoria; `--pre-post-sports-fee`: separa antes/despues de `2026-03-30` | Genera reporte de atribucion de fees, PnL bruto/neto estimado, ROI y cobertura para una wallet. |
| `pmr replay holdings` | `--wallet TEXT`: limita a una wallet; sin flag corre para todas las wallets del ledger | Reconstruye la proyeccion `holdings` (cantidad actual + WAC por wallet x token) replayeando `wallet_events` en orden `(ts, id)`. Reporta warnings de calidad de datos (cantidades negativas, condiciones sin metadata de tokens). |
| `pmr holdings show` | `--wallet TEXT`: requerido; `--nonzero`: solo holdings por encima del dust epsilon | Muestra la proyeccion de holdings con metadata de mercado (pregunta y outcome) cuando existe. |
| `pmr holdings dq` | `--wallet TEXT`: requerido; `--json`: salida en JSON en vez de texto | Reporte de calidad de datos (Phase 4): holdings negativos con causa diagnosticada (evento y timestamp), `condition_id` de MERGE/REDEEM sin match en `markets` (clasificados como bug de encoding bytea `\x` vs `0x`, o realmente no disponibles en Gamma), holdings sin metadata de token, y eventos fuera del enum documentado del ledger (p. ej. `CONVERSION`). Solo lectura; no cambia `wallet_events` ni `holdings`. |
| `pmr reconcile run` | `--wallet TEXT`: requerido; `--json`: salida JSON estable | Ejecuta reconciliacion Phase 5 contra Data API `/positions`: raw-storea las respuestas, compara `positions.size` contra `holdings.qty` por `token_id`, persiste facts y actualiza `wallet_trust`. La salida JSON expone `wallet_trust`, `known_exception_count`, `known_exceptions` y `analytics_trust_caveat` para que reportes downstream no oculten wallets no confiables. |
| `pmr reconcile status` | `--wallet TEXT`: opcional | Muestra la ultima reconciliacion por wallet: conteos, trust, excepciones conocidas, top discrepancias por cantidad/notional, negativos y holdings sin metadata presentes en `/positions`. |
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
pmr reconcile run --wallet 0x...
pmr reconcile status
pmr trust status
```
