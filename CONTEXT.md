# Context — Polymarket Wallet Research Platform

Glossary of domain terms. Definitions only — no implementation details.

## Wallet
A Polymarket proxy wallet address being studied. First-class entity: every fact in the system is attributable to a wallet. No component may assume a fixed or small number of wallets.

## Watchlist
The curated set of Wallets currently being synchronized and analyzed (MVP scale: 10–50). Wallets can be added, removed, and tagged dynamically. Distinct from the global wallet universe, which is out of MVP scope.

## Wallet Tag
A free-form label attached to a Wallet by the researcher (e.g. market-maker, sports, politics, crypto, experimental). Tags are hypotheses/organization, not computed facts — computed classifications are Strategy Labels.

## Ledger
The append-only stream of Wallet Events. The single source of truth for all wallet accounting. Rows are immutable; corrections are new events, never updates.

## Wallet Event
One atomic action by a Wallet, typed (TRADE, MERGE, SPLIT, REDEEM, REWARD, TRANSFER, …) with signed share and USDC deltas. A Fill is the TRADE-typed Wallet Event.

## Projection
Derived state (holdings, PnL, positions, metrics) computed by replaying the Ledger. Disposable by contract: any Projection can be dropped and rebuilt from the Ledger, is written only by replay, and is never a source of truth.

## Derived Event
A Wallet Event computed by the platform rather than reported by a source (e.g. redemption proceeds = terminal holdings × resolution price, because the source reports them as zero). Marked as derived; still append-only.

## Raw Snapshot
The verbatim payload of every external API response fetched by the Collector, stored append-only outside the database. Recovery tier: the Ledger can be rebuilt from Raw Snapshots without re-fetching.

## Token-level Position (Lot layer)
Holdings of a single outcome token by a Wallet, preserving individual lots. The unit of execution analysis: entries, scaling, partial exits, maker/taker behavior.

## Episode
The primary temporal boundary of a Token-level Position: opens when a Wallet's holdings of a token move from zero to non-zero; closes when holdings return to zero or the Market resolves. Consumes all Ledger event types that affect holdings (trades, merges, redeems, …), not just trades. No debounce: repeated flat crossings are separate micro-episodes by design — the micro-episode pattern is itself an analytical signal (e.g. market making, inventory cycling).

**Caveat:** episode-level metrics (count, duration, PnL per episode) are strategy-dependent and must not be compared blindly across wallet types without context — a market maker's thousands of micro-episodes and a value bettor's dozens of long episodes are different objects.

## Weighted-Average Cost (WAC)
The primary realized-PnL method: within an Episode, each exit realizes PnL against the running average entry cost. FIFO exists only as a possible later projection for hold-time/lot-aging analytics, never for primary PnL. LIFO is not used.

## Market-level Exposure
A Wallet's net economic exposure in one Market, computed per the Market's structure (see Market Structure Descriptor). For binary markets: the directional + bond decomposition. The unit of within-market strategy analysis: market making, inventory management, value betting.

## Directional + Bond decomposition
For a binary Market: `directional = qty(token₀) − qty(token₁)` (signed, in token₀-equivalent shares) and `bond = min(qty₀, qty₁)` — complete outcome pairs redeemable for $1 regardless of result. Sustained bond inventory plus MERGE events is an inventory-cycling / market-making signature; complementarity is determined by token index within the condition, never by outcome labels ("Yes"/"No"/team names).

## Market Structure Descriptor
Metadata describing how a Market's outcome tokens relate (binary complement, negRisk mutual exclusivity, event membership, or unclassified), derived from market metadata, not hardcoded. The Exposure Engine dispatches on it; unrecognized structures yield a raw per-token exposure vector flagged "unclassified", never a guessed decomposition.

## Exposure Engine
The component that computes Market-level and Event-level Exposure from the Ledger, data-driven by Market Structure Descriptors.

## Event
A group of related Markets as defined by Polymarket (e.g. one match, one election). Multi-outcome situations ("Mexico / Draw / Korea") are structured as sibling binary Markets under one Event, mutually exclusive when negRisk.

## Event-level Exposure
The vector of a Wallet's Market-level Exposures across sibling Markets of one Event, with mutual-exclusivity netting for negRisk Events. The unit of hedging and within-event inventory-allocation analysis. MVP scope: simple within-event relationships only; cross-Event correlation is out of scope.

## Mark
The price used to value an open holding at a point in time. Marks come from a pluggable Mark Source behind one interface; analytics never know which source produced a Mark. Historical marks are last-trade prices (the only honest retroactive source — historical bid/ask/midpoint/depth are never invented). Resolved tokens always mark at their terminal resolution value, overriding last-trade. Prospectively, live-collected book data may upgrade marks (midpoint, conservative exit, spread-aware).

## Price Point
A stored Mark fact: (token, timestamp, price, source, mark age, stale flag, metadata). The `price_points` table is the only place marks live.

## Staleness
The age of the trade underlying a Mark. Never hidden: every unrealized-PnL and equity figure carries a data-quality indicator (share of equity marked with stale prices). Illiquid markets make last-trade marks misleading; the indicator says when to distrust them.

**Standing caveat:** historical intraday drawdown is approximate (≈1-minute, last-trade granularity) and cannot be made exact retroactively without orderbook data that was never collected.

## Daily Equity
The primary portfolio projection: per wallet per day — portfolio value, drawdown, return curve, Sharpe-like estimates, comparison basis. Hourly equity is optional/on-demand; per-minute replay is computed on demand for a specific wallet/market/episode window, never precomputed globally.

## Behavioral Fingerprint
A versioned vector of mechanically computed, interpretable features describing a Wallet's behavior (maker share, reward income share, bond inventory ratio, episode duration distribution, calibration, …). Computed per Wallet and per Wallet × category where possible. Measurements only — never judgments. Part of Global Facts.

## Strategy Detector
A named, versioned rule (later possibly a model) that reads Behavioral Fingerprints and emits Strategy Labels. Every detector explicitly documents its Blind Spots. ML detectors, when added, emit the same schema.

## Strategy Label
A scored hypothesis, never a verdict: (wallet, scope/category, detector name + version, label, score 0–1, evidence features with values, blind spots, computed-at). Multiple labels coexist per wallet by design. Booleans like `is_market_maker` are forbidden. Part of Global Facts — strictly separate from human Wallet Tags (Workspace).

## Blind Spot
What a detector structurally cannot see, stored alongside its output (e.g. historical quote placement was never collected; momentum detection is noisy at ~1-min price fidelity; liquidity provision vs market making is hard to distinguish without quote data). The system prefers honest scored hypotheses with evidence over overconfident labels.

## Reconciliation
A permanent projection (not a one-time test) comparing ledger-derived state against Polymarket's own accounting after every sync: holdings vs `/positions.size` (exact; drift = hard alert), WAC vs `/positions.avgPrice` (near-exact), realized PnL vs `/positions.realizedPnl` (tolerance band), portfolio value vs `/value` (~1–2% band). Every result is a stored timestamped fact. Also the tripwire for upstream API changes.

## Trusted / Untrusted Wallet
A wallet's data-quality status derived from Reconciliation. Analytics for an Untrusted wallet are never silently presented as reliable; failures are visible and actionable.

## Global Facts
Data that is true regardless of who is researching: Wallet Events, Markets, resolutions, computed wallet metrics. Shared across all future tenants.

## Workspace
Researcher-owned data: Watchlist membership, Wallet Tags, notes, hypotheses. Kept strictly separate from Global Facts; the future multi-tenancy seam.

## Fill
A single executed trade event for a Wallet: (wallet, token, side, size, price, timestamp, transaction). The atomic unit of the ledger. Sourced canonically from the Data-API activity feed; may be enriched with maker/taker role, order hash, and fee from on-chain data.

## Enrichment
Attaching on-chain-derived facts (maker/taker role, order hash, fee) to an existing Fill. Enrichment lags the canonical feed and never creates or deletes Fills.

## Maker / Taker
The role of a wallet in a Fill: the Maker's resting order was matched by the Taker's incoming order. Ground truth exists per fill on-chain (OrderFilled events); it is not exposed by the Data-API.

## Market
A single Polymarket condition (identified by conditionId) with its outcome tokens, metadata, and resolution. Sourced from the Gamma API.

## Backfill
Retrieving the complete historical activity of a Wallet from the canonical sources, from its first activity to now. Triggered when a wallet joins the Watchlist.

## Incremental Sync
Periodic retrieval of a Wallet's new activity since its last successful sync.

## Sync Status
Per-wallet bookkeeping of synchronization progress: last synced timestamp, backfill completeness, failure/staleness state. Owned by the Wallet Manager.

## Wallet Manager
The component that maintains the Watchlist, schedules Backfills and Incremental Syncs, tracks Sync Status, and detects stale or failed synchronizations. It decides *what* to sync; the Collector performs the retrieval.

## Book Sampler
The MVP's minimal REST poller of current orderbook snapshots for Relevant Tokens (1–5 min cadence, top ~10 levels, raw JSON kept). Purpose: accrue otherwise-unrecoverable spread/depth context around watched wallets — not to reconstruct a complete historical book. Bounded by explicit retention/storage limits.

## Relevant Tokens
The Book Sampler's scope: tokens with open positions held by Watchlist wallets, plus tokens traded by Watchlist wallets in the last 24 hours.

## Core Library
The importable Python package containing all business logic: ingestion, projections, exposure, analytics, detectors, reports, SQL access. The only place logic exists; every surface (dashboard, notebooks, CLI, future API) is a thin consumer. The commercialization seam.

## Research Shell
The disposable MVP dashboard (Streamlit-class). May only call Core Library functions and render results. Deletion test: removing it must leave the platform fully functional via library + CLI.

## Collector
The component that talks to external data sources and lands raw records. The only component that changes when the wallet universe scales from watchlist to global.
