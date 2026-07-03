# Implementation Plan — Polymarket Wallet Research Platform

Status: architecture frozen. Source of truth: `DESIGN.md`, `CONTEXT.md`, `docs/adr/0001–0006`.
This document turns the design into 18 independently-implementable phases. Each phase is meant to be requested in its own prompt, implemented, verified, and committed before the next begins. No redesign unless a phase hits a critical implementation blocker — in that case the blocker is documented in a new ADR, not debated inline.

Global invariants (apply to every phase; violations are review-rejections):

- The copy bot is never touched, imported, or depended on. Separate repo, read-only toward the world.
- All persistent state lives under the mounted `/data` volume: `/data/db/`, `/data/raw/`, `/data/backups/`, `/data/exports/`, `/data/logs/`. Containers are disposable; data is not.
- All logic lives in the `pmresearch` core library. `apps/*` are thin shells. Deletion test must hold at every phase.
- SQLite in WAL mode; the collector process is the only writer. Alembic owns the schema; no `CREATE TABLE` outside migrations. SQL kept Postgres-portable.
- Every external HTTP response is written to the Raw Store before parsing.
- `wallet_events` is append-only. Corrections are new events. Projections are drop-and-rebuild.
- Free/near-free: no paid services. Optional free-tier keys (RPC, rclone remote) via config, never required to boot.
- Stack: Python 3.12, SQLite + Alembic, Polars, httpx, APScheduler, Docker Compose, pytest. No FastAPI/React. Streamlit only in Phase 16.

Conventions used below:

- CLI entrypoint is `pmr` (installed by the package).
- Migrations are numbered per phase (`m00xx_*`); each phase lists only *new* tables/columns.
- "Golden ledger tests" = small hand-written event fixtures with independently hand-computed expected outputs, committed under `tests/fixtures/`.
- HTTP tests use recorded JSON fixtures + a fake httpx transport (no live calls in CI/tests).

---



## Phase 0 — Repo scaffold, package, Compose, /data, SQLite/Alembic, config, logging, backup/restore

**Goal:** a bootable skeleton where the container is provably disposable and the data provably isn't.

**Scope:** project scaffold; installable `pmresearch` package with CLI; config and logging; Alembic wired to SQLite under `/data`; Docker Compose with host-mounted `/data`; backup/restore scripts; test harness. No external API calls yet.

**Files/modules:**

- `pyproject.toml` (package `pmresearch`, console script `pmr`), `.gitignore`, `Makefile` (`make test`, `make up`, `make backup`)
- `pmresearch/config.py` — env-first settings (`PMR_DATA_DIR` default `/data`, DB path, log level, optional keys as empty defaults)
- `pmresearch/logging_setup.py` — stdlib logging, console + rotating file under `/data/logs/`
- `pmresearch/db/engine.py` — engine/session factory, WAL + foreign_keys pragmas
- `alembic/` — env.py bound to config DB path; migration `m0001_baseline` (empty schema marker)
- `pmresearch/cli/__init__.py` + commands: `pmr version`, `pmr db upgrade`, `pmr db current`, `pmr backup`, `pmr restore <file>`
- `apps/collector/main.py` — starts logging, runs migrations, starts an (empty) APScheduler loop
- `Dockerfile` (code only), `docker-compose.yml` (service `collector`; volume `./data:/data` locally, named/host volume on Dokploy)
- `ops/backup.sh` (`sqlite3 ... "VACUUM INTO"` to `/data/backups/`, timestamped, prune old; optional rclone sync if remote configured), `ops/restore.sh`, `ops/restore_drill.sh`
- `tests/test_config.py`, `tests/test_db.py`, `tests/test_cli_smoke.py`

**Migrations:** `m0001_baseline` (alembic version table only).

**CLI:** `pmr version` · `pmr db upgrade` · `pmr db current` · `pmr backup` · `pmr restore <file>`.

**Tests:** config resolves from env with defaults; migration upgrade→downgrade→upgrade round-trip on a temp DB; WAL pragma active; `pmr backup` then `pmr restore` on a temp data dir yields byte-equivalent logical DB; CLI smoke (`pmr version` exit 0).

**Manual verification:** `docker compose up` → container starts, `/data/{db,raw,backups,exports,logs}` created on host, migrations applied; `docker compose down && docker compose build --no-cache && up` → DB still present; run `ops/restore_drill.sh` on the empty DB → green.

**Acceptance criteria:** restore drill passes on an empty DB; rebuilding the image loses nothing under `/data`; `pmr` works both inside the container and locally (`PMR_DATA_DIR=./data`); tests green.

**Common failure modes:** volume path baked into image instead of mounted (test by rebuild); Alembic needs `render_as_batch=True` for SQLite ALTERs — set it now; Windows dev vs Linux container path handling (always resolve through `config.data_dir`); backing up the live DB file directly instead of `VACUUM INTO` (WAL corruption).

**Prompt to implement:** see end of document (Phase 0 prompt given in full).

---



## Phase 1 — Data-API /activity adapter, Raw Store, Wallet Manager, watchlist, backfill, incremental sync

**Goal:** for any watchlist wallet, the platform can fetch its complete `/activity` history and keep it current — with every response stored verbatim first.

**Scope:** Data-API source adapter (activity only); raw snapshot persistence + index; watchlist CRUD; Wallet Manager scheduling backfill/incremental; sync state tracking; APScheduler jobs wired in the collector. **No parsing into the ledger yet** (Phase 2).

**Files/modules:**

- `pmresearch/sources/base.py` — adapter contract: fetch → persist raw → return raw_fetch ids; shared httpx client, retry/backoff (429/5xx), rate-limit config
- `pmresearch/sources/dataapi.py` — `/activity` windowed fetcher: descending pages within `[start, end)`; on offset-cap error or 3000 rows reached, split the window and recurse; `sortDirection`, `limit=500` paging (verified params per ADR 0001)
- `pmresearch/rawstore/store.py` — write gzipped payload to `/data/raw/dataapi/activity/{wallet}/{utc_ts}_{content_hash8}.json.gz`; insert `raw_fetches` row; skip write if identical content_hash already indexed for same params
- `pmresearch/walletmanager/manager.py` — add/remove/list wallets; decide next action per wallet (backfill → incremental); staleness detection (no success in N× cadence)
- `pmresearch/walletmanager/scheduler.py` — APScheduler job defs: incremental every 5 min/wallet; backfill as on-demand long job
- `apps/collector/main.py` — register jobs
- `pmresearch/cli/wallets.py`, `pmresearch/cli/sync.py`

**Migrations:** `m0002`: `wallets` (address PK, first_seen_at, display_name nullable) · `watchlist` (wallet FK, active, added_at, removed_at) · `sync_state` (wallet PK, backfill_complete, backfill_cursor_ts, last_incremental_ts, last_success_at, last_error, consecutive_failures, status) · `raw_fetches` (id, source, endpoint, params_json, fetched_at, http_status, file_path, content_hash, row_count).

**CLI:** `pmr wallet add <addr> [--name]` · `pmr wallet remove <addr>` · `pmr wallet list` · `pmr sync backfill <addr>` · `pmr sync incremental [<addr>]` · `pmr sync status` · `pmr run` (scheduler foreground — what the container runs).

**Tests:** window-splitting logic against a fake transport that enforces the 3000 cap (assert full coverage, no infinite recursion, no gaps: union of fetched windows == requested range); backoff on 429; raw store dedupe by content hash; sync_state transitions (new → backfilling → complete → incremental; failure increments counter); wallet add/remove idempotency.

**Manual verification:** `pmr wallet add 0x2005d16a84ceefa912d4e380cd32e7ff827875ea` (RN1); `pmr sync backfill` → watch raw files accumulate under `/data/raw/`; spot-check one gzip against the live API; `pmr sync status` shows backfill_complete; run `pmr sync incremental` twice — second run fetches only the new window and writes nothing if content identical.

**Acceptance criteria:** RN1 fully backfilled (raw files span from wallet's first activity to now, verified by earliest/latest timestamps in payloads); re-running backfill is a near-no-op (dedupe skips); incremental sync runs on schedule inside the container; failures visible in `pmr sync status`.

**Common failure modes:** infinite window-splitting on a single second with >3000 events (floor window size at 1s and accept the documented truncation, log loudly); timestamp unit confusion (API uses seconds); `/trades`-style silent parameter ignoring — assert response timestamps actually fall inside the requested window and fail loudly if not; rate-limit bans from over-parallelism (serialize per-wallet, global concurrency 1–2).

**Prompt:** `Implement Phase 1 of IMPLEMENTATION_PLAN.md exactly as scoped. Architecture is frozen (DESIGN.md + ADRs). Do not parse activity into the ledger yet. Write the tests listed for Phase 1, run the full test suite, then walk me through the manual verification steps using wallet 0xe9076a87c5ed90ef16e6fe6529c943baeca0cff6 and show the results. Commit when acceptance criteria pass.`

---



## Phase 2 — Ingest raw activity into the append-only wallet_events ledger

**Goal:** deterministic, idempotent parsing of Raw Store payloads into the immutable ledger.

**Scope:** ingest pipeline raw_fetches → `wallet_events`; dedupe keys; event typing (TRADE, MERGE, SPLIT, REDEEM, REWARD, TRANSFER, unknown-preserving); signed delta conventions; re-parse capability. No projections yet.

**Files/modules:**

- `pmresearch/ledger/model.py` — event dataclass, event_type enum (open set: unknown types stored as-is with a warning, never dropped), signed `delta_shares`/`delta_usdc` conventions per type (documented in module docstring): TRADE BUY = +shares/−usdc; SELL = −shares/+usdc; REWARD = +usdc; MERGE = −shares both tokens/+usdc (as reported); REDEEM as reported (payout derivation deferred to Phase 8)
- `pmresearch/ingest/activity.py` — parse one raw payload → normalized events; `dedupe_key = sha256(wallet|tx_hash|type|asset|side|size|price|timestamp)`; document the collision caveat (two byte-identical fills in one tx collapse — reconciliation in Phase 5 is the detector)
- `pmresearch/ingest/runner.py` — iterate unprocessed raw_fetches (track `ingested_at` on raw_fetches), insert-or-ignore on dedupe_key, mark processed; `--reparse` mode: wipe ledger rows for a wallet and re-ingest from raw (allowed because source of truth is raw + API, and ledger rows carry provenance)
- `pmresearch/cli/ingest.py`

**Migrations:** `m0003`: `wallet_events` (id PK, wallet, event_type, ts, tx_hash, condition_id, token_id, side, delta_shares, delta_usdc, price, usdc_size, source, is_derived default 0, raw_ref FK, dedupe_key UNIQUE, ingested_at) + indexes (wallet+ts, token_id, tx_hash, condition_id); `raw_fetches.ingested_at` column.

**CLI:** `pmr ingest run [--wallet]` · `pmr ingest reparse --wallet <addr>` · `pmr ledger stats [--wallet]` (counts by type, ts range).

**Tests:** golden fixture payload (hand-built, includes TRADE buy/sell, MERGE, REDEEM, REWARD, an unknown type) → exact expected rows; ingesting the same payload twice → zero new rows; ingesting two overlapping raw fetches (same events from window overlap) → deduped; reparse produces identical ledger (row-for-row) as first parse; sign conventions asserted per type.

**Manual verification:** `pmr ingest run --wallet <RN1>`; `pmr ledger stats` shows plausible counts (compare TRADE count order-of-magnitude with Polymarket profile); re-run ingest → 0 new rows; SQL spot-check three events against raw JSON and against the Polymarket UI activity page.

**Acceptance criteria:** full RN1 raw history ingested; idempotency proven (second run inserts 0); every ledger row traces to a raw_ref; unknown event types preserved, logged, and counted, not dropped.

**Common failure modes:** float precision drift making dedupe_keys unstable (normalize numbers to fixed-precision strings before hashing); window-overlap duplicates surviving because of field ordering differences (canonicalize field order in the key); silently dropping rows that fail validation (must go to a logged reject count with raw_ref).  
  
Important note:   
Important deduplication requirement: 

Do not deduplicate only by transactionHash or transactionHash + asset.

A single transaction can contain multiple legitimate fills.

The ledger must preserve multiple real fills from the same transaction.

Use a conservative composite dedupe key based on all available stable fields, and add tests with multiple fills in the same transaction.

If uniqueness is ambiguous, preserve the events and let reconciliation catch issues rather than accidentally dropping valid fills.

**Prompt:** `Implement Phase 2 of IMPLEMENTATION_PLAN.md exactly as scoped. Do not build projections. Pay special attention to dedupe-key stability and idempotency tests. Run tests, then do the manual verification against RN1's ingested ledger and show me ledger stats and three spot-checked events. Commit when acceptance criteria pass.`

---



## Phase 3 — Gamma metadata: markets, tokens, events, structure descriptors

**Goal:** every conditionId in the ledger resolves to a market row with tokens, event membership, resolution data, and a Market Structure Descriptor.

**Scope:** Gamma adapter (markets by condition_ids, batched; events); dimension tables; descriptor derivation (binary / negRisk-event-member / unclassified — by `clobTokenIds` count and flags, never outcome labels per CONTEXT.md); hourly refresh job for touched markets; resolution sweep job.

**Files/modules:** `pmresearch/sources/gamma.py`; `pmresearch/ingest/markets.py` (upsert dimensions — dimensions are mutable reference data, not ledger); `pmresearch/exposure/descriptors.py` (pure function market row → descriptor); scheduler: hourly `markets_refresh` (all condition_ids present in ledger or open holdings) + hourly `resolution_sweep` (unresolved markets past end date); `pmresearch/cli/markets.py`.

**Migrations:** `m0004`: `markets` (condition_id PK, question, slug, category, event_id, neg_risk, outcomes_json, clob_token_ids_json, start_date, end_date, closed, resolution_prices_json, closed_time, structure_type, updated_at) · `tokens` (token_id PK, condition_id FK, outcome_index, outcome_label) · `pm_events` (event_id PK, title, slug, neg_risk, tags_json).

**CLI:** `pmr markets sync [--all|--condition <id>]` · `pmr markets stats` (total, resolved, unclassified-descriptor count, ledger conditions missing a market row).

**Tests:** parse fixture of a binary market, a negRisk event member, a team-name-outcome market (descriptor must not depend on "Yes"/"No" strings); resolution parsing (outcomePrices → terminal values per token); upsert idempotency; "missing market" detection query.

**Manual verification:** `pmr markets sync` after Phase 2 RN1 ingest; `pmr markets stats` → missing count 0 (or explained); spot-check one resolved sports market's resolution prices vs the Polymarket page; verify a negRisk election market groups under its pm_event.

**Acceptance criteria:** 100% of ledger condition_ids have market + token rows; every market has a structure_type; unclassified structures flagged and counted, not guessed (per CONTEXT.md); resolution sweep marks resolved markets within an hour of Gamma exposing it.

**Common failure modes:** Gamma returning stringified JSON fields (`outcomes`, `clobTokenIds` are JSON *strings* — verified) — parse defensively; condition_ids batching limits (chunk requests); markets deleted/renamed upstream (keep last-known row, log staleness); assuming outcome index order matches clobTokenIds order without asserting it.

**Fee-attribution note (do not lose):** after Phase 3 metadata exists, add an explicit fee-attribution subphase before relying on category-level PnL claims. The current backfill/ingest only records Data-API cash flows and rebates as reported; it does **not** explicitly model fee schedules by market category/date. For RN1 analysis we need a small `fee_schedule`/rules layer keyed by effective date and category (notably sports fee regime starting 2026-03-30), then classify trades by Gamma category and compute estimated fees/gross-vs-net PnL. Phase 11 on-chain enrichment may provide actual per-fill `fee`; when available, actual fee should override any schedule estimate.

**Prompt:** `Implement Phase 3 of IMPLEMENTATION_PLAN.md exactly as scoped. Descriptors must be label-agnostic (token index, never outcome strings). Run tests, then manual verification on RN1's markets and show markets stats including unclassified count. Commit when acceptance criteria pass.`

---



## Phase 4 — Ledger replay: current token holdings

**Goal:** first real projection — replay `wallet_events` into per-token holdings with WAC cost basis.

**Scope:** replay engine core (streaming fold over events ordered by ts, id); holdings projection (current qty + running WAC per wallet×token); projection versioning + full rebuild command. Dust epsilon from config. MERGE/SPLIT/REDEEM affect quantity; REWARD does not.

**Files/modules:** `pmresearch/ledger/replay.py` (generic ordered event stream reader); `pmresearch/projections/base.py` (projection contract: name, version, rebuild(wallet), incremental apply later if needed — MVP rebuilds are cheap); `pmresearch/projections/holdings.py`; `pmresearch/cli/replay.py`.

**Migrations:** `m0005`: `holdings` (wallet, token_id, qty, wac_cost, as_of_ts, projection_version, PK wallet+token_id).

**CLI:** `pmr replay holdings [--wallet]` · `pmr holdings show --wallet <addr> [--nonzero]`.

**Tests:** golden ledger fixtures with hand-computed outcomes: buy/sell sequence → qty + WAC; sell-to-flat → zero row (or dust-epsilon flat); MERGE reduces both tokens; REDEEM zeroes the winning token; event ordering ties (same ts) resolved by insert id deterministically; rebuild is deterministic (two rebuilds byte-identical).

**Manual verification:** `pmr replay holdings --wallet <RN1>`; eyeball `pmr holdings show` against the Polymarket UI portfolio page for a handful of positions (exact check is Phase 5's job).

**Acceptance criteria:** rebuild runs over RN1's full ledger in reasonable time (seconds–minutes); deterministic; qty signs never negative beyond dust epsilon (a negative holding = missed events → logged as data-quality warning, not clamped silently).

**Common failure modes:** float accumulation error (accumulate in Decimal or integer micro-units; the API reports 6-decimal sizes); ordering nondeterminism for same-timestamp events; forgetting that SELL events must reduce WAC basis proportionally, not recompute it; negative holdings silently clamped (must be surfaced — they're a missing-events signal).

**Prompt:** `Implement Phase 4 of IMPLEMENTATION_PLAN.md exactly as scoped. Use golden ledger fixtures with hand-computed expectations. Watch numeric precision (Decimal/micro-units). Run tests, replay RN1 holdings, and show me the nonzero holdings next to the same positions on the Polymarket UI. Commit when acceptance criteria pass.`

---



## Phase 5 — Reconciliation v1: holdings vs /positions.size

**Goal:** the external-oracle tripwire goes live (ADR 0006). From here on, correctness is monitored, not hoped.

**Scope:** Data-API `/positions` adapter (raw-stored like everything else); holdings check (exact-match with tiny epsilon); reconciliation fact storage; trusted/untrusted derivation; post-sync hook (reconcile after each incremental cycle); CLI + alert logging.

**Files/modules:** `pmresearch/sources/dataapi.py` (+`fetch_positions`, `/value` deferred to Phase 9); `pmresearch/reconcile/checks.py` (holdings_vs_positions); `pmresearch/reconcile/runner.py` (run checks, persist facts, update trust); `pmresearch/reconcile/trust.py` (rules: any hard-check failure in last N runs → untrusted, with reason); `pmresearch/cli/reconcile.py`; scheduler hook after incremental sync.

**Migrations:** `m0006`: `reconciliation` (id, wallet, ts, check_type, subject (token/condition/portfolio), expected, computed, abs_diff, pct_diff, tolerance, status pass/warn/fail, source, notes) · `wallet_trust` (wallet PK, status, since, reason).

**CLI:** `pmr reconcile run [--wallet]` · `pmr reconcile status` (latest per wallet per check) · `pmr trust status`.

**Tests:** matching holdings → pass rows; injected drift (delete one ledger event, rebuild) → fail row with correct diff + wallet flips untrusted; positions the oracle has but we don't (and vice versa) both reported; epsilon handling (6-decimal rounding differences pass).

**Manual verification:** after full RN1 backfill+ingest+replay: `pmr reconcile run --wallet <RN1>` → expect pass on all open positions (this is the moment of truth for Phases 1–4; investigate every failure — likely causes: missed window, dedupe collision, sign convention on MERGE/REDEEM); repeat for 2 more wallets.

**Acceptance criteria:** RN1 + 2 other wallets reconcile with zero holdings drift (or every drift explained and fixed upstream in ingest — not tolerated away); reconciliation runs automatically after each sync cycle; `pmr trust status` reflects reality; failures produce actionable log lines (wallet, token, expected, computed, suspected cause).

**Common failure modes:** oracle `/positions` excludes closed/redeemable-claimed positions — compare only tokens where either side is nonzero and document; sub-cent dust mismatches (epsilon, don't widen beyond 1e-4 shares); treating an oracle outage as a reconciliation failure (distinguish check-errored from check-failed); tolerance creep to make red green (forbidden — thresholds change only with a documented cause).

**Prompt:** `Implement Phase 5 of IMPLEMENTATION_PLAN.md exactly as scoped. This is the correctness gate for everything before it: if RN1 doesn't reconcile cleanly, debug the ingest/replay pipeline until it does rather than loosening tolerances. Show me pmr reconcile status for RN1 and two more watchlist wallets. Commit when acceptance criteria pass.`

---



## Phase 6 — Episodes: flat-to-flat boundaries + WAC realized PnL

**Goal:** the primary analytical unit (ADR 0003) computed as a projection.

**Scope:** episode segmentation over the replay stream (open at zero→nonzero, close at return-to-zero within dust epsilon or market resolution); WAC realized PnL per exit inside episodes; adds/partial-exit counting; micro-episodes preserved (no debounce); `close_reason`; open episodes supported.

**Files/modules:** `pmresearch/projections/episodes.py` (consumes same ordered stream as holdings; all event types affect quantity per Phase 4 conventions); `pmresearch/cli/episodes.py`.

**Migrations:** `m0007`: `episodes` (id, wallet, token_id, condition_id, open_ts, close_ts nullable, close_reason flat/resolution/open, peak_qty, num_adds, num_partial_exits, wac_entry, realized_pnl, reward_income, fees_paid nullable, events_consumed, projection_version) + index (wallet, open_ts).

**CLI:** `pmr replay episodes [--wallet]` · `pmr episodes show --wallet <addr> [--token] [--open]` · `pmr episodes stats --wallet` (count, duration distribution summary, micro-episode share).

**Tests:** golden fixtures: single round trip (buy 10 @0.5, sell 10 @0.7 → realized 2.0); scale-in/partial-exit sequence with hand-computed WAC PnL; flat-crossing → two episodes; episode closed by resolution (REDEEM/resolution event); episode still open at stream end; dust-epsilon flat detection; consistency invariant: sum of episode realized PnL + open episode cost == holdings-projection state (cross-projection test).

**Manual verification:** `pmr episodes stats --wallet <RN1>`; sanity-check one specific well-understood market: list its episodes and verify against the raw event timeline by hand; confirm micro-episodes appear for a rapid-flat wallet if one is on the watchlist.

**Acceptance criteria:** episodes fully cover every ledger event that changes holdings (no orphan events); cross-projection consistency invariant holds for all watchlist wallets; deterministic rebuilds.

**Common failure modes:** re-entry same second as exit merging episodes (order by ts,id — a zero crossing is a boundary, period); WAC carry-over across episodes (basis resets at open — must not leak); REDEEM closing an episode before Phase 8's payout derivation exists (realized_pnl for resolution-closed episodes is understated until Phase 8 — document in output, don't fake it).

**Prompt:** `Implement Phase 6 of IMPLEMENTATION_PLAN.md exactly as scoped. Flat-to-flat, WAC, no debounce, per ADR 0003. Include the cross-projection consistency test against holdings. Note in output that resolution-closed episode PnL is understated until Phase 8. Show me episodes stats for RN1 and a hand-verified episode walkthrough. Commit when acceptance criteria pass.`

---



## Phase 7 — Reconciliation v2: avgPrice and realizedPnl vs oracle

**Goal:** validate our WAC implementation and realized-PnL accounting against Polymarket's own numbers.

**Scope:** two new checks: WAC (open positions) vs `/positions.avgPrice` (tight tolerance); our per-position realized PnL (episodes joined to open positions' history) vs `/positions.realizedPnl` (tolerance band — oracle semantics around redemptions/merges may differ; discrepancies get *categorized*, not hidden). Trust rules extended.

**Files/modules:** `pmresearch/reconcile/checks.py` (+wac_vs_avgprice, +realized_vs_oracle); tolerance config; `pmresearch/reconcile/trust.py` update (avgPrice persistent drift → untrusted; realizedPnl drift → warn until Phase 8, then tighten).

**Migrations:** none (reuses `reconciliation`).

**CLI:** `pmr reconcile run` gains the checks; `pmr reconcile status` shows per-check breakdown.

**Tests:** golden ledger where WAC is hand-computed → matches simulated oracle; injected WAC bug (e.g. sell not reducing basis) → fail; realizedPnl within band passes / outside fails with categorized note; oracle field missing/null handled as check-skipped not failed.

**Manual verification:** run against RN1 + 2 wallets; for one position, hand-walk fills → WAC and compare all three (ours, hand, oracle); inspect the largest realizedPnl discrepancy and write its suspected cause into the reconciliation notes.

**Acceptance criteria:** avgPrice matches within tight tolerance (≤1e-3 absolute or documented cause per position) for all open positions of 3 wallets; realizedPnl within band or discrepancy categorized (expected-Phase-8 vs unknown); trust status integrates both checks.

**Common failure modes:** comparing WAC across *all* history vs oracle's per-open-position basis (oracle avgPrice resets when position fully closes — compare per current open episode, not lifetime); rounding display precision of oracle values; band-widening to green (forbidden).

**Prompt:** `Implement Phase 7 of IMPLEMENTATION_PLAN.md exactly as scoped. Compare per current open episode, not lifetime. Discrepancies get categorized notes, never widened tolerances. Show me the per-check reconcile status for three wallets and a hand-walked WAC verification for one position. Commit when acceptance criteria pass.`

---



## Phase 8 — Derived events: redemption proceeds; full MERGE/SPLIT/REDEEM/REWARD semantics; PnL decomposition

**Goal:** complete, honest PnL — including the cash flows the API reports as zero.

**Scope:** derived REDEEM_PAYOUT events (terminal holdings at resolution × resolution price, per verified fact that `/activity` REDEEM rows carry `usdcSize: 0`); SPLIT/MERGE USDC-leg verification (1 pair ↔ $1); PnL decomposition projection: realized PnL split into directional (trade), bond/merge, rewards, redemption components per wallet and per category; episodes updated so resolution-closed episodes get correct realized PnL.

**Files/modules:** `pmresearch/ingest/derived.py` (idempotent derivation job: for each resolved market with terminal holdings and a REDEEM event or unclaimed resolution, emit `is_derived=1` events keyed deterministically — dedupe_key from wallet|condition|DERIVED_REDEEM); `pmresearch/projections/pnl_decomposition.py`; episodes projection updated; `pmresearch/cli/pnl.py`.

**Migrations:** `m0008`: `pnl_decomposition` (wallet, scope all/category, period all/daily later, directional_pnl, bond_merge_pnl, reward_income, redemption_pnl, fees, computed_at, projection_version).

**CLI:** `pmr derive run [--wallet]` · `pmr pnl show --wallet <addr> [--by-category]` · episodes stats now show corrected resolution PnL.

**Tests:** golden: hold winner to resolution → derived payout = qty×1, realized PnL correct; hold loser → payout 0, loss realized; MERGE round-trip (split $10 → merge back → net 0 minus nothing); derivation idempotency (run twice → no new events); decomposition sums to total realized PnL exactly (invariant test); Phase 7 realizedPnl reconciliation improves (categorized discrepancies shrink).

**Manual verification:** `pmr derive run --wallet <RN1>` then `pmr pnl show --by-category`; compare total PnL order-of-magnitude with Polymarket profile/leaderboard figure; confirm REWARD income share is nonzero for RN1 (verified fact: RN1 earns rewards); re-run Phase 7 reconciliation and compare before/after discrepancy counts.

**Acceptance criteria:** decomposition components sum exactly to total; derived events idempotent and marked `is_derived`; resolution-closed episodes now carry correct PnL; realizedPnl reconciliation discrepancies materially reduced and all remaining ones categorized.

**Common failure modes:** double-counting redemption when the API later starts reporting nonzero REDEEM usdcSize (derivation must check reported value first and only fill zeros); deriving payouts for unresolved markets (guard on resolution data present); negRisk resolution price format differences (test with a real negRisk fixture); attributing MERGE $1-pair proceeds to directional PnL (they're bond component).

**Prompt:** `Implement Phase 8 of IMPLEMENTATION_PLAN.md exactly as scoped. Derived events are idempotent, marked is_derived, and only fill values the API reports as zero. The decomposition-sums-to-total invariant is a required test. Show me RN1's PnL decomposition by category and the Phase 7 reconciliation before/after. Commit when acceptance criteria pass.`

---



## Phase 9 — Mark service, price_points, daily equity, staleness, /value reconciliation

**Goal:** honest valuation of open positions and the primary portfolio projection.

**Scope:** pluggable MarkSource interface; `prices_history` source (lazy fetch + cache per token, raw-stored); resolution source (terminal, always overrides); `price_points` persistence with mark_age/stale flag; `daily_equity` projection (portfolio value, realized/unrealized, reward income, drawdown, stale_equity_share); `/value` adapter + reconciliation v3 (1–2% band).

**Files/modules:** `pmresearch/marks/base.py` (interface: get_mark(token, ts) → price, source, age); `pmresearch/marks/prices_history.py`, `pmresearch/marks/resolution.py`, `pmresearch/marks/service.py` (priority: resolution > cached point within staleness window > lazy fetch); `pmresearch/projections/daily_equity.py`; `pmresearch/sources/dataapi.py` (+fetch_value); `pmresearch/reconcile/checks.py` (+value_check); `pmresearch/cli/equity.py`.

**Migrations:** `m0009`: `price_points` (token_id, ts, price, source, mark_age_s, stale, meta_json, PK token_id+ts+source) · `daily_equity` (wallet, date, portfolio_value, realized_pnl_cum, unrealized_pnl, reward_income_cum, drawdown, stale_equity_share, projection_version, PK wallet+date).

**CLI:** `pmr equity build [--wallet]` · `pmr equity show --wallet <addr>` · `pmr reconcile run` gains value check.

**Tests:** mark priority (resolved token always terminal value even with fresher trade print); staleness computed from underlying point age; daily equity golden fixture (2 positions, known marks → value/drawdown/stale share hand-computed); /value within band passes; historical daily equity is reproducible (same inputs → same curve); lazy fetch caches (second call = no HTTP).

**Manual verification:** `pmr equity build --wallet <RN1>`; `pmr reconcile run` → value check vs live `/value` (RN1 ≈ $1.2M scale); inspect stale_equity_share for a wallet holding illiquid sports props; confirm the standing caveat (approximate intraday drawdown) appears in `pmr equity show` output.

**Acceptance criteria:** daily equity builds for all watchlist wallets; /value reconciles within band (or categorized); every equity figure carries stale_equity_share; marks never invented (no bid/ask/midpoint for historical dates); prices-history responses raw-stored like everything else.

**Common failure modes:** prices-history empty for dead/pre-CLOB tokens (fall back to resolution or last ledger trade price, flagged stale, never crash); timezone drift in "daily" boundaries (define day as UTC, document); marking resolved-but-unredeemed positions at market price instead of terminal; fetch storms on first equity build (batch + throttle + cache).

**Prompt:** `Implement Phase 9 of IMPLEMENTATION_PLAN.md exactly as scoped. MarkSource is pluggable; resolution overrides everything; staleness is explicit on every figure; days are UTC. Show me RN1's equity curve summary, its /value reconciliation result, and the stale-share for one illiquid-market wallet. Commit when acceptance criteria pass.`

---



## Phase 10 — Exposure Engine: directional+bond, event-level exposure vectors

**Goal:** the strategy-analysis read models (CONTEXT.md: Market-level Exposure, Event-level Exposure), data-driven by descriptors.

**Scope:** exposure computation from holdings history: binary markets → directional (qty₀−qty₁ by token index) + bond (min); negRisk events → mutually-exclusive exposure vector with within-event netting; unclassified → raw per-token vector flagged unclassified; daily exposure snapshots projection; simple within-event views only (no cross-event correlation — out of MVP scope).

**Files/modules:** `pmresearch/exposure/engine.py` (dispatch on structure_type from Phase 3 descriptors); `pmresearch/exposure/binary.py`, `pmresearch/exposure/negrisk.py`, `pmresearch/exposure/unclassified.py`; `pmresearch/projections/exposures.py` (daily snapshots per wallet×condition + per wallet×event); `pmresearch/cli/exposure.py`.

**Migrations:** `m0010`: `exposures_daily` (wallet, condition_id, date, directional, bond, structure_type, event_id, projection_version, PK wallet+condition_id+date) · `event_exposures_daily` (wallet, event_id, date, exposure_vector_json, net_after_exclusivity, projection_version).

**CLI:** `pmr exposure build [--wallet]` · `pmr exposure show --wallet <addr> [--market|--event]`.

**Tests:** binary golden: 100 tok0 + 60 tok1 → directional +40, bond 60; team-name market (no Yes/No labels) works; negRisk 3-sibling event netting hand-computed; unclassified market → flagged vector, no decomposition; bond + MERGE interaction (bond drops when merged); dispatch never guesses (unknown structure_type → unclassified path, warning counted).

**Manual verification:** `pmr exposure show --wallet <RN1>` for one active sports event with multiple sibling markets; verify bond inventory appears for any wallet doing pair accumulation + merges; confirm unclassified count matches Phase 3 markets stats.

**Acceptance criteria:** exposures build for all wallets; binary decomposition verified against golden math; event vectors exist for negRisk events; zero hardcoded outcome-label logic (grep-able: no "Yes"/"No" string comparisons in exposure code).

**Common failure modes:** token-index order assumptions (always map through `tokens.outcome_index`); double-counting a condition in both market and event views without labeling; negRisk events where the wallet holds only one sibling (netting is a no-op — don't crash); performance of daily snapshots × history (build incrementally by date, vectorize with Polars).

**Prompt:** `Implement Phase 10 of IMPLEMENTATION_PLAN.md exactly as scoped. Dispatch strictly on structure descriptors; complementarity by token index only — no outcome-label string logic anywhere. Show me RN1's directional+bond for one market and the exposure vector for one negRisk event. Commit when acceptance criteria pass.`

---



## Phase 11 — Maker/taker enrichment: Goldsky subgraph + optional RPC recent-gap

**Goal:** ground-truth maker/taker roles, order hashes, and fees joined onto ledger fills (ADR 0001/0005).

**Scope:** Goldsky orderbook-subgraph adapter (`orderFilledEvents` by wallet, paginated, raw-stored); optional RPC adapter (`eth_getLogs` OrderFilled on both exchange contracts, config-keyed, off by default); join to `wallet_events` TRADE rows by (tx_hash, wallet, asset); `fill_enrichment` persistence; enrichment coverage metric; daily scheduled job; subgraph-lag awareness (track subgraph head vs now).

**Files/modules:** `pmresearch/sources/subgraph.py`; `pmresearch/sources/rpc.py` (event decoding: maker/taker/assetIds/amounts/fee; makerAssetId=0 ⇒ maker paid USDC — verified convention); `pmresearch/ingest/enrichment.py` (match + insert; unmatched enrichment rows logged, never force-matched); `pmresearch/cli/enrich.py`.

**Migrations:** `m0011`: `fill_enrichment` (event_id FK UNIQUE, role maker/taker, order_hash, fee, counterparty nullable, source subgraph/rpc, enriched_at) · `enrichment_watermarks` (wallet, subgraph_synced_to_ts, rpc_synced_to_block).

**CLI:** `pmr enrich run [--wallet] [--source subgraph|rpc]` · `pmr enrich coverage [--wallet]` (share of TRADE events enriched, by recency bucket).

**Tests:** decode fixture OrderFilled logs (both maker-pays-USDC and maker-pays-shares directions); join logic: one tx with multiple fills for same wallet+asset (match by amounts; ambiguous → leave unenriched + logged); idempotency; lag awareness (recent unenriched events counted as "pending", not "missing"); RPC disabled → subgraph-only works.

**Manual verification:** `pmr enrich run --wallet <RN1>`; `pmr enrich coverage` → high coverage for history older than subgraph lag, pending for recent window; if RPC key configured, gap shrinks; spot-check one enriched fill against the subgraph query by hand; RN1 shows maker fills (verified fact).

**Acceptance criteria:** enrichment never creates/deletes ledger events; coverage metric distinguishes enriched/pending/ambiguous; RN1 maker-share computable for the subgraph-covered period; RPC path optional and config-gated; all payloads raw-stored.

**Common failure modes:** joining by tx_hash alone (one tx contains many fills — must use wallet+asset+amount); size unit mismatch (subgraph amounts are 6-decimals integers; ledger uses decimal shares); proxy wallet vs signer address mismatch in subgraph maker/taker fields (verify with RN1 known fills; document which address space the subgraph uses); Goldsky endpoint throttling (backoff, cursor pagination by timestamp+id, not offset).

**Prompt:** `Implement Phase 11 of IMPLEMENTATION_PLAN.md exactly as scoped. Enrichment joins by tx_hash+wallet+asset+amount, never tx_hash alone; ambiguous matches stay unenriched and logged. RPC is optional via config. Show me RN1 enrichment coverage by recency bucket and one hand-verified enriched fill. Commit when acceptance criteria pass.`

---



## Phase 12 — Minimal REST book sampler with retention limits

**Goal:** start accruing irreplaceable spread/depth context for the relevant slice (ADR 0005).

**Scope:** Relevant Tokens query (open watchlist positions + tokens watchlist wallets traded in last 24h); CLOB `/book` poller every 1–5 min (config); store best_bid/best_ask/spread/mid/top-10-depth + raw JSON; retention: raw book gzips compressed immediately, pruned/archived per config (e.g. keep summaries forever, raw N days); storage budget check with loud warning.

**Files/modules:** `pmresearch/sources/clob.py` (+fetch_book); `pmresearch/booksampler/relevant.py` (the query); `pmresearch/booksampler/sampler.py` (poll loop, per-token throttle); `pmresearch/booksampler/retention.py` (prune job); scheduler wiring; `pmresearch/cli/books.py`.

**Migrations:** `m0012`: `book_snapshots` (token_id, ts, best_bid, best_ask, spread, mid, depth_top_json, raw_ref nullable-after-prune, PK token_id+ts).

**CLI:** `pmr books sample-once` · `pmr books status` (tokens tracked, snapshots count, storage used) · `pmr books prune`.

**Tests:** relevant-tokens query golden (open position + recent trade + neither); snapshot parse fixture (spread/mid math, one-sided books, empty books); retention prune removes raw beyond horizon but keeps summary rows; storage accounting; sampler skips tokens gone irrelevant.

**Manual verification:** with RN1 synced, `pmr books sample-once` → snapshots for RN1's open-position tokens; let the scheduler run an hour, `pmr books status` shows growth within budget; kill/restart container → sampling resumes (stateless job, durable data).

**Acceptance criteria:** samples only Relevant Tokens; storage bounded and observable; schema forward-compatible with future WSS collector (same table role); zero impact on sync jobs when CLOB is slow (isolated job, own timeouts).

**Common failure modes:** unbounded raw growth (retention from day one is the acceptance point, not a TODO); hammering /book for hundreds of tokens in one tick (batch with per-tick cap, rotate); empty/one-sided books crashing spread math (nullable fields); tokens of resolved markets never leaving the relevant set (exclude resolved).

**Prompt:** `Implement Phase 12 of IMPLEMENTATION_PLAN.md exactly as scoped. Relevant tokens only, retention and storage budget from day one, isolated from sync jobs. Show me books status after an hour of sampling and prove retention pruning works. Commit when acceptance criteria pass.`

---



## Phase 13 — Behavioral fingerprints

**Goal:** the measurement layer for strategy analysis (CONTEXT.md: Behavioral Fingerprint) — mechanical, interpretable, versioned.

**Scope:** fingerprint computation per wallet and per wallet×category over a config window (default: full history + trailing 90d variants). MVP feature set (from the agreed list): maker_fill_share, taker_fill_share, enrichment_coverage, reward_income_share, realized_pnl, unrealized_pnl, bond_inventory_ratio (time-weighted), merge_frequency, redeem_frequency, episode_count, episode_duration_p50/p90, micro_episode_share, adds_per_episode, partial_exit_frequency, avg/median position size, market_category_concentration (HHI), time_to_event_start_at_entry (sports, where Gamma dates allow), entry_price_distribution (buckets), resolution_outcome_calibration (win rate by entry-price bucket vs implied), stale_mark_share. Each feature = pure function over projections; NULL when uncomputable (e.g. maker share with zero enrichment coverage) — never silently 0.

**Files/modules:** `pmresearch/fingerprints/features/*.py` (one module per feature family: execution, inventory, income, calibration, quality); `pmresearch/fingerprints/compute.py` (registry, versioning); `pmresearch/cli/fingerprints.py`.

**Migrations:** `m0013`: `fingerprints` (wallet, scope, feature, value, window, computed_at, version, PK wallet+scope+feature+window+version).

**CLI:** `pmr fingerprints compute [--wallet]` · `pmr fingerprints show --wallet <addr> [--scope <category>]` · `pmr fingerprints compare --wallets a,b,c`.

**Tests:** each feature has a golden-fixture test with hand-computed value; NULL-when-uncomputable semantics; category scoping (event with mixed categories); calibration bucketing edges; determinism; version bump invalidates and recomputes.

**Manual verification:** `pmr fingerprints show --wallet <RN1>`; sanity-check against known facts: reward_income_share > 0, maker share present for covered period, category concentration matches RN1's sports/crypto mix seen in activity; `pmr fingerprints compare` across the 3 watchlist wallets shows meaningful contrast.

**Acceptance criteria:** all listed features computed for 3 wallets (or NULL with reason); per-category scopes present; every value reproducible; features carry version; no feature reads raw API data directly (projections only).

**Common failure modes:** division-by-zero wallets (new/empty scopes); survivorship in calibration (only resolved markets count; unresolved excluded, not assumed lost); maker_share computed over unenriched periods (must be conditioned on coverage window); category from Gamma missing (bucket "unknown", don't drop).

**Prompt:** `Implement Phase 13 of IMPLEMENTATION_PLAN.md exactly as scoped. Every feature is a pure function over projections with a hand-computed golden test; NULL-with-reason when uncomputable. Show me RN1's fingerprint, its sports-scope fingerprint, and a 3-wallet comparison. Commit when acceptance criteria pass.`

---



## Phase 14 — Strategy detectors: market_making, inventory_cycling, value_betting

**Goal:** first scored hypotheses with evidence and blind spots (CONTEXT.md: Strategy Detector/Label — no booleans).

**Scope:** detector framework (contract: read fingerprints → emit label rows with score 0–1, evidence feature/value map, blind_spots text, version); the three MVP detectors with transparent rule logic (weighted feature scoring documented in each detector's docstring); scheduled recompute after fingerprint updates.

**Files/modules:** `pmresearch/detectors/base.py`; `pmresearch/detectors/market_making.py` (maker_share, reward_income_share, bond_ratio, micro_episode_share, two-sided behavior; blind spots: quote placement unobservable, coverage window); `pmresearch/detectors/inventory_cycling.py` (bond accumulation, merge_frequency, capital recycling cadence); `pmresearch/detectors/value_betting.py` (taker-dominant, long episodes to resolution, positive calibration edge, category concentration; blind spots: calibration sample size, 1-min price fidelity); `pmresearch/cli/detect.py`.

**Migrations:** `m0014`: `strategy_labels` (wallet, scope, detector_name, detector_version, label, score, evidence_json, blind_spots, computed_at, PK wallet+scope+detector_name+detector_version+computed_at).

**CLI:** `pmr detect run [--wallet]` · `pmr detect show --wallet <addr>` (scores + expandable evidence) · `pmr detect explain --wallet <addr> --detector <name>` (full evidence + blind spots).

**Tests:** synthetic fingerprint fixtures: archetypal MM → market_making score high, value_betting low; archetypal value bettor → inverse; missing features (NULL) degrade score confidence per documented rule, never crash; evidence_json contains every input feature+value; multiple labels coexist; version bump recomputes.

**Manual verification:** `pmr detect run` for the 3 deliberately-different watchlist wallets; `pmr detect explain` for RN1 — every score must be manually explainable from its evidence; check no detector outputs a boolean anywhere.

**Acceptance criteria:** three detectors produce scored, evidenced, blind-spotted labels for 3 wallets; scores differ sensibly across the deliberately-different wallets; every score reproducible from stored evidence; detectors read fingerprints only (no direct projection/SQL access).

**Common failure modes:** score saturation (everything 0.9 — calibrate weights against the contrast wallets); hidden thresholds without docstring rationale; treating NULL features as 0 (biases against low-coverage wallets); evidence stored as prose instead of feature/value pairs (must be machine-readable).

**Prompt:** `Implement Phase 14 of IMPLEMENTATION_PLAN.md exactly as scoped. Detectors read fingerprints only, emit scores 0–1 with machine-readable evidence and explicit blind spots — no booleans anywhere. Show me detect explain output for all three watchlist wallets across all three detectors. Commit when acceptance criteria pass.`

---



## Phase 15 — Report generator: "Why is RN1 profitable?"

**Goal:** the actual research deliverable (ADR 0006 point 7) generated from platform data via CLI.

**Scope:** Markdown report generator in the core library: PnL decomposition (directional / bond+merge / rewards / redemptions), category breakdown, episode behavior summary, maker/taker evidence with coverage caveats, strategy hypothesis scores with evidence tables, reconciliation/trust status, limitations and data-quality notes (staleness, enrichment coverage, blind spots). Output to `/data/exports/`. Wallet-generic (`--wallet`), RN1 is just the first subject.

**Files/modules:** `pmresearch/reports/wallet_profile.py` (assembles from projections/fingerprints/labels/reconciliation — no new computation in the report layer); `pmresearch/reports/render.py` (Markdown templates); `pmresearch/cli/report.py`.

**Migrations:** none.

**CLI:** `pmr report wallet <addr> [--out <path>] [--window]`.

**Tests:** report builds from a fully-populated golden fixture DB (assembled from prior phases' fixtures); every numeric claim in the rendered output traces to a queried value (template has no literals); untrusted wallet → report renders with a prominent data-quality warning banner; missing sections (no enrichment) degrade to explicit "insufficient data" blocks.

**Manual verification:** `pmr report wallet <RN1>` → read the memo end-to-end; check it actually answers the question (decomposition percentages, dominant income source, hypothesis ranking); generate for the other two wallets and confirm the narratives differ.

**Acceptance criteria:** the RN1 memo generates end-to-end, contains all sections listed in ADR 0006 point 7, cites data-quality caveats, and is genuinely informative (the human test: you learn where RN1's PnL comes from). Reports for contrast wallets are meaningfully different.

**Common failure modes:** report layer computing new metrics (all numbers must come from stored projections — otherwise reports drift from dashboards); hiding untrusted status; hardcoded RN1 assumptions (must work for any wallet); stale data rendered without its staleness note.

**Prompt:** `Implement Phase 15 of IMPLEMENTATION_PLAN.md exactly as scoped. The report layer assembles and renders only — zero new computation. Generate the RN1 report and the two contrast-wallet reports and show me all three. Commit when acceptance criteria pass.`

---



## Phase 16 — Streamlit research shell

**Goal:** the disposable dashboard (ADR 0004), built only now that the core library already answers everything via CLI.

**Scope:** `apps/dashboard/` Streamlit app with the agreed views: wallet overview (equity, decomposition, trust badge) · ledger explorer · episode explorer (with on-demand fine-grained replay for a selected episode) · market exposure (directional+bond over time) · event exposure · PnL decomposition · reward/rebate analysis · fingerprint view (vs watchlist percentiles) · strategy hypotheses (scores + evidence + blind spots) · data quality (reconciliation, sync staleness, stale marks, enrichment coverage) · wallet comparison. Read-only DB access through library functions; dashboard service added to Compose.

**Files/modules:** `apps/dashboard/Home.py` + `apps/dashboard/pages/*.py` (one per view); `pmresearch/api.py` (thin façade module listing every function the dashboard may call — the audit surface); Compose service `dashboard` (own container, same image or slim variant, read-only mount consideration).

**Migrations:** none. **CLI:** none new.

**Tests:** import-boundary test: `apps/dashboard` imports only `pmresearch.api` (AST/grep check in CI); `pmresearch` never imports streamlit; façade functions covered by unit tests; deletion test automated: test suite passes with `apps/dashboard` renamed away (CI job or scripted check).

**Manual verification:** `docker compose up dashboard` → browse all views for RN1; verify trust badge and staleness indicators visible; delete/rename `apps/dashboard` locally → `pmr report wallet` and full test suite still pass (the deletion test, live).

**Acceptance criteria:** all views render from façade calls only; no SQL, no metric computation, no detector logic in dashboard code (enforced by the import-boundary test); deletion test passes; dashboard container restart loses nothing.

**Common failure modes:** "just one quick calculation" in a page file (the import-boundary test is the rejection mechanism); Streamlit caching stale data after resync (cache keyed on projection versions/timestamps); dashboard writing to the DB (read-only session enforcement); slow pages doing full-history queries (paginate via façade parameters).

**Prompt:** `Implement Phase 16 of IMPLEMENTATION_PLAN.md exactly as scoped. Dashboard calls only the pmresearch.api façade; include the automated import-boundary and deletion tests. Bring it up in Compose and walk me through every view for RN1. Commit when acceptance criteria pass.`

---



## Phase 17 — Hardening and MVP acceptance

**Goal:** close ADR 0006's definition of done with evidence.

**Scope:** restore drill on real data (scripted end-to-end: stop → simulate DB loss → restore backup → replay projections → reconcile → green); staleness/failure alerting (log-based alerts surfaced in dashboard data-quality view and `pmr sync status`; optional free notification hook e.g. Telegram, config-gated); 7-day soak with the full watchlist (3+ wallets incl. the two contrast wallets); trust gating verified end-to-end (untrusted wallet visibly flagged everywhere conclusions appear); `pmr acceptance` command that checks the 7-point list mechanically where possible; `docs/MVP_ACCEPTANCE.md` recording evidence (dates, outputs, drill logs).

**Files/modules:** `ops/restore_drill.sh` (extended for real data); `pmresearch/cli/acceptance.py`; alert plumbing in walletmanager; `docs/MVP_ACCEPTANCE.md`.

**Migrations:** none expected.

**CLI:** `pmr acceptance` (runs: reconciliation green?, soak-window sync uptime, deletion-test hook, report generation, backup freshness) · `pmr sync status` gains staleness alerts.

**Tests:** acceptance checks unit-tested against fixture states (each of the 7 points fail-able); alert triggers on synthetic stale sync.

**Manual verification:** run the restore drill for real and capture the log; run `pmr acceptance` daily during the soak week; generate the final RN1 memo from the restored-and-resynced database (proves the whole loop).

**Acceptance criteria:** all seven ADR 0006 points green with recorded evidence in `docs/MVP_ACCEPTANCE.md`; zero/near-zero holdings drift across the soak week; restore drill log committed; MVP declared done.

**Common failure modes:** drill performed against a fresh backup taken seconds before (must use the scheduled nightly one); soak "passing" because syncs silently stopped (uptime must be measured from sync_state history, not absence of errors); acceptance checklist hand-waved (the command + evidence doc exist precisely to prevent this).

**Prompt:** `Implement Phase 17 of IMPLEMENTATION_PLAN.md exactly as scoped. Run the real restore drill and capture logs, wire staleness alerts, implement pmr acceptance, and start the 7-day soak. At the end of the soak, produce docs/MVP_ACCEPTANCE.md with evidence for all seven ADR 0006 points. Commit when acceptance criteria pass.`

---



## Phase ordering rationale

Correctness infrastructure precedes analytics: raw store before parsing (1→2), external oracle immediately after the first projection (4→5) so every later phase builds on verified accounting, oracle-validated WAC before PnL claims (6→7→8), honest marks before portfolio metrics (9), enrichment and books (11–12) before fingerprints need them (13), detectors (14) before the report that cites them (15), dashboard only after the library can answer everything (16), and acceptance last with evidence (17). Irreplaceable-data collection (12) is as early as its dependency (relevant tokens ← holdings ← reconciliation) allows.

## First prompt to send (Phase 0)

```
Implement Phase 0 of IMPLEMENTATION_PLAN.md in this repo.

Context: architecture is frozen — DESIGN.md, CONTEXT.md, and docs/adr/0001–0006 are
the source of truth. Do not redesign anything; if you hit a genuine implementation
blocker, stop and tell me instead of improvising.

Scope: exactly the Phase 0 scope — repo scaffold, installable pmresearch package with
the pmr CLI, config (env-first, PMR_DATA_DIR), logging to console + /data/logs,
SQLite via Alembic (WAL, render_as_batch), Docker Compose with host-mounted /data,
Dockerfile (code only, disposable), ops/backup.sh (VACUUM INTO + optional rclone),
ops/restore.sh, ops/restore_drill.sh, and the test harness. No external API calls,
no business tables yet.

Then:
1. Run the full test suite and show results.
2. Do the Phase 0 manual verification: compose up, verify /data layout on the host,
   rebuild the image and prove the DB survives, and run the restore drill on the
   empty DB.
3. Confirm the acceptance criteria checklist for Phase 0 one by one.
4. Commit with a clear message and push.
```

