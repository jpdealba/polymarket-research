"""Shared bootstrap helpers for every dashboard page. Imports only
`pmresearch.api` and Streamlit — never any other `pmresearch` submodule (see
the module docstring on `pmresearch/api.py` and the import-boundary test)."""

from __future__ import annotations

import streamlit as st

from pmresearch import api

# Staleness cadence used only for the dashboard's own "is this wallet stale"
# display; presentation-layer choice, not a new metric.
DEFAULT_CADENCE_S = 3600


@st.cache_resource
def get_settings():
    settings = api.get_settings()
    api.ensure_data_dirs(settings)
    return settings


def wallet_selector():
    settings = get_settings()
    with api.open_session(settings) as session:
        wallets = api.list_wallets(session, active_only=False)
    addresses = [w.address for w in wallets]
    if not addresses:
        st.warning("No wallets on the watchlist. Run `pmr wallet add <addr>` first.")
        st.stop()
    return st.sidebar.selectbox(
        "Wallet",
        addresses,
        format_func=lambda a: next((w.display_name or a for w in wallets if w.address == a), a),
    )
