# Flujos Principales

Guía práctica de los flujos de trabajo más comunes con `pmr`.

---

## Setup inicial

```bash
# 1. Migrar la base de datos a la última versión
pmr db upgrade

# 2. Verificar que la migración applied correctamente
pmr db current

# 3. (Opcional) Verificar infraestructura
pmr version
```

---

## Agregar una wallet y sincronizar desde cero

```bash
# 1. Agregar wallet a la watchlist
pmr wallet add 0x2005d16a84ceefa912d4e380cd32e7ff827875ea --name "RN1"

# 2. Verificar que se agregó
pmr wallet list

# 3. Backfill completo (todas las activity)
pmr sync backfill 0x2005d16a84ceefa912d4e380cd32e7ff827875ea

# 4. Verificar estado del sync
pmr sync status

# 5. Ingerir en el ledger
pmr ingest run --wallet 0x2005d16a84ceefa912d4e380cd32e7ff827875ea

# 6. Verificar ledger
pmr ledger stats --wallet 0x2005d16a84ceefa912d4e380cd32e7ff827875ea
```

---



## Sync incremental (actualizar datos nuevos)

Después del backfill inicial, usar incremental para traer solo datos nuevos:

```bash
# Sync incremental de una wallet específica
pmr sync incremental 0x2005d16a84ceefa912d4e380cd32e7ff827875ea

# Sync incremental de todas las wallets activas
pmr sync incremental

# Verificar estado
pmr sync status
```

---



## Obtener metadata de mercados

```bash
# Sincronizar metadata de todos los mercados presentes en el ledger
pmr markets sync

# Verificar stats
pmr markets stats

# Sincronizar un mercado específico
pmr markets sync --condition 0xABC123...
```

---



## Calcular fees y PnL

```bash
# 1. Calcular estimaciones de fees
pmr fees compute --wallet 0x...

# 2. Ver reporte de fees por categoría
pmr fees report --wallet 0x... --by-category --pre-post-sports-fee

pmr derive run --wallet <addr>
# 3. Ver descomposición de PnL
pmr pnl show --wallet 0x... --by-category
```

---



## Holdings y calidad de datos

```bash
# 1. Reconstruir holdings (qty + WAC por token)
pmr replay holdings --wallet 0x...

# 2. Ver holdings actuales
pmr holdings show --wallet 0x... --nonzero

# 3. Reporte de calidad de datos
pmr holdings dq --wallet 0x...
```

---



## Episodios (flat-to-flat)

```bash
# 1. Reconstruir episodios
pmr replay episodes --wallet 0x...

# 2. Ver estadísticas de episodios
pmr episodes stats --wallet 0x...

# 3. Ver episodios detallados
pmr episodes show --wallet 0x...

# 4. Ver solo episodios abiertos
pmr episodes show --wallet 0x... --open
```

---



## Derivación y PnL completo

```bash
# 1. Derivar eventos REDEEM_PAYOUT y actualizar PnL
pmr derive run --wallet 0x...

# 2. Ver PnL descompuesto
pmr pnl show --wallet 0x... --by-category
```

---



## Equity diaria

```bash
# 1. Construir curva de equity
pmr equity build --wallet 0x...

# 2. Ver resumen de equity
pmr equity show --wallet 0x...
```

---



## Exposición de mercado

```bash
# 1. Construir exposiciones diarias
pmr exposure build --wallet 0x...

# 2. Ver exposición por mercado
pmr exposure show --wallet 0x...

# 3. Ver vector de exposición por evento
pmr exposure show --wallet 0x... --event <event_id>
```

---



## Enriquecimiento maker/taker

```bash
# 1. Enriquecer fills con datos del subgraph
pmr enrich run --wallet 0x...

# 2. Ver cobertura
pmr enrich coverage --wallet 0x...
```

---



## Orderbook snapshots

```bash
# 1. Tomar snapshot del orderbook para tokens relevantes
pmr books sample-once

# 2. Ver estado del sampler
pmr books status

# 3. Limpiar snapshots antiguos
pmr books prune
```

---


## World Cup forward watch

El collector permanente de Docker usa esta seleccion cuando
`PMR_WORLDCUP_WATCH_ENABLED=true`. Se pueden rastrear maximo 2 wallets.

```bash
# 1. Migrar tablas de Phase 18
pmr db upgrade

# 2. Seleccionar wallets a rastrear (reemplaza la seleccion anterior)
pmr worldcup wallets set 0xwallet1 0xwallet2

# 3. Ver seleccion activa
pmr worldcup wallets list

# 4. Construir/actualizar watchlist World Cup manualmente si hace falta
pmr watchlist build-world-cup --wallet 0xwallet1
pmr watchlist build-world-cup --wallet 0xwallet2

# 5. Tomar una muestra manual de libros de la watchlist
pmr books sample-watchlist --name world_cup_2026 --limit 200

# 6. Construir contexto maker-fill manualmente
pmr context maker-fills --wallet 0xwallet1 --watchlist world_cup_2026 --max-age-s 60

# 7. Ejecutar un ciclo completo manual para una wallet
pmr worldcup tick --wallet 0xwallet1

# 8. Limpiar seleccion si se quiere pausar el rastreo de wallets
pmr worldcup wallets clear
```

En servidor no se debe crear otro writer permanente. El servicio existente
`collector` registra los jobs World Cup y lee la seleccion desde la DB.

---



## Fingerprints behaviorales

```bash
# 1. Calcular fingerprints
pmr fingerprints compute --wallet 0x...

# 2. Ver fingerprint de una wallet
pmr fingerprints show --wallet 0x...

# 3. Comparar múltiples wallets
pmr fingerprints compare --wallets 0x...,0x...,0x...
```

---



## Detectores de estrategia

```bash
# 1. Ejecutar detectores
pmr detect run --wallet 0x...

# 2. Ver labels con scores
pmr detect show --wallet 0x...

# 3. Explicar un detector específico
pmr detect explain --wallet 0x... --detector market_making
```

---



## Generar reporte completo

```bash
# Generar reporte Markdown de una wallet
pmr report wallet 0x... --out /data/exports/rn1.md
```

---



## Reconciliación y confianza

```bash
# 1. Ejecutar reconciliación
pmr reconcile run --wallet 0x...

# 2. Ver estado de reconciliación
pmr reconcile status

# 3. Ver estado de confianza
pmr trust status
```

---



## Backups

```bash
# Crear backup
pmr backup

# Restaurar desde archivo
pmr restore /data/backups/backup_20260704.db
```

---



## Mantenimiento

```bash
# Reprocesar ledger desde raw (corregir parsing)
pmr ingest reparse --wallet 0x...

# Verificar aceptación MVP
pmr acceptance
```

---



## Flujo completo end-to-end (primera vez)

```bash
# Setup
pmr db upgrade
pmr wallet add 0x... --name "Mi wallet"

# Sincronización
pmr sync backfill 0x...
pmr ingest run --wallet 0x...
pmr markets sync

# Fees
pmr fees compute --wallet 0x...

# Holdings y episodios
pmr replay holdings --wallet 0x...
pmr replay episodes --wallet 0x...

# PnL
pmr derive run --wallet 0x...
pmr pnl show --wallet 0x... --by-category

# Equity y exposición
pmr equity build --wallet 0x...
pmr exposure build --wallet 0x...

# Enriquecimiento
pmr enrich run --wallet 0x...

# Análisis
pmr fingerprints compute --wallet 0x...
pmr detect run --wallet 0x...

# Reporte
pmr report wallet 0x...

# Verificación final
pmr reconcile run --wallet 0x...
pmr trust status
```

---



## Flujo de actualización diaria

```bash
# Después de que el scheduler corre (o manualmente)
pmr sync incremental
pmr ingest run
pmr markets sync
pmr fees compute
pmr derive run
pmr replay holdings
pmr replay episodes
pmr equity build
pmr exposure build
pmr enrich run
pmr fingerprints compute
pmr detect run
```
