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

## Flujos comunes

```bash
pmr db upgrade
pmr wallet add 0x... --name "Wallet ejemplo"
pmr sync backfill 0x...
pmr ingest run --wallet 0x...
pmr markets sync --all
pmr fees report --wallet 0x... --by-category --pre-post-sports-fee
```
