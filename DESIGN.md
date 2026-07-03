# Polymarket Wallet Research Platform — Technical Design

Status: design complete, pre-implementation. 2026-07-03.
Companions: `CONTEXT.md` (glossary — canonical definitions), `docs/adr/0001–0006` (decisions with rationale).
Every data-source capability claimed here was verified empirically on 2026-07-03 (see ADR 0001).

Independence constraint: fully separate project from the copy bot. Read-only toward the outside world. The bot is never modified, depended on, or redeployed.

---

## 1. High-level architecture

All components are modules of **one Python core library** running in **one collector/worker process** plus **one dashboard process** (MVP). Communication is in-process function calls plus the SQLite database. No queues, no brokers (ADR 0002, 0004).

| Component | Responsibility | Why it exists |
|---|---|---|
| **Wallet Manager** | Owns the watchlist; schedules backfills and incremental syncs per wallet; tracks sync status; detects stale/failed syncs | Makes scaling to thousands of wallets a collector problem, not an architecture problem |
| **Collector** | Source adapters: Data-API `/activity` (canonical feed, time-windowed), Gamma (markets/resolutions), Goldsky subgraph (maker/taker backfill), RPC `OrderFilled` (recent maker/taker gap), CLOB `/book` + `/prices-history` | The only component that touches the network; the only component that changes for global-scale ingestion |
| **Raw Store** | Persists every API response verbatim (gzipped, append-only) before parsing; indexed in `raw_fetches` | Recovery tier: rebuild DB without re-fetching; audit trail; upstream-change forensics |
| **Ingestor** | Parses raw payloads into `wallet_events` (idempotent, dedupe-keyed); emits Derived Events (e.g. redemption proceeds) | Separates fetching from interpretation; re-parseable when bugs are found |
| **Ledger** | Append-only `wallet_events` — the single source of truth (ADR 0002) | Immutable financial ledger; all accounting is a replay |
| **Projection Engine** | Replays the ledger into disposable projections: holdings, episodes (flat-to-flat + WAC, ADR 0003), exposures, daily equity, fingerprints | Fast reads without compromising replayability; drop-and-rebuild by contract |
| **Exposure Engine** | Computes market/event exposure data-driven by Market Structure Descriptors (binary → directional+bond; negRisk event → mutually-exclusive vector; unknown → flagged raw vector) | Polymarket market structures vary and will change; no hardcoded YES/NO logic |
| **Mark Service** | Pluggable mark sources behind one interface: prices-history (historical), resolution values (terminal, always override), live book (future); writes `price_points` with staleness | Honest valuation; upgradeable prospectively without touching analytics |
| **Book Sampler** | REST-polls CLOB `/book` for Relevant Tokens every 1–5 min; top ~10 levels + raw JSON; strict retention (ADR 0005) | Orderbook state is unrecoverable retroactively; collect the relevant slice early |
| **Strategy Layer** | Behavioral Fingerprints (measurements) + versioned Detectors emitting scored, evidence-carrying Strategy Labels with Blind Spots | Honest hypotheses over boolean verdicts; ML slots in later as new detectors on the same schema |
| **Reconciliation** | Permanent projection comparing ledger state to `/positions` + `/value` after every sync; drives trusted/untrusted status (ADR 0006) | Event sourcing fails silently without an external oracle; also detects upstream API changes |
| **Report Generator** | Produces written research reports (e.g. "Why is RN1 profitable?") from platform data | The actual deliverable; MVP acceptance gate |
| **CLI** | Operate everything headless: add wallet, backfill, replay, reconcile, report, backup, restore | Proves the deletion test; operations without UI |
| **Research Shell** | Streamlit-class dashboard; renders core-library outputs only; disposable (ADR 0004) | Research velocity now; thrown away at commercialization with zero logic loss |

## 2. Project structure

```
polymarket-research/            # new independent repo
├── CONTEXT.md                  # glossary (moved from design folder)
├── docs/adr/                   # 0001–0006 + future decisions
├── pyproject.toml              # one installable package: pmresearch
├── docker-compose.yml          # collector + dashboard; /data volume mounts
├── alembic/                    # schema migrations from day one (Postgres-portable)
├── pmresearch/                 # THE CORE LIBRARY (ADR 0004)
│   ├── sources/                # one adapter per external source (dataapi, gamma, subgraph, rpc, clob)
│   ├── rawstore/               # raw snapshot persistence + index
│   ├── ingest/                 # payload → wallet_events; dedupe; derived events
│   ├── ledger/                 # event model, replay primitives
│   ├── projections/            # holdings, episodes, equity, exposure, fingerprints (each drop-and-rebuild)
│   ├── exposure/               # structure descriptors + exposure engine
│   ├── marks/                  # mark-source interface + implementations
│   ├── detectors/              # base detector contract + market_making, inventory_cycling, value_betting
│   ├── reconcile/              # oracle checks, trust status
│   ├── reports/                # report generators (RN1 memo)
│   ├── walletmanager/          # watchlist, sync scheduling/state
│   ├── db/                     # schema, sessions, portable SQL only
│   └── cli/                    # pmr <command>
├── apps/
│   ├── collector/              # thin entrypoint: APScheduler + library calls
│   └── dashboard/              # thin Streamlit shell — DELETABLE (deletion test)
├── notebooks/                  # first-class research surface; imports pmresearch
├── tests/                      # unit + golden-ledger replay tests + reconciliation fixtures
└── ops/                        # backup.sh (VACUUM INTO + rclone), restore.sh, restore drill script
```

Why: `pmresearch/` vs `apps/` enforces library-first structurally; `sources/` isolates every external dependency behind an adapter (Goldsky deprecation = one file); `projections/` grouping makes the "disposable cache" contract auditable.

## 3. Data pipeline

```
External APIs (Data-API /activity ▸ Gamma ▸ Subgraph ▸ RPC ▸ CLOB book/prices)
   ↓ Collector (Wallet Manager schedules; time-windowed; rate-limited)
Raw Store (verbatim gzipped JSON, append-only)            ← recovery tier 1
   ↓ Ingestor (parse, dedupe, derive)
wallet_events LEDGER (append-only, immutable)             ← source of truth
   ↓ replay (Projection Engine)
holdings → episodes (WAC) → exposures (token/market/event) → daily equity (marks + staleness)
   ↓                                                    ↘
Behavioral Fingerprints                                  Reconciliation vs /positions,/value
   ↓                                                        ↓
Strategy Detectors (scored hypotheses + evidence)        trusted/untrusted status
   ↓
Reports ▸ Dashboard ▸ Notebooks ▸ CLI     (all via core library)
```

Enrichment (maker/taker/fee) joins onto existing TRADE events by `(transactionHash, wallet, asset)`; it lags the canonical feed and never creates/deletes events.

## 4. Development phases

Each phase has a verify gate; nothing proceeds on a red gate. Order rationale: correctness infrastructure (raw store, ledger, reconciliation) before any analytics, so bugs are caught before anything is built on top of them; irreplaceable collection (book sampler) as early as its dependencies allow.

| Phase | Scope | Verify gate |
|---|---|---|
| 0 | Repo scaffold, core lib skeleton, Alembic, Compose + `/data` volumes, backup/restore scripts | Restore drill passes on an empty DB; deletion test trivially true |
| 1 | Data-API adapter + Raw Store + Wallet Manager (add wallet, backfill via time windows, incremental sync, sync state) | Re-running backfill is a no-op (idempotent); event counts match API spot checks |
| 2 | Gamma adapter: markets, tokens, events, resolutions; structure descriptors | Every conditionId in the ledger has a market row; descriptors classify all watchlist markets or flag unclassified |
| 3 | Ledger replay → holdings; **reconciliation v1 (holdings vs /positions.size)** | Zero drift for 3 wallets incl. RN1 |
| 4 | Episodes + WAC + derived redemption events; reconciliation v2 (avgPrice, realizedPnl) | WAC matches /positions.avgPrice within tolerance |
| 5 | Mark Service + price_points + daily equity + staleness; reconciliation v3 (/value band) | Equity within 1–2% of /value; stale-share reported |
| 6 | Exposure Engine: directional+bond, event rollup, negRisk netting | Known MM wallet shows bond inventory; RN1 exposures sane vs UI |
| 7 | Enrichment: subgraph backfill + RPC recent-gap; maker/taker on fills | Maker share computable for recent 7 days, not just subgraph horizon |
| 8 | Book Sampler + retention/compression policies | Snapshots accrue only for Relevant Tokens; storage bounded |
| 9 | Fingerprints (per wallet & wallet×category) + 3 detectors with evidence/blind spots | Scores reproducible from stored evidence; versioned |
| 10 | Research Shell (10 views) + CLI reports + RN1 report generator | Deletion test passes; RN1 memo generates end-to-end |
| 11 | Hardening: staleness/failure alerts, 7-day soak, restore drill on real data | MVP definition of done (ADR 0006) — all seven points green |

## 5. Database design (SQLite, Alembic-managed, Postgres-portable SQL)

**Global facts — ledger & raw:**
- `wallet_events` — id PK; wallet; event_type (TRADE/MERGE/SPLIT/REDEEM/REWARD/TRANSFER/…); ts; tx_hash; condition_id; token_id; side; delta_shares (signed); delta_usdc (signed); price; source; is_derived; raw_ref → raw_fetches; dedupe_key UNIQUE; ingested_at. *Append-only; corrections are new events.*
- `fill_enrichment` — event_id FK UNIQUE; role (maker/taker); order_hash; fee; source (subgraph/rpc); enriched_at. *Nullable-by-absence; never blocks the canonical feed.*
- `raw_fetches` — id; source; endpoint; params_json; fetched_at; http_status; file_path (gzipped payload on /data volume); content_hash.

**Global facts — dimensions:**
- `markets` — condition_id PK; question; slug; category; event_id FK; neg_risk; outcomes_json; clob_token_ids_json; start/end dates; closed; resolution_prices_json; closed_time; structure_type (from descriptor); updated_at.
- `tokens` — token_id PK; condition_id FK; outcome_index; outcome_label. *(complementarity by index, never label)*
- `pm_events` — event_id PK; title; slug; neg_risk; tags_json.
- `price_points` — token_id; ts; price; source; mark_age_s; stale (bool); meta_json; PK(token_id, ts, source).
- `book_snapshots` — token_id; ts; best_bid; best_ask; spread; mid; depth_topN_json; raw_ref; PK(token_id, ts).

**Projections (disposable; each row carries projection_version):**
- `holdings` — wallet; token_id; as_of; qty; wac_cost.
- `episodes` — id; wallet; token_id; condition_id; open_ts; close_ts; close_reason (flat/resolution/open); peak_qty; num_adds; num_partial_exits; wac_entry; realized_pnl; reward_income; events_consumed.
- `exposures_daily` — wallet; condition_id; date; directional; bond; structure_type; event_id.
- `daily_equity` — wallet; date; portfolio_value; realized_pnl; unrealized_pnl; reward_income; drawdown; stale_equity_share.
- `fingerprints` — wallet; scope (all or category); feature; value; window; computed_at; version.
- `strategy_labels` — wallet; scope; detector_name; detector_version; label; score (0–1); evidence_json (features + values); blind_spots; computed_at. *No booleans.*

**Quality & ops:**
- `reconciliation` — wallet; ts; check_type; expected; computed; abs_diff; pct_diff; tolerance; status; source; notes.
- `wallet_trust` — wallet; status (trusted/untrusted); since; reason (derived from reconciliation).
- `sync_state` — wallet; backfill_complete; backfill_cursor_ts; last_incremental_ts; last_success_at; last_error; consecutive_failures; status.

**Workspace (the future multi-tenancy seam — only these tables ever gain workspace_id):**
- `watchlist` — wallet; active; added_at; removed_at.
- `wallet_tags` — wallet; tag; note; created_at.
- `notes` — id; subject_type/subject_id; text; created_at.

Relationships: everything joins through `wallet` (address), `condition_id`, `token_id`, `event_id`; enrichment joins fills via event_id (resolved from tx_hash+wallet+token at enrichment time).

## 6. Analytics (metric catalog)

All computed from ledger replay; per wallet and per wallet×category; episode metrics carry the strategy-dependence caveat (CONTEXT.md).

- **PnL & returns:** realized (WAC), unrealized (marked, with stale share), total; PnL decomposition by income type — directional / bond+merge / REWARD / redemption; daily PnL; ROI on deployed capital; return curve; max drawdown (approximate intraday — standing caveat); Sharpe-like ratio on daily returns; profit factor; win rate per episode.
- **Execution (token level):** episode count/duration distribution; adds per episode; partial-exit frequency; position size distribution; scaling profile (size vs time-in-episode); time-to-event-start at entry; fill fragmentation; maker share / taker share; fee paid vs rebate earned.
- **Inventory (market level):** bond inventory ratio (time-weighted); MERGE/SPLIT frequency; two-sided-fill ratio per market; directional flip rate; concurrent open positions; capital utilization.
- **Portfolio:** market/category concentration (HHI); event concentration; turnover; reward income share; redeem vs early-exit ratio.
- **Edge/calibration:** entry price distribution; resolution-outcome calibration (P(win) vs entry price buckets — the value-betting signal); markout (price at entry +5m/+1h/+resolution vs entry, where prices-history allows); Kelly-implied sizing vs actual sizing.
- **Data quality (first-class):** stale mark share; reconciliation status; enrichment coverage; backfill completeness.

## 7. Position reconstruction (decided — ADR 0003)

Flat-to-flat **Episodes** at token level consuming all event types; **WAC** for realized PnL; no debounce (micro-episodes are signal); FIFO later only as a hold-time/lot-aging projection; LIFO never. Edge cases handled by design: fill fragmentation (WAC-insensitive), MERGE/REDEEM closing positions without trades (ledger event types), resolution closes (derived redemption events at terminal value), dust (config epsilon for "flat"), simultaneous multi-market positions (episodes are per token; exposure layers aggregate).

## 8. Strategy detection (decided — Q7)

Two layers: **Behavioral Fingerprints** (mechanical measurements) → **Detectors** emitting scored hypotheses with evidence and blind spots. MVP detectors:
- `market_making_v1`: high maker share + reward income share + bond ratio + short episode median + two-sided fills.
- `inventory_cycling_v1`: bond accumulation + MERGE frequency + capital recycling cadence.
- `value_betting_v1`: taker-dominant + long episodes held to resolution + positive calibration edge (wins systematically above entry-price-implied probability) + category concentration.
Later (same schema): scalping (markout at short horizons), swing (episode duration + price-path), momentum/mean-reversion (entry vs prior price move — noisy at 1-min fidelity; blind spot recorded), arbitrage (within-event mutually-exclusive overround capture), hedging (event-level offsetting exposure).

## 9. Future orderbook integration

WSS market-channel collector lands in the same raw-capture area and `book_snapshots` role (schema-compatible with sampler); adds book-derived mark sources (midpoint, conservative exit) prospectively; enables queue-position and quote-behavior features. Nothing upstream changes: it is a new source adapter + new mark source + new fingerprint features.

## 10. Maker/taker estimation (verified)

Not estimation — **ground truth exists**: on-chain `OrderFilled` (maker, taker, fee per fill), indexed by Goldsky (weeks-lagged) and readable via RPC for the recent gap. Confidence tiers: **exact** (enriched fills), **pending** (recent, not yet enriched — never guessed), **inferred** (optional: Data-API takerOnly=true/false set difference; medium confidence; used only for coverage stats, not fingerprints). What can never be known: quote placement, cancellations, unfilled orders — recorded as detector blind spots.

## 11. Machine learning (deferred by design)

Realistic later modules, all consuming fingerprints and emitting the standard detector schema: wallet clustering over fingerprint vectors (needs the global universe or ≥hundreds of wallets to be meaningful); supervised strategy classification (training data = researcher-confirmed labels accumulated in the workspace); anomaly detection on behavior change (fingerprint drift); sequence models over event streams (most speculative — data-hungry, defer furthest). Not realistic near-term: outcome prediction / EV estimation from this data alone.

## 12. Dashboard views (MVP, all rendering library outputs)

Wallet overview (equity, decomposition, trust badge) · ledger explorer (filterable event stream) · episode explorer (list → drill to per-episode replay, on-demand fine marks) · market exposure (directional+bond over time) · event exposure (sibling markets, netting) · PnL decomposition · reward/rebate analysis · fingerprint view (vs watchlist percentiles) · strategy hypotheses (scores + expandable evidence + blind spots) · data quality (reconciliation results, sync staleness, stale-mark share, enrichment coverage) · wallet comparison (fingerprint side-by-side).

## 13. Technology stack (decided — ADR 0004)

Python 3.12 · SQLite (WAL; single writer = collector process) + Alembic · Polars · DuckDB optional (attach/Parquet ad-hoc) · APScheduler in-process · httpx with adaptive backoff (rate limits are unpublished — measured empirically, config-tunable) · Streamlit shell · Docker Compose on Dokploy VPS: code-only images, `/data` host volume {db, raw, backups, exports, logs} · Litestream optional later; MVP backup = nightly `VACUUM INTO` + rclone to S3-compatible free tier · monthly scripted restore drill.

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Upstream API change/removal (fields, offset caps, endpoint retirement) | Raw snapshots (re-parse without re-fetch); reconciliation as tripwire; one adapter per source |
| Goldsky subgraph deprecation or growing lag | RPC adapter is the fallback for the entire maker/taker role, not just the gap |
| Silent accounting bugs corrupting research conclusions | Reconciliation projection + trust status (ADR 0006); golden-ledger replay tests |
| Data loss on container rebuild/VPS failure | /data volume separation; nightly + off-VPS backups; **drilled** restores |
| Unknown rate limits → bans | Adaptive backoff, caching, lazy prices-history, watchlist-scale volumes are tiny |
| Storage growth (books, raw) | Retention/compression policies from day one (ADR 0005); Relevant-Tokens scoping |
| Stale marks distorting drawdown/Sharpe | Staleness explicit on every figure; documented approximation caveat |
| Scope creep toward product/infra | Library-first + deletion test (ADR 0004); phase gates; MVP definition of done |
| SQLite concurrency | WAL mode; single writer process; dashboard reads only |
| Wrong episode/WAC conventions discovered late | Projections are disposable — conventions are replayable; primary-method change is the one accepted irreversibility (ADR 0003) |

## 15. Roadmap

**MVP (phases 0–11 above).** Relative complexity: collector+ledger+reconciliation ≈ 50% of effort; projections+exposure ≈ 25%; detectors+reports+shell ≈ 25%. Critical path: 0→1→3 (nothing is trustworthy before reconciliation exists). Exit: the RN1 memo + 7-point definition of done (ADR 0006).

**v2 — deeper research:** WSS orderbook collector; book-derived marks; FIFO hold-time projection; markout analytics; cross-event correlation; more detectors; automatic wallet discovery (leaderboard scan → watchlist candidates); bot-log import for copy-fidelity cross-validation.

**v3 — scale:** global fill ingestion (subgraph/chain firehose → same ledger schema; collector-only change by design); wallet clustering/ML detectors; Postgres migration (Alembic-managed; portable SQL pays off).

**v4 — commercialization:** FastAPI wrapping the core library; React product UI; retire Streamlit; workspace_id on workspace tables only; auth/billing. The ledger, projections, detectors, and reports are untouched — that is the payoff of the seams chosen in ADRs 0002 and 0004.
