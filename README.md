# polymarket-research

Quantitative research platform for analyzing successful Polymarket wallets — understanding *why* certain wallets are profitable, and eventually discovering automatable strategies.

**Status:** Phase 15 complete (detectors, fingerprints, book sampler, enrichment, reports). Read-only toward the outside world; no dependency on or modification of any existing deployment.

## Quick start

```bash
# Install
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"

# Configure
copy .env.example .env        # set PMR_DATA_DIR, PMR_RPC_URL, etc.

# Run
pmr wallet add 0x... --name "..."
pmr sync backfill 0x...
pmr ingest run --wallet 0x...
pmr markets sync
```

## CLI reference

The `pmr` CLI is the primary interface. Core flow:

```bash
pmr wallet add 0x... --name "..."      # Add wallet to watchlist
pmr sync backfill 0x...                # Full historical sync
pmr sync incremental 0x...            # Incremental sync
pmr ingest run --wallet 0x...          # Parse raw -> ledger events
pmr markets sync                       # Sync market metadata (Gamma)
pmr replay holdings --wallet 0x...     # Rebuild holdings projection
pmr replay episodes --wallet 0x...     # Rebuild episodes (WAC)
pmr pnl show --wallet 0x...            # PnL report
pmr equity show --wallet 0x...         # Daily equity curve
pmr ledger stats --wallet 0x...        # Ledger statistics
pmr fees report --wallet 0x...         # Fee attribution
pmr exposure show --wallet 0x...       # Market/event exposure
pmr enrich run --wallet 0x...          # Maker/taker enrichment
pmr books sample --wallet 0x...        # Book snapshots
pmr fingerprints compute --wallet 0x... # Behavioral fingerprints
pmr detect run --wallet 0x...          # Strategy detectors
pmr report generate --wallet 0x...     # Research report
pmr reconcile --wallet 0x...           # Reconciliation check
```

## Development

```bash
make test              # Run full test suite
make up                # Docker compose up --build
make down              # Docker compose down
make backup            # VACUUM INTO backup
make restore-drill     # Full restore drill
streamlit run apps/dashboard/Home.py  # Research Shell dashboard
```

Run a single test: `pytest tests/test_ingest.py::test_name`

## Architecture

Data flows through immutable, replayable stages — each rebuildable from the one before it without re-fetching from external APIs:

```
External APIs (Data-API, Gamma, Goldsky, RPC, CLOB)
   ↓
Raw Store (verbatim gzipped JSON, append-only)     ← recovery tier
   ↓
wallet_events LEDGER (append-only, immutable)      ← source of truth
   ↓
holdings → episodes (WAC) → exposures → daily equity → fingerprints
   ↓
Strategy Detectors → Reports
```

**Key components:**
- **Wallet Manager** — watchlist, sync scheduling, staleness detection
- **Raw Store** — every API response persisted before parsing
- **Ledger** — append-only event stream (TRADE, MERGE, SPLIT, REDEEM, REWARD, …)
- **Exposure Engine** — directional+bond decomposition, event-level netting
- **Enrichment** — maker/taker/fee from subgraph + RPC
- **Book Sampler** — orderbook snapshots for relevant tokens
- **Detectors** — market_making, inventory_cycling, value_betting (scored hypotheses with evidence)

## Tech stack

Python 3.12 · SQLite (WAL) + Alembic · SQLAlchemy (raw SQL, no ORM) · Polars · httpx · APScheduler · Streamlit (disposable dashboard) · Docker Compose

## Documentation

- [DESIGN.md](DESIGN.md) — full technical plan: architecture, pipeline, phases, database schema, analytics, strategy detection, risks, roadmap.
- [CONTEXT.md](CONTEXT.md) — domain glossary (canonical terms).
- [CLAUDE.md](CLAUDE.md) — development guidelines and architecture reference.
- [docs/adr/](docs/adr/) — architecture decision records (0001–0006).
