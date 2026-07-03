# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Quantitative research platform for analyzing Polymarket wallets: understanding why certain wallets are profitable, eventually discovering automatable strategies. Read-only toward the outside world — no dependency on or modification of any existing deployment (e.g. the separate copy bot).

Canonical docs, read these before making design decisions:
- [DESIGN.md](DESIGN.md) — full technical plan: architecture, pipeline phases, DB schema, analytics, strategy detection, roadmap.
- [CONTEXT.md](CONTEXT.md) — domain glossary. Definitions only, no implementation details. Use these terms consistently in code and commits.
- [CLI.md](CLI.md) — full CLI reference (in Spanish) for every `pmr` command and env var.
- [docs/plan/IMPLEMENTATION_PLAN.md](docs/plan/IMPLEMENTATION_PLAN.md) — phase-by-phase implementation plan (code comments reference phases, e.g. "Phase 2", "Phase 8").
- [docs/adr/](docs/adr/) — architecture decision records; read the relevant ADR before touching the area it governs:
  - 0001 multi-source canonical data
  - 0002 event-sourced ledger on SQLite
  - 0003 episodes / weighted-average-cost accounting
  - 0004 library-first, disposable dashboard
  - 0005 forward collection: RPC and book sampler
  - 0006 reconciliation as projection

## Commands

```bash
pytest                         # run tests (Makefile: make test)
docker compose up --build      # run the collector container (make up)
docker compose down            # make down
bash ops/backup.sh              # make backup
bash ops/restore_drill.sh       # make restore-drill

pmr db upgrade                  # apply pending Alembic migrations
pmr db current                  # show current Alembic revision
```

Run a single test: `pytest tests/test_ledger_stats.py::test_name`.

The `pmr` CLI (entry point `pmresearch.cli:main`) is the primary way to exercise the pipeline end to end. See [CLI.md](CLI.md) for the full command table. Core flow:

```bash
pmr wallet add 0x... --name "..."
pmr sync backfill 0x...          # Data API -> Raw Store
pmr ingest run --wallet 0x...    # Raw Store -> wallet_events ledger
pmr markets sync --all           # Gamma metadata -> markets/tokens/events
pmr fees report --wallet 0x... --by-category --pre-post-sports-fee
```

Config is env-first (`pmresearch/config.py`): `PMR_DATA_DIR` (default `/data`) is the root for `db/`, `raw/`, `backups/`, `exports/`, `logs/`. Also `PMR_LOG_LEVEL`, `PMR_RPC_URL`, `PMR_RCLONE_REMOTE`, and `PMR_BACKUP_RETAIN` (bash-only, used by `ops/backup.sh`, default 14 backups kept). Tests override this via the `settings` fixture in `tests/conftest.py`, which points `data_dir` at `tmp_path` and runs Alembic migrations to head — never point real config at a shared DB in tests.

The Docker image (`Dockerfile`) runs `apps/collector/main.py`, a thin entrypoint delegating to `pmresearch.walletmanager.scheduler.run_forever` — same code path as `pmr run`. `apps/*` are thin shells over the core library (ADR 0004); business logic never lives there.

## Architecture: the pipeline

Data flows through immutable, replayable stages — each stage is rebuildable from the one before it without re-fetching from external APIs (ADR 0002, "recovery tier 1"):

1. **Sources** (`pmresearch/sources/`) — thin clients for external APIs: `dataapi.py` (Polymarket Data API, wallet activity) and `gamma.py` (Gamma API, market/event metadata). `base.py` defines the shared client contract.

2. **Raw Store** (`pmresearch/rawstore/store.py`) — every external API response is persisted verbatim (gzipped) before anything parses it, indexed in `raw_fetches`. Dedupes by content hash of `(source, endpoint, params)`, so re-running a backfill or incremental sync is a near no-op. This is the source of truth the DB can always be rebuilt from.

3. **Ledger** (`pmresearch/ledger/model.py`, populated via `pmresearch/ingest/activity.py`) — `wallet_events` is an **append-only** table; one row per atomic wallet action (TRADE, MERGE, SPLIT, REDEEM, REWARD, rebates, ...). Corrections are new events, never updates. Each event type has a documented signed `delta_shares`/`delta_usdc` convention in `ledger/model.py` — read the module docstring before changing parsing logic. Event types outside the documented set are still stored (never dropped) with zero deltas and a logged warning, per ADR 0006 ("never silently invented").

4. **Markets metadata** (`pmresearch/ingest/markets.py`, `pmresearch/exposure/descriptors.py`) — Gamma metadata (markets, tokens, events, structure descriptors) synced separately from wallet activity and joined against the ledger by `condition_id`.

5. **Fees** (`pmresearch/fees/`) — `schedules.py` defines fee schedules (e.g. the sports fee introduced 2026-03-30), `estimate.py` computes per-trade fee estimates.

6. **Reports** (`pmresearch/reports/fee_attribution.py`) — gross-vs-net PnL, fee attribution by category, pre/post-schedule splits. Reports are read-only projections over the ledger + fees; they never mutate ledger state (ADR 0006).

Orchestration: `pmresearch/walletmanager/` (`manager.py`, `scheduler.py`, `sync.py`) owns the watchlist and drives scheduled backfill/incremental sync across wallets — this is what `pmr run` executes in the container.

CLI commands (`pmresearch/cli/*.py`) are thin — one file per command group (`wallets.py`, `sync.py`, `ingest.py`, `markets.py`, `fees.py`), each wired into `pmresearch/cli/__init__.py`. Business logic belongs in the module it's exposing, not in the CLI layer.

`DESIGN.md`'s target architecture also specifies `projections/`, `marks/`, `detectors/`, `reconcile/`, and `apps/dashboard/` — these are planned but not yet implemented. Only `sources`, `rawstore`, `ingest`, `ledger`, `exposure`, `fees`, `reports`, `walletmanager`, `db`, `cli` exist today.

## Database

SQLite, migrated with Alembic (`alembic/versions/`, prefixed `m0001`...`m0005`, applied in order). **No SQLAlchemy ORM models** — the codebase deliberately uses raw parameterized SQL (`sqlalchemy.text(...)`) for all reads/writes; Alembic owns the schema exclusively (ADR 0002). `pmresearch/ledger/model.py`'s `WalletEvent` is a plain dataclass used only as an in-memory row shape, not an ORM model. Engine/session setup: `pmresearch/db/engine.py` (sets WAL journal mode + `foreign_keys=ON` per connection); migration runner: `pmresearch/db/migrations.py` (`upgrade_to_head`, `current_revision`). Every schema change needs a new Alembic revision — never hand-edit the SQLite file or bypass migrations.

Backups: `pmr backup`/`pmr restore` (or `ops/backup.sh`/`ops/restore.sh`) use `VACUUM INTO`, never a raw file copy of the live (WAL-mode) DB. `ops/restore.sh` runs `PRAGMA integrity_check` on the backup before swapping it in and refuses to restore a corrupt one. `ops/restore_drill.sh` scripts a full backup → simulated loss → restore → integrity-check → Alembic-head-check drill; run it periodically to prove backups are actually restorable, not just taken.

## Testing

Tests live in `tests/`, one file roughly per module (`test_rawstore.py`, `test_ingest.py`, `test_ledger_stats.py`, `test_fees.py`, `test_walletmanager.py`, `test_sync_backfill_resume.py`, `test_cli_smoke.py`, etc.). `tests/conftest.py` provides `settings` (tmp-dir-backed `Settings`, migrated to head) and `session` (SQLAlchemy session against that settings) fixtures — build on these rather than constructing DB state manually.
