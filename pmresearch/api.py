"""Phase 16 façade — the ONLY import boundary `apps/dashboard` may use.

Every function the disposable Streamlit dashboard (ADR 0004) is allowed to
call is re-exported here, flat, so this module doubles as an audit surface:
if a dashboard page needs something not listed below, that capability must be
added here first (and reviewed), never imported from `pmresearch.*` directly.

This module must never gain new metric computation. It only re-exports or
thinly wraps functions that already exist in the library — assembly and
formatting of what other modules already computed, nothing more. The two
"new" query helpers below (`ledger_event_counts`/`list_wallet_events`/
`fetch_events_by_ids`/`book_sampler_status`) are not new computation either:
they are raw SQL SELECTs (no aggregation beyond a GROUP BY count) that used to
live inline in CLI commands and have been extracted into library modules so
the CLI and this façade share one implementation.
"""

from __future__ import annotations

from contextlib import contextmanager

from .booksampler.status import BookSamplerStatus, book_sampler_status
from .config import Settings, ensure_data_dirs, get_settings
from .db.engine import get_session_factory
from .detectors.compute import all_detectors, fetch_labels, label_scopes
from .fingerprints.compute import fetch_fingerprints, fingerprint_scopes
from .ingest.enrichment import enrichment_coverage
from .ledger.stats import (
    fetch_events_by_ids,
    ledger_event_counts,
    list_wallet_events,
)
from .projections.daily_equity import fetch_daily_equity, latest_daily_equity
from .projections.episodes import episode_stats, fetch_episodes
from .projections.exposures import fetch_event_exposures, fetch_exposures
from .projections.holdings import fetch_holdings
from .projections.pnl_decomposition import fetch_pnl_decomposition
from .reconcile.runner import latest_reconciliation_result
from .reconcile.trust import fetch_wallet_trust
from .reports.fee_attribution import fee_attribution_coverage, fee_attribution_report
from .reports.holdings_dq import (
    missing_conditions_report,
    missing_token_metadata_report,
    negative_holdings_report,
    undocumented_events_report,
)
from .reports.render import render_wallet_profile
from .reports.wallet_profile import build_wallet_profile
from .walletmanager.manager import (
    get_sync_state,
    is_stale,
    list_sync_states,
    list_wallets,
)
# Phase 17 — alerting
from .alerts import check_wallet_alerts, AlertSeverity, WalletAlert

__all__ = [
    "get_settings",
    "ensure_data_dirs",
    "get_session_factory",
    "open_session",
    "list_wallets",
    "get_sync_state",
    "list_sync_states",
    "is_stale",
    "fetch_holdings",
    "fetch_episodes",
    "episode_stats",
    "fetch_daily_equity",
    "latest_daily_equity",
    "fetch_exposures",
    "fetch_event_exposures",
    "fetch_pnl_decomposition",
    "fee_attribution_report",
    "fee_attribution_coverage",
    "fetch_fingerprints",
    "fingerprint_scopes",
    "fetch_labels",
    "label_scopes",
    "all_detectors",
    "latest_reconciliation_result",
    "fetch_wallet_trust",
    "enrichment_coverage",
    "build_wallet_profile",
    "render_wallet_profile",
    "negative_holdings_report",
    "missing_conditions_report",
    "missing_token_metadata_report",
    "undocumented_events_report",
    "ledger_event_counts",
    "list_wallet_events",
    "fetch_events_by_ids",
    "book_sampler_status",
    "BookSamplerStatus",
    "check_wallet_alerts",
    "AlertSeverity",
    "WalletAlert",
]


@contextmanager
def open_session(settings: Settings):
    """Open a session from `settings` and guarantee it's closed on exit —
    the try/finally every CLI command duplicates around `get_session_factory`."""
    session = get_session_factory(settings)()
    try:
        yield session
    finally:
        session.close()
