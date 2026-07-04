"""Wallet comparison (Phase 16, view 11): side-by-side metrics across a
selected subset of the watchlist. Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st

from pmresearch import api

from _common import get_settings

st.title("Wallet Comparison")

settings = get_settings()

with api.open_session(settings) as session:
    all_wallets = api.list_wallets(session, active_only=False)

addresses = [w.address for w in all_wallets]
if not addresses:
    st.warning("No wallets on the watchlist. Run `pmr wallet add <addr>` first.")
    st.stop()

selected = st.multiselect("Wallets to compare", addresses, default=addresses[: min(3, len(addresses))])

rows = []
with api.open_session(settings) as session:
    for address in selected:
        pnl_rows = api.fetch_pnl_decomposition(session, address, by_category=False)
        pnl_all = next((r for r in pnl_rows if r.scope == "all"), None)
        equity = api.latest_daily_equity(session, address)
        stats = api.episode_stats(session, address)
        labels = api.fetch_labels(session, address, scope="all")
        top_label = max(labels, key=lambda lbl: float(lbl.score), default=None)

        rows.append(
            {
                "wallet": address,
                "total_pnl": str(pnl_all.total_pnl) if pnl_all is not None else None,
                "latest_portfolio_value": str(equity.portfolio_value) if equity is not None else None,
                "episode_count": stats.count,
                "top_hypothesis": top_label.detector_name if top_label is not None else None,
                "top_hypothesis_score": top_label.score if top_label is not None else None,
            }
        )

if not rows:
    st.info("Select at least one wallet to compare.")
else:
    st.dataframe(rows, use_container_width=True)
