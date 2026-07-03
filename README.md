# polymarket-research

.\.venv\Scripts\pmr ledger stats --wallet $WALLET
.\.venv\Scripts\pmr pnl show --wallet $WALLET --by-category
.\.venv\Scripts\pmr equity show --wallet $WALLET --limit 5

Quantitative research platform for analyzing successful Polymarket wallets — understanding *why* certain wallets are profitable, and eventually discovering automatable strategies.

**Status:** design complete, pre-implementation.

- [DESIGN.md](DESIGN.md) — full technical plan: architecture, pipeline, phases, database schema, analytics, strategy detection, risks, roadmap.
- [CONTEXT.md](CONTEXT.md) — domain glossary (canonical terms).
- [docs/adr/](docs/adr/) — architecture decision records (0001–0006).

Independent from the copy bot: read-only toward the outside world, no dependency on or modification of any existing deployment.
